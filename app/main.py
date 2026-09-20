"""App web de finanzas: FastAPI + plantillas Jinja2 + HTMX.

Arranque:  uvicorn app.main:app
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import backup, calc, config, db, export, importer, pin, repo, seed, views
from .money import (InvalidAmount, amount_input, eur, fmt_date, long_day, month_key, month_name,
                    month_short, parse_amount, parse_date, parse_month, pct, add_months_ym)

log = logging.getLogger("finanzas")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

OPEN_PATHS = {"/pin", "/salud", "/manifest.webmanifest", "/sw.js", "/offline"}
NAV = [("/", "inicio"), ("/movimientos", "movimientos"), ("/sobres", "sobres"),
       ("/presupuesto", "presupuesto"), ("/resumen", "mas"), ("/deudas", "mas"),
       ("/ajustes", "mas"), ("/mas", "mas")]

templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
templates.env.filters.update(
    eur=eur, eur_sign=lambda c: eur(c, sign=True), pct=pct, pct1=lambda x: pct(x, 1), fecha=fmt_date,
    dia_largo=long_day, importe_input=amount_input,
)
templates.env.globals.update(TYPES=calc.TYPES, month_name=month_name, month_short=month_short,
                             month_key=month_key, add_months=add_months_ym)


# ---------------------------------------------------------------- utilidades

def get_db(request: Request):
    conn = db.connect(request.app.state.db_path)
    request.state.conn = conn
    try:
        yield conn
    finally:
        conn.close()


def today(request: Request) -> date:
    return request.app.state.today()


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def set_flash(response: Response, message: str, kind: str = "ok") -> None:
    response.set_cookie("flash", quote(json.dumps({"msg": message, "kind": kind})),
                        max_age=60, httponly=True, samesite="lax")


def redirect(request: Request, url: str, flash: str | None = None, kind: str = "ok") -> Response:
    """Tras un POST: con HTMX navega sin recargar (HX-Location); sin HTMX, 303 normal."""
    if is_htmx(request):
        response = Response(status_code=200, headers={"HX-Location": url})
    else:
        response = RedirectResponse(url, status_code=303)
    if flash:
        set_flash(response, flash, kind)
    return response


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200, headers=None):
    ctx = dict(ctx or {})
    raw = request.cookies.get("flash")
    if raw:
        try:
            ctx["flash"] = json.loads(unquote(raw))
        except ValueError:
            pass
    response = templates.TemplateResponse(request, name, ctx, status_code=status, headers=headers)
    if raw:
        response.delete_cookie("flash")
    return response


def nav_section(path: str) -> str:
    for prefix, section in NAV:
        if prefix == "/" and path == "/":
            return section
        if prefix != "/" and path.startswith(prefix):
            return section
    return ""


def base_context(request: Request) -> dict:
    """Variables de todas las páginas: versión, menú y el formulario rápido del botón +."""
    ctx = {"version": config.APP_VERSION, "nav": nav_section(request.url.path),
           "pin_enabled": False}
    conn = getattr(request.state, "conn", None)
    if conn is not None:
        opts = views.tx_form_options(conn)
        ctx.update(qa=opts, qa_form=views.blank_tx_form(opts, today(request)),
                   pin_enabled=bool(repo.setting(conn, "pin_hash")),
                   envelope_account=views.envelope_account_name(conn))
    return ctx


templates.context_processors.append(base_context)


# ---------------------------------------------------------------- arranque y copias

def run_pending_sweep(db_path: Path, hoy: date) -> str | None:
    """Guarda solo las sobras del mes cerrado, si toca. Devuelve un texto para el registro."""
    conn = db.connect(db_path)
    try:
        pending = views.pending_sweep(conn, repo.all_txs(conn), hoy)
        if not pending or not pending["due"]:
            return None
        views.do_sweep(conn, pending)
        return (f"sobras de {month_name(pending['year'], pending['month'])}: "
                f"{eur(pending['amount'])} a «{pending['envelope']['name']}»")
    finally:
        conn.close()


async def backup_loop(app: FastAPI) -> None:
    """Cada hora: copia del día si falta y sobras del mes cerrado si toca."""
    while True:
        try:
            hecho = await asyncio.to_thread(run_pending_sweep, app.state.db_path, app.state.today())
            if hecho:
                log.info("Guardadas las %s", hecho)
        except Exception:  # noqa: BLE001 - que no pare el bucle de copias
            log.exception("Fallo guardando las sobras del mes")
        try:
            keep_days = await asyncio.to_thread(backup_keep_days, app.state.db_path)
            path = await asyncio.to_thread(backup.daily_backup_if_needed, app.state.db_path,
                                           app.state.backup_dir, config.now(), keep_days)
            if path:
                log.info("Copia diaria hecha: %s", path.name)
            app.state.backup_error = None
        except Exception as e:  # noqa: BLE001 - que un fallo no pare el bucle
            log.exception("Fallo en la copia diaria")
            app.state.backup_error = f"{config.now():%d/%m/%Y %H:%M}: {e}"
        await asyncio.sleep(app.state.backup_interval)


def backup_keep_days(path: Path) -> int:
    conn = db.connect(path)
    try:
        return repo.rule(conn, "backup_keep_days")
    finally:
        conn.close()


def prepare_database(path: Path) -> None:
    db.init_db(path)
    conn = db.connect(path)
    try:
        result = seed.seed(conn)
        if not result.get("omitido"):
            log.info("Base de datos nueva: datos iniciales cargados %s", result)
        if not repo.setting(conn, "secret_key"):
            with conn:
                repo.set_setting(conn, "secret_key", secrets.token_hex(32))
        # bases creadas antes de que existieran las «sobras del mes»: elegir el Colchón
        if repo.setting(conn, "sweep_envelope_id") is None and repo.setting(conn, "sweep_migrated") is None:
            colchon = conn.execute("SELECT id FROM envelopes WHERE name = 'Colchón'").fetchone()
            with conn:
                if colchon:
                    repo.set_setting(conn, "sweep_envelope_id", colchon["id"])
                repo.set_setting(conn, "sweep_migrated", "1")
    finally:
        conn.close()


def create_app(db_path: Path | None = None, backup_dir: Path | None = None,
               backups: bool = True, today_fn=None) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prepare_database(app.state.db_path)
        task = asyncio.create_task(backup_loop(app)) if backups else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Finanzas", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = Path(db_path or config.DB_PATH)
    app.state.backup_dir = Path(backup_dir or config.BACKUP_DIR)
    app.state.backup_interval = 3600
    app.state.backup_error = None
    app.state.today = today_fn or config.today
    app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")

    @app.middleware("http")
    async def pin_guard(request: Request, call_next):
        path = request.url.path
        pin_hash, copias_offline = None, True
        if not (path.startswith("/static/") or path in OPEN_PATHS):
            conn = db.connect(request.app.state.db_path)
            try:
                pin_hash = repo.setting(conn, "pin_hash")
                secret = repo.setting(conn, "secret_key", "")
                copias_offline = repo.offline_cache(conn)
            except sqlite3.Error:
                pin_hash, copias_offline = None, False
            finally:
                conn.close()
            if pin_hash and not pin.valid_token(request.cookies.get(pin.COOKIE), secret, pin_hash):
                target = path + (f"?{request.url.query}" if request.url.query else "")
                url = "/pin?" + urlencode({"next": target})
                if is_htmx(request):
                    return Response(status_code=200, headers={"HX-Redirect": url})
                return RedirectResponse(url, status_code=303)
        response = await call_next(request)
        if not path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
            if not copias_offline:
                # el móvil no guardará copias de estas pantallas (ver Ajustes →
                # Sin conexión): con PIN, esas copias se verían sin pedirlo
                response.headers["X-Sin-Copia"] = "1"
        return response

    app.include_router(router)
    return app


# ---------------------------------------------------------------- rutas
from fastapi import APIRouter  # noqa: E402

router = APIRouter()


@router.get("/salud")
def health(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        conn.execute("SELECT 1 FROM settings LIMIT 1").fetchall()
    finally:
        conn.close()
    return {"ok": True, "version": config.APP_VERSION}


# ---- Inicio

@router.get("/")
def panel(request: Request, mes: str = "", conn=Depends(get_db)):
    hoy = today(request)
    year, month = parse_month(mes, (hoy.year, hoy.month))
    txs = repo.all_txs(conn)
    cards, total = views.envelopes_overview(conn, txs)
    return render(request, "panel.html", {
        "sweep": views.pending_sweep(conn, txs, hoy),
        "year": year, "month": month, "is_current": (year, month) == (hoy.year, hoy.month),
        "m": calc.month_summary(txs, year, month),
        "cards": cards, "total_envelopes": total,
        "cuadre": views.reconciliation(conn, total),
        "debts": views.debts(conn, txs, hoy),
        "budget": views.budget(conn, txs, year, month),
    })


# ---- Movimientos

def read_tx_form(conn, form) -> tuple[dict, dict | None]:
    """Lee y valida el formulario de movimiento. Devuelve (valores para repintar, campos para guardar)."""
    type_ = form.get("tipo") or ""
    errors = []
    amount_ok = True
    try:
        amount = parse_amount(form.get("importe"))
    except InvalidAmount:
        amount, amount_ok = None, False
        errors.append("El importe no es válido: escribe solo la cifra, por ejemplo 12,50.")
    category_id = int_or_none(form.get("categoria_id"))
    envelope_id = int_or_none(form.get("sobre_id"))
    account_id = int_or_none(form.get("cuenta_id"))
    if type_ in calc.NEEDS_ENVELOPE:
        category_id = None
    if type_ == calc.INGRESO:
        envelope_id = None
    try:
        day = parse_date(form.get("fecha"))
    except ValueError:
        day = None
        errors.append("La fecha no es válida.")
    concept = (form.get("concepto") or "").strip()[:200]
    errors += calc.validate_tx(type_, amount if amount_ok else 1, category_id, envelope_id)
    if category_id and not repo.get_row(conn, "categories", category_id):
        errors.append("Esa categoría ya no existe.")
    if envelope_id and not repo.get_row(conn, "envelopes", envelope_id):
        errors.append("Ese sobre ya no existe.")
    if not account_id or not repo.get_row(conn, "accounts", account_id):
        errors.append("Elige la cuenta.")
    uid = (form.get("uid") or "").strip()[:40] or None
    values = {"tipo": type_, "importe": form.get("importe") or "", "categoria_id": category_id,
              "sobre_id": envelope_id, "cuenta_id": account_id,
              "fecha": day.isoformat() if day else (form.get("fecha") or ""), "concepto": concept,
              "errors": errors}
    if errors:
        return values, None
    return values, dict(date=day.isoformat(), type=type_, category_id=category_id, envelope_id=envelope_id,
                        account_id=account_id, concept=concept, amount=amount, client_uid=uid)


@router.get("/nuevo")
def new_tx_page(request: Request, conn=Depends(get_db)):
    return render(request, "nuevo.html", {"hide_fab": True})


@router.post("/movimientos/nuevo")
async def create_tx(request: Request, conn=Depends(get_db)):
    form = await request.form()
    values, fields = read_tx_form(conn, form)
    in_dialog = form.get("origen") == "dialogo" and is_htmx(request)
    # reenvío de algo apuntado sin conexión: si ya entró, no se duplica
    repetido = repo.tx_by_uid(conn, fields["client_uid"]) if fields and fields.get("client_uid") else None
    if repetido is not None:
        log.info("Movimiento ya guardado (uid %s), no lo duplico", fields["client_uid"])
        return Response(status_code=200, headers={"HX-Trigger": json.dumps({"txGuardado": "Ya estaba apuntado"})})
    if fields is None:
        opts = views.tx_form_options(conn)
        if in_dialog:
            return render(request, "partials/txform.html",
                          {"opts": opts, "f": values, "dialog": True, "action": "/movimientos/nuevo"})
        return render(request, "nuevo.html", {"hide_fab": True, "form_values": values})
    with conn:
        tx_id = repo.insert_tx(conn, **fields)
    tx = repo.get_tx(conn, tx_id)
    message = views.saved_message(tx["type"], tx["amount"], tx["category"], tx["envelope"])
    if in_dialog:
        opts = views.tx_form_options(conn)
        fresh = views.blank_tx_form(opts, today(request))
        trigger = json.dumps({"txGuardado": message})           # ensure_ascii: cabecera en ASCII
        return render(request, "partials/txform.html",
                      {"opts": opts, "f": fresh, "dialog": True, "action": "/movimientos/nuevo"},
                      headers={"HX-Trigger": trigger})
    return redirect(request, "/", message)


@router.post("/sobras")
def sweep_now(request: Request, conn=Depends(get_db)):
    """Guarda a mano las sobras del mes cerrado en el sobre elegido."""
    pending = views.pending_sweep(conn, repo.all_txs(conn), today(request))
    if not pending:
        return redirect(request, "/", "No hay sobras pendientes de guardar.", "error")
    views.do_sweep(conn, pending)
    return redirect(request, "/", f"Guardadas las sobras de {month_name(pending['year'], pending['month'])} "
                                  f"({eur(pending['amount'])}) en «{pending['envelope']['name']}»")


@router.get("/movimientos")
def tx_list(request: Request, mes: str = "", tipo: str = "", categoria: str = "", sobre: str = "",
            conn=Depends(get_db)):
    hoy = today(request)
    all_months = mes == "todos"
    year, month = parse_month(mes, (hoy.year, hoy.month))
    type_ = tipo if tipo in calc.TYPES else None
    cat_id, env_id = int_or_none(categoria), int_or_none(sobre)
    rows = repo.tx_rows(conn, month=None if all_months else (year, month), type_=type_,
                        category_id=cat_id, envelope_id=env_id, limit=1000 if all_months else None)
    filters = {"tipo": type_ or "", "categoria": cat_id or "", "sobre": env_id or ""}
    return render(request, "movimientos.html", {
        "rows": rows, "year": year, "month": month, "all_months": all_months,
        "filters": filters, "filtered": any(filters.values()),
        "filter_qs": urlencode({k: v for k, v in filters.items() if v}),
        "m": None if all_months else calc.month_summary(repo.all_txs(conn), year, month),
        "categories": repo.categories(conn), "envelopes": repo.envelopes(conn),
    })


def month_of(tx) -> str:
    return tx["date"][:7]


@router.get("/movimientos/{tx_id:int}")
def edit_tx_page(request: Request, tx_id: int, conn=Depends(get_db)):
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404, "Ese movimiento no existe")
    opts = views.tx_form_options(conn, tx["category_id"], tx["envelope_id"], tx["account_id"], tx["type"])
    values = {"tipo": tx["type"], "importe": amount_input(tx["amount"]), "categoria_id": tx["category_id"],
              "sobre_id": tx["envelope_id"], "cuenta_id": tx["account_id"], "fecha": tx["date"],
              "concepto": tx["concept"], "errors": []}
    return render(request, "movimiento.html", {"tx": tx, "opts": opts, "f": values, "back": f"/movimientos?mes={month_of(tx)}"})


@router.post("/movimientos/{tx_id:int}")
async def update_tx(request: Request, tx_id: int, conn=Depends(get_db)):
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404, "Ese movimiento no existe")
    values, fields = read_tx_form(conn, await request.form())
    if fields is None:
        opts = views.tx_form_options(conn, tx["category_id"], tx["envelope_id"], tx["account_id"],
                                     values["tipo"] or tx["type"])
        return render(request, "movimiento.html", {"tx": tx, "opts": opts, "f": values,
                                                   "back": f"/movimientos?mes={month_of(tx)}"})
    with conn:
        repo.update_tx(conn, tx_id, **fields)
    return redirect(request, f"/movimientos?mes={fields['date'][:7]}", "Cambios guardados")


@router.post("/movimientos/{tx_id:int}/borrar")
def delete_tx(request: Request, tx_id: int, conn=Depends(get_db)):
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404, "Ese movimiento no existe")
    with conn:
        repo.delete_tx(conn, tx_id)
    return redirect(request, f"/movimientos?mes={month_of(tx)}",
                    f"Movimiento borrado ({calc.TYPES[tx['type']].lower()} de {eur(tx['amount'])})")


# ---- Sobres y cuadre

@router.get("/sobres")
def envelopes_page(request: Request, conn=Depends(get_db)):
    txs = repo.all_txs(conn)
    cards, total = views.envelopes_overview(conn, txs, include_archived=True)
    return render(request, "sobres.html", {
        "cards": [c for c in cards if c["env"]["active"]],
        "archived": [c for c in cards if not c["env"]["active"]],
        "total_envelopes": total, "cuadre": views.reconciliation(conn, total),
        "checks": repo.checks(conn), "hoy": today(request).isoformat(),
    })


@router.post("/cuadre")
async def add_check(request: Request, conn=Depends(get_db)):
    form = await request.form()
    error = None
    try:
        balance = parse_amount(form.get("saldo"))
        if balance is None:
            error = "Escribe el saldo que ves en la app de tu banco."
    except InvalidAmount:
        balance, error = None, "El saldo no es válido: escribe solo la cifra, por ejemplo 3003,70."
    try:
        day = parse_date(form.get("fecha") or today(request).isoformat())
    except ValueError:
        day, error = None, error or "La fecha no es válida."
    if not error:
        with conn:
            repo.add_check(conn, day, balance)
    txs = repo.all_txs(conn)
    _, total = views.envelopes_overview(conn, txs)
    ctx = {"cuadre": views.reconciliation(conn, total), "checks": repo.checks(conn),
           "hoy": today(request).isoformat(), "total_envelopes": total, "cuadre_error": error,
           "saldo_escrito": form.get("saldo") or ""}
    if is_htmx(request):
        return render(request, "partials/cuadre.html", ctx)
    return redirect(request, "/sobres", error or "Saldo guardado", "error" if error else "ok")


@router.post("/cuadre/{check_id:int}/borrar")
def delete_check(request: Request, check_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM tr_checks WHERE id = ?", (check_id,))
    return redirect(request, "/sobres#cuadre", "Saldo borrado")


@router.get("/sobres/{env_id:int}")
def envelope_detail(request: Request, env_id: int, conn=Depends(get_db)):
    env = repo.get_row(conn, "envelopes", env_id)
    if not env:
        raise HTTPException(404, "Ese sobre no existe")
    history, balance = [], 0
    for row in repo.tx_rows(conn, envelope_id=env_id, oldest_first=True):
        delta = calc.envelope_delta(calc.Tx(date.fromisoformat(row["date"]), row["type"], row["amount"],
                                            row["category_id"], row["envelope_id"]))
        balance += delta
        history.append({"row": row, "delta": delta, "balance": balance})
    history.reverse()
    return render(request, "sobre.html", {
        "env": env, "st": calc.envelope_status(balance, env["target"], env["monthly"]),
        "history": history, "back": "/sobres",
    })


# ---- Presupuesto, resumen, deudas

@router.get("/presupuesto")
def budget_page(request: Request, mes: str = "", conn=Depends(get_db)):
    hoy = today(request)
    year, month = parse_month(mes, (hoy.year, hoy.month))
    return render(request, "presupuesto.html", {
        "year": year, "month": month, "b": views.budget(conn, repo.all_txs(conn), year, month)})


@router.get("/presupuesto/editar")
def budget_edit_page(request: Request, mes: str = "", conn=Depends(get_db)):
    hoy = today(request)
    year, month = parse_month(mes, (hoy.year, hoy.month))
    spent = calc.spent_by_category(repo.all_txs(conn), year, month)
    cats = [c for c in repo.categories(conn) if c["kind"] == "gasto" and (c["active"] or spent.get(c["id"]))]
    return render(request, "presupuesto_editar.html", {
        "year": year, "month": month, "cats": cats, "spent": spent, "hide_fab": True,
        "back": f"/presupuesto?mes={month_key(year, month)}"})


@router.post("/presupuesto/editar")
async def budget_edit_save(request: Request, conn=Depends(get_db)):
    form = await request.form()
    year, month = parse_month(form.get("mes"), (today(request).year, today(request).month))
    errors, updates = [], {}
    for cat in repo.categories(conn):
        field = f"presupuesto_{cat['id']}"
        if field not in form:
            continue
        value = parse_optional_amount(form.get(field), f"«{cat['name']}»", errors)
        updates[cat["id"]] = value
    nueva = " ".join((form.get("nueva_categoria") or "").split())[:60]
    nuevo_importe = parse_optional_amount(form.get("nuevo_presupuesto"), "La categoría nueva", errors) if nueva else None
    if errors:
        return redirect(request, f"/presupuesto/editar?mes={month_key(year, month)}", " ".join(errors), "error")
    try:
        with conn:
            for cat_id, value in updates.items():
                conn.execute("UPDATE categories SET budget = ? WHERE id = ?", (value, cat_id))
            if nueva:
                conn.execute("INSERT INTO categories (name, kind, budget, position) VALUES (?, 'gasto', ?, ?)",
                             (nueva, nuevo_importe, repo.next_position(conn, "categories")))
    except sqlite3.IntegrityError:
        return redirect(request, f"/presupuesto/editar?mes={month_key(year, month)}",
                        f"Ya existe una categoría llamada «{nueva}».", "error")
    hecho = "Presupuestos guardados" + (f" · categoría «{nueva}» creada" if nueva else "")
    return redirect(request, f"/presupuesto?mes={month_key(year, month)}", hecho)


@router.get("/resumen")
def summary_page(request: Request, desde: str = "", conn=Depends(get_db)):
    start = parse_month(desde, repo.start_month(conn))
    months, total = views.summary_months(repo.all_txs(conn), start)
    chart = {
        "labels": [month_short(m.year, m.month) for m in months],
        "series": [
            {"name": "Ingresos", "slot": 1, "data": [m.income / 100 for m in months]},
            {"name": "Gastos de la nómina", "slot": 2, "data": [m.payroll_spent / 100 for m in months]},
            {"name": "Aportado a sobres", "slot": 3, "data": [m.saved / 100 for m in months]},
        ],
    }
    return render(request, "resumen.html", {
        "months": months, "total": total, "start": start, "default_start": repo.start_month(conn),
        "chart_json": json.dumps(chart), "hoy": today(request)})


@router.get("/deudas")
def debts_page(request: Request, conn=Depends(get_db)):
    return render(request, "deudas.html", {"d": views.debts(conn, repo.all_txs(conn), today(request))})


@router.get("/mas")
def more_page(request: Request, conn=Depends(get_db)):
    backups = backup.list_backups(request.app.state.backup_dir)
    return render(request, "mas.html", {"last_backup": backups[0] if backups else None,
                                        "keep_days": repo.rule(conn, "backup_keep_days")})


# ---- Ajustes

CATALOGS = {
    "categorias": ("categories", "categoría"),
    "sobres": ("envelopes", "sobre"),
    "cuentas": ("accounts", "cuenta"),
}


def settings_url(section: str) -> str:
    return f"/ajustes?abierto={section}#{section}"


@router.get("/ajustes")
def settings_page(request: Request, abierto: str = "", conn=Depends(get_db)):
    s = repo.settings(conn)
    backups = backup.list_backups(request.app.state.backup_dir)
    start = repo.start_month(conn)
    return render(request, "ajustes.html", {
        "open": abierto, "s": s, "salary": repo.salary(conn), "start_month": month_key(*start),
        "categories": repo.categories(conn), "envelopes": repo.envelopes(conn),
        "accounts": repo.accounts(conn), "loans": repo.loans(conn),
        "amort_id": repo.int_setting(conn, "amortization_category_id"),
        "default_account_id": repo.int_setting(conn, "default_account_id"),
        "envelope_account_id": repo.int_setting(conn, "envelope_account_id"),
        "rules": {k: repo.rule(conn, k) for k in repo.RULES},
        "usados": views.used_ids(conn),
        "sweep": views.sweep_settings(conn),
        "copias_offline": repo.offline_cache(conn),
        "backups": backups[:10], "backup_count": len(backups),
        "backup_error": request.app.state.backup_error,
        "backup_problem": backup.dir_problem(request.app.state.backup_dir), "keep_days": repo.rule(conn, "backup_keep_days"),
        "db_size": request.app.state.db_path.stat().st_size if request.app.state.db_path.exists() else 0,
    })


def parse_optional_amount(text, label: str, errors: list, allow_zero: bool = True) -> int | None:
    try:
        value = parse_amount(text)
    except InvalidAmount:
        errors.append(f"{label}: «{text}» no es un importe válido.")
        return None
    if value is not None and (value < 0 or (value == 0 and not allow_zero)):
        errors.append(f"{label} tiene que ser mayor que 0.")
        return None
    return value


@router.post("/ajustes/general")
async def save_general(request: Request, conn=Depends(get_db)):
    form = await request.form()
    errors = []
    salary = parse_optional_amount(form.get("nomina"), "La nómina", errors, allow_zero=False)
    start = parse_month(form.get("mes_inicio"), (0, 0))
    if start == (0, 0):
        errors.append("El mes de inicio no es válido.")
    if errors:
        return redirect(request, settings_url("general"), " ".join(errors), "error")
    with conn:
        repo.set_setting(conn, "salary", salary)
        repo.set_setting(conn, "start_month", f"{month_key(*start)}-01")
        for key, field in (("amortization_category_id", "categoria_amortizacion"),
                           ("default_account_id", "cuenta_general"), ("envelope_account_id", "cuenta_sobres")):
            repo.set_setting(conn, key, int_or_none(form.get(field)))
    return redirect(request, settings_url("general"), "Ajustes guardados")


@router.post("/ajustes/sobras")
async def save_sweep(request: Request, conn=Depends(get_db)):
    form = await request.form()
    envelope_id = int_or_none(form.get("sobre"))
    if envelope_id and not repo.get_row(conn, "envelopes", envelope_id):
        return redirect(request, settings_url("sobras"), "Ese sobre no existe.", "error")
    day = int_or_none(form.get("dia"))
    if day is None or not (1 <= day <= 28):
        return redirect(request, settings_url("sobras"), "El día tiene que estar entre 1 y 28.", "error")
    with conn:
        repo.set_setting(conn, "sweep_envelope_id", envelope_id)
        repo.set_setting(conn, "sweep_enabled", "1" if form.get("activo") else "0")
        repo.set_setting(conn, "sweep_day", day)
    return redirect(request, settings_url("sobras"), "Guardado")


@router.post("/ajustes/reglas")
async def save_rules(request: Request, conn=Depends(get_db)):
    form = await request.form()
    limites = {"budget_warn_pct": (1, 100), "installments_limit_pct": (1, 100),
               "backup_keep_days": (1, 3650)}
    errors, values = [], {}
    for key, (minimo, maximo) in limites.items():
        value = int_or_none(form.get(key))
        if value is None or not (minimo <= value <= maximo):
            errors.append(f"«{RULE_LABELS[key]}» tiene que ser un número entre {minimo} y {maximo}.")
        else:
            values[key] = value
    values["reconcile_small_cents"] = parse_optional_amount(
        form.get("reconcile_small_cents"), f"«{RULE_LABELS['reconcile_small_cents']}»", errors) or 0
    if errors:
        return redirect(request, settings_url("reglas"), " ".join(errors), "error")
    with conn:
        for key, value in values.items():
            repo.set_setting(conn, key, value)
    return redirect(request, settings_url("reglas"), "Reglas guardadas")


@router.post("/ajustes/restablecer-reglas")
def reset_rules(request: Request, conn=Depends(get_db)):
    with conn:
        for key in repo.RULES:
            repo.set_setting(conn, key, None)
    return redirect(request, settings_url("reglas"), "Reglas vueltas a los valores de fábrica")


WIPE_WORD = "BORRAR"


@router.post("/ajustes/empezar-de-cero")
async def wipe(request: Request, conn=Depends(get_db)):
    """Vacía la app para empezar limpio. Hace una copia de seguridad antes."""
    form = await request.form()
    scope = form.get("alcance")
    if (form.get("confirmacion") or "").strip().upper() != WIPE_WORD:
        return redirect(request, settings_url("cero"),
                        f"Para borrar hay que escribir {WIPE_WORD} en la casilla.", "error")
    if scope not in ("movimientos", "todo"):
        return redirect(request, settings_url("cero"), "Elige qué quieres borrar.", "error")
    try:
        copia = await asyncio.to_thread(backup.make_backup, request.app.state.db_path,
                                        request.app.state.backup_dir, config.now(), True)
    except Exception as e:  # noqa: BLE001 - sin copia previa no se borra nada
        log.exception("No se pudo hacer la copia previa al borrado")
        return redirect(request, settings_url("cero"),
                        f"No he borrado nada: no pude hacer la copia de seguridad previa ({e}).", "error")
    with conn:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM tr_checks")
        if scope == "todo":
            conn.execute("DELETE FROM loans")
            conn.execute("DELETE FROM categories")
            conn.execute("DELETE FROM envelopes")
            conn.execute("DELETE FROM accounts")
            repo.set_setting(conn, "amortization_category_id", None)
            cur = conn.execute("INSERT INTO accounts (name, position) VALUES ('Cuenta principal', 0)")
            repo.set_setting(conn, "default_account_id", cur.lastrowid)
            repo.set_setting(conn, "envelope_account_id", cur.lastrowid)
    que = "los movimientos" if scope == "movimientos" else "todo"
    return redirect(request, "/", f"Borrado {que}. Antes guardé una copia: {copia.name}")


RULE_LABELS = {
    "budget_warn_pct": "Aviso del presupuesto",
    "installments_limit_pct": "Límite de cuotas sobre la nómina",
    "reconcile_small_cents": "Diferencia de cuadre que no preocupa",
    "backup_keep_days": "Días de copias guardadas",
}


def catalog_fields(kind: str, form, errors: list) -> dict:
    name = " ".join((form.get("nombre") or "").split())[:60]
    if not name:
        errors.append("Escribe un nombre.")
    fields = {"name": name}
    if kind == "categorias":
        fields["kind"] = "ingreso" if form.get("tipo") == "ingreso" else "gasto"
        fields["budget"] = parse_optional_amount(form.get("presupuesto"), "El presupuesto", errors)
    elif kind == "sobres":
        fields["target"] = parse_optional_amount(form.get("objetivo"), "El objetivo", errors, allow_zero=False)
        fields["monthly"] = parse_optional_amount(form.get("aporte"), "El aporte mensual", errors, allow_zero=False)
        fields["note"] = (form.get("nota") or "").strip()[:200]
    return fields


@router.post("/ajustes/lista/{kind}")
async def catalog_create(request: Request, kind: str, conn=Depends(get_db)):
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, label = CATALOGS[kind]
    errors = []
    fields = catalog_fields(kind, await request.form(), errors)
    if not errors:
        fields["position"] = repo.next_position(conn, table)
        try:
            with conn:
                conn.execute(f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                             list(fields.values()))
        except sqlite3.IntegrityError:
            errors.append(f"Ya existe una {label} llamada «{fields['name']}»." if label != "sobre"
                          else f"Ya existe un sobre llamado «{fields['name']}».")
    if errors:
        return redirect(request, settings_url(kind), " ".join(errors), "error")
    return redirect(request, settings_url(kind), f"Creado: {fields['name']}")


@router.post("/ajustes/lista/{kind}/{row_id:int}")
async def catalog_update(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, label = CATALOGS[kind]
    if not repo.get_row(conn, table, row_id):
        raise HTTPException(404)
    errors = []
    fields = catalog_fields(kind, await request.form(), errors)
    if not errors:
        try:
            with conn:
                conn.execute(f"UPDATE {table} SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                             list(fields.values()) + [row_id])
        except sqlite3.IntegrityError:
            errors.append(f"Ya hay otro elemento llamado «{fields['name']}».")
    if errors:
        return redirect(request, settings_url(kind), " ".join(errors), "error")
    return redirect(request, settings_url(kind), f"Guardado: {fields['name']}")


@router.post("/ajustes/lista/{kind}/{row_id:int}/archivar")
def catalog_toggle(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, _ = CATALOGS[kind]
    row = repo.get_row(conn, table, row_id)
    if not row:
        raise HTTPException(404)
    with conn:
        conn.execute(f"UPDATE {table} SET active = ? WHERE id = ?", (0 if row["active"] else 1, row_id))
    what = "archivado" if row["active"] else "reactivado"
    return redirect(request, settings_url(kind), f"«{row['name']}» {what}")


@router.post("/ajustes/lista/{kind}/{row_id:int}/borrar")
def catalog_delete(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    """Borra de verdad, pero solo lo que no ha usado nunca nadie."""
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, label = CATALOGS[kind]
    row = repo.get_row(conn, table, row_id)
    if not row:
        raise HTTPException(404)
    if row_id in views.used_ids(conn)[kind]:
        return redirect(request, settings_url(kind),
                        f"«{row['name']}» no se puede borrar: tiene movimientos o está elegido en los ajustes. "
                        f"Archívalo y desaparece de en medio sin tocar el historial.", "error")
    if table == "accounts" and len(repo.accounts(conn)) <= 1:
        return redirect(request, settings_url(kind), "Tiene que quedar al menos una cuenta.", "error")
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    return redirect(request, settings_url(kind), f"«{row['name']}» borrado")


@router.post("/ajustes/lista/{kind}/{row_id:int}/mover")
async def catalog_move(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    if kind not in CATALOGS and kind != "prestamos":
        raise HTTPException(404)
    table = "loans" if kind == "prestamos" else CATALOGS[kind][0]
    direction = -1 if (await request.form()).get("dir") == "arriba" else 1
    with conn:
        repo.move(conn, table, row_id, direction)
    return redirect(request, settings_url(kind))


def loan_fields(form, errors: list) -> dict:
    name = " ".join((form.get("nombre") or "").split())[:60]
    if not name:
        errors.append("Escribe un nombre.")
    installment = parse_optional_amount(form.get("cuota"), "La cuota", errors, allow_zero=False)
    if installment is None and not errors:
        errors.append("Escribe la cuota.")
    try:
        first = parse_date(form.get("primera")).isoformat()
    except ValueError:
        first = None
        errors.append("La fecha de la 1.ª cuota no es válida.")
    count = int_or_none(form.get("cuotas"))
    if not count or count <= 0:
        errors.append("El número de cuotas tiene que ser mayor que 0.")
    return {"name": name, "installment": installment, "first_date": first, "n_installments": count}


@router.post("/ajustes/prestamos")
async def loan_create(request: Request, conn=Depends(get_db)):
    errors = []
    fields = loan_fields(await request.form(), errors)
    if errors:
        return redirect(request, settings_url("prestamos"), " ".join(errors), "error")
    fields["position"] = repo.next_position(conn, "loans")
    with conn:
        conn.execute(f"INSERT INTO loans ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                     list(fields.values()))
    return redirect(request, settings_url("prestamos"), f"Préstamo creado: {fields['name']}")


@router.post("/ajustes/prestamos/{loan_id:int}")
async def loan_update(request: Request, loan_id: int, conn=Depends(get_db)):
    errors = []
    fields = loan_fields(await request.form(), errors)
    if errors:
        return redirect(request, settings_url("prestamos"), " ".join(errors), "error")
    with conn:
        conn.execute(f"UPDATE loans SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                     list(fields.values()) + [loan_id])
    return redirect(request, settings_url("prestamos"), f"Guardado: {fields['name']}")


@router.post("/ajustes/prestamos/{loan_id:int}/borrar")
def loan_delete(request: Request, loan_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
    return redirect(request, settings_url("prestamos"), "Préstamo borrado")


# ---- PIN

@router.post("/ajustes/sin-conexion")
async def save_offline(request: Request, conn=Depends(get_db)):
    form = await request.form()
    with conn:
        repo.set_setting(conn, "offline_cache", "1" if form.get("activo") else "0")
    return redirect(request, settings_url("sinconexion"),
                    "Guardado. Cierra la app y vuelve a abrirla para que el móvil se entere.")


@router.post("/ajustes/pin")
async def pin_set(request: Request, conn=Depends(get_db)):
    form = await request.form()
    new, again = (form.get("pin") or "").strip(), (form.get("pin2") or "").strip()
    if not re.fullmatch(r"\d{4,8}", new):
        return redirect(request, settings_url("pin"), "El PIN tiene que tener entre 4 y 8 cifras.", "error")
    if new != again:
        return redirect(request, settings_url("pin"), "Los dos PIN no coinciden.", "error")
    pin_hash = pin.hash_pin(new)
    with conn:
        repo.set_setting(conn, "pin_hash", pin_hash)
    response = redirect(request, settings_url("pin"), "PIN activado")
    set_pin_cookie(request, response, repo.setting(conn, "secret_key", ""), pin_hash)
    return response


@router.post("/ajustes/pin/quitar")
def pin_remove(request: Request, conn=Depends(get_db)):
    with conn:
        repo.set_setting(conn, "pin_hash", None)
    response = redirect(request, settings_url("pin"), "PIN quitado: la app ya no lo pedirá")
    response.delete_cookie(pin.COOKIE)
    return response


def set_pin_cookie(request: Request, response: Response, secret: str, pin_hash: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(pin.COOKIE, pin.token(secret, pin_hash), max_age=365 * 24 * 3600,
                        httponly=True, samesite="lax", secure=secure)


def safe_next(target: str | None) -> str:
    ok = target and target.startswith("/") and not target.startswith("//") and "\\" not in target
    return target if ok else "/"


@router.get("/pin")
def pin_page(request: Request, next: str = "/"):
    return render(request, "pin.html", {"next": safe_next(next), "hide_fab": True, "hide_nav": True})


@router.post("/pin")
async def pin_check(request: Request):
    form = await request.form()
    target = safe_next(form.get("next"))
    ctx = {"next": target, "hide_fab": True, "hide_nav": True}
    wait = pin.locked_seconds()
    if wait:
        return render(request, "pin.html", {**ctx, "error": f"Demasiados intentos. Espera {wait // 60 + 1} min."})
    conn = db.connect(request.app.state.db_path)
    try:
        pin_hash, secret = repo.setting(conn, "pin_hash"), repo.setting(conn, "secret_key", "")
    finally:
        conn.close()
    if not pin_hash:
        return RedirectResponse(target, status_code=303)
    ok = pin.check_pin((form.get("pin") or "").strip(), pin_hash)
    pin.register_attempt(ok)
    if not ok:
        return render(request, "pin.html", {**ctx, "error": "PIN incorrecto."})
    response = RedirectResponse(target, status_code=303)
    set_pin_cookie(request, response, secret, pin_hash)
    return response


# ---- Copias, exportar, importar

@router.post("/ajustes/copia")
async def manual_backup(request: Request):
    try:
        path = await asyncio.to_thread(backup.make_backup, request.app.state.db_path,
                                       request.app.state.backup_dir, config.now(), True)
    except Exception as e:  # noqa: BLE001
        log.exception("Fallo en la copia manual")
        return redirect(request, settings_url("copias"), f"No se pudo hacer la copia: {e}", "error")
    return redirect(request, settings_url("copias"), f"Copia hecha: {path.name}")


@router.get("/ajustes/copia/{name}")
def download_backup(request: Request, name: str):
    if not backup.NAME_RE.match(name):
        raise HTTPException(404)
    path = request.app.state.backup_dir / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=name, media_type="application/vnd.sqlite3")


def stamp() -> str:
    return config.now().strftime("%Y-%m-%d_%H%M")


@router.get("/exportar/json")
def export_json(conn=Depends(get_db)):
    data = export.to_json(conn, config.now().isoformat(timespec="seconds"), config.APP_VERSION)
    return Response(data, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="finanzas-{stamp()}.json"'})


@router.get("/exportar/csv")
def export_csv(conn=Depends(get_db)):
    return Response(export.to_csv_zip(conn), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="finanzas-csv-{stamp()}.zip"'})


@router.get("/ajustes/importar")
def import_page(request: Request, conn=Depends(get_db)):
    return render(request, "importar.html", {"back": settings_url("importar")})


MAX_IMPORT = 5 * 1024 * 1024


@router.post("/ajustes/importar")
async def import_preview(request: Request, conn=Depends(get_db)):
    form = await request.form()
    upload = form.get("archivo")
    data = await upload.read(MAX_IMPORT + 1) if upload and hasattr(upload, "read") else b""
    if not data:
        return render(request, "importar.html", {"error": "Elige el archivo CSV.", "back": settings_url("importar")})
    if len(data) > MAX_IMPORT:
        return render(request, "importar.html", {"error": "El archivo es demasiado grande (máx. 5 MB).",
                                                 "back": settings_url("importar")})
    text = importer.decode(data)
    preview = importer.parse(conn, text)
    return render(request, "importar.html", {
        "preview": preview, "payload": base64.b64encode(text.encode()).decode(),
        "filename": getattr(upload, "filename", ""), "back": settings_url("importar")})


@router.post("/ajustes/importar/confirmar")
async def import_confirm(request: Request, conn=Depends(get_db)):
    form = await request.form()
    try:
        text = base64.b64decode(form.get("payload") or "").decode()
    except (ValueError, UnicodeDecodeError):
        return redirect(request, "/ajustes/importar", "No he podido leer los datos. Vuelve a subir el archivo.", "error")
    preview = importer.parse(conn, text)
    if preview.error:
        return redirect(request, "/ajustes/importar", preview.error, "error")
    added = importer.apply(conn, preview, create_missing=form.get("crear") == "1")
    return redirect(request, "/movimientos?mes=todos", f"Importados {added} movimientos")


# ---- PWA

@router.get("/manifest.webmanifest")
def manifest():
    data = {
        "name": "Finanzas", "short_name": "Finanzas", "lang": "es-ES", "dir": "ltr",
        "description": "Mis finanzas personales",
        "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
        "background_color": "#f9f9f7", "theme_color": "#f9f9f7",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return JSONResponse(data, media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker(request: Request):
    return templates.TemplateResponse(request, "sw.js", {"version": config.APP_VERSION},
                                      media_type="text/javascript",
                                      headers={"Cache-Control": "no-cache"})


@router.get("/offline")
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {"version": config.APP_VERSION})


app = create_app()
