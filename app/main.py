"""App web de finanzas: FastAPI + plantillas Jinja2 + HTMX.

Arranque:  uvicorn app.main:app
"""
from __future__ import annotations

import asyncio
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import backup, banco, config, db, pin, repo, seed, views
from .money import eur, month_name
from .web import is_htmx, log, OPEN_PATHS
from .rutas import ROUTERS


def run_pending_sweep(db_path: Path, hoy: date) -> str | None:
    """Guarda solo las sobras del mes cerrado, si toca. Devuelve un texto para el registro."""
    conn = db.connect(db_path)
    try:
        pending = views.pending_sweep(conn, repo.all_txs(conn), hoy)
        if not pending or not pending["due"]:
            return None
        views.do_sweep(conn, pending)
        if pending["mode"] == "cobro":
            return f"lo que quedaba antes de cobrar el {pending['payday']:%d/%m}: {eur(pending['amount'])}"
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
            resumen = await asyncio.to_thread(run_bank_sync, app.state.db_path, app.state.today())
            if resumen:
                log.info("Banco: %s", resumen)
        except Exception:  # noqa: BLE001 - que un fallo del banco no pare las copias
            log.exception("Fallo sincronizando con el banco")
        try:
            if await asyncio.to_thread(run_weekly_summary, app.state.db_path):
                log.info("Resumen de la semana enviado por Telegram")
        except Exception:  # noqa: BLE001 - que no pare el bucle
            log.exception("Fallo mandando el resumen de la semana")
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


def run_bank_sync(path: Path, today_: date) -> str | None:
    """Sincroniza con el banco si toca (cada 6 horas). Devuelve un resumen o None."""
    conn = db.connect(path)
    try:
        if not banco.configured(conn) or not banco.links(conn) or not banco.auto_sync_due(conn):
            return None
        resultados = banco.sync_all(conn, banco.Client.from_settings(conn), today_)
        with conn:
            repo.set_setting(conn, "bank_last_auto_sync",
                             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return "; ".join(f"{nombre}: {banco.summary_text(r)}" for nombre, r in resultados) or None
    finally:
        conn.close()


def run_weekly_summary(path: Path) -> bool:
    """El resumen del domingo por Telegram, si toca."""
    from . import avisos
    if not avisos.configurado():
        return False
    conn = db.connect(path)
    try:
        return avisos.resumen_semanal(conn, config.now())
    finally:
        conn.close()


def backup_keep_days(path: Path) -> int:
    conn = db.connect(path)
    try:
        return repo.rule(conn, "backup_keep_days")
    finally:
        conn.close()


def prepare_database(path: Path) -> None:
    try:
        db.init_db(path)
    except OSError as e:
        log.error("No puedo escribir en %s (%s). Si es la primera vez, crea las carpetas antes de "
                  "levantar el contenedor para que sean tuyas y no de root:  mkdir -p data backups",
                  path.parent, e)
        raise
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
        # lo que sobra se cuenta antes de cada cobro: los cobros que ya pasaron no se cierran hacia atrás
        if repo.setting(conn, "last_sweep_payday") is None:
            pasados = [d for d in views.paydays(conn) if d <= config.today()]
            with conn:
                repo.set_setting(conn, "last_sweep_payday", (pasados[-1] if pasados else config.today()).isoformat())
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
    # Comprimido: las páginas y el CSS pasan de 25-45 KB a 5-9 KB (se nota con poca cobertura)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def pin_guard(request: Request, call_next):
        path = request.url.path
        pin_hash, copias_offline = None, True
        if not (path.startswith(("/static/", "/api/")) or path in OPEN_PATHS):
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

    for router in ROUTERS:
        app.include_router(router)
    return app


app = create_app()
