"""Consultas a la base de datos. Nada de reglas de negocio: eso está en calc.py."""
from __future__ import annotations

import sqlite3
from datetime import date

from . import calc
from .money import to_date

# tablas que se pueden ordenar y archivar desde Ajustes
ORDERED_TABLES = {"categories", "envelopes", "accounts", "loans"}


# ---------------------------------------------------------------- ajustes

def settings(conn) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn, key: str, value) -> None:
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def int_setting(conn, key: str) -> int | None:
    value = setting(conn, key)
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


# reglas que se pueden cambiar en Ajustes (valor de fábrica a la derecha)
RULES = {
    "budget_warn_pct": 80,          # % del presupuesto a partir del cual avisa
    "installments_limit_pct": 35,   # % de la nómina en cuotas que se considera demasiado
    "reconcile_small_cents": 2000,  # diferencia de cuadre que se achaca a intereses
    "backup_keep_days": 30,         # días de copias que se guardan
}


def rule(conn, key: str) -> int:
    """Valor de una regla: el que haya puesto el usuario o el de fábrica."""
    value = int_setting(conn, key)
    return RULES[key] if value is None else value


def offline_cache(conn) -> bool:
    """¿Puede el móvil guardar copias de las pantallas para verlas sin conexión?

    Sin PIN, sí (no hay nada que proteger más allá de la propia red). Con PIN,
    solo si se activa a propósito: esas copias se ven sin pedir el PIN.
    """
    elegido = setting(conn, "offline_cache")
    if elegido is not None:
        return elegido == "1"
    return not setting(conn, "pin_hash")


def salary(conn) -> int | None:
    return int_setting(conn, "salary")


def start_month(conn) -> tuple[int, int]:
    d = to_date(setting(conn, "start_month", "2026-09-01"))
    return d.year, d.month


# ---------------------------------------------------------------- catálogos

def categories(conn, active: bool | None = None, kind: str | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM categories WHERE 1=1", []
    if active is not None:
        sql += " AND active = ?"
        args.append(int(active))
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    return conn.execute(sql + " ORDER BY position, name", args).fetchall()


def envelopes(conn, active: bool | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM envelopes"
    if active is not None:
        sql += f" WHERE active = {int(active)}"
    return conn.execute(sql + " ORDER BY position, name").fetchall()


def accounts(conn, active: bool | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM accounts"
    if active is not None:
        sql += f" WHERE active = {int(active)}"
    return conn.execute(sql + " ORDER BY position, name").fetchall()


def loans(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM loans ORDER BY position, id").fetchall()


def get_row(conn, table: str, row_id: int) -> sqlite3.Row | None:
    assert table in ORDERED_TABLES
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


def next_position(conn, table: str) -> int:
    assert table in ORDERED_TABLES
    return conn.execute(f"SELECT COALESCE(MAX(position), -1) + 1 FROM {table}").fetchone()[0]


def move(conn, table: str, row_id: int, direction: int) -> None:
    """Sube (-1) o baja (+1) un elemento en la lista, renumerando posiciones."""
    assert table in ORDERED_TABLES
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM {table} ORDER BY position, id")]
    if row_id not in ids:
        return
    i = ids.index(row_id)
    j = i + direction
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for pos, rid in enumerate(ids):
        conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, rid))


# ---------------------------------------------------------------- movimientos

def all_txs(conn) -> list[calc.Tx]:
    """Todos los movimientos, en el formato que usan las reglas de calc.py."""
    return [
        calc.Tx(date.fromisoformat(r["date"]), r["type"], r["amount"], r["category_id"],
                r["envelope_id"], r["account_id"], r["other_account_id"])
        for r in conn.execute("SELECT date, type, amount, category_id, envelope_id, account_id, "
                              "other_account_id FROM transactions ORDER BY date, id")
    ]


TX_SELECT = """
SELECT t.*, c.name AS category, e.name AS envelope, a.name AS account, o.name AS other_account
FROM transactions t
LEFT JOIN categories c ON c.id = t.category_id
LEFT JOIN envelopes e ON e.id = t.envelope_id
LEFT JOIN accounts a ON a.id = t.account_id
LEFT JOIN accounts o ON o.id = t.other_account_id
"""


def tx_rows(conn, month: tuple[int, int] | None = None, type_: str | None = None,
            category_id: int | None = None, envelope_id: int | None = None,
            account_id: int | None = None, limit: int | None = None,
            oldest_first: bool = False) -> list[sqlite3.Row]:
    sql, args = TX_SELECT + " WHERE 1=1", []
    if month:
        sql += " AND substr(t.date, 1, 7) = ?"
        args.append(f"{month[0]:04d}-{month[1]:02d}")
    if type_:
        sql += " AND t.type = ?"
        args.append(type_)
    if category_id:
        sql += " AND t.category_id = ?"
        args.append(category_id)
    if envelope_id:
        sql += " AND t.envelope_id = ?"
        args.append(envelope_id)
    if account_id:
        sql += " AND (t.account_id = ? OR t.other_account_id = ?)"
        args += [account_id, account_id]
    sql += " ORDER BY t.date, t.id" if oldest_first else " ORDER BY t.date DESC, t.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def get_tx(conn, tx_id: int) -> sqlite3.Row | None:
    return conn.execute(TX_SELECT + " WHERE t.id = ?", (tx_id,)).fetchone()


TX_FIELDS = ("date", "type", "category_id", "envelope_id", "account_id", "other_account_id",
             "concept", "amount")
TX_INSERT_FIELDS = TX_FIELDS + ("client_uid",)


def insert_tx(conn, **fields) -> int:
    cur = conn.execute(
        f"INSERT INTO transactions ({', '.join(TX_INSERT_FIELDS)}) "
        f"VALUES ({', '.join('?' * len(TX_INSERT_FIELDS))})",
        [fields.get(f) for f in TX_INSERT_FIELDS])
    return cur.lastrowid


def tx_by_uid(conn, uid: str):
    """El movimiento que ya se guardó con ese identificador, si existe."""
    return conn.execute(TX_SELECT + " WHERE t.client_uid = ?", (uid,)).fetchone()


def update_tx(conn, tx_id: int, **fields) -> None:
    conn.execute(f"UPDATE transactions SET {', '.join(f + ' = ?' for f in TX_FIELDS)} WHERE id = ?",
                 [fields[f] for f in TX_FIELDS] + [tx_id])


def delete_tx(conn, tx_id: int) -> None:
    conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))


def usage_counts(conn) -> tuple[dict[int, int], dict[int, int]]:
    """Cuántas veces se ha usado cada categoría y cada sobre (para ordenar chips).

    Los últimos 120 días pesan más que el historial antiguo.
    """
    cats = {r[0]: r[1] for r in conn.execute(
        "SELECT category_id, SUM(CASE WHEN date >= date('now', '-120 days') THEN 10 ELSE 1 END) "
        "FROM transactions WHERE category_id IS NOT NULL GROUP BY category_id")}
    envs = {r[0]: r[1] for r in conn.execute(
        "SELECT envelope_id, SUM(CASE WHEN date >= date('now', '-120 days') THEN 10 ELSE 1 END) "
        "FROM transactions WHERE envelope_id IS NOT NULL GROUP BY envelope_id")}
    return cats, envs


# ---------------------------------------------------------------- cuadre

def last_check(conn, account_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM account_checks WHERE account_id = ? "
                        "ORDER BY date DESC, id DESC LIMIT 1", (account_id,)).fetchone()


def checks(conn, account_id: int | None = None, limit: int = 6) -> list[sqlite3.Row]:
    sql = "SELECT k.*, a.name AS account FROM account_checks k LEFT JOIN accounts a ON a.id = k.account_id"
    args: list = []
    if account_id:
        sql += " WHERE k.account_id = ?"
        args.append(account_id)
    return conn.execute(sql + " ORDER BY k.date DESC, k.id DESC LIMIT ?", args + [limit]).fetchall()


def add_check(conn, account_id: int, day: date, balance: int) -> None:
    conn.execute("INSERT INTO account_checks (account_id, date, balance) VALUES (?, ?, ?)",
                 (account_id, day.isoformat(), balance))
