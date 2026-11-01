"""Las cuentas: saldos y cuadre con el banco."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import calc, repo, views
from ..money import eur, InvalidAmount, parse_amount, parse_date
from ..web import get_db, parse_optional_amount, redirect, render, today

router = APIRouter()


@router.get("/cuentas")
def accounts_page(request: Request, conn=Depends(get_db)):
    txs = repo.all_txs(conn)
    tarjetas, total = views.accounts_overview(conn, txs, include_archived=True)
    return render(request, "cuentas.html", {
        "cards": [c for c in tarjetas if c["account"]["active"]],
        "archived": [c for c in tarjetas if not c["account"]["active"]],
        "total": total, "hoy": today(request).isoformat()})


@router.get("/cuentas/{account_id:int}")
def account_detail(request: Request, account_id: int, conn=Depends(get_db)):
    cuenta = repo.get_row(conn, "accounts", account_id)
    if not cuenta:
        raise HTTPException(404, "Esa cuenta no existe")
    txs = repo.all_txs(conn)
    saldos = calc.account_balances(txs, repo.accounts(conn))
    saldo = saldos.get(account_id, 0)
    history, acumulado = [], cuenta["initial_balance"] or 0
    for row in repo.tx_rows(conn, account_id=account_id, oldest_first=True):
        delta = calc.account_moves(views.tx_de_fila(row)).get(account_id, 0)
        acumulado += delta
        history.append({"row": row, "delta": delta, "balance": acumulado})
    history.reverse()
    sobres = [e for e in repo.envelopes(conn) if e["account_id"] == account_id]
    apartado = sum(calc.envelope_balances(txs).get(e["id"], 0) for e in sobres)
    return render(request, "cuenta.html", {
        "cuenta": cuenta, "balance": saldo, "in_envelopes": apartado, "free": saldo - apartado,
        "sobres": sobres, "history": history, "checks": repo.checks(conn, account_id),
        "cuadre": views.reconciliation(conn, cuenta, txs),
        "hoy": today(request).isoformat(), "back": "/cuentas"})


@router.post("/cuentas/{account_id:int}/comprobar")
async def add_check(request: Request, account_id: int, conn=Depends(get_db)):
    """Apunta el saldo que dice el banco, para ver si cuadra con el de la app."""
    cuenta = repo.get_row(conn, "accounts", account_id)
    if not cuenta:
        raise HTTPException(404)
    form = await request.form()
    error, balance, day = None, None, None
    try:
        balance = parse_amount(form.get("saldo"))
        if balance is None:
            error = f"Escribe el saldo que ves en {cuenta['name']}."
    except InvalidAmount:
        error = "El saldo no es válido: escribe solo la cifra, por ejemplo 1234,56."
    try:
        day = parse_date(form.get("fecha") or today(request).isoformat())
    except ValueError:
        error = error or "La fecha no es válida."
    if error:
        return redirect(request, f"/cuentas/{account_id}", error, "error")
    with conn:
        repo.add_check(conn, account_id, day, balance)
    return redirect(request, f"/cuentas/{account_id}", "Saldo apuntado")


@router.post("/cuentas/{account_id:int}/ajustar-inicial")
def adjust_initial(request: Request, account_id: int, conn=Depends(get_db)):
    """Pone el saldo inicial que hace falta para que la cuenta cuadre con el banco.

    Sirve para empezar: apuntas lo que dice el banco hoy y la app deduce lo que
    había antes de tu primer movimiento.
    """
    cuenta = repo.get_row(conn, "accounts", account_id)
    ultima = repo.last_check(conn, account_id) if cuenta else None
    if not cuenta or not ultima:
        return redirect(request, f"/cuentas/{account_id}",
                        "Primero apunta el saldo que dice el banco.", "error")
    hasta = date.fromisoformat(ultima["date"])
    calculado = calc.account_balance_at(repo.all_txs(conn), account_id,
                                        cuenta["initial_balance"] or 0, hasta)
    diferencia = ultima["balance"] - calculado
    if diferencia == 0:
        return redirect(request, f"/cuentas/{account_id}", "Ya cuadraba: no he tocado nada.")
    with conn:
        conn.execute("UPDATE accounts SET initial_balance = initial_balance + ? WHERE id = ?",
                     (diferencia, account_id))
    return redirect(request, f"/cuentas/{account_id}",
                    f"Saldo inicial ajustado en {eur(diferencia)}: ahora cuadra con el banco")


@router.post("/cuentas/comprobaciones/{check_id:int}/borrar")
def delete_check(request: Request, check_id: int, conn=Depends(get_db)):
    fila = conn.execute("SELECT account_id FROM account_checks WHERE id = ?", (check_id,)).fetchone()
    with conn:
        conn.execute("DELETE FROM account_checks WHERE id = ?", (check_id,))
    destino = f"/cuentas/{fila['account_id']}" if fila and fila["account_id"] else "/cuentas"
    return redirect(request, destino, "Saldo borrado")


# ---- Lo que te deben

@router.get("/me-deben")
def owed_page(request: Request, desde: int | None = None, conn=Depends(get_db)):
    """Lo que te deben: pendiente y cobrado. «desde» rellena el formulario con un gasto (una cena que pagaste)."""
    base = repo.get_tx(conn, desde) if desde else None
    pendiente = repo.owed(conn, pending=True)
    return render(request, "me_deben.html", {
        "pendiente": pendiente, "cobrado": repo.owed(conn, pending=False)[:20],
        "total": sum(o["amount"] for o in pendiente), "base": base, "hoy": today(request), "back": "/cuentas",
    })


@router.post("/me-deben")
async def owed_add(request: Request, conn=Depends(get_db)):
    """Apunta lo que te deben. Con «dividir», el importe es el total y se reparte entre los que digas."""
    form = await request.form()
    errors = []
    dividir = bool(form.get("dividir"))
    personas = [" ".join(p.split())[:40] for p in (form.get("persona") or "").split(",")] if dividir \
        else [" ".join((form.get("persona") or "").split())[:40]]
    personas = [p for p in personas if p][:20]
    if not personas:
        errors.append("¿Quién te lo debe?")
    importe = parse_optional_amount(form.get("importe"), "El importe", errors, allow_zero=False)
    if importe is None and not errors:
        errors.append("Escribe el importe.")
    try:
        dia = parse_date(form.get("fecha") or today(request).isoformat()).isoformat()
    except ValueError:
        dia = None
        errors.append("La fecha no es válida.")
    if not errors and dividir:
        importe = views.split_amount(importe, len(personas), bool(form.get("yo_tambien")))
        if importe <= 0:
            errors.append("Sale a 0 € por persona: revisa el importe.")
    if errors:
        return redirect(request, "/me-deben", " ".join(errors), "error")
    concepto = (form.get("concepto") or "").strip()[:120]
    with conn:
        for persona in personas:
            conn.execute("INSERT INTO owed (person, concept, amount, date) VALUES (?, ?, ?, ?)",
                         (persona, concepto, importe, dia))
    if len(personas) > 1:
        return redirect(request, "/me-deben", f"Apuntado: {', '.join(personas)} te deben {eur(importe)} cada uno "
                                              f"({eur(importe * len(personas))} en total).")
    return redirect(request, "/me-deben", f"Apuntado: {personas[0]} te debe {eur(importe)}. "
                                          f"Cuando te llegue, la app lo dará por cobrado sola.")


@router.post("/me-deben/{owed_id:int}/cobrado")
def owed_settle(request: Request, owed_id: int, conn=Depends(get_db)):
    """Darlo por cobrado a mano (te lo pagaron en efectivo, o lo das por perdido)."""
    with conn:
        conn.execute("UPDATE owed SET settled_date = ? WHERE id = ? AND settled_date IS NULL",
                     (today(request).isoformat(), owed_id))
    return redirect(request, "/me-deben", "Dado por cobrado")


@router.post("/me-deben/{owed_id:int}/borrar")
def owed_delete(request: Request, owed_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM owed WHERE id = ?", (owed_id,))
    return redirect(request, "/me-deben", "Borrado")
