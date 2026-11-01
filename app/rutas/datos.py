"""Copias de seguridad, exportar e importar."""
from __future__ import annotations

import asyncio
import base64

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .. import backup, config, export, importer
from ..web import get_db, log, redirect, render, settings_url

router = APIRouter()


@router.post("/ajustes/copia")
async def manual_backup(request: Request):
    try:
        path = await asyncio.to_thread(backup.make_backup, request.app.state.db_path,
                                       request.app.state.backup_dir, config.now(), True)
    except Exception as e:  # noqa: BLE001
        log.exception("Fallo en la copia manual")
        return redirect(request, settings_url("copias"), f"No se pudo hacer la copia: {e}", "error")
    return redirect(request, settings_url("copias"), f"Copia hecha: {path.name}")


@router.get("/ajustes/copia/{name}")
def download_backup(request: Request, name: str):
    if not backup.NAME_RE.match(name):
        raise HTTPException(404)
    path = request.app.state.backup_dir / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=name, media_type="application/vnd.sqlite3")


def stamp() -> str:
    return config.now().strftime("%Y-%m-%d_%H%M")


@router.get("/exportar/json")
def export_json(conn=Depends(get_db)):
    data = export.to_json(conn, config.now().isoformat(timespec="seconds"), config.APP_VERSION)
    return Response(data, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="finanzas-{stamp()}.json"'})


@router.get("/exportar/csv")
def export_csv(conn=Depends(get_db)):
    return Response(export.to_csv_zip(conn), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="finanzas-csv-{stamp()}.zip"'})


@router.get("/ajustes/importar")
def import_page(request: Request, conn=Depends(get_db)):
    return render(request, "importar.html", {"back": settings_url("importar")})


MAX_IMPORT = 5 * 1024 * 1024


@router.post("/ajustes/importar")
async def import_preview(request: Request, conn=Depends(get_db)):
    form = await request.form()
    upload = form.get("archivo")
    data = await upload.read(MAX_IMPORT + 1) if upload and hasattr(upload, "read") else b""
    if not data:
        return render(request, "importar.html", {"error": "Elige el archivo CSV.", "back": settings_url("importar")})
    if len(data) > MAX_IMPORT:
        return render(request, "importar.html", {"error": "El archivo es demasiado grande (máx. 5 MB).",
                                                 "back": settings_url("importar")})
    text = importer.decode(data)
    preview = importer.parse(conn, text)
    return render(request, "importar.html", {
        "preview": preview, "payload": base64.b64encode(text.encode()).decode(),
        "filename": getattr(upload, "filename", ""), "back": settings_url("importar")})


@router.post("/ajustes/importar/confirmar")
async def import_confirm(request: Request, conn=Depends(get_db)):
    form = await request.form()
    try:
        text = base64.b64decode(form.get("payload") or "").decode()
    except (ValueError, UnicodeDecodeError):
        return redirect(request, "/ajustes/importar", "No he podido leer los datos. Vuelve a subir el archivo.", "error")
    preview = importer.parse(conn, text)
    if preview.error:
        return redirect(request, "/ajustes/importar", preview.error, "error")
    added = importer.apply(conn, preview, create_missing=form.get("crear") == "1")
    return redirect(request, "/movimientos?mes=todos", f"Importados {added} movimientos")
