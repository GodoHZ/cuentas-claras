"""Enlazar movimientos con sus facturas y tickets de Paperless-ngx.

Sin nada configurado, cada movimiento puede llevar enlaces pegados a mano. Con la dirección de
Paperless (Ajustes › Paperless), además, un botón abre en Paperless la búsqueda por el importe.
Y con una clave de su API (PAPERLESS_TOKEN, y PAPERLESS_API si el contenedor llega a Paperless por
otra dirección), la app propone ella misma los documentos que encajan y, al enlazar uno, le deja
en Paperless una nota con el enlace de vuelta al movimiento.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta

log = logging.getLogger("finanzas")

DOC_RE = re.compile(r"/documents/(\d+)")
SUGGEST_DAYS = 7                   # los documentos de una semana antes o después del movimiento


def token() -> str:
    return os.environ.get("PAPERLESS_TOKEN", "").strip()


def base_url(conn) -> str:
    """La dirección de Paperless que ves en el navegador (sin la barra final)."""
    from . import repo
    url = (repo.setting(conn, "paperless_url") or os.environ.get("PAPERLESS_URL", "")).strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else ""


def api_url(conn) -> str:
    return (os.environ.get("PAPERLESS_API", "").strip().rstrip("/") or base_url(conn))


def api_ready(conn) -> bool:
    return bool(token() and api_url(conn))


def doc_id(url: str) -> int | None:
    m = DOC_RE.search(url or "")
    return int(m.group(1)) if m else None


def doc_url(conn, paperless_id: int) -> str:
    return f"{base_url(conn)}/documents/{paperless_id}/details"


def amount_text(cents: int) -> str:
    """Como sale en un ticket: 12,50."""
    return f"{cents // 100},{cents % 100:02d}"


def search_url(conn, cents: int) -> str:
    """La búsqueda en Paperless por el importe, para abrirla en el navegador."""
    base = base_url(conn)
    return f"{base}/documents?query={urllib.parse.quote(amount_text(cents))}" if base else ""


def _api(conn, path: str, data: dict | None = None):
    peticion = urllib.request.Request(
        api_url(conn) + path, data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Token {token()}", "Accept": "application/json; version=5",
                 **({"Content-Type": "application/json"} if data is not None else {})})
    with urllib.request.urlopen(peticion, timeout=8) as r:
        return json.loads(r.read() or b"null")


def suggestions(conn, cents: int, day: date, limit: int = 5) -> list[dict]:
    """Los documentos de Paperless con ese importe en el texto, de fechas cercanas. [] si falla."""
    if not api_ready(conn):
        return []
    q = urllib.parse.urlencode({
        "query": f'"{amount_text(cents)}"', "page_size": limit,
        "created__date__gte": (day - timedelta(days=SUGGEST_DAYS)).isoformat(),
        "created__date__lte": (day + timedelta(days=SUGGEST_DAYS)).isoformat()})
    try:
        datos = _api(conn, f"/api/documents/?{q}")
    except Exception as e:  # noqa: BLE001 - si Paperless no responde, simplemente no hay sugerencias
        log.warning("Paperless no ha respondido: %s", type(e).__name__)
        return []
    return [{"id": d["id"], "title": d.get("title") or f"Documento {d['id']}", "created": (d.get("created") or "")[:10],
             "url": doc_url(conn, d["id"])} for d in (datos or {}).get("results", [])]


def title(conn, paperless_id: int) -> str:
    if not api_ready(conn):
        return ""
    try:
        return (_api(conn, f"/api/documents/{paperless_id}/") or {}).get("title") or ""
    except Exception:  # noqa: BLE001
        return ""


def add_note(conn, paperless_id: int, text: str) -> bool:
    """El enlace de vuelta: una nota en el documento de Paperless. False si no se ha podido."""
    if not api_ready(conn):
        return False
    try:
        _api(conn, f"/api/documents/{paperless_id}/notes/", {"note": text})
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("No he podido dejar la nota en Paperless: %s", type(e).__name__)
        return False
