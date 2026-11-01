"""Apuntar, revisar, editar y borrar movimientos."""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import banco, calc, paperless, repo, views
from ..money import add_months_ym, amount_input, eur, InvalidAmount, parse_amount, parse_date, parse_month
from ..web import get_db, int_or_none, is_htmx, log, redirect, render, set_flash, today

router = APIRouter()


def read_tx_form(conn, form) -> tuple[dict, dict | None]:
    """Lee y valida el formulario de movimiento. Devuelve (valores para repintar, campos para guardar)."""
    type_ = form.get("tipo") or ""
    if type_ == "mover":                       # «Mover»: entre cuentas, a un sobre o de un sobre
        type_ = form.get("mover") if form.get("mover") in calc.MOVE_MONEY else calc.TRASPASO
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
    other_id = int_or_none(form.get("otra_cuenta_id"))
    envelope = repo.get_row(conn, "envelopes", envelope_id) if envelope_id else None
    if type_ in calc.NEEDS_ENVELOPE:
        category_id = None
        if envelope and envelope["account_id"]:
            account_id = envelope["account_id"]          # el dinero del sobre vive en su cuenta
    if type_ == calc.GASTO:
        other_id = None
        if envelope and envelope["account_id"]:
            account_id = envelope["account_id"]          # se paga con el dinero del sobre
    if type_ == calc.INGRESO:
        envelope_id, other_id = None, None
    if type_ == calc.TRASPASO:
        category_id, envelope_id = None, None
    try:
        day = parse_date(form.get("fecha"))
    except ValueError:
        day = None
        errors.append("La fecha no es válida.")
    concept = (form.get("concepto") or "").strip()[:200]
    errors += calc.validate_tx(type_, amount if amount_ok else 1, category_id, envelope_id,
                               account_id, other_id)
    if category_id and not repo.get_row(conn, "categories", category_id):
        errors.append("Esa categoría ya no existe.")
    if envelope_id and not repo.get_row(conn, "envelopes", envelope_id):
        errors.append("Ese sobre ya no existe.")
    if not account_id or not repo.get_row(conn, "accounts", account_id):
        errors.append("Elige la cuenta.")
    if other_id and not repo.get_row(conn, "accounts", other_id):
        errors.append("Esa otra cuenta ya no existe.")
    uid = (form.get("uid") or "").strip()[:40] or None
    # El mes al que cuenta: «auto» (la regla de cobro a final de mes), el de la fecha (""), o el
    # siguiente / el anterior a la fecha.
    cuenta_mes = form.get("cuenta_mes", "")
    cuenta_mes = cuenta_mes if cuenta_mes in ("", "+1", "-1") else ""
    values = {"tipo": type_, "importe": form.get("importe") or "", "categoria_id": category_id,
              "sobre_id": envelope_id, "cuenta_id": account_id, "otra_cuenta_id": other_id,
              "fecha": day.isoformat() if day else (form.get("fecha") or ""), "concepto": concept,
              "cuenta_mes": cuenta_mes, "errors": errors}
    if errors:
        return values, None
    fields = dict(date=day.isoformat(), type=type_, category_id=category_id, envelope_id=envelope_id,
                  account_id=account_id, other_account_id=other_id, concept=concept,
                  amount=amount, client_uid=uid)
    # el siguiente o el anterior al «mes» de su fecha (que, si va de nómina a nómina, no es el natural)
    fields["month"] = None
    if cuenta_mes in ("+1", "-1"):
        fields["month"] = repo.period_key(*add_months_ym(*repo.period_of(conn, day), int(cuenta_mes)))
    return values, fields


def apartar_ingreso(conn, form, fields: dict):
    """«¿Lo apartas en un sobre?» de un ingreso: un aporte por lo mismo que lo aparta sin moverlo.

    Si el sobre vive en otra cuenta, el aporte se lleva el dinero allí. Devuelve el sobre, o None.
    """
    sobre = repo.get_row(conn, "envelopes", int_or_none(form.get("sobre_ingreso")) or 0)
    if not sobre or fields.get("type") != calc.INGRESO:
        return None
    cuenta = sobre["account_id"] or fields["account_id"]
    repo.insert_tx(conn, date=fields["date"], type=calc.APORTE, category_id=None, envelope_id=sobre["id"],
                   account_id=cuenta, other_account_id=fields["account_id"] if cuenta != fields["account_id"] else None,
                   concept=f"Apartado de un ingreso{': ' + fields['concept'][:60] if fields.get('concept') else ''}",
                   amount=fields["amount"], month=fields.get("month"))
    return sobre


def guardar_etiquetas(conn, form, tx_id: int) -> None:
    """Las etiquetas del formulario (solo si el formulario las pregunta: el rápido no lo hace)."""
    if form.get("pregunta_etiquetas"):
        repo.set_tx_tags(conn, tx_id, repo.parse_tag_names(form.get("etiquetas")))


@router.get("/nuevo")
def new_tx_page(request: Request, conn=Depends(get_db)):
    return render(request, "nuevo.html", {"hide_fab": True, "todas_etiquetas": [t["name"] for t in repo.tags(conn)]})


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
        return render(request, "nuevo.html", {"hide_fab": True, "form_values": values,
                                              "etiquetas": form.get("etiquetas") or "",
                                              "todas_etiquetas": [t["name"] for t in repo.tags(conn)]})
    with conn:
        tx_id = repo.insert_tx(conn, **fields)
        guardar_etiquetas(conn, form, tx_id)
        sobre = apartar_ingreso(conn, form, fields)
        deuda = repo.settle_owed(conn, tx_id, fields["amount"], fields["concept"], fields["date"]) \
            if fields["type"] == calc.INGRESO else None
    tx = repo.get_tx(conn, tx_id)
    message = views.saved_message(tx["type"], tx["amount"], tx["category"], tx["envelope"],
                                  tx["account"], tx["other_account"])
    if sobre:
        message += f" → apartado en «{sobre['name']}»"
    if deuda:
        message += f" · cobrado lo que te debía {deuda['person']}"
    if in_dialog:
        opts = views.tx_form_options(conn)
        fresh = views.blank_tx_form(opts, today(request))
        trigger = json.dumps({"txGuardado": message})           # ensure_ascii: cabecera en ASCII
        return render(request, "partials/txform.html",
                      {"opts": opts, "f": fresh, "dialog": True, "action": "/movimientos/nuevo"},
                      headers={"HX-Trigger": trigger})
    return redirect(request, "/", message)


@router.get("/movimientos")
def tx_list(request: Request, mes: str = "", tipo: str = "", categoria: str = "", sobre: str = "",
            cuenta: str = "", revisar: str = "", q: str = "", etiqueta: str = "", conn=Depends(get_db)):
    hoy = today(request)
    q = " ".join(q.split())[:60]
    tag_id = int_or_none(etiqueta)
    all_months = mes == "todos" or bool(q) or bool(tag_id)   # buscar o una etiqueta: en todos los meses
    year, month = parse_month(mes, repo.period_of(conn, hoy))
    type_ = tipo if tipo in calc.TYPES else None
    cat_id, env_id, acc_id = int_or_none(categoria), int_or_none(sobre), int_or_none(cuenta)
    rows = repo.tx_rows(conn, month=None if all_months else (year, month), type_=type_,
                        category_id=cat_id, envelope_id=env_id, account_id=acc_id,
                        limit=1000 if all_months else None, review=bool(revisar), search=q or None, tag_id=tag_id)
    filters = {"tipo": type_ or "", "categoria": cat_id or "", "sobre": env_id or "", "cuenta": acc_id or "",
               "revisar": "1" if revisar else "", "etiqueta": tag_id or ""}
    return render(request, "movimientos.html", {
        "rows": rows, "year": year, "month": month, "all_months": all_months, "q": q,
        "etiqueta": repo.get_tag(conn, tag_id) if tag_id else None,
        "q_total": sum(r["amount"] * (1 if r["type"] == calc.INGRESO else -1 if r["type"] == calc.GASTO else 0)
                       for r in rows) if q else None,
        "filters": filters, "filtered": any(filters.values()),
        # lo retenido por el banco, en gris encima de la lista (solo en el mes en curso y sin filtros)
        "retenidos": banco.pending(conn) if (not all_months and not any(filters.values())
                                             and (year, month) == repo.period_of(conn, hoy)
                                             and repo.modules(conn)["bancos"]) else [],
        "filter_qs": urlencode({k: v for k, v in filters.items() if v}),
        "m": None if all_months else calc.month_summary(repo.all_txs(conn), year, month),
        "categories": repo.categories(conn), "envelopes": repo.envelopes(conn),
        "accounts": repo.accounts(conn),
        # la bandeja: lo que trajo el banco y falta confirmar, sea del mes que sea
        "revisar": (revisar := repo.tx_rows(conn, review=True, limit=60)),
        "cats_activas": {k: repo.categories(conn, active=True, kind=k) for k in ("gasto", "ingreso")},
        "sobres_traspaso": views.transfer_envelopes(conn, [r for r in revisar if r["type"] == calc.TRASPASO],
                                                    today(request)),
    })


@router.post("/movimientos/{tx_id:int}/confirmar")
async def confirm_tx(request: Request, tx_id: int, conn=Depends(get_db)):
    """Confirmar un movimiento del banco (y, si hace falta, cambiarle la categoría) con un toque."""
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        # Lo más normal: era la mitad de una transferencia entre tus cuentas y, al llegar la otra mitad,
        # la app las juntó en un traspaso. La pantalla estaba vieja: se recarga y lo dice.
        aviso = "Ese movimiento ya no estaba (seguramente se juntó con su otra mitad en un traspaso)"
        if is_htmx(request):
            respuesta = Response(status_code=200, headers={"HX-Refresh": "true"})
            set_flash(respuesta, aviso)                    # se ve al recargar
            return respuesta
        return redirect(request, "/movimientos", aviso)
    form = await request.form()
    categoria = repo.get_row(conn, "categories", int_or_none(form.get("categoria_id")) or 0)
    sobre = repo.get_row(conn, "envelopes", int_or_none(form.get("sobre_id")) or 0)
    with conn:
        if categoria and tx["type"] in calc.NEEDS_CATEGORY and categoria["kind"] == tx["type"]:
            conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (categoria["id"], tx_id))
        # un traspaso a la cuenta de un sobre que va a ese sobre: aporte que mueve el dinero de cuenta
        if sobre and tx["type"] == calc.TRASPASO and sobre["account_id"] in (tx["account_id"], None):
            conn.execute("UPDATE transactions SET type = ?, envelope_id = ? WHERE id = ?",
                         (calc.APORTE, sobre["id"], tx_id))
        conn.execute("UPDATE transactions SET review = 0 WHERE id = ?", (tx_id,))
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Trigger": json.dumps({"txConfirmado": "Confirmado"})})
    return redirect(request, "/movimientos", "Confirmado")


@router.post("/movimientos/confirmar-todos")
def confirm_all(request: Request, conn=Depends(get_db)):
    with conn:
        n = conn.execute("UPDATE transactions SET review = 0 WHERE review = 1").rowcount
    return redirect(request, "/movimientos", f"{n} movimiento{'s' if n != 1 else ''} confirmado{'s' if n != 1 else ''}")


def month_of(conn, tx) -> str:
    """El «mes» en que sale en la lista de movimientos (de nómina a nómina, o el puesto a mano)."""
    return tx["month"] or repo.period_key(*repo.period_of(conn, date.fromisoformat(tx["date"])))


def cuenta_mes_de(conn, tx) -> str:
    """Cómo está el «cuenta para el mes» de un movimiento ya guardado: "", "+1" o "-1"."""
    suyo = repo.period_key(*repo.period_of(conn, date.fromisoformat(tx["date"])))
    if not tx["month"] or tx["month"] == suyo:
        return ""
    return "+1" if tx["month"] > suyo else "-1"


@router.get("/movimientos/{tx_id:int}")
def edit_tx_page(request: Request, tx_id: int, conn=Depends(get_db)):
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404, "Ese movimiento no existe")
    opts = views.tx_form_options(conn, tx["category_id"], tx["envelope_id"], tx["account_id"], tx["type"])
    values = {"tipo": tx["type"], "importe": amount_input(tx["amount"]), "categoria_id": tx["category_id"],
              "sobre_id": tx["envelope_id"], "cuenta_id": tx["account_id"], "fecha": tx["date"],
              "concepto": tx["concept"], "cuenta_mes": cuenta_mes_de(conn, tx), "errors": []}
    return render(request, "movimiento.html", {"tx": tx, "opts": opts, "f": values,
                                               "back": f"/movimientos?mes={month_of(conn, tx)}", **extras_tx(conn, tx)})


def extras_tx(conn, tx) -> dict:
    """Lo que acompaña al formulario de un movimiento: etiquetas y facturas enlazadas."""
    return {"etiquetas": tx["tags"] or "", "todas_etiquetas": [t["name"] for t in repo.tags(conn)],
            "docs": repo.tx_docs(conn, tx["id"]), "paperless_busqueda": paperless.search_url(conn, tx["amount"]),
            "paperless_api": paperless.api_ready(conn)}


@router.post("/movimientos/{tx_id:int}")
async def update_tx(request: Request, tx_id: int, conn=Depends(get_db)):
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404, "Ese movimiento no existe")
    form = await request.form()
    values, fields = read_tx_form(conn, form)
    if fields is None:
        opts = views.tx_form_options(conn, tx["category_id"], tx["envelope_id"], tx["account_id"],
                                     values["tipo"] or tx["type"])
        return render(request, "movimiento.html", {"tx": tx, "opts": opts, "f": values,
                                                   "back": f"/movimientos?mes={month_of(conn, tx)}",
                                                   **extras_tx(conn, tx), "etiquetas": form.get("etiquetas") or ""})
    fields.pop("client_uid", None)
    with conn:
        repo.update_tx(conn, tx_id, **fields)
        guardar_etiquetas(conn, form, tx_id)
        apartar_ingreso(conn, form, fields)
    return redirect(request, f"/movimientos?mes={month_of(conn, repo.get_tx(conn, tx_id))}", "Cambios guardados")


@router.post("/movimientos/{tx_id:int}/borrar")
def delete_tx(request: Request, tx_id: int, conn=Depends(get_db)):
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404, "Ese movimiento no existe")
    with conn:
        repo.delete_tx(conn, tx_id)
    return redirect(request, f"/movimientos?mes={month_of(conn, tx)}",
                    f"Movimiento borrado ({calc.TYPES[tx['type']].lower()} de {eur(tx['amount'])})")


# ---------------------------------------------------------------- facturas y tickets

@router.post("/movimientos/{tx_id:int}/documentos")
async def add_doc(request: Request, tx_id: int, conn=Depends(get_db)):
    """Enlaza una factura o un ticket (un enlace de Paperless o de donde sea) con el movimiento."""
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404, "Ese movimiento no existe")
    form = await request.form()
    url = (form.get("url") or "").strip()[:500]
    pid = int_or_none(form.get("paperless_id"))
    if pid and paperless.base_url(conn):
        url = paperless.doc_url(conn, pid)
    if not url.startswith(("http://", "https://")):
        return redirect(request, f"/movimientos/{tx_id}", "Pega el enlace entero (empieza por https://).", "error")
    pid = pid or paperless.doc_id(url)
    titulo = (form.get("titulo") or "").strip()[:120] or (paperless.title(conn, pid) if pid else "")
    with conn:
        conn.execute("INSERT OR IGNORE INTO tx_docs (tx_id, url, title, paperless_id) VALUES (?, ?, ?, ?)",
                     (tx_id, url, titulo, pid))
    nota = False
    if pid:
        vuelta = str(request.base_url).rstrip("/") + f"/movimientos/{tx_id}"
        nota = paperless.add_note(conn, pid, f"Finanzas: {calc.TYPES[tx['type']].lower()} de {eur(tx['amount'])} "
                                             f"del {date.fromisoformat(tx['date']):%d/%m/%Y} · {vuelta}")
    return redirect(request, f"/movimientos/{tx_id}", "Documento enlazado" + (" (y anotado en Paperless)" if nota else ""))


@router.post("/movimientos/{tx_id:int}/documentos/{doc_id:int}/quitar")
def remove_doc(request: Request, tx_id: int, doc_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM tx_docs WHERE id = ? AND tx_id = ?", (doc_id, tx_id))
    return redirect(request, f"/movimientos/{tx_id}", "Enlace quitado")


@router.get("/movimientos/{tx_id:int}/paperless")
def paperless_suggestions(request: Request, tx_id: int, conn=Depends(get_db)):
    """Los documentos de Paperless que podrían ser de este movimiento (se carga aparte: Paperless puede tardar)."""
    tx = repo.get_tx(conn, tx_id)
    if not tx:
        raise HTTPException(404)
    ya = {d["paperless_id"] for d in repo.tx_docs(conn, tx_id)}
    docs = [d for d in paperless.suggestions(conn, tx["amount"], date.fromisoformat(tx["date"])) if d["id"] not in ya]
    return render(request, "partials/paperless.html", {"tx": tx, "sugeridos": docs})


# ---------------------------------------------------------------- etiquetas

@router.get("/etiquetas")
def tags_page(request: Request, conn=Depends(get_db)):
    return render(request, "etiquetas.html", {"etiquetas": repo.tags(conn), "back": "/resumen"})


@router.get("/etiquetas/{tag_id:int}")
def tag_page(request: Request, tag_id: int, conn=Depends(get_db)):
    etiqueta = repo.get_tag(conn, tag_id)
    if not etiqueta:
        raise HTTPException(404, "Esa etiqueta no existe")
    filas = repo.tx_rows(conn, tag_id=tag_id)
    gastos = [r for r in filas if r["type"] == calc.GASTO]
    devuelto = sum(r["amount"] for r in filas if r["type"] == calc.INGRESO)
    por_cat: dict[str, int] = {}
    for r in gastos:
        por_cat[r["category"] or "Sin categoría"] = por_cat.get(r["category"] or "Sin categoría", 0) + r["amount"]
    tope = max(por_cat.values(), default=1)
    dias = sorted({r["date"] for r in gastos})
    n_dias = (date.fromisoformat(dias[-1]) - date.fromisoformat(dias[0])).days + 1 if dias else 0
    return render(request, "etiqueta.html", {
        "etiqueta": etiqueta, "rows": filas, "gastado": sum(r["amount"] for r in gastos), "devuelto": devuelto,
        "categorias": [{"name": k, "spent": v, "bar": v / tope} for k, v in sorted(por_cat.items(), key=lambda kv: -kv[1])],
        "desde": dias[0] if dias else None, "hasta": dias[-1] if dias else None, "n_dias": n_dias,
        "hoy": today(request), "back": "/etiquetas"})


@router.post("/etiquetas")
async def tag_create(request: Request, conn=Depends(get_db)):
    """Una etiqueta nueva con los gastos de unas fechas (un viaje: del 10 al 15 de diciembre)."""
    form = await request.form()
    nombres = repo.parse_tag_names(form.get("nombre"))
    if not nombres:
        return redirect(request, "/etiquetas", "Ponle un nombre (por ejemplo, «Roma 2026»).", "error")
    with conn:
        tag_id = repo.tag_id_for(conn, nombres[0])
        n = rango_etiqueta(conn, tag_id, form)
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM tx_tags)")
    if not repo.get_tag(conn, tag_id):
        return redirect(request, "/etiquetas", "No hay gastos en esas fechas. Las etiquetas también se ponen "
                                               "desde cada movimiento.", "error")
    return redirect(request, f"/etiquetas/{tag_id}", f"Etiqueta «{nombres[0]}»: {n} gasto{'s' if n != 1 else ''}")


def rango_etiqueta(conn, tag_id: int, form) -> int:
    try:
        desde, hasta = parse_date(form.get("desde")), parse_date(form.get("hasta") or form.get("desde"))
    except ValueError:
        return 0
    if hasta < desde:
        desde, hasta = hasta, desde
    return repo.tag_range(conn, tag_id, desde.isoformat(), hasta.isoformat())


@router.post("/etiquetas/{tag_id:int}/rango")
async def tag_add_range(request: Request, tag_id: int, conn=Depends(get_db)):
    if not repo.get_tag(conn, tag_id):
        raise HTTPException(404)
    form = await request.form()
    with conn:
        n = rango_etiqueta(conn, tag_id, form)
    return redirect(request, f"/etiquetas/{tag_id}", f"Añadido{'s' if n != 1 else ''} {n} gasto{'s' if n != 1 else ''}")


@router.post("/etiquetas/{tag_id:int}/nombre")
async def tag_rename(request: Request, tag_id: int, conn=Depends(get_db)):
    form = await request.form()
    nombres = repo.parse_tag_names(form.get("nombre"))
    if not nombres or not repo.get_tag(conn, tag_id):
        return redirect(request, f"/etiquetas/{tag_id}", "Escribe un nombre.", "error")
    try:
        with conn:
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (nombres[0], tag_id))
    except sqlite3.IntegrityError:
        return redirect(request, f"/etiquetas/{tag_id}", f"Ya hay otra etiqueta llamada «{nombres[0]}».", "error")
    return redirect(request, f"/etiquetas/{tag_id}", "Nombre cambiado")


@router.post("/etiquetas/{tag_id:int}/quitar/{tx_id:int}")
def tag_remove_tx(request: Request, tag_id: int, tx_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM tx_tags WHERE tag_id = ? AND tx_id = ?", (tag_id, tx_id))
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM tx_tags)")
    if not repo.get_tag(conn, tag_id):
        return redirect(request, "/etiquetas", "Etiqueta borrada: se ha quedado sin movimientos")
    return redirect(request, f"/etiquetas/{tag_id}", "Quitado de la etiqueta")


@router.post("/etiquetas/{tag_id:int}/borrar")
def tag_delete(request: Request, tag_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    return redirect(request, "/etiquetas", "Etiqueta borrada (los movimientos siguen ahí)")
