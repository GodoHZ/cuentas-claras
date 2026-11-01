"""Cifras ya calculadas para cada pantalla (junta repo.py y calc.py)."""
from __future__ import annotations

import calendar
import json
from dataclasses import replace
from datetime import date, timedelta

from . import calc, repo
from .money import add_months_ym, eur, month_name, month_short


def envelope_status(env, balance: int, today: date | None = None) -> calc.EnvelopeStatus:
    return calc.envelope_status(balance, env["target"], env["monthly"], env["target_date"], today)


def envelopes_overview(conn, txs, include_archived: bool = False, today: date | None = None):
    """Tarjetas de sobres y total en sobres (el total incluye los archivados)."""
    balances = calc.envelope_balances(txs)
    cuentas = {a["id"]: a["name"] for a in repo.accounts(conn)}
    cards = []
    for env in repo.envelopes(conn):
        balance = balances.get(env["id"], 0)
        if env["active"] or (include_archived and balance):
            cards.append({"env": env, "st": envelope_status(env, balance, today),
                          "account": cuentas.get(env["account_id"])})
    return cards, sum(balances.values())


SWEEP_MODES = ("cobro", "mes")
CLOSE_VISIBLE_DAYS = 10            # cuántos días se ve en Inicio cómo cerró el último ciclo


def sweep_settings(conn) -> dict:
    """Ajustes de «lo que sobra va al sobre de ahorro».

    Dos maneras de contar lo que sobra: «cobro» (la de fábrica) es lo que te queda en las
    cuentas el día antes de cobrar; «mes» es lo cobrado en el mes menos lo gastado y lo guardado.
    """
    envelope_id = repo.int_setting(conn, "sweep_envelope_id")
    envelope = repo.get_row(conn, "envelopes", envelope_id) if envelope_id else None
    mode = repo.setting(conn, "sweep_mode", "cobro")
    if repo.month_mode(conn) == "nomina":
        mode = "cobro"                 # si el mes va de nómina a nómina, lo que sobra es lo de antes de cobrar
    return {
        "enabled": repo.setting(conn, "sweep_enabled", "1") == "1" and envelope is not None,
        "envelope": envelope,
        "mode": mode if mode in SWEEP_MODES else "cobro",
        "day": min(max(repo.int_setting(conn, "sweep_day") or 1, 1), 28),
        "done_until": repo.setting(conn, "last_sweep_month", ""),
        "done_payday": repo.setting(conn, "last_sweep_payday", ""),
    }


def paydays(conn) -> list[date]:
    """Los días en que has cobrado la nómina (el primero de cada tanda)."""
    cat = repo.payroll_category_id(conn)
    if not cat:
        return []
    return calc.payday_starts(date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT date FROM transactions WHERE type = ? AND category_id = ?", (calc.INGRESO, cat)))


def _pending_payday_sweep(conn, txs, today: date, cfg: dict):
    """Lo que te quedaba el día antes del último cobro, si ese ciclo aún no se ha cerrado.

    Es el «puedes gastar» de ese día: lo que había en las cuentas sin lo apartado en sobres ni lo
    que quedaba por pagar y guardar. Se cierra el día siguiente al cobro (que al banco le dé tiempo
    a traer lo de los últimos días). Puede salir negativo: llegaste justo.
    """
    cobros = [d for d in paydays(conn) if d <= today]
    if not cobros:
        return None
    cobro = cobros[-1]
    if cfg["done_payday"] and cobro.isoformat() <= cfg["done_payday"]:
        return None
    antes = cobro - timedelta(days=1)
    previos = [t for t in txs if t.date < cobro]
    left = forecast(conn, previos, *repo.period_of(conn, antes), antes).left
    return {"mode": "cobro", "payday": cobro, "until": antes, "amount": left, "envelope": cfg["envelope"],
            "enabled": cfg["enabled"], "due": today > cobro and cfg["enabled"]}


def last_close(conn, today: date) -> dict | None:
    """Cómo cerró el último ciclo (lo que quedaba antes de cobrar), los primeros días tras cobrar."""
    try:
        cierre = json.loads(repo.setting(conn, "last_close") or "null")
        cobro = date.fromisoformat(cierre["payday"])
    except (ValueError, TypeError, KeyError):
        return None
    if not 0 <= (today - cobro).days <= CLOSE_VISIBLE_DAYS:
        return None
    return {**cierre, "payday": cobro}


def sweep_concept(year: int, month: int) -> str:
    return f"Sobras de {month_name(year, month)}"


def pending_sweep(conn, txs, today: date):
    """Lo que sobró y todavía no se ha guardado (o no se ha cerrado). None si no hay nada que hacer."""
    cfg = sweep_settings(conn)
    if not cfg["envelope"]:
        return None
    if cfg["mode"] == "cobro":
        return _pending_payday_sweep(conn, txs, today, cfg)
    year, month = add_months_ym(today.year, today.month, -1)     # el mes anterior al de hoy
    key = f"{year:04d}-{month:02d}"
    if cfg["done_until"] >= key:
        return None
    start = repo.start_month(conn)
    if (year, month) < start:
        return None
    left = calc.month_summary(txs, year, month).free
    if left <= 0:
        return None
    already = conn.execute(
        "SELECT 1 FROM transactions WHERE type = ? AND envelope_id = ? AND concept = ? LIMIT 1",
        (calc.APORTE, cfg["envelope"]["id"], sweep_concept(year, month))).fetchone()
    if already:
        return None
    return {"mode": "mes", "year": year, "month": month, "amount": left, "envelope": cfg["envelope"],
            "day": cfg["day"], "enabled": cfg["enabled"],
            "due": today.day >= cfg["day"] and cfg["enabled"]}


def do_sweep(conn, pending: dict) -> int | None:
    """Guarda lo que sobró en el sobre elegido. Devuelve el id del movimiento (None si no sobró nada)."""
    if pending.get("mode") == "cobro":
        return _close_payday(conn, pending)
    year, month = pending["year"], pending["month"]
    last_day = calendar.monthrange(year, month)[1]
    account = repo.int_setting(conn, "envelope_account_id") or repo.int_setting(conn, "default_account_id")
    with conn:
        tx_id = repo.insert_tx(conn, date=f"{year:04d}-{month:02d}-{last_day:02d}", type=calc.APORTE,
                               category_id=None, envelope_id=pending["envelope"]["id"],
                               account_id=account, concept=sweep_concept(year, month),
                               amount=pending["amount"])
        repo.set_setting(conn, "last_sweep_month", f"{year:04d}-{month:02d}")
    return tx_id


def payday_concept(payday: date) -> str:
    return f"Sobrante antes de cobrar ({payday.strftime('%d/%m/%Y')})"


def _close_payday(conn, pending: dict) -> int | None:
    """Cierra el ciclo: si sobró algo, lo aparta en el sobre (con fecha del día antes de cobrar)."""
    account = (pending["envelope"]["account_id"] or repo.int_setting(conn, "envelope_account_id")
               or repo.int_setting(conn, "default_account_id"))
    tx_id = None
    with conn:
        if pending["amount"] > 0:
            tx_id = repo.insert_tx(conn, date=pending["until"].isoformat(), type=calc.APORTE,
                                   category_id=None, envelope_id=pending["envelope"]["id"], account_id=account,
                                   concept=payday_concept(pending["payday"]), amount=pending["amount"])
        repo.set_setting(conn, "last_sweep_payday", pending["payday"].isoformat())
        repo.set_setting(conn, "last_close", json.dumps({
            "payday": pending["payday"].isoformat(), "amount": pending["amount"],
            "envelope": pending["envelope"]["name"], "saved": tx_id is not None}))
    from . import avisos
    if avisos.configurado():
        avisos.enviar(close_message(conn, pending["payday"], pending["amount"], pending["envelope"]["name"],
                                    tx_id is not None))
    return tx_id


def used_ids(conn) -> dict[str, set[int]]:
    """Qué categorías, sobres y cuentas están en uso: no se pueden borrar, solo archivar."""
    used = {
        "categorias": {r[0] for r in conn.execute(
            "SELECT DISTINCT category_id FROM transactions WHERE category_id IS NOT NULL")},
        "sobres": {r[0] for r in conn.execute(
            "SELECT DISTINCT envelope_id FROM transactions WHERE envelope_id IS NOT NULL")},
        "cuentas": {r[0] for r in conn.execute("SELECT DISTINCT account_id FROM transactions")},
    }
    for key, kind in (("amortization_category_id", "categorias"), ("sweep_envelope_id", "sobres"),
                      ("default_account_id", "cuentas"), ("envelope_account_id", "cuentas")):
        elegido = repo.int_setting(conn, key)
        if elegido:
            used[kind].add(elegido)
    return used


def envelope_account_name(conn) -> str:
    """Cómo se llama la cuenta donde vive el dinero de los sobres (configurable)."""
    row = repo.get_row(conn, "accounts", repo.int_setting(conn, "envelope_account_id") or 0)
    return row["name"] if row else "tu cuenta de ahorro"


def tx_de_fila(row) -> calc.Tx:
    """Una fila de la base de datos en el formato que usan las reglas."""
    return calc.Tx(date.fromisoformat(row["date"]), row["type"], row["amount"], row["category_id"],
                   row["envelope_id"], row["account_id"], row["other_account_id"], row["month"])



def accounts_overview(conn, txs, include_archived: bool = False):
    """Cada cuenta con su saldo, lo que tiene apartado en sobres y lo que queda libre."""
    cuentas = repo.accounts(conn) if include_archived else repo.accounts(conn, active=True)
    saldos = calc.account_balances(txs, repo.accounts(conn))
    en_sobres = calc.envelope_balances(txs)
    tarjetas = []
    for cuenta in cuentas:
        apartado = sum(saldo for env, saldo in en_sobres.items()
                       if (repo.get_row(conn, "envelopes", env) or {"account_id": None})["account_id"] == cuenta["id"])
        saldo = saldos.get(cuenta["id"], 0)
        tarjetas.append({
            "account": cuenta, "balance": saldo, "in_envelopes": apartado, "free": saldo - apartado,
            "check": reconciliation(conn, cuenta, txs),
        })
    return tarjetas, sum(saldos.get(c["id"], 0) for c in cuentas)


def reconciliation(conn, account, txs):
    """Lo que dijo el banco la última vez frente a lo que calculaba la app ESE DÍA.

    La comparación es contra el saldo a la fecha de la comprobación, no contra el
    de hoy: si no, todo lo que apuntaras después saldría como un descuadre que no
    existe.
    """
    last = repo.last_check(conn, account["id"])
    if not last:
        return None
    calculated = calc.account_balance_at(txs, account["id"], account["initial_balance"] or 0,
                                         date.fromisoformat(last["date"]))
    return {
        "check": last,
        "r": calc.reconcile(last["balance"], calculated, repo.rule(conn, "reconcile_small_cents")),
    }


def loan_upcoming(loan, spent: dict[int, int], year: int, month: int, today: date):
    """La cuota de ese mes y si ya la has apuntado. None si ese mes no toca ninguna.

    Para saber si está apuntada se miran los gastos de la categoría del préstamo.
    Si no le has puesto categoría, se da por pagada cuando pasa su día: las cuotas
    van domiciliadas, así que el banco la cobra solo.

    Lo gastado se va descontando de `spent`: si varios pagos fijos comparten categoría
    (tres suscripciones en «Suscripciones»), cada euro solo cubre a uno.
    """
    due = calc.installment_in_month(date.fromisoformat(loan["first_date"]),
                                    loan["n_installments"], year, month)
    if due is None:
        return None
    if loan["category_id"]:
        done = min(spent.get(loan["category_id"], 0), loan["installment"])
        spent[loan["category_id"]] = spent.get(loan["category_id"], 0) - done
    else:
        done = loan["installment"] if today > due else 0
    return calc.Upcoming(calc.CUOTA, loan["name"], loan["installment"],
                         min(done, loan["installment"]), due.day,
                         category_id=loan["category_id"], due=due)


def available_money(conn, txs) -> int:
    """El dinero que tienes y no tiene nombre todavía.

    Lo que hay en las cuentas activas que cuentan para el día a día, menos lo que
    guardan los sobres. Es el punto de partida de todo: el dinero que tienes es el
    que tienes, no el que hayas cobrado este mes. Las cuentas que no son para
    gastar (una tarjeta de comida, por ejemplo) se dejan fuera en Ajustes.
    """
    cuentan = {a["id"] for a in repo.accounts(conn, active=True) if a["spendable"]}
    saldos = calc.account_balances(txs, repo.accounts(conn))
    cuenta_del_sobre = {e["id"]: e["account_id"] for e in repo.envelopes(conn)}
    # de los sobres se descuenta lo que viva en esas cuentas (y lo que no viva en ninguna)
    apartado = sum(saldo for env, saldo in calc.envelope_balances(txs).items()
                   if cuenta_del_sobre.get(env) in cuentan or cuenta_del_sobre.get(env) is None)
    return sum(saldos.get(i, 0) for i in cuentan) - apartado


def forecast(conn, txs, year: int, month: int, today: date, held: int = 0) -> calc.Forecast:
    """Lo que puedes gastar: lo que tienes menos lo que ya está comprometido.

    Lo comprometido son dos cosas: la cuota de cada préstamo que toque este mes y
    el aporte mensual de cada sobre, descontando lo que ya hayas apuntado de cada
    una. Así, a principio de mes, el dinero que va a salir sí o sí no parece tuyo.
    `held` es lo que el banco tiene retenido (ver banco.pending): ya no está, aunque aún no se vea.
    """
    lines = _upcoming(conn, txs, year, month, today)
    # Si ya ha llegado la nómina del mes que viene (cobro a final de mes), ese dinero ya está en
    # la cuenta: lo que toca pagar y guardar ese mes también tiene dueño.
    ny, nm = add_months_ym(year, month, 1)
    if any(t.type == calc.INGRESO and t.ym == (ny, nm) and t.date <= today for t in txs):
        lines += [replace(u, next_month=True) for u in _upcoming(conn, txs, ny, nm, today)]
    return calc.Forecast(available_money(conn, txs), tuple(lines), held)


def _upcoming(conn, txs, year: int, month: int, today: date) -> list[calc.Upcoming]:
    """Las cuotas y los aportes mensuales de ese mes, con lo que ya está apuntado de cada uno."""
    spent = calc.spent_by_category(txs, year, month)
    contributed = calc.contributed_by_envelope(txs, year, month)
    usa = repo.modules(conn)
    lines = []
    for loan in repo.loans(conn) if usa["fijos"] else []:
        cuota = loan_upcoming(loan, spent, year, month, today)
        if cuota:
            lines.append(cuota)
    for env in repo.envelopes(conn, active=True) if usa["sobres"] else []:
        if env["monthly"]:
            lines.append(calc.Upcoming(calc.APORTE_MENSUAL, env["name"], env["monthly"],
                                       min(contributed.get(env["id"], 0), env["monthly"]),
                                       envelope_id=env["id"]))
    return lines


def until_payday(conn, left: int, today: date) -> dict | None:
    """Hasta el próximo cobro (más o menos: un mes después del último): días y cuánto al día."""
    cobro = calc.next_payday_estimate(repo.cycles(conn), today)
    if not cobro:
        return None
    dias = max(1, (cobro - today).days)
    return {"date": cobro, "days": dias, "per_day": left // dias if left > 0 else 0}


def debts(conn, txs, today: date):
    year, month = repo.period_of(conn, today)
    spent = calc.spent_by_category(txs, year, month)
    items = []
    for loan in repo.loans(conn):
        st = calc.loan_status(loan["installment"], date.fromisoformat(loan["first_date"]),
                              loan["n_installments"], today)
        items.append({"loan": loan, "st": st,
                      "month": loan_upcoming(loan, spent, year, month, today),
                      "category": repo.get_row(conn, "categories", loan["category_id"])
                                  if loan["category_id"] else None})
    salary = repo.salary(conn)
    limit = repo.rule(conn, "installments_limit_pct") / 100
    amort_id = repo.int_setting(conn, "amortization_category_id")
    extra = sum(t.amount for t in txs if t.type == calc.GASTO and amort_id and t.category_id == amort_id)
    alive = [i["st"] for i in items if not i["st"].finished]
    return {
        "loans": items,
        "salary": salary,
        "ratio": calc.installments_ratio([i["st"] for i in items], salary),
        "limit": limit,
        "monthly_total": sum(s.installment for s in alive),
        "pending_total": sum(s.pending_total or 0 for s in alive),
        "extra": extra,
        "amort_category": repo.get_row(conn, "categories", amort_id) if amort_id else None,
        "month_pending": sum(i["month"].pending for i in items if i["month"]),
        "month_total": sum(i["month"].expected for i in items if i["month"]),
    }


def budget(conn, txs, year: int, month: int):
    warn = repo.rule(conn, "budget_warn_pct") / 100
    spent = calc.spent_by_category(txs, year, month)
    # las categorías de un pago fijo no se vigilan con un tope: su «presupuesto» es la cuota, y va aparte
    hoy = date.today()
    fijos = {l["category_id"]: l for l in repo.loans(conn) if l["category_id"] and not calc.loan_status(
        l["installment"], date.fromisoformat(l["first_date"]), l["n_installments"], hoy).finished}
    budgeted, unbudgeted, fixed = [], [], []
    aportado = calc.contributed_by_envelope(txs, year, month)
    for cat in repo.categories(conn):
        s = spent.get(cat["id"], 0)
        if cat["id"] in fijos:
            fixed.append({"cat": cat, "loan": fijos[cat["id"]],
                          "line": calc.budget_line(fijos[cat["id"]]["installment"], s, warn)})
            continue
        if cat["kind"] != "gasto" and not s:
            continue
        if not cat["active"] and not s:
            continue
        line = calc.budget_line(cat["budget"], s, warn)
        if line.budget > 0:
            budgeted.append({"cat": cat, "line": line})
        elif s:
            unbudgeted.append({"cat": cat, "line": line})
    total = calc.budget_line(sum(b["line"].budget for b in budgeted),
                             sum(b["line"].spent for b in budgeted), warn)
    return {
        "lines": budgeted,
        "warn_pct": warn,
        "others": unbudgeted,
        "others_total": sum(o["line"].spent for o in unbudgeted),
        "total": total,
        "alerts": [b for b in budgeted if b["line"].level in ("aviso", "pasado")],
        "fixed": fixed,
        "fixed_total": sum(f["loan"]["installment"] for f in fixed),
        # lo que guardas cada mes en sobres: no es gasto, pero también es parte del plan del mes
        "savings": [{"env": e, "monthly": e["monthly"], "done": min(aportado.get(e["id"], 0), e["monthly"])}
                    for e in repo.envelopes(conn, active=True) if e["monthly"]] if repo.modules(conn)["sobres"] else [],
    }


def summary_months(txs, start: tuple[int, int], count: int = 12):
    months = [calc.month_summary(txs, *add_months_ym(*start, i)) for i in range(count)]
    total = calc.MonthSummary(
        0, 0,
        sum(m.income for m in months), sum(m.payroll_spent for m in months),
        sum(m.saved for m in months), sum(m.envelope_spent for m in months))
    return months, total


def tx_form_options(conn, include_category: int | None = None, include_envelope: int | None = None,
                    include_account: int | None = None, tx_type: str | None = None):
    """Chips del formulario: lo más usado primero; solo lo activo (+ lo que ya tenga el movimiento)."""
    cat_use, env_use = repo.usage_counts(conn)
    cats = [c for c in repo.categories(conn) if c["active"] or c["id"] == include_category]
    by_use = lambda use: (lambda r: (-use.get(r["id"], 0), r["position"], r["name"]))  # noqa: E731
    groups = {
        "gasto": sorted([c for c in cats if c["kind"] == "gasto"], key=by_use(cat_use)),
        "ingreso": sorted([c for c in cats if c["kind"] == "ingreso"], key=by_use(cat_use)),
    }
    # un movimiento importado puede tener una categoría del "otro" tipo: que se vea igualmente
    if include_category and tx_type in groups and not any(c["id"] == include_category for c in groups[tx_type]):
        groups[tx_type].insert(0, next(c for c in cats if c["id"] == include_category))
    envs = sorted([e for e in repo.envelopes(conn) if e["active"] or e["id"] == include_envelope],
                  key=by_use(env_use))
    accounts = [a for a in repo.accounts(conn) if a["active"] or a["id"] == include_account]
    default_acc = repo.int_setting(conn, "default_account_id") or (accounts[0]["id"] if accounts else None)
    envelope_acc = repo.int_setting(conn, "envelope_account_id") or default_acc
    return {
        # en qué cuenta vive cada sobre: al elegir sobre, la cuenta se pone sola
        "envelope_accounts": {e["id"]: e["account_id"] for e in envs},
        "cats_gasto": groups["gasto"],
        "cats_ingreso": groups["ingreso"],
        "envelopes": envs,
        "accounts": accounts,
        "default_account": default_acc,
        "envelope_account": envelope_acc,
    }


def blank_tx_form(opts, today: date, type_: str = calc.GASTO) -> dict:
    return {
        "tipo": type_, "importe": "", "categoria_id": None, "sobre_id": None,
        "cuenta_id": opts["envelope_account"] if type_ in calc.NEEDS_ENVELOPE else opts["default_account"],
        "fecha": today.isoformat(), "concepto": "", "cuenta_mes": "", "errors": [],
    }


def saved_message(type_: str, amount: int, category: str | None, envelope: str | None,
                  account: str | None = None, other_account: str | None = None) -> str:
    nombres = {calc.GASTO: "Gasto", calc.INGRESO: "Ingreso", calc.APORTE: "Aporte",
               calc.RETIRO: "Retiro", calc.TRASPASO: "Traspaso"}
    what = nombres.get(type_, "Movimiento")
    if type_ == calc.TRASPASO:
        return f"{what} de {eur(amount)} · {other_account} → {account}"
    where = envelope if type_ in calc.NEEDS_ENVELOPE else category
    if type_ == calc.GASTO and envelope:
        where = f"{category} (desde {envelope})"
    return f"{what} de {eur(amount)} · {where}"


def recurring(conn, today: date) -> dict:
    """La pantalla de gastos fijos: lo que se repite cada mes, con su nombre y su categoría."""
    filas = conn.execute("SELECT date, category_id, concept, amount FROM transactions WHERE type = ?",
                         (calc.GASTO,)).fetchall()
    fijos = calc.recurring([(date.fromisoformat(f["date"]), f["category_id"], f["concept"], f["amount"])
                            for f in filas], today)
    categorias = {c["id"]: c["name"] for c in repo.categories(conn)}
    items = [{"r": r, "name": r.name or categorias.get(r.category_id, "Sin categoría"),
              "category": categorias.get(r.category_id) if r.name else None} for r in fijos]
    total = sum(r.amount for r in fijos)
    return {"lista": items, "monthly": total, "yearly": total * 12}



def period_range(conn, year: int, month: int) -> dict | None:
    """Las fechas de un «mes» de nómina a nómina, para enseñarlas («del 28/10 al 27/11»).

    None si se cuenta por meses naturales (no hace falta decir nada) o si ese mes no es de cobro
    a cobro. El final del mes en curso es una estimación: un mes después del último cobro.
    """
    ciclos = repo.cycles(conn)
    ciclo = next((c for c in ciclos if c.label == (year, month)), None)
    if not ciclo:
        return None
    if ciclo.end:
        return {"desde": ciclo.start, "hasta": ciclo.end - timedelta(days=1), "estimado": False}
    return {"desde": ciclo.start, "hasta": calc.add_months(ciclo.start, 1) - timedelta(days=1), "estimado": True}


SOON_DAYS = 7                      # los pagos de la próxima semana salen en «Para ti»


def attention(forecast_: calc.Forecast | None, cuentas: list[dict], today: date) -> list[dict]:
    """Lo que pide que hagas algo, para lo alto del Inicio (lo del banco y el presupuesto, aparte).

    - El dinero apartado en los sobres de una cuenta es más de lo que hay en ella: falta hacer la
      transferencia (por ejemplo, lo que sobró antes de cobrar y la app ya guardó en el ahorro).
    - Pagos fijos que se cargan en los próximos días (o que ya pasaron) y no están apuntados.
    """
    cosas = []
    for c in cuentas:
        if c["in_envelopes"] > 0 and c["free"] < 0:
            cosas.append({"kind": "transferir", "account": c["account"], "amount": -c["free"]})
    if forecast_:
        for u in forecast_.lines:
            if u.kind == calc.CUOTA and not u.paid and u.due and (u.due - today).days <= SOON_DAYS:
                cosas.append({"kind": "pago", "line": u, "late": u.due < today})
    return cosas


KINDS = {"prestamo": "Préstamo", "seguro": "Seguro", "suscripcion": "Suscripción", "otro": "Pago fijo"}


def fixed_payments(conn, txs, today: date) -> dict:
    """Los pagos fijos (préstamos, seguros, suscripciones...), los que parece que tienes y no están, y
    cuánto se va al mes. Lo que está aquí es lo que el previsto descuenta de «Puedes gastar»."""
    year, month = repo.period_of(conn, today)
    spent = calc.spent_by_category(txs, year, month)
    categorias = {c["id"]: c["name"] for c in repo.categories(conn)}
    items = []
    for loan in repo.loans(conn):
        primera = date.fromisoformat(loan["first_date"])
        st = calc.loan_status(loan["installment"], primera, loan["n_installments"], today)
        items.append({"loan": loan, "st": st, "kind": KINDS.get(loan["kind"], "Pago fijo"),
                      "next": calc.next_installment(primera, loan["n_installments"], today),
                      "month": loan_upcoming(loan, spent, year, month, today),
                      "category": categorias.get(loan["category_id"])})
    vivos = [i for i in items if not i["st"].finished]
    prestamos = [i["st"] for i in vivos if i["loan"]["kind"] == "prestamo"]
    salary = repo.salary(conn)
    # lo que se repite cada mes y no está entre los pagos fijos (por categoría): ¿lo añades?
    cubiertas = {i["loan"]["category_id"] for i in vivos if i["loan"]["category_id"]}
    sugerencias = [s for s in recurring(conn, today)["lista"] if s["r"].category_id not in cubiertas]
    amort_id = repo.int_setting(conn, "amortization_category_id")
    return {
        "pagos": vivos,
        "finished": [i for i in items if i["st"].finished],
        "monthly": sum(i["loan"]["installment"] for i in vivos),
        "loans_monthly": sum(s.installment for s in prestamos),
        "ratio": calc.installments_ratio(prestamos, salary),
        "limit": repo.rule(conn, "installments_limit_pct") / 100,
        "salary": salary,
        "suggestions": sugerencias,
        "extra": sum(t.amount for t in txs if t.type == calc.GASTO and amort_id and t.category_id == amort_id),
        "amort_category": categorias.get(amort_id) if amort_id else None,
        "categories": repo.categories(conn, active=True, kind="gasto"),
        "kinds": KINDS,
    }


def bank_status(conn) -> dict | None:
    """Cuándo se miró el banco por última vez (la más antigua de las conexiones) y si falló algo."""
    from datetime import datetime, timezone
    filas = conn.execute("SELECT aspsp_name, last_sync, last_error FROM bank_links WHERE session_id IS NOT NULL").fetchall()
    fechas = [datetime.fromisoformat(f["last_sync"].replace("Z", "+00:00")) for f in filas if f["last_sync"]]
    if not fechas:
        return None
    minutos = int((datetime.now(timezone.utc) - min(fechas)).total_seconds() // 60)
    hace = ("ahora mismo" if minutos < 1 else f"hace {minutos} min" if minutos < 60
            else f"hace {minutos // 60} h" if minutos < 1440 else f"hace {minutos // 1440} días")
    return {"hace": hace, "errores": [f["aspsp_name"] for f in filas if f["last_error"]]}


def transfer_envelopes(conn, transfers, today: date) -> dict[int, dict]:
    """Para cada traspaso por revisar: los sobres de la cuenta a la que llega y cuál parece el suyo.

    Parece el suyo el sobre de esa cuenta con un aporte mensual pendiente por ese mismo importe
    (los 300 € que guardas cada mes para amortizar, por ejemplo).
    """
    if not transfers:
        return {}
    hoy_mes = repo.period_of(conn, today)
    pendientes = {u.envelope_id: u.pending for u in forecast(conn, repo.all_txs(conn), *hoy_mes, today).lines
                  if u.kind == calc.APORTE_MENSUAL and u.pending}
    sobres = repo.envelopes(conn, active=True)
    resultado = {}
    for t in transfers:
        suyos = [e for e in sobres if e["account_id"] == t["account_id"]]
        sugerido = next((e["id"] for e in suyos if pendientes.get(e["id"]) == t["amount"]), None)
        resultado[t["id"]] = {"sobres": suyos, "sugerido": sugerido}
    return resultado


# ---------------------------------------------------------------- a este ritmo

PACE_DAYS = 30                     # el ritmo, con lo gastado en los últimos 30 días
MIN_DAYS_FOR_PACE = 14             # con menos historia, sale a voleo


def fixed_categories(conn, today: date) -> set[int]:
    """Las categorías de los pagos fijos vivos: ya están en lo previsto, no son «ritmo»."""
    return {l["category_id"] for l in repo.loans(conn) if l["category_id"] and not calc.loan_status(
        l["installment"], date.fromisoformat(l["first_date"]), l["n_installments"], today).finished}


def pace(conn, txs, left: int, today: date, cobro: dict | None) -> dict | None:
    """A este ritmo, con cuánto llegas al próximo cobro.

    El ritmo es lo gastado al día en los últimos 30 días en lo variable (sin pagos fijos, que ya
    están en lo previsto, ni lo pagado con sobres). Con 30 días, un gasto grande de una vez al mes
    (la aportación a casa) pesa lo que tiene que pesar, y no como si fuera diario.
    """
    if not cobro:
        return None
    gastos = [t for t in txs if t.type == calc.GASTO and t.envelope_id is None and t.date <= today]
    if not gastos or (today - min(t.date for t in gastos)).days < MIN_DAYS_FOR_PACE:
        return None
    desde = today - timedelta(days=PACE_DAYS - 1)
    fijas = fixed_categories(conn, today)
    gastado = sum(t.amount for t in gastos if t.date >= desde and t.category_id not in fijas)
    dias = min(PACE_DAYS, (today - min(t.date for t in gastos)).days + 1)
    al_dia = gastado // dias
    return {"per_day": al_dia, "end": left - al_dia * cobro["days"], "days": cobro["days"], "date": cobro["date"]}


# ---------------------------------------------------------------- en qué se va

def category_trends(txs, year: int, month: int, categories, months_back: int = 3) -> list[dict]:
    """Lo gastado en cada categoría ese mes frente a la media de los meses anteriores."""
    actual = calc.spent_by_category(txs, year, month)
    anteriores = [calc.spent_by_category(txs, *add_months_ym(year, month, -i)) for i in range(1, months_back + 1)]
    con_datos = [a for a in anteriores if a]
    nombres = {c["id"]: c["name"] for c in categories}
    filas = []
    for cat_id in set(actual) | {k for a in con_datos for k in a}:
        media = sum(a.get(cat_id, 0) for a in con_datos) // len(con_datos) if con_datos else None
        gasto = actual.get(cat_id, 0)
        if not gasto and not media:
            continue
        cambio = (gasto - media) / media if media else None
        filas.append({"name": nombres.get(cat_id, "Sin categoría"), "spent": gasto, "average": media,
                      "change": cambio})
    filas.sort(key=lambda f: -f["spent"])
    tope = max([f["spent"] for f in filas] + [f["average"] or 0 for f in filas] + [1])
    for f in filas:
        f["bar"] = f["spent"] / tope
        f["avg_bar"] = (f["average"] or 0) / tope
    return filas


# ---------------------------------------------------------------- el cierre, en un mensaje

def close_message(conn, payday: date, sobrante: int, envelope: str | None, saved: bool) -> str:
    """El resumen del mes que acaba de cerrar, para mandarlo por Telegram al cobrar."""
    txs = repo.all_txs(conn)
    antes = payday - timedelta(days=1)
    year, month = repo.period_of(conn, antes)
    m = calc.month_summary(txs, year, month)
    lineas = [f"🧾 {month_name(year, month).capitalize()} cerrado",
              f"Cobraste {eur(m.income)} · gastaste {eur(m.payroll_spent)} · guardaste {eur(m.saved)}"]
    if sobrante > 0:
        lineas.append(f"Te sobraron {eur(sobrante)}" + (f" → «{envelope}». Haz la transferencia." if saved else "."))
    elif sobrante < 0:
        lineas.append(f"Llegaste al cobro con {eur(sobrante)}.")
    else:
        lineas.append("Llegaste justo al cobro.")
    tendencias = [f for f in category_trends(txs, year, month, repo.categories(conn))
                  if f["change"] is not None and f["change"] > 0.25 and f["spent"] >= 2000][:2]
    gastos = sorted(calc.spent_by_category(txs, year, month).items(), key=lambda kv: -kv[1])[:3]
    nombres = {c["id"]: c["name"] for c in repo.categories(conn)}
    if gastos:
        lineas.append("Lo que más: " + ", ".join(f"{nombres.get(k, '?')} {eur(v)}" for k, v in gastos))
    for f in tendencias:
        lineas.append(f"⚠️ {f['name']}: {f['change']:+.0%} sobre tu media")
    return "\n".join(lineas)



# ---------------------------------------------------------------- repartir la nómina

PLAN_VISIBLE_DAYS = 10             # cuántos días después de cobrar te recuerda repartir la nómina


def last_payday(conn, today: date) -> date | None:
    cobros = [d for d in paydays(conn) if d <= today]
    return cobros[-1] if cobros else None


def plan_due(conn, today: date) -> date | None:
    """El cobro que aún no has repartido (si fue hace pocos días). None si no toca."""
    cobro = last_payday(conn, today)
    if not cobro or (today - cobro).days > PLAN_VISIBLE_DAYS:
        return None
    hecho = repo.setting(conn, "plan_payday", "")
    return cobro if cobro.isoformat() > hecho else None


def payday_plan(conn, txs, today: date, held: int = 0) -> dict:
    """La pantalla de repartir la nómina: lo cobrado, lo que ya tiene dueño y cuánto meter en cada sobre.

    `base` es lo que queda para sobres y día a día: lo que hay (sin lo apartado ni lo retenido) menos
    los pagos fijos que faltan. Cada sobre viene con su aporte mensual pendiente ya puesto; lo que no
    metas en sobres es para el día a día.
    """
    year, month = repo.period_of(conn, today)
    f = forecast(conn, txs, year, month, today, held)
    cobro = last_payday(conn, today)
    cat = repo.payroll_category_id(conn)
    cobrado = sum(t.amount for t in txs if t.type == calc.INGRESO and t.category_id == cat
                  and cobro and cobro <= t.date <= today and t.ym == (year, month))
    pendiente_sobre = {u.envelope_id: u.pending for u in f.lines if u.kind == calc.APORTE_MENSUAL}
    fijos = sorted((u for u in f.lines if u.kind == calc.CUOTA), key=lambda u: (u.due or date.max, u.name))
    base = f.left + sum(pendiente_sobre.values())
    cuentas = {a["id"]: a["name"] for a in repo.accounts(conn)}
    defecto = repo.int_setting(conn, "envelope_account_id") or repo.int_setting(conn, "default_account_id")
    saldos = calc.envelope_balances(txs)
    sobres = [{"env": e, "suggested": pendiente_sobre.get(e["id"], 0), "monthly": e["monthly"],
               "account": cuentas.get(e["account_id"] or defecto),
               "st": envelope_status(e, saldos.get(e["id"], 0), today)}
              for e in repo.envelopes(conn, active=True)] if repo.modules(conn)["sobres"] else []
    presupuestado = budget(conn, txs, year, month)["total"] if repo.modules(conn)["presupuesto"] else None
    siguiente = calc.next_payday_estimate(repo.cycles(conn), today)
    return {
        "payday": cobro, "income": cobrado, "available": f.available, "held": f.held,
        "fixed": fijos, "fixed_pending": sum(u.pending for u in fijos), "base": base,
        "envelopes": sobres, "suggested_total": sum(s["suggested"] for s in sobres),
        "budget_left": presupuestado.left if presupuestado and presupuestado.budget else None,
        "next_payday": siguiente, "days": max(1, (siguiente - today).days) if siguiente else None,
        "done": bool(cobro) and repo.setting(conn, "plan_payday", "") >= cobro.isoformat(),
    }


def plan_concept(payday: date | None) -> str:
    return f"Reparto de la nómina ({payday.strftime('%d/%m/%Y')})" if payday else "Reparto de la nómina"


# ---------------------------------------------------------------- evolución de tu dinero

def money_history(conn, txs, today: date, start: date) -> dict | None:
    """Cuánto tenías cada día desde `start`: en todas tus cuentas (también las «aparte») y en sobres.

    Sale de los movimientos, no de lo que dijo el banco: así hay datos desde el primer día y cuadra
    con lo que ves en Cuentas. None si aún hay menos de una semana de historia.
    """
    if (today - start).days < 7:
        return None
    total = sum(a["initial_balance"] or 0 for a in repo.accounts(conn))
    sobres = 0
    orden = sorted(txs, key=lambda t: t.date)
    i, dias, cuentas, apartado = 0, [], [], []
    dia = start
    while dia <= today:
        while i < len(orden) and orden[i].date <= dia:
            t = orden[i]
            total += sum(calc.account_moves(t).values())
            sobres += calc.envelope_delta(t)
            i += 1
        dias.append(dia)
        cuentas.append(total)
        apartado.append(sobres)
        dia += timedelta(days=1)
    hace_30 = cuentas[-31] if len(cuentas) > 30 else cuentas[0]
    return {
        "labels": [d.strftime("%d/%m") for d in dias],
        "now": cuentas[-1], "envelopes": apartado[-1],
        "change_30": cuentas[-1] - hace_30, "days_30": min(30, len(cuentas) - 1),
        "change_all": cuentas[-1] - cuentas[0], "since": dias[0],
        "chart": {"type": "line", "labels": [d.strftime("%d/%m") for d in dias], "series": [
            {"name": "En tus cuentas", "slot": 1, "data": [c / 100 for c in cuentas]},
            {"name": "Apartado en sobres", "slot": 3, "data": [a / 100 for a in apartado]},
        ]},
    }


# ---------------------------------------------------------------- calendario del mes

def month_calendar(conn, txs, year: int, month: int, today: date) -> dict:
    """Los días del «mes» (de cobro a cobro, o el natural) con lo gastado y lo que toca cada día.

    Lo que toca: los pagos fijos (en su día) y el próximo cobro (estimado). Los aportes a sobres no
    tienen día: van aparte, debajo.
    """
    rango = period_range(conn, year, month)
    if rango:
        desde, hasta = rango["desde"], rango["hasta"]
    else:
        desde = date(year, month, 1)
        hasta = calc.add_months(desde, 1) - timedelta(days=1)
    lineas = _upcoming(conn, txs, year, month, today)
    eventos: dict[date, list[dict]] = {}
    for u in lineas:
        if u.kind == calc.CUOTA and u.due:
            eventos.setdefault(u.due, []).append({"kind": "pago", "name": u.name, "amount": u.expected,
                                                  "paid": u.paid, "line": u})
    nomina = repo.payroll_category_id(conn)
    cobro = calc.next_payday_estimate(repo.cycles(conn), today)
    if cobro and desde <= cobro <= hasta + timedelta(days=3):
        # lo que cobraste la última vez (si no, la nómina de referencia de Ajustes)
        ultima = next((t.amount for t in reversed(txs) if t.type == calc.INGRESO and nomina
                       and t.category_id == nomina), None)
        eventos.setdefault(cobro, []).append({"kind": "cobro", "name": "Cobro de la nómina (aprox.)",
                                              "amount": ultima or repo.salary(conn), "paid": False})
    for t in txs:
        if t.type == calc.INGRESO and nomina and t.category_id == nomina and desde <= t.date <= hasta:
            eventos.setdefault(t.date, []).append({"kind": "cobro", "name": "Cobraste la nómina", "amount": t.amount,
                                                   "paid": True})
    fin = max([hasta] + list(eventos))
    gasto: dict[date, int] = {}
    for t in txs:
        if t.type == calc.GASTO and desde <= t.date <= fin:
            gasto[t.date] = gasto.get(t.date, 0) + t.amount
    semanas, semana = [], [None] * desde.weekday()
    dia = desde
    while dia <= fin:
        semana.append({"date": dia, "spent": gasto.get(dia, 0), "events": eventos.get(dia, []),
                       "today": dia == today, "past": dia < today})
        if len(semana) == 7:
            semanas.append(semana)
            semana = []
        dia += timedelta(days=1)
    if semana:
        semanas.append(semana + [None] * (7 - len(semana)))
    agenda = sorted(((d, e) for d, es in eventos.items() for e in es), key=lambda de: de[0])
    mayor = max(gasto.values(), default=0)
    return {
        "weeks": semanas, "from": desde, "to": fin, "agenda": agenda, "max_spent": mayor,
        "spent_total": sum(gasto.values()),
        "upcoming": [(d, e) for d, e in agenda if d >= today and not e["paid"]],
        "envelopes": [u for u in lineas if u.kind == calc.APORTE_MENSUAL],
    }


# ---------------------------------------------------------------- el ritmo del mes, frente a otros meses

PACE_COMPARE_MONTHS = 3            # se compara con los tres meses anteriores (los que tengan datos)
PACE_MIN_DAY = 3                   # los dos primeros días no dicen nada
PACE_WARN_CENTS = 3000             # se avisa en «Para ti» si vas 30 € por encima…
PACE_WARN_PCT = 0.15               # …y un 15 % más que otros meses


def month_pace(conn, txs, today: date) -> dict | None:
    """Lo gastado en lo que va de mes frente a lo que llevabas otros meses el mismo día del mes.

    Solo el gasto del día a día: sin pagos fijos (ya están en lo previsto) ni lo pagado con sobres. Se
    compara cada mes desde su inicio (de cobro a cobro, si va así) y solo con meses de los que hay
    datos desde el principio. None si aún no hay con qué comparar.
    """
    year, month = repo.period_of(conn, today)
    ciclos = repo.cycles(conn)
    desde, _ = calc.period_range(year, month, ciclos)
    dia = (today - desde).days                 # 0 = el primer día del mes
    if dia + 1 < PACE_MIN_DAY or not txs:
        return None
    primero = min(t.date for t in txs)
    fijas = fixed_categories(conn, today)

    def gastado(inicio: date, fin: date) -> int:
        return sum(t.amount for t in txs if t.type == calc.GASTO and t.envelope_id is None
                   and t.category_id not in fijas and inicio <= t.date <= fin)

    previos = []
    for i in range(1, PACE_COMPARE_MONTHS + 1):
        y, m = add_months_ym(year, month, -i)
        inicio, fin = calc.period_range(y, m, ciclos)
        if inicio < primero or inicio >= desde or (fin and fin <= inicio):
            continue
        hasta = inicio + timedelta(days=dia)
        if fin:
            hasta = min(hasta, fin - timedelta(days=1))
        previos.append(gastado(inicio, hasta))
    if not previos:
        return None
    media = sum(previos) // len(previos)
    actual = gastado(desde, today)
    diff = actual - media
    return {"day": dia + 1, "spent": actual, "average": media, "diff": diff, "months": len(previos),
            "warn": diff >= max(PACE_WARN_CENTS, media * PACE_WARN_PCT) if media else False}


# ---------------------------------------------------------------- resumen del domingo

def week_spending(txs, desde: date, hasta: date) -> tuple[int, dict[int | None, int]]:
    total, por_cat = 0, {}
    for t in txs:
        if t.type == calc.GASTO and desde <= t.date <= hasta:
            total += t.amount
            por_cat[t.category_id] = por_cat.get(t.category_id, 0) + t.amount
    return total, por_cat


def weekly_message(conn, today: date, held: int = 0) -> str:
    """El resumen de la semana para Telegram: lo gastado, en qué, cómo vas y lo que viene."""
    txs = repo.all_txs(conn)
    desde = today - timedelta(days=6)
    total, por_cat = week_spending(txs, desde, today)
    anterior, _ = week_spending(txs, desde - timedelta(days=7), desde - timedelta(days=1))
    nombres = {c["id"]: c["name"] for c in repo.categories(conn)}
    lineas = [f"📅 Tu semana ({desde:%d/%m} – {today:%d/%m})"]
    if total:
        comparado = ""
        if anterior:
            cambio = total - anterior
            comparado = f" ({eur(abs(cambio))} {'más' if cambio > 0 else 'menos'} que la anterior)" if cambio else " (igual que la anterior)"
        lineas.append(f"Gastaste {eur(total)}{comparado}")
        top = sorted(por_cat.items(), key=lambda kv: -kv[1])[:3]
        lineas.append("Lo que más: " + ", ".join(f"{nombres.get(k, 'Sin categoría')} {eur(v)}" for k, v in top))
    else:
        lineas.append("No has gastado nada esta semana 👏")
    year, month = repo.period_of(conn, today)
    previsto = forecast(conn, txs, year, month, today, held)
    cobro = until_payday(conn, previsto.left, today)
    if previsto.left >= 0 and cobro:
        lineas.append(f"Puedes gastar {eur(previsto.left)}: {eur(cobro['per_day'])} al día hasta el cobro (~{cobro['date']:%d/%m})")
    else:
        lineas.append(f"Puedes gastar {eur(previsto.left)}")
    ritmo = month_pace(conn, txs, today)
    if ritmo and ritmo["average"] and abs(ritmo["diff"]) >= 1000:
        lineas.append(f"{'⚠️ ' if ritmo['warn'] else ''}Este mes llevas {eur(abs(ritmo['diff']))} "
                      f"{'más' if ritmo['diff'] > 0 else 'menos'} que otros meses a estas alturas")
    if repo.modules(conn)["presupuesto"]:
        for b in budget(conn, txs, year, month)["alerts"]:
            lineas.append(f"• {b['cat']['name']}: {eur(b['line'].spent)} de {eur(b['line'].budget)}")
    proximos = [u for u in previsto.lines if u.kind == calc.CUOTA and not u.paid and u.due
                and today < u.due <= today + timedelta(days=7)]
    if proximos:
        lineas.append("Esta semana se cargan: " + ", ".join(f"{u.name} {eur(u.pending)} ({u.due:%d/%m})" for u in proximos))
    return "\n".join(lineas)


WEEKLY_WEEKDAY = 6                 # domingo
WEEKLY_HOUR = 20                   # a partir de las 20:00


def weekly_due(conn, now) -> bool:
    """¿Toca mandar el resumen de la semana? Los domingos desde las 20:00, una vez."""
    if repo.setting(conn, "alert_weekly", "1") != "1":
        return False
    if now.weekday() != WEEKLY_WEEKDAY or now.hour < WEEKLY_HOUR:
        return False
    return repo.setting(conn, "last_weekly", "") != now.date().isoformat()


# ---------------------------------------------------------------- subidas de precio y cobros dobles

def _charges(conn, today: date) -> list[tuple]:
    """Los gastos, con la clave de su comercio (el concepto limpio y normalizado), para calc.price_changes."""
    from .clasificar import normalize
    from .nombres import limpio
    desde = (today - timedelta(days=calc.PRICE_LOOKBACK_DAYS)).isoformat()
    filas = conn.execute("SELECT date, concept, amount, category_id FROM transactions WHERE type = ? AND date >= ?",
                         (calc.GASTO, desde)).fetchall()
    cargos = []
    for f in filas:
        nombre = limpio(f["concept"])
        clave = normalize(nombre)
        if clave:
            cargos.append((date.fromisoformat(f["date"]), clave, nombre, f["amount"], f["category_id"]))
    return cargos


def dismissed_changes(conn) -> list[str]:
    try:
        return list(json.loads(repo.setting(conn, "price_dismissed") or "[]"))
    except ValueError:
        return []


def price_alerts(conn, today: date, include_dismissed: bool = False) -> list[calc.PriceChange]:
    """Lo que ha subido de precio o se ha cobrado dos veces (sin lo que ya has visto y descartado)."""
    cambios = calc.price_changes(_charges(conn, today), today)
    if include_dismissed:
        return cambios
    vistos = set(dismissed_changes(conn))
    return [c for c in cambios if c.id not in vistos]


def dismiss_change(conn, change_id: str) -> None:
    vistos = [v for v in dismissed_changes(conn) if v != change_id] + [change_id]
    repo.set_setting(conn, "price_dismissed", json.dumps(vistos[-100:]))


def change_text(c: calc.PriceChange) -> str:
    if c.kind == "doble":
        return f"{c.name}: te han cobrado dos veces {eur(c.now)}"
    return f"{c.name} ha subido {eur(c.diff)}: ahora {eur(c.now)} (antes {eur(c.before)})"


# ---------------------------------------------------------------- patrimonio

def _owed_at(owed_rows, day: date) -> int:
    total = 0
    for o in owed_rows:
        if o["date"] <= day.isoformat():
            cobrado = o["settled_on"] or o["settled_date"]
            if not cobrado or cobrado > day.isoformat():
                total += o["amount"]
    return total


def _loans_pending_at(loans, day: date) -> int:
    """Lo que te queda por pagar de tus préstamos ese día (lo que no tiene fin no es deuda)."""
    total = 0
    for l in loans:
        if l["kind"] == "prestamo" and l["n_installments"]:
            total += calc.loan_status(l["installment"], date.fromisoformat(l["first_date"]),
                                      l["n_installments"], day).pending_total or 0
    return total


def net_worth(conn, txs, today: date) -> dict:
    """Todo lo tuyo menos lo que debes: las cuentas (con los sobres, que viven en ellas), lo que te deben
    y lo que te queda por pagar de los préstamos. Y cómo ha ido al final de cada mes."""
    cuentas = repo.accounts(conn)
    owed_rows = repo.owed(conn)
    loans = repo.loans(conn)
    saldos = calc.account_balances(txs, cuentas)
    en_cuentas = sum(saldos.values())
    te_deben = _owed_at(owed_rows, today)
    debes = _loans_pending_at(loans, today)
    puntos = []
    if txs:
        inicial = sum(a["initial_balance"] or 0 for a in cuentas)
        primero = min(t.date for t in txs)
        dia = calc.add_months(date(primero.year, primero.month, 1), 1) - timedelta(days=1)
        fechas = []
        while dia < today:
            fechas.append(dia)
            dia = calc.add_months(dia.replace(day=1), 2) - timedelta(days=1)
        fechas.append(today)
        orden = sorted(txs, key=lambda t: t.date)
        i, acumulado = 0, inicial
        for f in fechas:
            while i < len(orden) and orden[i].date <= f:
                acumulado += sum(calc.account_moves(orden[i]).values())
                i += 1
            puntos.append((f, acumulado + _owed_at(owed_rows, f) - _loans_pending_at(loans, f)))
    total = en_cuentas + te_deben - debes
    cambio = total - puntos[-2][1] if len(puntos) >= 2 else None
    return {
        "accounts": en_cuentas, "owed": te_deben, "debt": debes, "total": total,
        "points": puntos, "change_month": cambio,
        "chart": {"type": "line", "points": True,
                  "labels": [("hoy" if f == today else month_short(f.year, f.month)) for f, _ in puntos],
                  "series": [{"name": "Patrimonio", "slot": 1, "data": [v / 100 for _, v in puntos]}]}
                 if len(puntos) >= 2 else None,
    }


# ---------------------------------------------------------------- cuántos meses aguantas

RUNWAY_DAYS = 90                   # la media de gasto, con los últimos tres meses
RUNWAY_MIN_DAYS = 21               # con menos historia no se calcula


def runway(conn, txs, today: date) -> calc.Runway | None:
    """Si dejaras de cobrar, cuántos meses tirarías con lo que tienes (todas tus cuentas, sobres incluidos).

    El gasto de cada mes es la media de los últimos tres meses, sin lo pagado desde sobres: eso son
    gastos puntuales (la entrada del coche, un viaje) que ya tenían su dinero apartado.
    """
    gastos = [t for t in txs if t.type == calc.GASTO and t.envelope_id is None and t.date <= today]
    if not txs:
        return None
    dias = min(RUNWAY_DAYS, (today - min(t.date for t in txs)).days + 1)
    if dias < RUNWAY_MIN_DAYS:
        return None
    desde = today - timedelta(days=dias - 1)
    gastado = sum(t.amount for t in gastos if t.date >= desde)
    al_mes = round(gastado / dias * 365 / 12)
    cuentas = repo.accounts(conn, active=True)
    saldos = calc.account_balances(txs, repo.accounts(conn))
    fijos = sum(l["installment"] for l in repo.loans(conn) if not calc.loan_status(
        l["installment"], date.fromisoformat(l["first_date"]), l["n_installments"], today).finished)
    return calc.Runway(sum(saldos.get(a["id"], 0) for a in cuentas), al_mes, fijos, round(dias / 30.4))


# ---------------------------------------------------------------- tu año

def year_review(conn, txs, year: int, today: date | None = None) -> dict | None:
    """El año en números: lo cobrado, gastado y guardado, el mejor mes, en qué y dónde se fue más.

    El mes en curso cuenta en los totales, pero no compite por «mejor mes» (acaba de empezar: aún no
    se ha gastado casi nada)."""
    from .nombres import limpio
    meses = [calc.month_summary(txs, year, m) for m in range(1, 13)]
    con_datos = [m for m in meses if m.income or m.payroll_spent or m.saved or m.envelope_spent]
    if not con_datos:
        return None
    gastos = [t for t in txs if t.type == calc.GASTO and t.ym[0] == year]
    por_cat: dict[int | None, int] = {}
    por_dia: dict[date, int] = {}
    for t in gastos:
        por_cat[t.category_id] = por_cat.get(t.category_id, 0) + t.amount
        por_dia[t.date] = por_dia.get(t.date, 0) + t.amount
    nombres = {c["id"]: c["name"] for c in repo.categories(conn)}
    filas = conn.execute("SELECT date, concept, amount, month, category_id FROM transactions WHERE type = ?",
                         (calc.GASTO,)).fetchall()
    ciclos = repo.cycles(conn)
    comercios: dict[str, list] = {}
    mayor = None
    for f in filas:
        mes = f["month"] or repo.period_key(*calc.period_of(date.fromisoformat(f["date"]), ciclos))
        if int(mes[:4]) != year:
            continue
        nombre = limpio(f["concept"])
        if nombre:
            c = comercios.setdefault(nombre.lower(), [nombre, 0, 0])
            c[1] += f["amount"]
            c[2] += 1
        if not mayor or f["amount"] > mayor["amount"]:
            mayor = {"amount": f["amount"], "name": nombre or nombres.get(f["category_id"], "Sin concepto"),
                     "date": date.fromisoformat(f["date"])}
    total = calc.MonthSummary(year, 0, sum(m.income for m in meses), sum(m.payroll_spent for m in meses),
                              sum(m.saved for m in meses), sum(m.envelope_spent for m in meses))
    tope_cat = max(por_cat.values(), default=1)
    en_curso = repo.period_of(conn, today) if today else None
    cerrados = [m for m in con_datos if (m.year, m.month) != en_curso] or con_datos
    dia_mas = max(por_dia.items(), key=lambda kv: kv[1]) if por_dia else None
    return {
        "year": year, "months": con_datos, "n_months": len(con_datos), "total": total,
        "spent_all": sum(t.amount for t in gastos),
        "best": max(cerrados, key=lambda m: m.free), "most_spent": max(cerrados, key=lambda m: m.payroll_spent),
        "n_closed": len(cerrados),
        "categories": [{"name": nombres.get(k, "Sin categoría"), "spent": v, "bar": v / tope_cat}
                       for k, v in sorted(por_cat.items(), key=lambda kv: -kv[1])[:6]],
        "merchants": [{"name": n, "spent": s, "count": c} for n, s, c in
                      sorted(comercios.values(), key=lambda c: -c[1])[:5]],
        "most_visited": max(comercios.values(), key=lambda c: c[2]) if comercios else None,
        "biggest": mayor, "top_day": dia_mas,
        "n_txs": sum(1 for t in txs if t.ym[0] == year),
        "per_day": sum(t.amount for t in gastos) // max(1, len({d for d in por_dia})) if por_dia else 0,
    }


YEAR_VISIBLE = ((12, 15), (1, 31))    # «Tu año» sale en «Para ti» del 15 de diciembre al 31 de enero


def year_review_due(today: date) -> int | None:
    """El año que toca enseñar en «Para ti», si estamos en esas fechas."""
    if (today.month, today.day) >= YEAR_VISIBLE[0]:
        return today.year
    if (today.month, today.day) <= YEAR_VISIBLE[1]:
        return today.year - 1
    return None


# ---------------------------------------------------------------- dividir un gasto

def split_amount(total: int, people: int, me_too: bool) -> int:
    """Lo que te debe cada uno: el total entre todos (tú incluido, si pagabas tu parte).
    El céntimo que no se puede repartir te lo comes tú."""
    partes = people + (1 if me_too else 0)
    return total // partes if partes else 0
