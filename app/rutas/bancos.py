"""Conexión con los bancos (Enable Banking)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from .. import banco, clasificar, config, repo
from ..money import parse_date
from ..web import get_db, int_or_none, redirect, render, today

router = APIRouter()


def bank_redirect_url(request: Request) -> str:
    """La dirección a la que vuelve el banco. Hay que darla de alta tal cual en Enable Banking."""
    return str(request.base_url).rstrip("/") + "/banco/vuelta"


def _local(texto: str | None):
    if not texto:
        return None
    try:
        return banco._parse_dt(texto).astimezone(config.TZ)
    except ValueError:
        return None


@router.get("/banco")
def bank_page(request: Request, conn=Depends(get_db)):
    ahora = datetime.now(timezone.utc)
    conexiones = []
    for link in banco.links(conn):
        hasta = _local(link["valid_until"])
        conexiones.append({
            "link": link, "accounts": banco.accounts_of(conn, link["id"]),
            "until": hasta, "expired": bool(hasta and hasta <= ahora),
            "last_sync": _local(link["last_sync"]),
        })
    return render(request, "banco.html", {
        "app_id": repo.setting(conn, "bank_app_id") or "",
        "key_ok": banco.key_path().is_file(),
        "configured": banco.configured(conn),
        "redirect_url": bank_redirect_url(request),
        "conexiones": conexiones,
        "cuentas": repo.accounts(conn, active=True),
        "categories": repo.categories(conn, active=True, kind="gasto"),
        "por_si_acaso": clasificar.fallback_category(conn),
        "back": "/cuentas",
    })


@router.post("/banco/config")
async def bank_config(request: Request, app_id: str = Form(""), clave: UploadFile | None = File(None),
                      conn=Depends(get_db)):
    app_id = app_id.strip()
    if not app_id:
        return redirect(request, "/banco", "Escribe el App ID de tu aplicación de Enable Banking.", "error")
    if clave is not None and clave.filename:
        contenido = await clave.read()
        try:
            banco.save_key(contenido)
        except banco.BankError as e:
            return redirect(request, "/banco", str(e), "error")
    with conn:
        repo.set_setting(conn, "bank_app_id", app_id)
    if not banco.key_path().is_file():
        return redirect(request, "/banco", "Guardado el App ID. Falta subir la clave (.pem).", "error")
    return redirect(request, "/banco", "Configuración guardada")


@router.get("/banco/conectar")
def bank_choose(request: Request, conn=Depends(get_db)):
    if not banco.configured(conn):
        return redirect(request, "/banco", "Primero guarda el App ID y la clave.", "error")
    client = banco.Client.from_settings(conn)
    bancos, error = [], None
    try:
        for pais in ("ES", "DE"):
            bancos += [dict(a, pais=pais) for a in client.aspsps(pais)]
    except banco.BankError as e:
        error = str(e)
    bancos.sort(key=lambda a: (a["pais"] != "ES", a.get("name", "").lower()))
    return render(request, "banco_conectar.html", {"bancos": bancos, "error": error, "back": "/banco"})


@router.post("/banco/conectar")
async def bank_start(request: Request, conn=Depends(get_db)):
    form = await request.form()
    nombre, pais = (form.get("nombre") or "").strip(), (form.get("pais") or "").strip()
    if not nombre or not pais:
        return redirect(request, "/banco/conectar", "Elige un banco.", "error")
    try:
        url = banco.start_connection(conn, banco.Client.from_settings(conn), nombre, pais,
                                     bank_redirect_url(request), int_or_none(form.get("max")))
    except banco.BankError as e:
        return redirect(request, "/banco", str(e), "error")
    return RedirectResponse(url, status_code=303)          # a la web del banco, fuera de HTMX


@router.get("/banco/vuelta")
def bank_return(request: Request, code: str = "", state: str = "", error: str = "",
                error_description: str = "", conn=Depends(get_db)):
    if error or not code:
        with conn:
            conn.execute("DELETE FROM bank_links WHERE state = ? AND session_id IS NULL", (state,))
        motivo = error_description or error or "el banco no ha devuelto el permiso"
        return redirect(request, "/banco", f"No se ha conectado: {motivo}.", "error")
    try:
        client = banco.Client.from_settings(conn)
        link = banco.finish_connection(conn, client, code, state, today(request))
        resumen = banco.sync_link(conn, client, link, today(request))
    except banco.BankError as e:
        return redirect(request, "/banco", str(e), "error")
    return redirect(request, "/banco", f"{link['aspsp_name']} conectado. Mira en qué cuenta va cada una y desde "
                                       f"qué fecha traer movimientos. {banco.summary_text(resumen)}.")


@router.post("/banco/cuenta")
async def bank_account_save(request: Request, conn=Depends(get_db)):
    form = await request.form()
    ident = form.get("ident") or ""
    cuenta = conn.execute("SELECT * FROM bank_accounts WHERE ident = ?", (ident,)).fetchone()
    if not cuenta:
        raise HTTPException(404)
    try:
        desde = parse_date(form.get("desde")).isoformat()
    except ValueError:
        return redirect(request, "/banco", "La fecha no es válida.", "error")
    destino = int_or_none(form.get("cuenta"))
    if destino and not repo.get_row(conn, "accounts", destino):
        destino = None
    with conn:
        conn.execute("UPDATE bank_accounts SET account_id = ?, since = ? WHERE ident = ?", (destino, desde, ident))
    return redirect(request, "/banco", "Guardado. Se aplicará en la próxima sincronización.")


def psu_headers(request: Request) -> dict:
    """Lo que el banco necesita para saber que eres tú quien mira, desde tu navegador."""
    h = request.headers
    return {
        "Psu-Ip-Address": request.client.host if request.client else "",
        "Psu-User-Agent": h.get("user-agent", ""),
        "Psu-Referer": h.get("referer", ""),
        "Psu-Accept": h.get("accept") or "*/*",
        "Psu-Accept-Charset": h.get("accept-charset") or "utf-8",
        "Psu-Accept-Encoding": h.get("accept-encoding") or "gzip",
        "Psu-Accept-language": h.get("accept-language") or "es-ES",
    }


@router.post("/banco/al-abrir")
def bank_sync_on_open(request: Request, conn=Depends(get_db)):
    """Lo pide el panel al abrirse: mira el banco ahora, estando tú delante."""
    if not banco.configured(conn) or not banco.present_sync_due(conn):
        return Response(status_code=200)
    try:
        client = banco.Client.from_settings(conn)
    except banco.BankError:
        return Response(status_code=200)
    resultados = banco.sync_all_present(conn, client, psu_headers(request), today(request))
    nuevos = sum(r["nuevos"] for _, r in resultados)
    if not nuevos:
        return Response(status_code=200)
    return render(request, "partials/banco_nuevos.html", {"nuevos": nuevos})


@router.post("/banco/actualizar")
def bank_sync_all_now(request: Request, conn=Depends(get_db)):
    """«Actualizar» del Inicio: mira todos los bancos ya, sin esperar los 15 minutos.

    Va con las cabeceras de «estoy delante», así que no gasta de las 4 consultas diarias.
    """
    try:
        client = banco.Client.from_settings(conn)
    except banco.BankError as e:
        return redirect(request, "/", str(e), "error")
    resultados = banco.sync_all_present(conn, client, psu_headers(request), today(request))
    if not resultados:
        return redirect(request, "/", "No hay ningún banco conectado.", "error")
    errores = [n for n, r in resultados if r.get("error")]
    texto = " · ".join(f"{n}: {banco.summary_text(r)}" for n, r in resultados)
    return redirect(request, "/", texto, "error" if errores else "ok")


@router.post("/banco/{link_id:int}/sincronizar")
def bank_sync_now(request: Request, link_id: int, conn=Depends(get_db)):
    link = conn.execute("SELECT * FROM bank_links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        raise HTTPException(404)
    try:
        client = banco.Client.from_settings(conn)
    except banco.BankError as e:
        return redirect(request, "/banco", str(e), "error")
    # lo pulsas tú: va con las cabeceras de «estoy delante» y no gasta de las 4 diarias
    r = banco.sync_link(conn, client.with_psu(psu_headers(request)), link, today(request))
    if r.get("error") and "PSU" in r["error"].upper():
        r = banco.sync_link(conn, client, link, today(request))
    return redirect(request, "/banco", f"{link['aspsp_name']}: {banco.summary_text(r)}.",
                    "error" if r.get("error") else "ok")


@router.post("/banco/{link_id:int}/quitar")
def bank_remove(request: Request, link_id: int, conn=Depends(get_db)):
    try:
        client = banco.Client.from_settings(conn)
    except banco.BankError:
        client = None
    banco.disconnect(conn, client, link_id)
    return redirect(request, "/banco", "Conexión quitada. Lo que ya se había traído se queda.")


@router.post("/banco/por-si-acaso")
async def bank_fallback(request: Request, conn=Depends(get_db)):
    form = await request.form()
    with conn:
        repo.set_setting(conn, "auto_fallback_category_id", int_or_none(form.get("por_si_acaso")))
    return redirect(request, "/banco", "Guardado")
