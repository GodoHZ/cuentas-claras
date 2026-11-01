"""API para el bot de Telegram y los Atajos del iPhone."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import calc, config, repo, views
from ..money import eur
from ..web import get_db, today
from .movimientos import read_tx_form

router = APIRouter()


#
# Solo existe si API_KEY está puesta. Va con «Authorization: Bearer <API_KEY>» y no pasa
# por el PIN. Apunta gastos con la misma validación que el formulario.

def _api_autorizada(request: Request) -> bool:
    dada = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bool(config.API_KEY) and secrets.compare_digest(dada, config.API_KEY)


def _api_no() -> JSONResponse:
    if not config.API_KEY:
        return JSONResponse({"error": "La API está apagada: falta API_KEY."}, status_code=404)
    return JSONResponse({"error": "Clave incorrecta."}, status_code=401)


def _sin_acentos(texto: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


@router.get("/api/saldo")
def api_saldo(request: Request, conn=Depends(get_db)):
    if not _api_autorizada(request):
        return _api_no()
    hoy = today(request)
    txs = repo.all_txs(conn)
    previsto = views.forecast(conn, txs, *repo.period_of(conn, hoy), hoy)
    return {"puedes_gastar": previsto.left, "disponible": previsto.available,
            "comprometido": previsto.pending_total, "texto": eur(previsto.left)}


@router.get("/api/categorias")
def api_categorias(request: Request, conn=Depends(get_db)):
    if not _api_autorizada(request):
        return _api_no()
    opts = views.tx_form_options(conn)
    return {"gasto": [c["name"] for c in opts["cats_gasto"]]}


@router.post("/api/gastos")
async def api_gasto(request: Request, conn=Depends(get_db)):
    """{"importe": "12,50", "categoria": "comida", "concepto": "opcional"}"""
    if not _api_autorizada(request):
        return _api_no()
    try:
        datos = await request.json()
    except ValueError:
        return JSONResponse({"error": "Manda un JSON."}, status_code=400)
    opts = views.tx_form_options(conn)
    buscada = _sin_acentos(str(datos.get("categoria") or ""))
    cats = opts["cats_gasto"]
    elegida = (next((c for c in cats if _sin_acentos(c["name"]) == buscada), None)
               or next((c for c in cats if buscada and _sin_acentos(c["name"]).startswith(buscada)), None)
               or next((c for c in cats if buscada and buscada in _sin_acentos(c["name"])), None))
    if not elegida:
        return JSONResponse({"error": "No conozco esa categoría.",
                             "categorias": [c["name"] for c in cats]}, status_code=400)
    formulario = {"tipo": calc.GASTO, "importe": str(datos.get("importe") or ""), "categoria_id": str(elegida["id"]),
                  "cuenta_id": str(opts["default_account"] or ""), "fecha": today(request).isoformat(),
                  "concepto": str(datos.get("concepto") or "")}
    valores, campos = read_tx_form(conn, formulario)
    if campos is None:
        return JSONResponse({"error": " ".join(valores.get("errors") or ["No se ha podido apuntar."])}, status_code=400)
    with conn:
        tx_id = repo.insert_tx(conn, **campos)
    tx = repo.get_tx(conn, tx_id)
    hoy = today(request)
    previsto = views.forecast(conn, repo.all_txs(conn), *repo.period_of(conn, hoy), hoy)
    return {"id": tx_id, "mensaje": views.saved_message(tx["type"], tx["amount"], tx["category"], tx["envelope"],
                                                         tx["account"], tx["other_account"]),
            "puedes_gastar": eur(previsto.left)}
