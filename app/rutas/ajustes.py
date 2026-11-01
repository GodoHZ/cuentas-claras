"""Ajustes: tu mes, categorías, sobres, cuentas, reglas y empezar de cero."""
from __future__ import annotations

import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import avisos, backup, calc, config, paperless, repo, views
from ..money import month_key, parse_month
from ..web import get_db, int_or_none, log, parse_optional_amount, redirect, render, settings_url, today
from .planificar import loan_fields

router = APIRouter()


CATALOGS = {
    "categorias": ("categories", "categoría"),
    "sobres": ("envelopes", "sobre"),
    "cuentas": ("accounts", "cuenta"),
}


@router.get("/ajustes")
def settings_page(request: Request, abierto: str = "", conn=Depends(get_db)):
    s = repo.settings(conn)
    backups = backup.list_backups(request.app.state.backup_dir)
    start = repo.start_month(conn)
    return render(request, "ajustes.html", {
        "open": abierto, "s": s, "salary": repo.salary(conn), "start_month": month_key(*start),
        "month_mode": repo.month_mode(conn), "payroll_cat": repo.payroll_category_id(conn),
        "categories": repo.categories(conn), "envelopes": repo.envelopes(conn),
        "accounts": repo.accounts(conn), "loans": repo.loans(conn),
        "amort_id": repo.int_setting(conn, "amortization_category_id"),
        "default_account_id": repo.int_setting(conn, "default_account_id"),
        "envelope_account_id": repo.int_setting(conn, "envelope_account_id"),
        "rules": {k: repo.rule(conn, k) for k in repo.RULES},
        "reglas_banco": repo.rules(conn), "telegram": avisos.configurado(), "avisos_pref": avisos.preferencias(conn),
        "paperless_url": paperless.base_url(conn), "paperless_api": paperless.api_ready(conn),
        "usados": views.used_ids(conn),
        "sweep": views.sweep_settings(conn),
        "copias_offline": repo.offline_cache(conn),
        "backups": backups[:10], "backup_count": len(backups),
        "backup_error": request.app.state.backup_error,
        "backup_problem": backup.dir_problem(request.app.state.backup_dir), "keep_days": repo.rule(conn, "backup_keep_days"),
        "db_size": request.app.state.db_path.stat().st_size if request.app.state.db_path.exists() else 0,
    })


@router.post("/ajustes/general")
async def save_general(request: Request, conn=Depends(get_db)):
    form = await request.form()
    errors = []
    salary = parse_optional_amount(form.get("nomina"), "La nómina", errors, allow_zero=False)
    start = parse_month(form.get("mes_inicio"), (0, 0))
    if start == (0, 0):
        errors.append("El mes de inicio no es válido.")
    if errors:
        return redirect(request, settings_url("general"), " ".join(errors), "error")
    with conn:
        repo.set_setting(conn, "salary", salary)
        repo.set_setting(conn, "month_start", form.get("inicio_mes") if form.get("inicio_mes") in repo.MONTH_MODES else "nomina")
        repo.set_setting(conn, "payroll_category_id", int_or_none(form.get("categoria_nomina")))
        repo.set_setting(conn, "start_month", f"{month_key(*start)}-01")
        for key, field in (("amortization_category_id", "categoria_amortizacion"),
                           ("default_account_id", "cuenta_general"), ("envelope_account_id", "cuenta_sobres")):
            repo.set_setting(conn, key, int_or_none(form.get(field)))
    return redirect(request, settings_url("general"), "Ajustes guardados")


@router.post("/ajustes/sobras")
async def save_sweep(request: Request, conn=Depends(get_db)):
    form = await request.form()
    envelope_id = int_or_none(form.get("sobre"))
    if envelope_id and not repo.get_row(conn, "envelopes", envelope_id):
        return redirect(request, settings_url("sobras"), "Ese sobre no existe.", "error")
    mode = form.get("modo") if form.get("modo") in views.SWEEP_MODES else "cobro"
    day = int_or_none(form.get("dia")) or 1
    if not (1 <= day <= 28):
        return redirect(request, settings_url("sobras"), "El día tiene que estar entre 1 y 28.", "error")
    with conn:
        repo.set_setting(conn, "sweep_mode", mode)
        repo.set_setting(conn, "sweep_envelope_id", envelope_id)
        repo.set_setting(conn, "sweep_enabled", "1" if form.get("activo") else "0")
        repo.set_setting(conn, "sweep_day", day)
    return redirect(request, settings_url("sobras"), "Guardado")


@router.post("/ajustes/reglas")
async def save_rules(request: Request, conn=Depends(get_db)):
    form = await request.form()
    limites = {"budget_warn_pct": (1, 100), "installments_limit_pct": (1, 100),
               "backup_keep_days": (1, 3650)}
    errors, values = [], {}
    for key, (minimo, maximo) in limites.items():
        value = int_or_none(form.get(key))
        if value is None or not (minimo <= value <= maximo):
            errors.append(f"«{RULE_LABELS[key]}» tiene que ser un número entre {minimo} y {maximo}.")
        else:
            values[key] = value
    values["reconcile_small_cents"] = parse_optional_amount(
        form.get("reconcile_small_cents"), f"«{RULE_LABELS['reconcile_small_cents']}»", errors) or 0
    if errors:
        return redirect(request, settings_url("reglas"), " ".join(errors), "error")
    with conn:
        for key, value in values.items():
            repo.set_setting(conn, key, value)
    return redirect(request, settings_url("reglas"), "Reglas guardadas")


@router.post("/ajustes/restablecer-reglas")
def reset_rules(request: Request, conn=Depends(get_db)):
    with conn:
        for key in repo.RULES:
            repo.set_setting(conn, key, None)
    return redirect(request, settings_url("reglas"), "Reglas vueltas a los valores de fábrica")


WIPE_WORD = "BORRAR"


@router.post("/ajustes/empezar-de-cero")
async def wipe(request: Request, conn=Depends(get_db)):
    """Vacía la app para empezar limpio. Hace una copia de seguridad antes."""
    form = await request.form()
    scope = form.get("alcance")
    if (form.get("confirmacion") or "").strip().upper() != WIPE_WORD:
        return redirect(request, settings_url("cero"),
                        f"Para borrar hay que escribir {WIPE_WORD} en la casilla.", "error")
    if scope not in ("movimientos", "todo"):
        return redirect(request, settings_url("cero"), "Elige qué quieres borrar.", "error")
    try:
        copia = await asyncio.to_thread(backup.make_backup, request.app.state.db_path,
                                        request.app.state.backup_dir, config.now(), True)
    except Exception as e:  # noqa: BLE001 - sin copia previa no se borra nada
        log.exception("No se pudo hacer la copia previa al borrado")
        return redirect(request, settings_url("cero"),
                        f"No he borrado nada: no pude hacer la copia de seguridad previa ({e}).", "error")
    with conn:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM tags")                     # sus movimientos ya no están
        conn.execute("DELETE FROM account_checks")
        if scope == "todo":
            conn.execute("DELETE FROM loans")
            conn.execute("DELETE FROM categories")
            conn.execute("DELETE FROM envelopes")
            conn.execute("DELETE FROM accounts")
            repo.set_setting(conn, "amortization_category_id", None)
            cur = conn.execute("INSERT INTO accounts (name, position) VALUES ('Cuenta principal', 0)")
            repo.set_setting(conn, "default_account_id", cur.lastrowid)
            repo.set_setting(conn, "envelope_account_id", cur.lastrowid)
    que = "los movimientos" if scope == "movimientos" else "todo"
    return redirect(request, "/", f"Borrado {que}. Antes guardé una copia: {copia.name}")


RULE_LABELS = {
    "budget_warn_pct": "Aviso del presupuesto",
    "installments_limit_pct": "Límite de cuotas sobre la nómina",
    "reconcile_small_cents": "Diferencia de cuadre que no preocupa",
    "backup_keep_days": "Días de copias guardadas",
}


def volver_a(form, kind: str) -> str:
    """A dónde volver tras guardar: la pantalla desde la que se editó (Sobres...), o Ajustes."""
    destino = form.get("volver") or ""
    return destino if destino.startswith("/") and not destino.startswith("//") else settings_url(kind)


def catalog_fields(kind: str, form, errors: list) -> dict:
    name = " ".join((form.get("nombre") or "").split())[:60]
    if not name:
        errors.append("Escribe un nombre.")
    fields = {"name": name}
    if kind == "categorias":
        fields["kind"] = "ingreso" if form.get("tipo") == "ingreso" else "gasto"
        fields["budget"] = parse_optional_amount(form.get("presupuesto"), "El presupuesto", errors)
    elif kind == "sobres":
        fields["target"] = parse_optional_amount(form.get("objetivo"), "El objetivo", errors, allow_zero=False)
        fields["monthly"] = parse_optional_amount(form.get("aporte"), "El aporte mensual", errors, allow_zero=False)
        fields["note"] = (form.get("nota") or "").strip()[:200]
        fields["account_id"] = int_or_none(form.get("cuenta"))
        if form.get("pregunta_temporal"):                   # solo los formularios que lo preguntan
            fields["temporary"] = 1 if form.get("temporal") else 0
        if form.get("pregunta_fecha"):
            fecha = (form.get("fecha_meta") or "").strip()
            fields["target_date"] = fecha if calc.parse_ym(fecha) else None
            if fecha and not fields["target_date"]:
                errors.append("La fecha de la meta no es válida.")
            elif fields["target_date"] and not fields["target"]:
                errors.append("Para ponerle fecha, pon también la meta (cuánto quieres juntar).")
    elif kind == "cuentas":
        fields["initial_balance"] = parse_optional_amount(form.get("saldo_inicial"), "El saldo inicial",
                                                          errors) or 0
        # la marca distingue "lo ha desmarcado" de "este formulario no lo pregunta"
        if form.get("pregunta_gastar"):
            fields["spendable"] = 1 if form.get("gastar") else 0
    return fields


@router.post("/ajustes/lista/{kind}")
async def catalog_create(request: Request, kind: str, conn=Depends(get_db)):
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, label = CATALOGS[kind]
    errors = []
    form = await request.form()
    fields = catalog_fields(kind, form, errors)
    if not errors:
        fields["position"] = repo.next_position(conn, table)
        try:
            with conn:
                conn.execute(f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                             list(fields.values()))
        except sqlite3.IntegrityError:
            errors.append(f"Ya existe una {label} llamada «{fields['name']}»." if label != "sobre"
                          else f"Ya existe un sobre llamado «{fields['name']}».")
    if errors:
        return redirect(request, volver_a(form, kind), " ".join(errors), "error")
    return redirect(request, volver_a(form, kind), f"Creado: {fields['name']}")


@router.post("/ajustes/lista/{kind}/{row_id:int}")
async def catalog_update(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, label = CATALOGS[kind]
    if not repo.get_row(conn, table, row_id):
        raise HTTPException(404)
    errors = []
    form = await request.form()
    fields = catalog_fields(kind, form, errors)
    if not errors:
        try:
            with conn:
                conn.execute(f"UPDATE {table} SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                             list(fields.values()) + [row_id])
        except sqlite3.IntegrityError:
            errors.append(f"Ya hay otro elemento llamado «{fields['name']}».")
    if errors:
        return redirect(request, volver_a(form, kind), " ".join(errors), "error")
    return redirect(request, volver_a(form, kind), f"Guardado: {fields['name']}")


@router.post("/ajustes/lista/{kind}/{row_id:int}/archivar")
async def catalog_toggle(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, _ = CATALOGS[kind]
    row = repo.get_row(conn, table, row_id)
    if not row:
        raise HTTPException(404)
    with conn:
        conn.execute(f"UPDATE {table} SET active = ? WHERE id = ?", (0 if row["active"] else 1, row_id))
    what = "archivado" if row["active"] else "reactivado"
    return redirect(request, volver_a(await request.form(), kind), f"«{row['name']}» {what}")


@router.post("/ajustes/lista/{kind}/{row_id:int}/borrar")
def catalog_delete(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    """Borra de verdad, pero solo lo que no ha usado nunca nadie."""
    if kind not in CATALOGS:
        raise HTTPException(404)
    table, label = CATALOGS[kind]
    row = repo.get_row(conn, table, row_id)
    if not row:
        raise HTTPException(404)
    if row_id in views.used_ids(conn)[kind]:
        return redirect(request, settings_url(kind),
                        f"«{row['name']}» no se puede borrar: tiene movimientos o está elegido en los ajustes. "
                        f"Archívalo y desaparece de en medio sin tocar el historial.", "error")
    if table == "accounts" and len(repo.accounts(conn)) <= 1:
        return redirect(request, settings_url(kind), "Tiene que quedar al menos una cuenta.", "error")
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    return redirect(request, settings_url(kind), f"«{row['name']}» borrado")


@router.post("/ajustes/lista/{kind}/{row_id:int}/mover")
async def catalog_move(request: Request, kind: str, row_id: int, conn=Depends(get_db)):
    if kind not in CATALOGS and kind != "prestamos":
        raise HTTPException(404)
    table = "loans" if kind == "prestamos" else CATALOGS[kind][0]
    direction = -1 if (await request.form()).get("dir") == "arriba" else 1
    with conn:
        repo.move(conn, table, row_id, direction)
    return redirect(request, settings_url(kind))


@router.post("/ajustes/prestamos")
async def loan_create(request: Request, conn=Depends(get_db)):
    errors = []
    fields = loan_fields(await request.form(), errors)
    if errors:
        return redirect(request, settings_url("prestamos"), " ".join(errors), "error")
    fields["position"] = repo.next_position(conn, "loans")
    with conn:
        conn.execute(f"INSERT INTO loans ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                     list(fields.values()))
    return redirect(request, settings_url("prestamos"), f"Préstamo creado: {fields['name']}")


@router.post("/ajustes/prestamos/{loan_id:int}")
async def loan_update(request: Request, loan_id: int, conn=Depends(get_db)):
    errors = []
    fields = loan_fields(await request.form(), errors)
    if errors:
        return redirect(request, settings_url("prestamos"), " ".join(errors), "error")
    with conn:
        conn.execute(f"UPDATE loans SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                     list(fields.values()) + [loan_id])
    return redirect(request, settings_url("prestamos"), f"Guardado: {fields['name']}")


@router.post("/ajustes/prestamos/{loan_id:int}/borrar")
def loan_delete(request: Request, loan_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM loans WHERE id = ?", (loan_id,))
    return redirect(request, settings_url("prestamos"), "Préstamo borrado")


@router.post("/ajustes/modulos")
async def save_modules(request: Request, conn=Depends(get_db)):
    """Qué partes de la app usas: lo que apagues desaparece (sus datos se quedan)."""
    form = await request.form()
    apagados = [m for m in repo.MODULES if not form.get(m)]
    with conn:
        repo.set_setting(conn, "modules_off", ",".join(apagados))
    return redirect(request, settings_url("modulos"), "Guardado")



# ---- Reglas propias para lo que trae el banco

@router.post("/ajustes/reglas-banco")
async def rule_create(request: Request, conn=Depends(get_db)):
    form = await request.form()
    patron = " ".join((form.get("texto") or "").split())[:60]
    tipo = "ingreso" if form.get("tipo") == "ingreso" else "gasto"
    categoria = repo.get_row(conn, "categories", int_or_none(form.get("categoria_id")) or 0)
    sobre = repo.get_row(conn, "envelopes", int_or_none(form.get("sobre_id")) or 0)
    if not patron:
        return redirect(request, settings_url("reglasbanco"), "Escribe el texto que tiene que llevar el concepto.", "error")
    if not categoria and not sobre:
        return redirect(request, settings_url("reglasbanco"), "Elige una categoría o un sobre.", "error")
    if categoria and categoria["kind"] != tipo:
        return redirect(request, settings_url("reglasbanco"), "Esa categoría no es de ese tipo.", "error")
    with conn:
        conn.execute("INSERT INTO rules (pattern, kind, category_id, envelope_id, position) VALUES (?, ?, ?, ?, ?)",
                     (patron, tipo, categoria["id"] if categoria else None, sobre["id"] if sobre else None,
                      repo.next_position(conn, "rules")))
        # lo que está por revisar y la cumple, ya no hace falta revisarlo
        hechos = 0
        if categoria:
            for r in repo.tx_rows(conn, review=True, type_=tipo):
                if repo.matching_rule(conn, r["concept"], tipo):
                    conn.execute("UPDATE transactions SET category_id = ?, review = 0 WHERE id = ?",
                                 (categoria["id"], r["id"]))
                    hechos += 1
    extra = f" Aplicada a {hechos} por revisar." if hechos else ""
    return redirect(request, settings_url("reglasbanco"), f"Regla guardada: «{patron}».{extra}")


@router.post("/ajustes/reglas-banco/{rule_id:int}/borrar")
def rule_delete(request: Request, rule_id: int, conn=Depends(get_db)):
    with conn:
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return redirect(request, settings_url("reglasbanco"), "Regla borrada")


@router.post("/ajustes/telegram/prueba")
def telegram_test(request: Request, conn=Depends(get_db)):
    if not avisos.configurado():
        return redirect(request, settings_url("telegram"), "Falta configurar el bot (TELEGRAM_TOKEN y TELEGRAM_CHAT).", "error")
    ok = avisos.enviar("✅ Finanzas: así te llegará el resumen cuando cierre cada mes.")
    return redirect(request, settings_url("telegram"), "Mensaje de prueba enviado" if ok else
                    "No se ha podido enviar: revisa el token y el chat.", "ok" if ok else "error")


@router.post("/ajustes/telegram/semana")
def telegram_week_now(request: Request, conn=Depends(get_db)):
    """Manda ya el resumen de la semana (para ver cómo queda)."""
    from .. import banco
    if not avisos.configurado():
        return redirect(request, settings_url("telegram"), "Falta configurar el bot (TELEGRAM_TOKEN y TELEGRAM_CHAT).", "error")
    retenidos = banco.pending(conn) if repo.modules(conn)["bancos"] else []
    ok = avisos.enviar(views.weekly_message(conn, today(request), banco.held(retenidos)))
    return redirect(request, settings_url("telegram"), "Resumen enviado" if ok else "No se ha podido enviar.",
                    "ok" if ok else "error")


@router.post("/ajustes/telegram")
async def telegram_save(request: Request, conn=Depends(get_db)):
    form = await request.form()
    errors = []
    desde = parse_optional_amount(form.get("grande_desde"), "El gasto grande", errors, allow_zero=False)
    if errors:
        return redirect(request, settings_url("telegram"), " ".join(errors), "error")
    with conn:
        for clave, campo in (("alert_big", "grande"), ("alert_budget", "presupuesto"), ("alert_owed", "deudas"),
                             ("alert_weekly", "semanal"), ("alert_changes", "cambios")):
            repo.set_setting(conn, clave, "1" if form.get(campo) else "0")
        repo.set_setting(conn, "alert_big_cents", desde)
    return redirect(request, settings_url("telegram"), "Avisos guardados")


@router.post("/ajustes/paperless")
async def paperless_save(request: Request, conn=Depends(get_db)):
    form = await request.form()
    url = (form.get("url") or "").strip().rstrip("/")[:200]
    if url and not url.startswith(("http://", "https://")):
        return redirect(request, settings_url("paperless"), "La dirección tiene que empezar por https://", "error")
    with conn:
        repo.set_setting(conn, "paperless_url", url or None)
    return redirect(request, settings_url("paperless"), "Guardado" if url else "Paperless desconectado")
