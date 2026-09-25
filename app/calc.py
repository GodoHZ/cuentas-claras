"""Reglas de cálculo: las mismas que la hoja de Google (sección 4 del plan).

Funciones puras, sin base de datos: reciben movimientos y devuelven cifras.
Todos los importes van en céntimos.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .money import eur

INGRESO = "ingreso"
GASTO = "gasto"
APORTE = "aporte_sobre"
RETIRO = "retiro_sobre"
TRASPASO = "traspaso"
TYPES = {
    GASTO: "Gasto",
    INGRESO: "Ingreso",
    APORTE: "Aporte a sobre",
    RETIRO: "Retiro de sobre",
    TRASPASO: "Traspaso entre cuentas",
}
NEEDS_ENVELOPE = (APORTE, RETIRO)
NEEDS_CATEGORY = (INGRESO, GASTO)
MOVE_MONEY = (APORTE, RETIRO, TRASPASO)        # los que pueden cambiar el dinero de cuenta

# Valores de fábrica; todos se pueden cambiar en Ajustes → Reglas y avisos.
INSTALLMENTS_LIMIT = 0.35          # cuotas / nómina: en rojo por encima
BUDGET_WARN = 0.80                 # presupuesto: amarillo desde el 80 %
SMALL_SURPLUS = 2000               # cuadre: hasta 20 € de más suele ser intereses


@dataclass(frozen=True)
class Tx:
    date: date
    type: str
    amount: int                    # céntimos, siempre > 0
    category_id: int | None = None
    envelope_id: int | None = None
    account_id: int | None = None
    other_account_id: int | None = None       # la otra cuenta cuando el dinero cambia de sitio


def validate_tx(type_: str | None, amount: int | None, category_id: int | None,
                envelope_id: int | None, account_id: int | None = 1,
                other_account_id: int | None = None) -> list[str]:
    errors = []
    if type_ not in TYPES:
        errors.append("Elige el tipo de movimiento.")
    if amount is None:
        errors.append("Escribe el importe.")
    elif amount <= 0:
        errors.append("El importe tiene que ser mayor que 0.")
    if type_ in NEEDS_ENVELOPE and not envelope_id:
        errors.append("Elige el sobre.")
    if type_ in NEEDS_CATEGORY and not category_id:
        errors.append("Elige una categoría.")
    if type_ == TRASPASO:
        if not other_account_id:
            errors.append("Elige de qué cuenta sale el dinero.")
        elif other_account_id == account_id:
            errors.append("El dinero tiene que ir a una cuenta distinta.")
    elif type_ in NEEDS_ENVELOPE and other_account_id and other_account_id == account_id:
        errors.append("La cuenta del sobre y la otra cuenta no pueden ser la misma.")
    return errors


# ---------------------------------------------------------------- cuentas

def account_moves(tx: Tx) -> dict[int, int]:
    """Cuánto sube o baja cada cuenta con este movimiento.

    Un aporte o un retiro solo mueven dinero si dices de qué otra cuenta sale o
    a cuál va; si no, son solo una etiqueta (el dinero ya estaba donde toca).
    """
    if tx.account_id is None:
        return {}
    if tx.type == INGRESO:
        return {tx.account_id: tx.amount}
    if tx.type == GASTO:
        return {tx.account_id: -tx.amount}
    if tx.type == TRASPASO and tx.other_account_id:
        return {tx.other_account_id: -tx.amount, tx.account_id: tx.amount}
    if tx.type == APORTE and tx.other_account_id:          # sale de la otra, entra en la del sobre
        return {tx.other_account_id: -tx.amount, tx.account_id: tx.amount}
    if tx.type == RETIRO and tx.other_account_id:          # sale de la del sobre, entra en la otra
        return {tx.account_id: -tx.amount, tx.other_account_id: tx.amount}
    return {}


def account_balance_at(txs, account_id: int, initial: int, until: date) -> int:
    """Saldo de una cuenta contando solo lo apuntado hasta esa fecha (incluida)."""
    saldo = initial
    for tx in txs:
        if tx.date <= until:
            saldo += account_moves(tx).get(account_id, 0)
    return saldo


def account_balances(txs, accounts) -> dict[int, int]:
    """Saldo de cada cuenta: lo que había al empezar más todo lo que ha pasado."""
    saldos = {a["id"]: (a["initial_balance"] or 0) for a in accounts}
    for tx in txs:
        for cuenta, delta in account_moves(tx).items():
            if cuenta in saldos:
                saldos[cuenta] += delta
    return saldos


# ---------------------------------------------------------------- sobres

def envelope_delta(tx: Tx) -> int:
    """Cuánto mueve un movimiento el saldo de su sobre."""
    if tx.envelope_id is None:
        return 0
    if tx.type == APORTE:
        return tx.amount
    if tx.type in (GASTO, RETIRO):
        return -tx.amount
    return 0                       # un ingreso con sobre no cuenta (como en la hoja)


def envelope_balances(txs) -> dict[int, int]:
    """Saldo de cada sobre = Σ aportes − Σ gastos con ese sobre − Σ retiros."""
    balances: dict[int, int] = {}
    for tx in txs:
        if tx.envelope_id is not None:
            balances[tx.envelope_id] = balances.get(tx.envelope_id, 0) + envelope_delta(tx)
    return balances


@dataclass(frozen=True)
class EnvelopeStatus:
    balance: int
    target: int | None
    monthly: int | None
    progress: float | None         # 0..1
    missing: int | None            # lo que falta para el objetivo
    months: int | None             # meses para llegar al objetivo
    label: str                     # "3 meses", "¡Conseguido!", "Sin aporte", "Sin objetivo"

    @property
    def done(self) -> bool:
        return self.missing == 0


def envelope_status(balance: int, target: int | None, monthly: int | None) -> EnvelopeStatus:
    target = target or None
    monthly = monthly or None
    if not target:
        return EnvelopeStatus(balance, None, monthly, None, None, None, "Sin objetivo")
    progress = min(1.0, max(0.0, balance / target))
    missing = max(0, target - balance)
    months = None
    if missing == 0:
        label = "¡Conseguido!"
    elif not monthly:
        label = "Sin aporte"
    else:
        months = -(-missing // monthly)          # redondeo hacia arriba, en enteros
        label = "1 mes" if months == 1 else f"{months} meses"
    return EnvelopeStatus(balance, target, monthly, progress, missing, months, label)


# ---------------------------------------------------------------- mes

@dataclass(frozen=True)
class MonthSummary:
    year: int
    month: int
    income: int = 0                # ingresos
    payroll_spent: int = 0         # gastos de la nómina (gastos SIN sobre)
    saved: int = 0                 # aportado a sobres
    envelope_spent: int = 0        # gastado desde sobres (solo informativo)

    @property
    def free(self) -> int:
        """Libre = Ingresos − Gastos de la nómina − Aportado."""
        return self.income - self.payroll_spent - self.saved

    @property
    def saved_ratio(self) -> float | None:
        """% ahorrado = Aportado / Ingresos."""
        return self.saved / self.income if self.income else None


def in_month(tx: Tx, year: int, month: int) -> bool:
    return tx.date.year == year and tx.date.month == month


def month_summary(txs, year: int, month: int) -> MonthSummary:
    """Un traspaso entre cuentas no es ni gasto ni ingreso: no aparece aquí."""
    income = payroll = saved = from_env = 0
    for tx in txs:
        if not in_month(tx, year, month):
            continue
        if tx.type == INGRESO:
            income += tx.amount
        elif tx.type == APORTE:
            saved += tx.amount
        elif tx.type == GASTO:
            if tx.envelope_id is None:
                payroll += tx.amount
            else:
                from_env += tx.amount
    return MonthSummary(year, month, income, payroll, saved, from_env)


# ---------------------------------------------------------------- presupuesto

def spent_by_category(txs, year: int, month: int) -> dict[int, int]:
    """Gastos del mes por categoría, INCLUIDOS los pagados desde un sobre."""
    spent: dict[int, int] = {}
    for tx in txs:
        if tx.type == GASTO and tx.category_id is not None and in_month(tx, year, month):
            spent[tx.category_id] = spent.get(tx.category_id, 0) + tx.amount
    return spent


def budget_level(ratio: float | None, warn: float = BUDGET_WARN) -> str:
    """'sin' (sin presupuesto), 'ok' (por debajo del aviso), 'aviso', 'pasado' (> 100 %)."""
    if ratio is None:
        return "sin"
    if ratio < warn:
        return "ok"
    if ratio <= 1:
        return "aviso"
    return "pasado"


@dataclass(frozen=True)
class BudgetLine:
    budget: int
    spent: int
    left: int                      # queda = presupuesto − gastado
    ratio: float | None            # % usado
    level: str


def budget_line(budget: int | None, spent: int, warn: float = BUDGET_WARN) -> BudgetLine:
    budget = budget or 0
    ratio = spent / budget if budget > 0 else None
    return BudgetLine(budget, spent, budget - spent, ratio, budget_level(ratio, warn))


# ---------------------------------------------------------------- deudas

def add_months(d: date, n: int) -> date:
    """Como FECHA.MES de la hoja: si el día no existe, el último del mes."""
    idx = d.year * 12 + (d.month - 1) + n
    year, month = idx // 12, idx % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def full_months(start: date, end: date) -> int:
    """Meses completos entre dos fechas (como SIFECHA(...;"M"))."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(0, months)


@dataclass(frozen=True)
class LoanStatus:
    installment: int
    first: date
    count: int
    last: date                     # última cuota
    paid: int                      # cuotas pagadas
    pending: int                   # cuotas pendientes
    pending_total: int             # total pendiente

    @property
    def finished(self) -> bool:
        return self.pending == 0


def loan_status(installment: int, first: date, count: int, today: date) -> LoanStatus:
    last = add_months(first, count - 1)
    paid = 0 if today < first else min(count, full_months(first, today) + 1)
    pending = count - paid
    return LoanStatus(installment, first, count, last, paid, pending, pending * installment)


def installments_ratio(statuses, salary: int | None) -> float | None:
    """Cuotas / nómina: suma de las cuotas de los préstamos que siguen vivos."""
    if not salary:
        return None
    return sum(s.installment for s in statuses if not s.finished) / salary


# ---------------------------------------------------------------- cuadre

@dataclass(frozen=True)
class Reconciliation:
    real: int                      # saldo real de la cuenta, el que dice el banco
    calculated: int                # saldo que calcula la app
    difference: int
    level: str                     # 'ok' | 'sobra' | 'falta'
    message: str


def reconcile(real: int, calculated: int, small: int = SMALL_SURPLUS) -> Reconciliation:
    """Compara el saldo que dice el banco con el que calcula la app."""
    diff = real - calculated
    if diff == 0:
        return Reconciliation(real, calculated, 0, "ok", "Cuadra con el banco")
    if diff > 0:
        if diff <= small:
            msg = f"En el banco hay {eur(diff)} más (¿intereses?)"
        else:
            msg = f"En el banco hay {eur(diff)} más: algún ingreso sin apuntar"
        return Reconciliation(real, calculated, diff, "sobra", msg)
    return Reconciliation(real, calculated, diff, "falta",
                          f"En el banco hay {eur(-diff)} menos: algún gasto sin apuntar")
