"""Planificar: sobres, presupuesto y pagos fijos."""
from __future__ import annotations

import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import calc, repo, views
from ..money import eur, month_key, parse_date, parse_month
from ..web import get_db, int_or_none, parse_optional_amount, redirect, render, today

router = APIRouter()


@router.get("/sobres")
def envelopes_page(request: Request, conn=Depends(get_db)):
    txs = repo.all_txs(conn)
    cards, total = views.envelopes_overview(conn, txs, include_archived=True, today=today(request))
    return render(request, "sobres.html", {
        "cards": [c for c in cards if c["env"]["active"]],
        "archived": [c for c in cards if not c["env"]["active"]],
        "total_envelopes": total, "accounts": repo.accounts(conn, active=True),
        "envelope_account_id": repo.int_setting(conn, "envelope_account_id"),
    })


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
        "env": env, "st": views.envelope_status(env, balance, today(request)),
        "history": history, "back": "/sobres", "accounts": repo.accounts(conn, active=True),
        # un sobre para algo puntual que ya se ha gastado: ¿archivarlo?
        "gastado": env["temporary"] and env["active"] and balance == 0 and bool(history),
    })


@router.post("/sobres/{env_id:int}/aporte-meta")
def envelope_match_deadline(request: Request, env_id: int, conn=Depends(get_db)):
    """Pone como aporte mensual lo que hace falta para llegar a la meta a tiempo."""
    env = repo.get_row(conn, "envelopes", env_id)
    if not env:
        raise HTTPException(404, "Ese sobre no existe")
    balance = calc.envelope_balances(repo.all_txs(conn)).get(env_id, 0)
    st = views.envelope_status(env, balance, today(request))
    if not st.needed:
        return redirect(request, f"/sobres/{env_id}", "Este sobre no tiene una meta con fecha pendiente.", "error")
    with conn:
        conn.execute("UPDATE envelopes SET monthly = ? WHERE id = ?", (st.needed, env_id))
    return redirect(request, f"/sobres/{env_id}", f"Ahora guardas {eur(st.needed)} al mes en «{env['name']}»: "
                                                  f"llegas a tiempo")


@router.get("/presupuesto")
def budget_page(request: Request, mes: str = "", conn=Depends(get_db)):
    hoy = today(request)
    year, month = parse_month(mes, repo.period_of(conn, hoy))
    return render(request, "presupuesto.html", {
        "year": year, "month": month, "b": views.budget(conn, repo.all_txs(conn), year, month)})


@router.get("/presupuesto/editar")
def budget_edit_page(request: Request, mes: str = "", conn=Depends(get_db)):
    hoy = today(request)
    year, month = parse_month(mes, repo.period_of(conn, hoy))
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


@router.get("/fijos")
def fixed_page(request: Request, conn=Depends(get_db)):
    """Pagos fijos: préstamos, seguros y suscripciones (antes «Deudas» y «Gastos fijos», por separado)."""
    return render(request, "fijos.html", {"p": views.fixed_payments(conn, repo.all_txs(conn), today(request)),
                                          "cambios": views.price_alerts(conn, today(request), include_dismissed=True)})


@router.get("/deudas")
def debts_page():
    return RedirectResponse("/fijos", status_code=303)


@router.post("/fijos")
async def fixed_create(request: Request, conn=Depends(get_db)):
    errors = []
    fields = loan_fields(await request.form(), errors)
    if errors:
        return redirect(request, "/fijos", " ".join(errors), "error")
    fields["position"] = repo.next_position(conn, "loans")
    with conn:
        conn.execute(f"INSERT INTO loans ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                     list(fields.values()))
    return redirect(request, "/fijos", f"Añadido: {fields['name']}")


@router.post("/fijos/{loan_id:int}")
async def fixed_update(request: Request, loan_id: int, conn=Depends(get_db)):
    errors = []
    fields = loan_fields(await request.form(), errors)
    if errors:
        return redirect(request, "/fijos", " ".join(errors), "error")
    with conn:
        conn.execute(f"UPDATE loans SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                     list(fields.values()) + [loan_id])
    return redirect(request, "/fijos", f"Guardado: {fields['name']}")


@router.post("/fijos/{loan_id:int}/borrar")
def fixed_delete(request: Request, loan_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
    return redirect(request, "/fijos", "Borrado")


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
    count = None
    if not form.get("sin_fin"):                       # una suscripción o un seguro que se renueva no acaba
        count = int_or_none(form.get("cuotas"))
        if not count or count <= 0:
            errors.append("El número de cuotas tiene que ser mayor que 0 (o marca «sin fin»).")
    kind = form.get("tipo") if form.get("tipo") in views.KINDS else "prestamo"
    return {"name": name, "installment": installment, "first_date": first, "n_installments": count,
            "category_id": int_or_none(form.get("categoria_id")), "kind": kind}


# ---------------------------------------------------------------- repartir la nómina

def _plan_context(request: Request, conn):
    from .. import banco
    retenidos = banco.pending(conn) if repo.modules(conn)["bancos"] else []
    return views.payday_plan(conn, repo.all_txs(conn), today(request), banco.held(retenidos))


@router.get("/reparto")
def plan_page(request: Request, conn=Depends(get_db)):
    return render(request, "reparto.html", {"p": _plan_context(request, conn)})


@router.post("/reparto")
async def plan_save(request: Request, conn=Depends(get_db)):
    """Aparta en cada sobre lo que hayas puesto y da la nómina por repartida."""
    form = await request.form()
    p = _plan_context(request, conn)
    errors, aportes = [], []
    for s in p["envelopes"]:
        importe = parse_optional_amount(form.get(f"sobre_{s['env']['id']}"), f"«{s['env']['name']}»", errors)
        if importe:
            aportes.append((s["env"], importe))
    if errors:
        return redirect(request, "/reparto", " ".join(errors), "error")
    hoy = today(request)
    defecto = repo.int_setting(conn, "envelope_account_id") or repo.int_setting(conn, "default_account_id")
    with conn:
        for env, importe in aportes:
            repo.insert_tx(conn, date=hoy.isoformat(), type=calc.APORTE, category_id=None, envelope_id=env["id"],
                           account_id=env["account_id"] or defecto, concept=views.plan_concept(p["payday"]),
                           amount=importe)
        if p["payday"]:
            repo.set_setting(conn, "plan_payday", p["payday"].isoformat())
    total = sum(i for _, i in aportes)
    queda = p["base"] - total
    if not aportes:
        return redirect(request, "/", f"Nómina repartida: todo para el día a día ({eur(queda)})")
    return redirect(request, "/", f"Apartado {eur(total)} en {len(aportes)} sobre{'s' if len(aportes) != 1 else ''}. "
                                  f"Te quedan {eur(queda)} para el día a día")


@router.post("/reparto/omitir")
def plan_skip(request: Request, conn=Depends(get_db)):
    cobro = views.last_payday(conn, today(request))
    if cobro:
        with conn:
            repo.set_setting(conn, "plan_payday", cobro.isoformat())
    return redirect(request, "/", "Vale, no te lo recuerdo hasta el próximo cobro")


@router.get("/calendario")
def calendar_page(request: Request, mes: str = "", conn=Depends(get_db)):
    hoy = today(request)
    year, month = parse_month(mes, repo.period_of(conn, hoy))
    return render(request, "calendario.html", {
        "c": views.month_calendar(conn, repo.all_txs(conn), year, month, hoy), "year": year, "month": month})
