"""Reglas de cálculo: las mismas que la hoja de Google (sección 4 del plan).

Funciones puras, sin base de datos: reciben movimientos y devuelven cifras.
Todos los importes van en céntimos.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .money import eur
from .nombres import limpio

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
    month: str | None = None                  # el mes al que cuenta ('2026-11'), si no es el de su fecha

    @property
    def ym(self) -> tuple[int, int]:
        """El mes al que cuenta: el suyo si lo tiene, si no el de su fecha."""
        if self.month:
            return int(self.month[:4]), int(self.month[5:7])
        return self.date.year, self.date.month


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
    deadline: tuple[int, int] | None = None   # para cuándo quieres llegar (año, mes)
    months_left: int | None = None            # meses que quedan hasta esa fecha (contando el actual)
    needed: int | None = None                 # lo que tendrías que guardar al mes para llegar a tiempo
    eta: tuple[int, int] | None = None        # cuándo llegas guardando lo que guardas ahora

    @property
    def done(self) -> bool:
        return self.missing == 0

    @property
    def on_track(self) -> bool | None:
        """¿Llegas a tiempo guardando lo de cada mes? None si no hay fecha o ya está conseguido."""
        if self.needed is None:
            return None
        return bool(self.monthly) and self.monthly >= self.needed


def parse_ym(text: str | None) -> tuple[int, int] | None:
    """'2027-07' -> (2027, 7); cualquier otra cosa, None."""
    try:
        year, month = int(str(text)[:4]), int(str(text)[5:7])
    except (TypeError, ValueError):
        return None
    return (year, month) if len(str(text)) == 7 and 1 <= month <= 12 and 2000 <= year <= 2200 else None


def envelope_status(balance: int, target: int | None, monthly: int | None,
                    target_date: str | None = None, today: date | None = None) -> EnvelopeStatus:
    """Cómo va un sobre. Con fecha («1.200 € para julio»), cuánto hay que guardar al mes para llegar:
    lo que falta repartido entre los meses que quedan, este incluido."""
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
    deadline = parse_ym(target_date)
    months_left = needed = eta = None
    hoy = today or date.today()
    if months:
        idx = hoy.year * 12 + hoy.month - 1 + months - 1
        eta = (idx // 12, idx % 12 + 1)
    if deadline and missing:
        months_left = max(1, (deadline[0] * 12 + deadline[1]) - (hoy.year * 12 + hoy.month) + 1)
        needed = -(-missing // months_left)
    return EnvelopeStatus(balance, target, monthly, progress, missing, months, label,
                          deadline, months_left, needed, eta)


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
    """Si cuenta para ese mes (el suyo, que no siempre es el de la fecha: ver Tx.month)."""
    return tx.ym == (year, month)


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


# ---------------------------------------------------------------- de nómina a nómina

MIN_CYCLE_DAYS = 20                # una nómina a menos de 20 días de la anterior no abre otro mes
LABEL_OFFSET_DAYS = 15             # el mes se llama como el que cubre casi entero


@dataclass(frozen=True)
class Cycle:
    """Un «mes» que va de un cobro de la nómina al siguiente.

    Se llama como el mes natural que cubre casi entero: del 28/10 al 27/11 es noviembre.
    """
    label: tuple[int, int]
    start: date
    end: date | None               # el día del siguiente cobro (no incluido); None = el mes en curso


def payday_starts(payroll_dates) -> list[date]:
    """Los días en que empieza un mes: cada cobro de nómina, sin las que llegan pegadas a la anterior."""
    starts: list[date] = []
    for day in sorted(set(payroll_dates)):
        if not starts or (day - starts[-1]).days >= MIN_CYCLE_DAYS:
            starts.append(day)
    return starts


def cycles(starts: list[date]) -> list[Cycle]:
    resultado: list[Cycle] = []
    for i, start in enumerate(starts):
        mitad = start.toordinal() + LABEL_OFFSET_DAYS
        label = (date.fromordinal(mitad).year, date.fromordinal(mitad).month)
        if resultado and label <= resultado[-1].label:           # dos cobros en el mismo mes natural
            y, m = resultado[-1].label
            label = (y + m // 12, m % 12 + 1)
        resultado.append(Cycle(label, start, starts[i + 1] if i + 1 < len(starts) else None))
    return resultado


def period_of(day: date, cycles_: list[Cycle]) -> tuple[int, int]:
    """El «mes» de un día: el del cobro de nómina en que cae, o el mes natural si aún no hay cobros."""
    actual = None
    for c in cycles_:
        if c.start <= day:
            actual = c
        else:
            break
    return actual.label if actual else (day.year, day.month)


def period_range(year: int, month: int, cycles_: list[Cycle]) -> tuple[date, date | None]:
    """Del día a (incluido) al día b (sin incluir) de un «mes». b = None: sigue abierto."""
    for c in cycles_:
        if c.label == (year, month):
            return c.start, c.end
    inicio = date(year, month, 1)
    fin = add_months(inicio, 1)
    if cycles_ and fin > cycles_[0].start:                       # antes del primer cobro: el mes natural, recortado
        fin = cycles_[0].start
    if cycles_ and inicio >= cycles_[0].start:                   # un mes que no existe (no hubo cobro)
        return inicio, inicio
    return inicio, fin


def next_payday_estimate(cycles_: list[Cycle], today: date) -> date | None:
    """Cuándo cobrarás, más o menos: un mes después del último cobro (y nunca antes de mañana)."""
    if not cycles_:
        return None
    estimado = add_months(cycles_[-1].start, 1)
    return max(estimado, date.fromordinal(today.toordinal() + 1))


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
    count: int | None              # None: sin fin
    last: date | None              # última cuota
    paid: int                      # cuotas pagadas
    pending: int | None            # cuotas pendientes
    pending_total: int | None      # total pendiente

    @property
    def finished(self) -> bool:
        return self.pending == 0          # sin fin (pending None), nunca


def loan_status(installment: int, first: date, count: int | None, today: date) -> LoanStatus:
    """Cómo va un pago fijo. Sin número de cuotas (una suscripción), no termina nunca."""
    paid = 0 if today < first else full_months(first, today) + 1
    if count is None:
        return LoanStatus(installment, first, None, None, paid, None, None)
    last = add_months(first, count - 1)
    paid = min(count, paid)
    pending = count - paid
    return LoanStatus(installment, first, count, last, paid, pending, pending * installment)


def next_installment(first: date, count: int | None, today: date) -> date | None:
    """El próximo cargo de un pago fijo, hoy incluido (None si ya terminó)."""
    index = max(0, (today.year * 12 + today.month) - (first.year * 12 + first.month))
    dia = add_months(first, index)
    if dia < today:
        index += 1
        dia = add_months(first, index)
    return dia if count is None or index < count else None


def installments_ratio(statuses, salary: int | None) -> float | None:
    """Cuotas / nómina: suma de las cuotas de los préstamos que siguen vivos."""
    if not salary:
        return None
    return sum(s.installment for s in statuses if not s.finished) / salary


# ---------------------------------------------------------------- previsto

CUOTA = "cuota"                    # la cuota de un préstamo
APORTE_MENSUAL = "aporte"          # el aporte mensual de un sobre


def installment_in_month(first: date, count: int | None, year: int, month: int) -> date | None:
    """Fecha de la cuota que toca ese mes, o None si ese mes no hay ninguna (sin fin: todos)."""
    index = (year * 12 + month - 1) - (first.year * 12 + first.month - 1)
    return add_months(first, index) if 0 <= index and (count is None or index < count) else None


def contributed_by_envelope(txs, year: int, month: int) -> dict[int, int]:
    """Lo aportado a cada sobre durante el mes (los retiros y gastos no cuentan)."""
    aportado: dict[int, int] = {}
    for tx in txs:
        if tx.type == APORTE and tx.envelope_id is not None and in_month(tx, year, month):
            aportado[tx.envelope_id] = aportado.get(tx.envelope_id, 0) + tx.amount
    return aportado


@dataclass(frozen=True)
class Upcoming:
    """Algo que ya sabes que va a salir este mes, apuntado o no."""
    kind: str                      # 'cuota' | 'aporte'
    name: str
    expected: int                  # el importe que toca este mes
    done: int                      # cuánto de eso has apuntado ya
    day: int | None = None         # día en que se carga, si se sabe
    category_id: int | None = None
    envelope_id: int | None = None
    next_month: bool = False       # es del mes que viene (ya cobrado: ver views.forecast)
    due: date | None = None        # el día exacto en que se carga, si se sabe

    @property
    def pending(self) -> int:
        """Lo que queda por apuntar (nunca negativo: pagar de más no da crédito)."""
        return max(0, self.expected - self.done)

    @property
    def paid(self) -> bool:
        return self.pending == 0

    @property
    def partial(self) -> bool:
        return 0 < self.done < self.expected


@dataclass(frozen=True)
class Forecast:
    """Lo que puedes gastar: el dinero que tienes menos lo que ya tiene dueño.

    Se parte de lo que hay **en las cuentas** (sin lo apartado en sobres), no de
    la nómina del mes: el dinero que tienes es el dinero que tienes.
    """
    available: int                 # en las cuentas y sin apartar en sobres
    lines: tuple[Upcoming, ...] = ()
    held: int = 0                  # lo que el banco tiene retenido (pagado, pero aún no contabilizado)

    @property
    def pending(self) -> list[Upcoming]:
        return [u for u in self.lines if not u.paid]

    @property
    def pending_total(self) -> int:
        return sum(u.pending for u in self.lines)

    @property
    def pending_next(self) -> int:
        """Lo pendiente del mes que viene (solo si ya has cobrado su nómina)."""
        return sum(u.pending for u in self.lines if u.next_month)

    @property
    def left(self) -> int:
        """Lo que te quedará cuando hayas pagado y guardado lo que falta del mes."""
        return self.available - self.held - self.pending_total

    @property
    def expected_total(self) -> int:
        return sum(u.expected for u in self.lines)


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


# ---------------------------------------------------------------- gastos fijos

@dataclass(frozen=True)
class Recurring:
    """Un gasto que se repite cada mes: el seguro, una suscripción, la aportación a casa..."""
    name: str
    category_id: int | None
    amount: int                    # lo que suele ser (la mediana)
    months: int                    # en cuántos meses ha salido
    last: date
    next: date                     # cuándo debería volver a salir, más o menos

    @property
    def yearly(self) -> int:
        return self.amount * 12


RECURRING_DAYS = 120               # se mira lo de los últimos cuatro meses
RECURRING_TOLERANCE = 0.10         # importes a menos de un 10 % se consideran el mismo


def _mediana(valores: list[int]) -> int:
    orden = sorted(valores)
    medio = len(orden) // 2
    return orden[medio] if len(orden) % 2 else (orden[medio - 1] + orden[medio]) // 2


def _nombre_legible(concepto: str) -> str | None:
    """Un nombre para el gasto fijo: el concepto (los del banco, limpios: «Basic-Fit»), si es corto."""
    concepto = limpio(concepto)
    if not concepto or "//" in concepto or len(concepto) > 40 or sum(c.isdigit() for c in concepto) > 4:
        return None
    return concepto


def recurring(expenses, today: date) -> list[Recurring]:
    """Los gastos fijos: misma categoría e importe parecido, en meses seguidos, una o dos veces al mes.

    `expenses` son (fecha, categoría, concepto, importe). Lo que sale muchas veces en un mismo mes
    (cafés, gasolina) no es un gasto fijo aunque se repita.
    """
    desde = today.toordinal() - RECURRING_DAYS
    por_categoria: dict[int | None, list] = {}
    for dia, categoria, concepto, importe in expenses:
        if dia.toordinal() >= desde and dia <= today:
            por_categoria.setdefault(categoria, []).append((dia, concepto, importe))
    fijos = []
    for categoria, filas in por_categoria.items():
        grupos: list[list] = []
        for fila in sorted(filas, key=lambda f: f[2]):
            if grupos:
                tipico = _mediana([f[2] for f in grupos[-1]])
                if abs(fila[2] - tipico) <= max(tipico * RECURRING_TOLERANCE, 100):
                    grupos[-1].append(fila)
                    continue
            grupos.append([fila])
        for grupo in grupos:
            meses: dict[tuple[int, int], int] = {}
            for dia, _, _ in grupo:
                meses[(dia.year, dia.month)] = meses.get((dia.year, dia.month), 0) + 1
            indices = sorted(y * 12 + m for y, m in meses)
            seguidos = any(b - a == 1 for a, b in zip(indices, indices[1:]))
            if len(meses) < 2 or not seguidos or max(meses.values()) > 2:
                continue
            ultimo = max(f[0] for f in grupo)
            nombres = [n for n in (_nombre_legible(f[1]) for f in grupo) if n]
            nombre = max(set(nombres), key=nombres.count) if nombres else ""
            fijos.append(Recurring(nombre, categoria, _mediana([f[2] for f in grupo]), len(meses),
                                   ultimo, add_months(ultimo, 1)))
    return sorted(fijos, key=lambda r: -r.amount)


# ---------------------------------------------------------------- subidas y cobros repetidos

PRICE_RISE_MIN = 50                # una subida de menos de 50 céntimos no merece aviso
PRICE_RISE_PCT = 0.02              # ni una de menos de un 2 %
PRICE_LOOKBACK_DAYS = 400          # se mira algo más de un año (los seguros suben una vez al año)
PRICE_RECENT_DAYS = 45             # y solo se avisa de lo cobrado en el último mes y medio
DOUBLE_DAYS = 3                    # el mismo cobro dos veces en tres días: ¿te han cobrado doble?


@dataclass(frozen=True)
class PriceChange:
    """Algo que pagas cada mes (o cada año) y ha cambiado: sube de precio o se ha cobrado dos veces."""
    kind: str                      # 'subida' | 'doble'
    name: str
    key: str                       # el comercio, normalizado (para no repetir el aviso)
    before: int                    # lo que costaba (en 'doble', el importe de cada cobro)
    now: int
    date: date                     # el día del cobro nuevo
    category_id: int | None = None

    @property
    def diff(self) -> int:
        return self.now - self.before

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}:{self.date.isoformat()}:{self.now}"


def price_changes(charges, today: date) -> list[PriceChange]:
    """Las subidas de precio y los cobros dobles en lo que pagas de forma periódica.

    `charges` son (fecha, clave del comercio, nombre, importe, categoría), solo gastos. Un comercio es
    «periódico» si sale en al menos dos meses distintos y nunca más de dos veces en un mismo mes
    (así el súper o la gasolinera, que salen muchas veces, no cuentan). Hay subida si el último cobro
    es más alto que el anterior; cobro doble si el mismo importe sale dos veces en pocos días y ese
    comercio normalmente cobra una vez al mes.
    """
    desde = today.toordinal() - PRICE_LOOKBACK_DAYS
    por_comercio: dict[str, list] = {}
    for dia, clave, nombre, importe, categoria in charges:
        if clave and desde <= dia.toordinal() and dia <= today:
            por_comercio.setdefault(clave, []).append((dia, nombre, importe, categoria))
    cambios = []
    for clave, filas in por_comercio.items():
        filas.sort(key=lambda f: f[0])
        meses: dict[tuple[int, int], int] = {}
        for f in filas:
            meses[(f[0].year, f[0].month)] = meses.get((f[0].year, f[0].month), 0) + 1
        if len(meses) < 2 or max(meses.values()) > 2:
            continue
        ultimo = filas[-1]
        if (today - ultimo[0]).days > PRICE_RECENT_DAYS:
            continue
        nombre = ultimo[1] or clave
        anterior = filas[-2]
        if (ultimo[0] - anterior[0]).days <= DOUBLE_DAYS and ultimo[2] == anterior[2]:
            # dos veces lo mismo seguido: doble solo si lo normal es una vez al mes
            if sum(1 for n in meses.values() if n > 1) == 1:
                cambios.append(PriceChange("doble", nombre, clave, ultimo[2], ultimo[2], ultimo[0], ultimo[3]))
            continue
        # la subida: cada cobro del último mes frente a lo que se cobró el mes anterior en que hubo
        # (si cobra dos cosas al mes, cada una frente a la más parecida de las de entonces)
        mes = (ultimo[0].year, ultimo[0].month)
        previos = [f for f in filas if (f[0].year, f[0].month) < mes]
        if not previos:
            continue
        mes_previo = (previos[-1][0].year, previos[-1][0].month)
        antes_importes = [f[2] for f in previos if (f[0].year, f[0].month) == mes_previo]
        for f in (f for f in filas if (f[0].year, f[0].month) == mes):
            if f[2] in antes_importes or (today - f[0]).days > PRICE_RECENT_DAYS:
                continue
            menores = [a for a in antes_importes if a < f[2]]
            if not menores:
                continue
            antes = max(menores)
            if f[2] - antes >= max(PRICE_RISE_MIN, antes * PRICE_RISE_PCT) and f[2] <= antes * 3:
                cambios.append(PriceChange("subida", f[1] or nombre, clave, antes, f[2], f[0], f[3]))
    return sorted(cambios, key=lambda c: c.date, reverse=True)


# ---------------------------------------------------------------- cuánto aguantas

@dataclass(frozen=True)
class Runway:
    """Cuántos meses podrías vivir con lo que tienes si dejaras de cobrar."""
    money: int                     # todo lo que hay en tus cuentas (sobres incluidos)
    monthly_spend: int             # lo que gastas al mes de media
    monthly_fixed: int             # solo los pagos fijos
    months_seen: int               # con cuántos meses se ha calculado la media

    @property
    def months(self) -> float | None:
        return self.money / self.monthly_spend if self.monthly_spend > 0 and self.money > 0 else None

    @property
    def months_fixed(self) -> float | None:
        """Si solo pagaras lo fijo (préstamos, seguros, suscripciones...)."""
        return self.money / self.monthly_fixed if self.monthly_fixed > 0 and self.money > 0 else None
