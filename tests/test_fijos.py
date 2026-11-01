"""Gastos fijos: lo que se repite cada mes, detectado solo."""
from datetime import date

from app import calc, views
from conftest import apuntar

HOY = date(2026, 11, 10)


def test_lo_que_se_repite_cada_mes_es_fijo():
    seguro, cafe, casa = 1, 2, 3
    gastos = [
        (date(2026, 9, 23), seguro, "Seguro", 33405), (date(2026, 10, 23), seguro, "", 32242),
        (date(2026, 9, 1), casa, "", 20000), (date(2026, 10, 1), casa, "", 20000), (date(2026, 11, 1), casa, "", 20000),
        # el café sale muchas veces al mes: no es un gasto fijo aunque se repita
        *[(date(2026, m, d), cafe, "", 380) for m in (9, 10) for d in (3, 10, 17, 24)],
        # una compra suelta tampoco
        (date(2026, 10, 5), seguro, "Funda", 5498),
    ]
    fijos = calc.recurring(gastos, HOY)
    assert [(r.category_id, r.amount, r.months) for r in fijos] == [(seguro, 32823, 2), (casa, 20000, 3)]
    assert fijos[0].name == "Seguro" and fijos[1].name == ""
    assert fijos[1].next == date(2026, 12, 1) and fijos[1].yearly == 240000


def test_hacen_falta_meses_seguidos_y_recientes():
    assert calc.recurring([(date(2026, 7, 1), 1, "", 1000), (date(2026, 9, 1), 1, "", 1000)], HOY) == []
    assert calc.recurring([(date(2026, 1, 1), 1, "", 1000), (date(2026, 2, 1), 1, "", 1000)], HOY) == []


def test_los_conceptos_del_banco_no_sirven_de_nombre():
    assert calc._nombre_legible("ADEUDO A SU CARGO // PAGO DE ADEUDO DIRECTO SEPA") is None
    assert calc._nombre_legible("Youtube") == "Youtube"
    # los del banco, limpios
    assert calc._nombre_legible("ADEUDO A SU CARGO // PAGO DE ADEUDO DIRECTO SEPA // N 2026296000031121 "
                                "Basic-Fit Spain S.A.U (BNP)") == "Basic-Fit"


def test_la_pantalla(conn):
    apuntar(conn, "2026-10-05", calc.GASTO, 1049, categoria="Ocio", concepto="Youtube")
    apuntar(conn, "2026-11-05", calc.GASTO, 1049, categoria="Ocio", concepto="Youtube")
    f = views.recurring(conn, HOY)
    assert f["monthly"] == 1049 and f["lista"][0]["name"] == "Youtube" and f["lista"][0]["category"] == "Ocio"
