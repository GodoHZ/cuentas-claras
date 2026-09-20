"""Cifras ya calculadas para cada pantalla (junta repo.py y calc.py)."""
from __future__ import annotations

import calendar
from datetime import date

from . import calc, repo
from .money import add_months_ym, eur, month_name


def envelopes_overview(conn, txs, include_archived: bool = False):
    """Tarjetas de sobres y total en sobres (el total incluye los archivados)."""
    balances = calc.envelope_balances(txs)
    cards = []
    for env in repo.envelopes(conn):
        balance = balances.get(env["id"], 0)
        if env["active"] or (include_archived and balance):
            cards.append({"env": env, "st": calc.envelope_status(balance, env["target"], env["monthly"])})
    return cards, sum(balances.values())


def sweep_settings(conn) -> dict:
    """Ajustes de «las sobras del mes van al Colchón»."""
    envelope_id = repo.int_setting(conn, "sweep_envelope_id")
    envelope = repo.get_row(conn, "envelopes", envelope_id) if envelope_id else None
    return {
        "enabled": repo.setting(conn, "sweep_enabled", "1") == "1" and envelope is not None,
        "envelope": envelope,
        "day": min(max(repo.int_setting(conn, "sweep_day") or 1, 1), 28),
        "done_until": repo.setting(conn, "last_sweep_month", ""),
    }


def sweep_concept(year: int, month: int) -> str:
    return f"Sobras de {month_name(year, month)}"


def pending_sweep(conn, txs, today: date):
    """Mes cerrado cuyas sobras todavía no se han guardado. None si no hay nada que hacer."""
    cfg = sweep_settings(conn)
    if not cfg["envelope"]:
        return None
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
    return {"year": year, "month": month, "amount": left, "envelope": cfg["envelope"],
            "day": cfg["day"], "enabled": cfg["enabled"],
            "due": today.day >= cfg["day"] and cfg["enabled"]}


def do_sweep(conn, pending: dict) -> int:
    """Guarda las sobras del mes en el sobre elegido. Devuelve el id del movimiento."""
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


def reconciliation(conn, total_in_envelopes: int):
    last = repo.last_check(conn)
    if not last:
        return None
    return {
        "check": last,
        "r": calc.reconcile(last["balance"], total_in_envelopes,
                            repo.rule(conn, "reconcile_small_cents")),
        "moves_after": repo.envelope_moves_after(conn, last["date"]),
    }


def debts(conn, txs, today: date):
    items = []
    for loan in repo.loans(conn):
        st = calc.loan_status(loan["installment"], date.fromisoformat(loan["first_date"]),
                              loan["n_installments"], today)
        items.append({"loan": loan, "st": st})
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
        "pending_total": sum(s.pending_total for s in alive),
        "extra": extra,
        "amort_category": repo.get_row(conn, "categories", amort_id) if amort_id else None,
    }


def budget(conn, txs, year: int, month: int):
    warn = repo.rule(conn, "budget_warn_pct") / 100
    spent = calc.spent_by_category(txs, year, month)
    budgeted, unbudgeted = [], []
    for cat in repo.categories(conn):
        s = spent.get(cat["id"], 0)
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
        "fecha": today.isoformat(), "concepto": "", "errors": [],
    }


def saved_message(type_: str, amount: int, category: str | None, envelope: str | None) -> str:
    what = {calc.GASTO: "Gasto", calc.INGRESO: "Ingreso", calc.APORTE: "Aporte", calc.RETIRO: "Retiro"}[type_]
    where = envelope if type_ in calc.NEEDS_ENVELOPE else category
    if type_ == calc.GASTO and envelope:
        where = f"{category} (desde {envelope})"
    return f"{what} de {eur(amount)} · {where}"
