"""PIN y copias de las pantallas en el móvil (sin conexión)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from .. import db, pin, repo
from ..web import get_db, redirect, render, settings_url

router = APIRouter()


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
