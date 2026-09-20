"""Base de datos SQLite: conexión y esquema.

Importes en céntimos. Categorías, sobres y cuentas se referencian por id,
así que renombrarlos no rompe el historial.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind     TEXT NOT NULL DEFAULT 'gasto' CHECK (kind IN ('gasto', 'ingreso')),
    budget   INTEGER CHECK (budget IS NULL OR budget >= 0),
    position INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS envelopes (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    target   INTEGER CHECK (target IS NULL OR target > 0),
    monthly  INTEGER CHECK (monthly IS NULL OR monthly > 0),
    note     TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS accounts (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    position INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    type        TEXT NOT NULL CHECK (type IN ('ingreso', 'gasto', 'aporte_sobre', 'retiro_sobre')),
    category_id INTEGER REFERENCES categories(id),
    envelope_id INTEGER REFERENCES envelopes(id),
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    concept     TEXT NOT NULL DEFAULT '',
    amount      INTEGER NOT NULL CHECK (amount > 0),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- identificador que pone el móvil al apuntar sin conexión: si el envío se
    -- repite, el movimiento no se duplica (ver el índice de más abajo)
    client_uid  TEXT,
    CHECK (type NOT IN ('aporte_sobre', 'retiro_sobre') OR envelope_id IS NOT NULL),
    CHECK (type NOT IN ('ingreso', 'gasto') OR category_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_transactions_envelope ON transactions(envelope_id);
CREATE INDEX IF NOT EXISTS ix_transactions_category ON transactions(category_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_uid ON transactions(client_uid) WHERE client_uid IS NOT NULL;

CREATE TABLE IF NOT EXISTS loans (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    installment    INTEGER NOT NULL CHECK (installment > 0),
    first_date     TEXT NOT NULL,
    n_installments INTEGER NOT NULL CHECK (n_installments > 0),
    position       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tr_checks (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    balance    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI puede abrir y usar la conexión en hilos distintos
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrar(conn) -> None:
    """Cambios de esquema sobre bases de datos que ya existían.

    Se ejecuta ANTES de crear el esquema: si no, los índices nuevos fallarían
    al apoyarse en columnas que la tabla vieja todavía no tiene.
    """
    existe = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()
    if not existe:
        return                                             # base de datos nueva: la crea el esquema
    columnas = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "client_uid" not in columnas:                       # versión 1 -> 2
        conn.execute("ALTER TABLE transactions ADD COLUMN client_uid TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_uid "
                     "ON transactions(client_uid) WHERE client_uid IS NOT NULL")


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        migrar(conn)
        conn.executescript(SCHEMA)
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
