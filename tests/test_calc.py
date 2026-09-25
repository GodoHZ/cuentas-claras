"""Las reglas de la sección 4 del plan, una por una."""
from datetime import date

import pytest

from app import calc
from app.calc import APORTE, GASTO, INGRESO, RETIRO, Tx


def tx(day, type_, amount, category=None, envelope=None):
    return Tx(date.fromisoformat(day), type_, amount, category, envelope)


# ---------------------------------------------------------------- validación

def test_validacion_exige_sobre_y_categoria():
    assert calc.validate_tx(APORTE, 100, None, None) == ["Elige el sobre."]
    assert calc.validate_tx(RETIRO, 100, 3, None) == ["Elige el sobre."]
    assert calc.validate_tx(GASTO, 100, None, None) == ["Elige una categoría."]
    assert calc.validate_tx(INGRESO, 100, 1, None) == []
    assert calc.validate_tx(GASTO, 100, 1, 2) == []          # gasto pagado desde un sobre


def test_validacion_importe():
    assert "Escribe el importe." in calc.validate_tx(GASTO, None, 1, None)
    assert "El importe tiene que ser mayor que 0." in calc.validate_tx(GASTO, 0, 1, None)
    assert "El importe tiene que ser mayor que 0." in calc.validate_tx(GASTO, -5, 1, None)


# ---------------------------------------------------------------- sobres

def test_saldo_sobre():
    txs = [tx("2026-09-01", APORTE, 10000, envelope=1),
           tx("2026-09-05", GASTO, 2500, category=3, envelope=1),
           tx("2026-09-06", RETIRO, 1500, envelope=1),
           tx("2026-09-07", GASTO, 4000, category=3),          # sin sobre: no cuenta
           tx("2026-09-08", APORTE, 3000, envelope=2)]
    assert calc.envelope_balances(txs) == {1: 6000, 2: 3000}


@pytest.mark.parametrize("saldo, objetivo, aporte, progreso, falta, meses, texto", [
    (123456, None, 10000, None, None, None, "Sin objetivo"),
    (200000, 200000, None, 1.0, 0, None, "¡Conseguido!"),
    (250000, 200000, None, 1.0, 0, None, "¡Conseguido!"),      # progreso no pasa de 1
    (0, 100000, None, 0.0, 100000, None, "Sin aporte"),
    (50000, 100000, 30000, 0.5, 50000, 2, "2 meses"),          # 50000/30000 -> 2 (hacia arriba)
    (70000, 100000, 30000, 0.7, 30000, 1, "1 mes"),
    (-500, 100000, 30000, 0.0, 100500, 4, "4 meses"),          # progreso nunca negativo
])
def test_estado_sobre(saldo, objetivo, aporte, progreso, falta, meses, texto):
    st = calc.envelope_status(saldo, objetivo, aporte)
    assert (st.progress, st.missing, st.months, st.label) == (progreso, falta, meses, texto)


# ---------------------------------------------------------------- mes

def test_resumen_mes():
    txs = [tx("2026-09-01", INGRESO, 150000, category=17),
           tx("2026-09-02", GASTO, 20000, category=1),               # de la nómina
           tx("2026-09-03", GASTO, 5000, category=5, envelope=2),    # desde un sobre
           tx("2026-09-04", APORTE, 10000, envelope=2),
           tx("2026-09-05", RETIRO, 3000, envelope=2),               # no toca el libre
           tx("2026-08-31", GASTO, 99900, category=1)]               # otro mes
    m = calc.month_summary(txs, 2026, 9)
    assert (m.income, m.payroll_spent, m.saved, m.envelope_spent) == (150000, 20000, 10000, 5000)
    assert m.free == 150000 - 20000 - 10000
    assert m.saved_ratio == pytest.approx(10000 / 150000)


def test_resumen_mes_vacio():
    m = calc.month_summary([], 2026, 9)
    assert (m.free, m.saved_ratio) == (0, None)


# ---------------------------------------------------------------- presupuesto

def test_gastado_por_categoria_incluye_lo_pagado_desde_sobres():
    txs = [tx("2026-09-02", GASTO, 3000, category=10),
           tx("2026-09-03", GASTO, 2000, category=10, envelope=1),
           tx("2026-09-04", INGRESO, 5000, category=10),             # un ingreso no gasta
           tx("2026-10-02", GASTO, 9900, category=10)]
    assert calc.spent_by_category(txs, 2026, 9) == {10: 5000}


@pytest.mark.parametrize("presupuesto, gastado, ratio, nivel", [
    (10000, 0, 0.0, "ok"),
    (10000, 7999, 0.7999, "ok"),
    (10000, 8000, 0.8, "aviso"),
    (10000, 10000, 1.0, "aviso"),
    (10000, 10001, 1.0001, "pasado"),
    (0, 5000, None, "sin"),
    (None, 0, None, "sin"),
])
def test_linea_presupuesto(presupuesto, gastado, ratio, nivel):
    line = calc.budget_line(presupuesto, gastado)
    assert line.level == nivel
    if ratio is None:
        assert line.ratio is None
    else:
        assert line.ratio == pytest.approx(ratio)
    assert line.left == (presupuesto or 0) - gastado


# ---------------------------------------------------------------- deudas

def test_sumar_meses_y_ultima_cuota():
    assert calc.add_months(date(2026, 10, 2), 119) == date(2036, 9, 2)
    assert calc.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)     # día que no existe


@pytest.mark.parametrize("hoy, pagadas", [
    ("2026-09-19", 0),        # antes de la primera cuota
    ("2026-10-01", 0),
    ("2026-10-02", 1),        # justo el día de la primera
    ("2026-11-01", 1),
    ("2026-11-02", 2),
    ("2027-10-02", 13),
    ("2036-09-02", 120),
    ("2040-01-01", 120),      # nunca más de las que hay
])
def test_estado_prestamo(hoy, pagadas):
    st = calc.loan_status(25000, date(2026, 10, 2), 120, date.fromisoformat(hoy))
    assert st.paid == pagadas
    assert st.pending == 120 - pagadas
    assert st.pending_total == (120 - pagadas) * 25000
    assert st.last == date(2036, 9, 2)


def test_ratio_cuotas():
    st = calc.loan_status(25000, date(2026, 10, 2), 120, date(2026, 9, 19))
    assert calc.installments_ratio([st], 150000) == pytest.approx(0.16667, abs=1e-5)
    assert calc.installments_ratio([st], None) is None
    pagado = calc.loan_status(10000, date(2020, 1, 1), 12, date(2026, 9, 19))
    assert calc.installments_ratio([pagado], 150000) == 0          # terminado: no suma


# ---------------------------------------------------------------- cuadre

def test_cuadre():
    """Compara lo que dice el banco con lo que calcula la app."""
    assert calc.reconcile(300370, 300370).message == "Cuadra con el banco"
    assert calc.reconcile(300370, 300370).level == "ok"
    pequeño = calc.reconcile(300520, 300370)
    assert pequeño.level == "sobra" and "intereses" in pequeño.message and "1,50" in pequeño.message
    grande = calc.reconcile(400370, 300370)
    assert grande.level == "sobra" and "ingreso sin apuntar" in grande.message
    falta = calc.reconcile(290370, 300370)
    assert falta.level == "falta" and "100,00" in falta.message and "menos" in falta.message


def test_umbrales_configurables():
    assert calc.budget_level(0.85) == "aviso"                 # de fábrica avisa al 80 %
    assert calc.budget_level(0.85, warn=0.9) == "ok"          # con el aviso al 90 %, aún no
    assert calc.budget_line(10000, 8500, warn=0.9).level == "ok"
    assert calc.reconcile(300370 + 500, 300370, small=100).level == "sobra"
    assert "ingreso" in calc.reconcile(300370 + 500, 300370, small=100).message
    assert "intereses" in calc.reconcile(300370 + 500, 300370, small=1000).message



# ---------------------------------------------------------------- cuentas

CUENTAS = [{"id": 1, "initial_balance": 100000}, {"id": 2, "initial_balance": 0}]


def tx_cuentas(day, type_, amount, cuenta, otra=None, envelope=None, category=None):
    return Tx(date.fromisoformat(day), type_, amount, category, envelope, cuenta, otra)


def test_saldo_de_una_cuenta():
    """Saldo = lo que había al empezar + ingresos − gastos ± traspasos."""
    txs = [tx_cuentas("2026-09-01", INGRESO, 150000, 1, category=1),
           tx_cuentas("2026-09-02", GASTO, 20000, 1, category=2),
           tx_cuentas("2026-09-03", calc.TRASPASO, 50000, 2, otra=1)]      # de la 1 a la 2
    saldos = calc.account_balances(txs, CUENTAS)
    assert saldos[1] == 100000 + 150000 - 20000 - 50000
    assert saldos[2] == 50000
    assert sum(saldos.values()) == 100000 + 150000 - 20000                 # un traspaso no crea dinero


def test_un_traspaso_no_es_gasto_ni_ingreso():
    txs = [tx_cuentas("2026-09-03", calc.TRASPASO, 50000, 2, otra=1)]
    m = calc.month_summary(txs, 2026, 9)
    assert (m.income, m.payroll_spent, m.saved, m.free) == (0, 0, 0, 0)


def test_un_aporte_mueve_el_dinero_si_dices_de_donde_sale():
    con_origen = tx_cuentas("2026-09-04", APORTE, 30000, 2, otra=1, envelope=7)
    assert calc.account_moves(con_origen) == {1: -30000, 2: 30000}
    solo_etiqueta = tx_cuentas("2026-09-04", APORTE, 30000, 2, envelope=7)
    assert calc.account_moves(solo_etiqueta) == {}                         # ya estaba ahí
    # en los dos casos el sobre sube igual
    assert calc.envelope_balances([con_origen])[7] == 30000
    assert calc.envelope_balances([solo_etiqueta])[7] == 30000


def test_un_retiro_devuelve_el_dinero_a_la_otra_cuenta():
    retiro = tx_cuentas("2026-09-05", RETIRO, 10000, 2, otra=1, envelope=7)
    assert calc.account_moves(retiro) == {2: -10000, 1: 10000}
    assert calc.envelope_balances([retiro])[7] == -10000


def test_un_gasto_desde_un_sobre_sale_de_su_cuenta():
    gasto = tx_cuentas("2026-09-06", GASTO, 5000, 2, envelope=7, category=3)
    assert calc.account_moves(gasto) == {2: -5000}
    assert calc.envelope_balances([gasto])[7] == -5000


def test_validacion_del_traspaso():
    assert "Elige de qué cuenta sale el dinero." in calc.validate_tx(calc.TRASPASO, 100, None, None, 1, None)
    assert "El dinero tiene que ir a una cuenta distinta." in calc.validate_tx(calc.TRASPASO, 100, None, None, 1, 1)
    assert calc.validate_tx(calc.TRASPASO, 100, None, None, 1, 2) == []
