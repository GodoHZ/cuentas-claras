from datetime import date

import pytest

from app.money import (InvalidAmount, amount_input, eur, fmt_date, month_name, parse_amount,
                       parse_date, parse_month, pct)


@pytest.mark.parametrize("texto, cent", [
    ("12,50", 1250), ("12.50", 1250), ("1.234,56 €", 123456), ("1234.56", 123456),
    ("2.000", 200000), ("2000", 200000), ("0,05", 5), ("1.234.567,89", 123456789),
    ("  7 € ", 700), ("12,345", 1235), ("", None), (None, None), ("-3,50", -350), ("€", None),
])
def test_parse_amount(texto, cent):
    assert parse_amount(texto) == cent


@pytest.mark.parametrize("texto", ["hola", "12,,5x", "12€34x"])
def test_parse_amount_invalido(texto):
    with pytest.raises(InvalidAmount):
        parse_amount(texto)


def test_eur_formato_español():
    assert eur(123456) == "1.234,56 €"
    assert eur(0) == "0,00 €"
    assert eur(-250) == "−2,50 €"
    assert eur(1250, sign=True) == "+12,50 €"
    assert eur(None) == "—"


def test_amount_input_ida_y_vuelta():
    assert amount_input(123456) == "1234,56"
    assert parse_amount(amount_input(25000)) == 25000
    assert amount_input(None) == ""


def test_pct():
    assert pct(0.1943) == "19 %"
    assert pct(0.1943, 1) == "19,4 %"
    assert pct(None) == "—"


def test_fechas():
    assert fmt_date("2026-09-19") == "19/09/2026"
    assert parse_date("19/09/2026") == date(2026, 9, 19)
    assert parse_date("2026-09-19") == date(2026, 9, 19)
    assert parse_date("1-9-26") == date(2026, 9, 1)
    with pytest.raises(ValueError):
        parse_date("ayer")


def test_meses():
    assert month_name(2026, 9) == "septiembre de 2026"
    assert month_name(2026, 9, True) == "Septiembre de 2026"
    assert parse_month("2026-09", (2000, 1)) == (2026, 9)
    assert parse_month("cualquier cosa", (2026, 9)) == (2026, 9)
    assert parse_month("2026-13", (2026, 9)) == (2026, 9)
