"""Lo que hace de la web una app instalable: manifiesto, service worker y pantalla sin conexión."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config
from ..web import templates

router = APIRouter()


@router.get("/manifest.webmanifest")
def manifest():
    data = {
        "name": "Finanzas", "short_name": "Finanzas", "lang": "es-ES", "dir": "ltr",
        "description": "Mis finanzas personales",
        "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
        "background_color": "#eef4f8", "theme_color": "#0276b5",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return JSONResponse(data, media_type="application/manifest+json")


@router.get("/sw.js")
def service_worker(request: Request):
    return templates.TemplateResponse(request, "sw.js", {"version": config.APP_VERSION},
                                      media_type="text/javascript",
                                      headers={"Cache-Control": "no-cache"})


@router.get("/offline")
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {"version": config.APP_VERSION})
