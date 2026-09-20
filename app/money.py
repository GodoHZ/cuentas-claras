"""Importes, fechas y meses con formato español.

Los importes se guardan SIEMPRE en céntimos (enteros) para no arrastrar
errores de coma flotante: 1.234,56 € -> 123456.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NBSP = " "
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MESES_CORTOS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


class InvalidAmount(ValueError):
    pass


def parse_amount(text) -> int | None:
    """'1.234,56 €', '1234.56', '12,5', '2.000' -> céntimos. Vacío -> None.

    Con coma y punto a la vez, el último que aparece es el decimal. Con solo
    puntos, un grupo final de 3 cifras se toma como miles (1.003 -> 1003 €),
    que es como escribimos en España; '12.5' sigue siendo 12,50 €.
    """
    if text is None:
        return None
    s = str(text).strip().replace("€", "").replace(NBSP, "").replace(" ", "")
    if not s:
        return None
    negative = s[0] in "-−"
    s = s.lstrip("-−+")
    if not re.fullmatch(r"[0-9.,]*[0-9][0-9.,]*", s):
        raise InvalidAmount(f"«{text}» no es un importe válido")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2 or len(parts[1]) == 3:
            s = s.replace(".", "")
    try:
        value = Decimal(s)
    except InvalidOperation as e:
        raise InvalidAmount(f"«{text}» no es un importe válido") from e
    cents = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def eur(cents, sign: bool = False) -> str:
    """123456 -> '1.234,56 €' (con espacio duro antes del €)."""
    if cents is None:
        return "—"
    cents = int(cents)
    whole, frac = divmod(abs(cents), 100)
    txt = f"{whole:,}".replace(",", ".") + f",{frac:02d}"
    prefix = "−" if cents < 0 else ("+" if sign and cents > 0 else "")
    return f"{prefix}{txt}{NBSP}€"


def amount_input(cents) -> str:
    """Valor para un <input>: 123456 -> '1234,56'; None -> ''."""
    if cents is None or cents == "":
        return ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{'-' if int(cents) < 0 else ''}{whole},{frac:02d}"


def pct(x, decimals: int = 0) -> str:
    """0.1943 -> '19 %'."""
    if x is None:
        return "—"
    return f"{x * 100:.{decimals}f}".replace(".", ",") + f"{NBSP}%"


def to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def fmt_date(value) -> str:
    """date o '2026-09-19' -> '19/09/2026'."""
    if not value:
        return ""
    return to_date(value).strftime("%d/%m/%Y")


def long_day(value) -> str:
    """'2026-09-19' -> 'Sábado 19 de septiembre'."""
    d = to_date(value)
    return f"{DIAS[d.weekday()].capitalize()} {d.day} de {MESES[d.month - 1]}"


def parse_date(text) -> date:
    """Acepta 2026-09-19, 19/09/2026, 19-9-2026, 19.09.26..."""
    s = str(text or "").strip()
    if not s:
        raise ValueError("fecha vacía")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})", s)
    if m:
        year = int(m[3])
        if year < 100:
            year += 2000
        return date(year, int(m[2]), int(m[1]))
    raise ValueError(f"«{text}» no es una fecha válida")


def month_name(year: int, month: int, capital: bool = False) -> str:
    txt = f"{MESES[month - 1]} de {year}"
    return txt[0].upper() + txt[1:] if capital else txt


def month_short(year: int, month: int) -> str:
    return f"{MESES_CORTOS[month - 1]} {str(year)[2:]}"


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month(text, default: tuple[int, int]) -> tuple[int, int]:
    """'2026-09' -> (2026, 9). Si no es válido, devuelve default."""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", str(text or "").strip())
    if m and 1 <= int(m[2]) <= 12:
        return int(m[1]), int(m[2])
    return default


def add_months_ym(year: int, month: int, n: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1
