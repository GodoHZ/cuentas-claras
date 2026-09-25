"""Importador del CSV de la pestaña Movimientos de la hoja de Google.

Columnas: Fecha, Tipo, Categoría, Sobre, Cuenta, Concepto, Importe (€)
Formato español: "1.234,56 €", fechas dd/mm/aaaa. Separador "," o ";".

- Ignora las filas sin importe y las marcadas "(ejemplo)".
- Avisa de categorías, sobres y cuentas que no existen (y puede crearlas).
- Marca como "ya existe" lo que ya está en la app, para poder reimportar sin duplicar.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from . import calc, repo
from .money import InvalidAmount, parse_amount, parse_date


def norm(text) -> str:
    """Minúsculas, sin tildes ni espacios sobrantes: para comparar nombres."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(s.casefold().split())


HEADERS = {
    "fecha": "date", "tipo": "type", "categoria": "category", "sobre": "envelope",
    "cuenta": "account", "concepto": "concept", "importe": "amount", "importe eur": "amount",
    "otra cuenta": "other_account", "desde": "other_account", "cuenta origen": "other_account",
}


def header_key(text) -> str | None:
    """'Importe (€)' -> 'amount'; 'Categoría' -> 'category'."""
    return HEADERS.get(re.sub(r"\s*\(.*?\)\s*", " ", norm(text)).strip())


TYPE_ALIASES = {
    "ingreso": calc.INGRESO, "ingresos": calc.INGRESO,
    "gasto": calc.GASTO, "gastos": calc.GASTO,
    "aporte_sobre": calc.APORTE, "aporte a sobre": calc.APORTE, "aporte sobre": calc.APORTE,
    "aporte al sobre": calc.APORTE, "aporte": calc.APORTE,
    "retiro_sobre": calc.RETIRO, "retiro de sobre": calc.RETIRO, "retiro sobre": calc.RETIRO,
    "retiro del sobre": calc.RETIRO, "retiro": calc.RETIRO,
    "traspaso": calc.TRASPASO, "traspaso entre cuentas": calc.TRASPASO, "transferencia": calc.TRASPASO,
}


@dataclass
class ImportRow:
    line: int
    raw: dict
    status: str = "ok"             # ok | falta | duplicada | ignorada | error
    reason: str = ""
    date: str = ""
    type: str = ""
    category: str = ""
    envelope: str = ""
    account: str = ""
    other_account: str = ""
    concept: str = ""
    amount: int | None = None


@dataclass
class Preview:
    rows: list[ImportRow] = field(default_factory=list)
    missing_categories: set[str] = field(default_factory=set)
    missing_envelopes: set[str] = field(default_factory=set)
    missing_accounts: set[str] = field(default_factory=set)
    error: str = ""

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.status == status)

    @property
    def has_missing(self) -> bool:
        return bool(self.missing_categories or self.missing_envelopes or self.missing_accounts)


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _reader(text: str):
    sample = text[:4096]
    first = sample.splitlines()[0] if sample else ""
    delimiter = ";" if first.count(";") > first.count(",") else ","
    if first.count("\t") > max(first.count(";"), first.count(",")):
        delimiter = "\t"
    return csv.reader(io.StringIO(text), delimiter=delimiter)


def _lookup(rows) -> dict[str, dict]:
    return {norm(r["name"]): dict(r) for r in rows}


def _key(date, type_, amount, category, envelope, concept) -> tuple:
    return (date, type_, amount, norm(category), norm(envelope), norm(concept))


def parse(conn, text: str) -> Preview:
    preview = Preview()
    reader = _reader(text)
    try:
        header = next(reader)
    except StopIteration:
        preview.error = "El archivo está vacío."
        return preview
    columns = [header_key(h) for h in header]
    for need, label in (("date", "Fecha"), ("type", "Tipo"), ("amount", "Importe")):
        if need not in columns:
            preview.error = (f"No encuentro la columna «{label}». Las columnas tienen que ser: "
                             "Fecha, Tipo, Categoría, Sobre, Cuenta, Concepto, Importe (€).")
            return preview

    cats = _lookup(repo.categories(conn))
    envs = _lookup(repo.envelopes(conn))
    accs = _lookup(repo.accounts(conn))
    acc_by_id = {a["id"]: a["name"] for a in accs.values()}
    default_acc = acc_by_id.get(repo.int_setting(conn, "default_account_id"), "")
    envelope_acc = acc_by_id.get(repo.int_setting(conn, "envelope_account_id"), default_acc)

    existing = Counter(
        _key(r["date"], r["type"], r["amount"], r["category"], r["envelope"], r["concept"])
        for r in repo.tx_rows(conn))

    for line, values in enumerate(reader, start=2):
        if not any(v.strip() for v in values):
            continue
        raw = {col: (values[i].strip() if i < len(values) else "")
               for i, col in enumerate(columns) if col}
        row = ImportRow(line=line, raw=raw)
        preview.rows.append(row)

        if any("(ejemplo)" in v.casefold() for v in values):
            row.status, row.reason = "ignorada", "Fila de ejemplo"
            continue
        if not raw.get("amount"):
            row.status, row.reason = "ignorada", "Sin importe"
            continue

        errors = []
        try:
            row.amount = parse_amount(raw["amount"])
        except InvalidAmount as e:
            errors.append(str(e))
        try:
            row.date = parse_date(raw.get("date")).isoformat()
        except ValueError:
            errors.append(f"Fecha «{raw.get('date', '')}» no válida")
        row.type = TYPE_ALIASES.get(norm(raw.get("type")), "")
        if not row.type:
            errors.append(f"Tipo «{raw.get('type', '')}» desconocido")
        row.concept = raw.get("concept", "")

        if row.type in calc.NEEDS_CATEGORY and raw.get("category"):
            match = cats.get(norm(raw["category"]))
            row.category = match["name"] if match else raw["category"]
        if row.type != calc.INGRESO and raw.get("envelope"):
            match = envs.get(norm(raw["envelope"]))
            row.envelope = match["name"] if match else raw["envelope"]
        if raw.get("account"):
            match = accs.get(norm(raw["account"]))
            row.account = match["name"] if match else raw["account"]
        else:
            row.account = envelope_acc if row.type in calc.NEEDS_ENVELOPE else default_acc

        if raw.get("other_account"):
            match = accs.get(norm(raw["other_account"]))
            row.other_account = match["name"] if match else raw["other_account"]
        errors += calc.validate_tx(row.type or None, row.amount, row.category or None,
                                   row.envelope or None, row.account or None, row.other_account or None)
        if not row.account:
            errors.append("Falta la cuenta")
        if errors:
            row.status, row.reason = "error", "; ".join(dict.fromkeys(errors))
            continue
        missing = []
        if row.category and norm(row.category) not in cats:
            missing.append(f"la categoría «{row.category}» no existe")
            preview.missing_categories.add(row.category)
        if row.envelope and norm(row.envelope) not in envs:
            missing.append(f"el sobre «{row.envelope}» no existe")
            preview.missing_envelopes.add(row.envelope)
        if row.account and norm(row.account) not in accs:
            missing.append(f"la cuenta «{row.account}» no existe")
            preview.missing_accounts.add(row.account)
        if row.other_account and norm(row.other_account) not in accs:
            missing.append(f"la cuenta «{row.other_account}» no existe")
            preview.missing_accounts.add(row.other_account)
        if missing:
            reason = "; ".join(missing)
            row.status, row.reason = "falta", reason[0].upper() + reason[1:]
            continue

        key = _key(row.date, row.type, row.amount, row.category, row.envelope, row.concept)
        if existing[key] > 0:
            existing[key] -= 1
            row.status, row.reason = "duplicada", "Ya está en la app"
    return preview


def apply(conn, preview: Preview, create_missing: bool = False) -> int:
    """Inserta las filas OK. Con create_missing, crea antes las categorías, sobres
    y cuentas que falten e importa también esas filas. Devuelve cuántas."""
    wanted = {"ok", "falta"} if create_missing else {"ok"}
    if not create_missing:
        preview.missing_categories, preview.missing_envelopes, preview.missing_accounts = set(), set(), set()
    with conn:
        for name in sorted(preview.missing_categories):
            kind = "ingreso" if any(r.category == name and r.type == calc.INGRESO for r in preview.rows) else "gasto"
            conn.execute("INSERT OR IGNORE INTO categories (name, kind, position) VALUES (?, ?, ?)",
                         (name, kind, repo.next_position(conn, "categories")))
        for name in sorted(preview.missing_envelopes):
            conn.execute("INSERT OR IGNORE INTO envelopes (name, position) VALUES (?, ?)",
                         (name, repo.next_position(conn, "envelopes")))
        for name in sorted(preview.missing_accounts):
            conn.execute("INSERT OR IGNORE INTO accounts (name, position) VALUES (?, ?)",
                         (name, repo.next_position(conn, "accounts")))
        cats = {norm(r["name"]): r["id"] for r in repo.categories(conn)}
        envs = {norm(r["name"]): r["id"] for r in repo.envelopes(conn)}
        accs = {norm(r["name"]): r["id"] for r in repo.accounts(conn)}
        added = 0
        for r in preview.rows:
            if r.status not in wanted:
                continue
            repo.insert_tx(conn, date=r.date, type=r.type,
                           category_id=cats.get(norm(r.category)) if r.category else None,
                           envelope_id=envs.get(norm(r.envelope)) if r.envelope else None,
                           account_id=accs[norm(r.account)],
                           other_account_id=accs.get(norm(r.other_account)) if r.other_account else None,
                           concept=r.concept, amount=r.amount)
            added += 1
    return added
