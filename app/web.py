"""Lo que comparten todas las pantallas: plantillas, la base de datos de cada petición, avisos y redirecciones."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import calc, config, db, repo, views
from .money import (add_months_ym, amount_input, eur, fmt_date, InvalidAmount, long_day, month_key,
                    month_name, month_short, parse_amount, pct)
from .nombres import limpio


log = logging.getLogger("finanzas")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


OPEN_PATHS = {"/pin", "/salud", "/manifest.webmanifest", "/sw.js", "/offline"}


NAV = [("/", "inicio"), ("/movimientos", "movimientos"), ("/nuevo", "movimientos"),
       ("/sobres", "planificar"), ("/presupuesto", "planificar"), ("/deudas", "planificar"), ("/fijos", "planificar"),
       ("/calendario", "planificar"), ("/reparto", "inicio"),
       ("/cuentas", "cuentas"), ("/banco", "cuentas"), ("/resumen", "resumen"), ("/etiquetas", "resumen"),
       ("/me-deben", "cuentas"), ("/ajustes", "ajustes"),
       ("/mas", "ajustes")]


templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


def cuando(marca: str | None) -> str:
    """'2026-10-30T18:53:35Z' (UTC) -> 'hoy a las 19:53' / 'el 29/10 a las 21:44' (hora de aquí)."""
    if not marca:
        return ""
    momento = datetime.fromisoformat(marca.replace("Z", "+00:00")).astimezone(config.TZ)
    dia = "hoy" if momento.date() == datetime.now(config.TZ).date() else f"el {momento:%d/%m}"
    return f"{dia} a las {momento:%H:%M}"


templates.env.filters.update(
    cuando=cuando, eur=eur, eur_sign=lambda c: eur(c, sign=True), pct=pct, pct1=lambda x: pct(x, 1), fecha=fmt_date,
    dia_largo=long_day, importe_input=amount_input, limpio=limpio,
)


templates.env.globals.update(TYPES=calc.TYPES, month_name=month_name, month_short=month_short,
                             month_key=month_key, add_months=add_months_ym)


def get_db(request: Request):
    conn = db.connect(request.app.state.db_path)
    request.state.conn = conn
    try:
        yield conn
    finally:
        conn.close()


def today(request: Request) -> date:
    return request.app.state.today()


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def set_flash(response: Response, message: str, kind: str = "ok") -> None:
    response.set_cookie("flash", quote(json.dumps({"msg": message, "kind": kind})),
                        max_age=60, httponly=True, samesite="lax")


def redirect(request: Request, url: str, flash: str | None = None, kind: str = "ok") -> Response:
    """Tras un POST: con HTMX navega sin recargar (HX-Location); sin HTMX, 303 normal."""
    if is_htmx(request):
        response = Response(status_code=200, headers={"HX-Location": url})
    else:
        response = RedirectResponse(url, status_code=303)
    if flash:
        set_flash(response, flash, kind)
    return response


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200, headers=None):
    ctx = dict(ctx or {})
    raw = request.cookies.get("flash")
    if raw:
        try:
            ctx["flash"] = json.loads(unquote(raw))
        except ValueError:
            pass
    response = templates.TemplateResponse(request, name, ctx, status_code=status, headers=headers)
    if raw:
        response.delete_cookie("flash")
    return response


def nav_section(path: str) -> str:
    for prefix, section in NAV:
        if prefix == "/" and path == "/":
            return section
        if prefix != "/" and path.startswith(prefix):
            return section
    return ""


def base_context(request: Request) -> dict:
    """Variables de todas las páginas: versión, menú y el formulario rápido del botón +."""
    ctx = {"version": config.APP_VERSION, "nav": nav_section(request.url.path),
           "pin_enabled": False, "usa": {m: True for m in repo.MODULES}}
    conn = getattr(request.state, "conn", None)
    if conn is not None:
        opts = views.tx_form_options(conn)
        ctx.update(qa=opts, qa_form=views.blank_tx_form(opts, today(request)),
                   pin_enabled=bool(repo.setting(conn, "pin_hash")), usa=repo.modules(conn),
                   rango_mes=lambda y, m, c=conn: views.period_range(c, y, m),
                   envelope_account=views.envelope_account_name(conn))
    return ctx


templates.context_processors.append(base_context)


def settings_url(section: str) -> str:
    return f"/ajustes?abierto={section}#{section}"


def parse_optional_amount(text, label: str, errors: list, allow_zero: bool = True) -> int | None:
    try:
        value = parse_amount(text)
    except InvalidAmount:
        errors.append(f"{label}: «{text}» no es un importe válido.")
        return None
    if value is not None and (value < 0 or (value == 0 and not allow_zero)):
        errors.append(f"{label} tiene que ser mayor que 0.")
        return None
    return value
