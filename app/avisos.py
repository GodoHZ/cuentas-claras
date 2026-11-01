"""Mensajes al móvil por Telegram: el cierre de cada mes al cobrar, el resumen de cada domingo y, al
momento, lo que llega del banco y merece que lo sepas (un gasto grande, un presupuesto que se pasa,
alguien que te devuelve lo suyo, una suscripción que sube o un cobro repetido).

Hace falta un bot (se crea con @BotFather) y el chat al que escribe, en las variables de entorno
TELEGRAM_TOKEN y TELEGRAM_CHAT (y TELEGRAM_THREAD si es un tema de un grupo). Sin ellas no hace nada.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date

from . import config

log = logging.getLogger("finanzas")


def configurado() -> bool:
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT)


def enviar(texto: str) -> bool:
    """Manda un mensaje. Si falla, lo apunta en el registro y sigue (nunca rompe lo demás)."""
    if not configurado():
        return False
    datos = {"chat_id": config.TELEGRAM_CHAT, "text": texto, "disable_web_page_preview": True}
    if config.TELEGRAM_THREAD:
        datos["message_thread_id"] = config.TELEGRAM_THREAD
    peticion = urllib.request.Request(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
                                      data=json.dumps(datos).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(peticion, timeout=10) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001 - un aviso que no sale no debe parar nada
        log.warning("No he podido mandar el aviso por Telegram: %s", type(e).__name__)
        return False


# ---------------------------------------------------------------- avisos al momento

BIG_DEFAULT = 10000                # un gasto de 100 € o más es «grande» (se cambia en Ajustes)
NIVELES = {"sin": 0, "ok": 0, "aviso": 1, "pasado": 2}


def preferencias(conn) -> dict:
    """Qué avisos quieres (todos encendidos de fábrica) y desde cuánto un gasto es grande."""
    from . import repo
    return {
        "grande": repo.setting(conn, "alert_big", "1") == "1",
        "grande_desde": repo.int_setting(conn, "alert_big_cents") or BIG_DEFAULT,
        "presupuesto": repo.setting(conn, "alert_budget", "1") == "1",
        "deudas": repo.setting(conn, "alert_owed", "1") == "1",
        "semanal": repo.setting(conn, "alert_weekly", "1") == "1",
        "cambios": repo.setting(conn, "alert_changes", "1") == "1",
    }


def instantanea(conn, today) -> dict:
    """Cómo está cada presupuesto ahora y el último movimiento: para ver luego qué ha cambiado."""
    from . import repo, views
    if not configurado():
        return {}
    year, month = repo.period_of(conn, today)
    lineas = views.budget(conn, repo.all_txs(conn), year, month)["lines"]
    return {"ultimo": conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0],
            "niveles": {b["cat"]["id"]: b["line"].level for b in lineas}, "mes": (year, month)}


def mensajes_nuevos(conn, antes: dict) -> list[str]:
    """Lo que merece un aviso de lo que acaba de traer el banco: gastos grandes, presupuestos que se
    pasan del aviso o del tope y lo que te debían y ya te han devuelto."""
    from . import calc, repo, views
    from .money import eur
    from .nombres import limpio
    if not antes:
        return []
    pref = preferencias(conn)
    nuevos = conn.execute(
        "SELECT t.*, c.name AS category FROM transactions t LEFT JOIN categories c ON c.id = t.category_id "
        "WHERE t.id > ? AND t.source = 'banco' ORDER BY t.date, t.id", (antes["ultimo"],)).fetchall()
    if not nuevos:
        return []
    lineas = []
    if pref["grande"]:
        for t in nuevos:
            if t["type"] == calc.GASTO and t["amount"] >= pref["grande_desde"]:
                lineas.append(f"💸 Gasto de {eur(t['amount'])}: {limpio(t['concept']) or t['category']}"
                              f" ({t['category']})")
    if pref["presupuesto"]:
        lineas_ahora = views.budget(conn, repo.all_txs(conn), *antes["mes"])["lines"]
        for b in lineas_ahora:
            antes_nivel = NIVELES.get(antes["niveles"].get(b["cat"]["id"], "ok"), 0)
            if NIVELES.get(b["line"].level, 0) > antes_nivel:
                que = "te has pasado" if b["line"].level == "pasado" else "cerca del límite"
                lineas.append(f"⚠️ {b['cat']['name']}: {eur(b['line'].spent)} de {eur(b['line'].budget)} · {que}")
    if pref["cambios"]:
        # lo que acaba de llegar y sube de precio o está cobrado dos veces
        fechas = {(date.fromisoformat(t["date"]), t["amount"]) for t in nuevos if t["type"] == calc.GASTO}
        hoy = max(f for f, _ in fechas) if fechas else None
        for c in views.price_alerts(conn, hoy) if hoy else []:
            if (c.date, c.now) in fechas:
                lineas.append(("👀 " if c.kind == "doble" else "📈 ") + views.change_text(c))
    if pref["deudas"]:
        ids = [t["id"] for t in nuevos]
        marcas = ",".join("?" * len(ids))
        for o in conn.execute(f"SELECT * FROM owed WHERE settled_tx_id IN ({marcas})", ids):
            lineas.append(f"✅ {o['person']} te ha devuelto {eur(o['amount'])}"
                          + (f" ({o['concept']})" if o["concept"] else ""))
    return lineas


def tras_sincronizar(conn, antes: dict) -> bool:
    """Manda, en un solo mensaje, los avisos de lo que acaba de llegar del banco (si hay alguno)."""
    try:
        lineas = mensajes_nuevos(conn, antes)
    except Exception:  # noqa: BLE001 - un aviso que falla no debe romper la sincronización
        log.exception("No he podido preparar los avisos")
        return False
    return enviar("\n".join(lineas)) if lineas else False


def resumen_semanal(conn, now) -> bool:
    """El resumen del domingo, si toca (ver views.weekly_due). Se apunta como hecho aunque falle el envío:
    mejor perderse uno que mandar diez."""
    from . import banco, repo, views
    if not configurado() or not views.weekly_due(conn, now):
        return False
    try:
        retenidos = banco.pending(conn) if repo.modules(conn)["bancos"] else []
        texto = views.weekly_message(conn, now.date(), banco.held(retenidos))
    except Exception:  # noqa: BLE001
        log.exception("No he podido preparar el resumen de la semana")
        texto = None
    with conn:
        repo.set_setting(conn, "last_weekly", now.date().isoformat())
    return enviar(texto) if texto else False
