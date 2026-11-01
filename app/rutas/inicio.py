"""Inicio («Puedes gastar» y lo que pide tu atención), el resumen de 12 meses y la salud de la app."""
from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .. import banco, calc, config, db, repo, views
from ..money import eur, month_name, month_short, parse_month
from ..web import get_db, redirect, render, today

router = APIRouter()


@router.get("/salud")
def health(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        conn.execute("SELECT 1 FROM settings LIMIT 1").fetchall()
    finally:
        conn.close()
    return {"ok": True, "version": config.APP_VERSION}


@router.get("/")
def panel(request: Request, mes: str = "", conn=Depends(get_db)):
    hoy = today(request)
    year, month = parse_month(mes, repo.period_of(conn, hoy))
    txs = repo.all_txs(conn)
    cards, total = views.envelopes_overview(conn, txs, today=hoy)
    cuentas, total_cuentas = views.accounts_overview(conn, txs)
    es_actual = (year, month) == repo.period_of(conn, hoy)
    fuera = [c["account"]["name"] for c in cuentas if not c["account"]["spendable"]]
    # adelantar lo que falta por pagar solo tiene sentido en el mes en curso
    retenidos = banco.pending(conn) if es_actual and repo.modules(conn)["bancos"] else []
    previsto = views.forecast(conn, txs, year, month, hoy, banco.held(retenidos)) if es_actual else None
    cobro = views.until_payday(conn, previsto.left, hoy) if previsto else None
    return render(request, "panel.html", {
        "sweep": views.pending_sweep(conn, txs, hoy),
        "cierre": views.last_close(conn, hoy) if es_actual else None,
        "reparto": views.plan_due(conn, hoy) if es_actual else None,
        "para_ti": views.attention(previsto, cuentas, hoy) if es_actual else [],
        "cuentas": cuentas, "total_cuentas": total_cuentas,
        "forecast": previsto,
        "retenidos": retenidos,
        "cobro": cobro,
        "ritmo": views.pace(conn, txs, previsto.left, hoy, cobro) if previsto else None,
        "ritmo_mes": views.month_pace(conn, txs, hoy) if es_actual else None,
        "cambios": views.price_alerts(conn, hoy) if es_actual else [],
        "tu_ano": views.year_review_due(hoy) if es_actual else None,
        "te_deben": sum(o["amount"] for o in repo.owed(conn, pending=True)),
        "fuera_de_la_suma": fuera,
        "por_revisar": repo.review_count(conn),
        "banco_caduca": banco.expiring(conn),
        "banco_estado": views.bank_status(conn),
        "banco_al_abrir": es_actual and banco.configured(conn) and banco.present_sync_due(conn),
        "year": year, "month": month, "is_current": es_actual,
        "m": calc.month_summary(txs, year, month),
        "cards": cards, "total_envelopes": total,
        "budget": views.budget(conn, txs, year, month),
    })


@router.post("/avisos/cambio/visto")
async def change_seen(request: Request, conn=Depends(get_db)):
    """«Vale, ya lo he visto» de una subida de precio o un cobro doble: no vuelve a salir."""
    form = await request.form()
    with conn:
        views.dismiss_change(conn, (form.get("id") or "")[:200])
    return redirect(request, form.get("volver") if (form.get("volver") or "").startswith("/") else "/", "Vale, no te lo vuelvo a enseñar")


@router.post("/sobras")
def sweep_now(request: Request, conn=Depends(get_db)):
    """Guarda a mano las sobras del mes cerrado en el sobre elegido."""
    pending = views.pending_sweep(conn, repo.all_txs(conn), today(request))
    if not pending or pending["amount"] <= 0:
        return redirect(request, "/", "No hay sobras pendientes de guardar.", "error")
    views.do_sweep(conn, pending)
    if pending["mode"] == "cobro":
        return redirect(request, "/", f"Guardado lo que te quedaba antes de cobrar ({eur(pending['amount'])}) "
                                      f"en «{pending['envelope']['name']}»")
    return redirect(request, "/", f"Guardadas las sobras de {month_name(pending['year'], pending['month'])} "
                                  f"({eur(pending['amount'])}) en «{pending['envelope']['name']}»")


@router.get("/resumen")
def summary_page(request: Request, desde: str = "", mes: str = "", conn=Depends(get_db)):
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
    hoy = today(request)
    txs = repo.all_txs(conn)
    # desde el primer movimiento (el «dinero inicial», si lo apuntaste así, ya es el punto de partida)
    inicio = min(t.date for t in txs) if txs else date(*repo.start_month(conn), 1)
    evolucion = views.money_history(conn, txs, hoy, max(inicio, hoy - timedelta(days=365)))
    patrimonio = views.net_worth(conn, txs, hoy)
    return render(request, "resumen.html", {
        "evolucion": evolucion, "evolucion_json": json.dumps(evolucion["chart"]) if evolucion else "",
        "patrimonio": patrimonio, "patrimonio_json": json.dumps(patrimonio["chart"]) if patrimonio["chart"] else "",
        "aguante": views.runway(conn, txs, hoy), "n_etiquetas": len(repo.tags(conn)),
        "anos": sorted({t.ym[0] for t in txs}, reverse=True),
        "months": months, "total": total, "start": start, "default_start": repo.start_month(conn),
        "chart_json": json.dumps(chart), "hoy": today(request), "actual": repo.period_of(conn, today(request)),
        # en qué se va: el mes elegido (o el actual) frente a la media de los tres anteriores
        "trends_month": (tm := parse_month(mes, repo.period_of(conn, today(request)))),
        "trends": views.category_trends(repo.all_txs(conn), *tm, repo.categories(conn))})


@router.get("/resumen/anual")
def year_page(request: Request, ano: int | None = None, conn=Depends(get_db)):
    """Tu año en números."""
    hoy = today(request)
    txs = repo.all_txs(conn)
    anos = sorted({t.ym[0] for t in txs}) or [hoy.year]
    ano = ano if ano in anos else (views.year_review_due(hoy) or hoy.year)
    ano = ano if ano in anos else anos[-1]
    return render(request, "ano.html", {"r": views.year_review(conn, txs, ano, hoy), "ano": ano, "anos": anos,
                                        "en_curso": ano == hoy.year, "back": "/resumen"})


@router.get("/mas")
def more_page(request: Request):
    """«Más» ya no existe: Resumen y Ajustes están en la cabecera (enlaces viejos, a Ajustes)."""
    return RedirectResponse("/ajustes", status_code=303)
