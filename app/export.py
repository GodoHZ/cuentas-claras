"""Exportar todo a JSON o a CSV (un zip con un CSV por tabla).

El CSV de movimientos usa las mismas columnas que la hoja de Google, así que
se puede volver a importar con el importador.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile

from . import calc, repo
from .money import amount_input, eur, fmt_date

PRIVATE_SETTINGS = {"pin_hash", "secret_key"}


def _euros(cents):
    return None if cents is None else round(cents / 100, 2)


def as_dict(conn, exported_at: str, version: str) -> dict:
    return {
        "app": "finanzas", "version": version, "exportado": exported_at,
        "ajustes": {k: v for k, v in repo.settings(conn).items() if k not in PRIVATE_SETTINGS},
        "cuentas": [dict(r) for r in repo.accounts(conn)],
        "categorias": [{**dict(r), "budget": _euros(r["budget"])} for r in repo.categories(conn)],
        "sobres": [{**dict(r), "target": _euros(r["target"]), "monthly": _euros(r["monthly"])}
                   for r in repo.envelopes(conn)],
        "prestamos": [{**dict(r), "installment": _euros(r["installment"])} for r in repo.loans(conn)],
        "movimientos": [{**dict(r), "amount": _euros(r["amount"])} for r in repo.tx_rows(conn, oldest_first=True)],
        "cuadres": [{**dict(r), "balance": _euros(r["balance"])}
                    for r in conn.execute("SELECT * FROM tr_checks ORDER BY date, id")],
    }


def to_json(conn, exported_at: str, version: str) -> bytes:
    return json.dumps(as_dict(conn, exported_at, version), ensure_ascii=False, indent=2).encode()


def _csv(header, rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    return ("﻿" + buf.getvalue()).encode()          # BOM: Excel lo abre bien


def to_csv_zip(conn) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("movimientos.csv", _csv(
            ["Fecha", "Tipo", "Categoría", "Sobre", "Cuenta", "Concepto", "Importe (€)"],
            [[fmt_date(r["date"]), calc.TYPES[r["type"]], r["category"] or "", r["envelope"] or "",
              r["account"] or "", r["concept"], eur(r["amount"]).replace(" ", " ")]
             for r in repo.tx_rows(conn, oldest_first=True)]))
        z.writestr("categorias.csv", _csv(
            ["Nombre", "Tipo", "Presupuesto mensual (€)", "Orden", "Activa"],
            [[r["name"], r["kind"], amount_input(r["budget"]), r["position"], "sí" if r["active"] else "no"]
             for r in repo.categories(conn)]))
        z.writestr("sobres.csv", _csv(
            ["Nombre", "Objetivo (€)", "Aporte mensual (€)", "Nota", "Orden", "Activo"],
            [[r["name"], amount_input(r["target"]), amount_input(r["monthly"]), r["note"], r["position"],
              "sí" if r["active"] else "no"] for r in repo.envelopes(conn)]))
        z.writestr("cuentas.csv", _csv(
            ["Nombre", "Orden", "Activa"],
            [[r["name"], r["position"], "sí" if r["active"] else "no"] for r in repo.accounts(conn)]))
        z.writestr("prestamos.csv", _csv(
            ["Nombre", "Cuota (€)", "1.ª cuota", "Nº cuotas"],
            [[r["name"], amount_input(r["installment"]), fmt_date(r["first_date"]), r["n_installments"]]
             for r in repo.loans(conn)]))
        z.writestr("cuadres.csv", _csv(
            ["Fecha", "Saldo real de la cuenta (€)"],
            [[fmt_date(r["date"]), amount_input(r["balance"])]
             for r in conn.execute("SELECT * FROM tr_checks ORDER BY date, id")]))
    return out.getvalue()
