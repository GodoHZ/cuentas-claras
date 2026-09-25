"""Base de datos SQLite: conexión y esquema.

Importes en céntimos. Categorías, sobres y cuentas se referencian por id,
así que renombrarlos no rompe el historial.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

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
    -- en qué cuenta vive el dinero de este sobre
    account_id INTEGER REFERENCES accounts(id),
    position INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    -- lo que había en la cuenta el día que la creaste: el saldo se calcula
    -- sumando a esto todos los movimientos
    initial_balance INTEGER NOT NULL DEFAULT 0,
    position        INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    type        TEXT NOT NULL CHECK (type IN ('ingreso', 'gasto', 'aporte_sobre', 'retiro_sobre', 'traspaso')),
    category_id INTEGER REFERENCES categories(id),
    envelope_id INTEGER REFERENCES envelopes(id),
    account_id  INTEGER NOT NULL REFERENCES accounts(id),
    -- la otra cuenta cuando el dinero cambia de sitio: de dónde sale en un
    -- traspaso o un aporte, a dónde va en un retiro. Vacío = solo etiqueta.
    other_account_id INTEGER REFERENCES accounts(id),
    concept     TEXT NOT NULL DEFAULT '',
    amount      INTEGER NOT NULL CHECK (amount > 0),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- identificador que pone el móvil al apuntar sin conexión: si el envío se
    -- repite, el movimiento no se duplica (ver el índice de más abajo)
    client_uid  TEXT,
    CHECK (type NOT IN ('aporte_sobre', 'retiro_sobre') OR envelope_id IS NOT NULL),
    CHECK (type NOT IN ('ingreso', 'gasto') OR category_id IS NOT NULL),
    CHECK (type <> 'traspaso' OR (other_account_id IS NOT NULL AND other_account_id <> account_id))
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

-- saldos reales apuntados a mano de vez en cuando, para comprobar que la app
-- y el banco dicen lo mismo
CREATE TABLE IF NOT EXISTS account_checks (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER REFERENCES accounts(id),
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

    if "other_account_id" not in columnas:                 # versión 2 -> 3
        _a_la_version_3(conn)


def _a_la_version_3(conn) -> None:
    """Cuentas con saldo de verdad: traspasos, sobres con cuenta y cuadre por cuenta.

    La tabla de movimientos hay que rehacerla: SQLite no sabe cambiar un CHECK
    (el de los tipos, que ahora admite 'traspaso'), así que se crea la nueva, se
    copia lo que había y se cambia el nombre. Todo dentro de la transacción que
    abre init_db, así que o sale entero o no cambia nada.
    """
    columnas_cuentas = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "initial_balance" not in columnas_cuentas:
        conn.execute("ALTER TABLE accounts ADD COLUMN initial_balance INTEGER NOT NULL DEFAULT 0")
    columnas_sobres = {r["name"] for r in conn.execute("PRAGMA table_info(envelopes)")}
    if "account_id" not in columnas_sobres:
        conn.execute("ALTER TABLE envelopes ADD COLUMN account_id INTEGER REFERENCES accounts(id)")
        # los sobres pasan a vivir en la cuenta que estuviera marcada para ellos
        fila = conn.execute("SELECT value FROM settings WHERE key = 'envelope_account_id'").fetchone()
        if fila and fila[0]:
            conn.execute("UPDATE envelopes SET account_id = ?", (int(fila[0]),))

    conn.execute("ALTER TABLE transactions RENAME TO transactions_v2")
    conn.executescript(SCHEMA)                             # crea la tabla nueva (y lo que falte)
    viejas = [r["name"] for r in conn.execute("PRAGMA table_info(transactions_v2)")]
    comunes = ", ".join(c for c in viejas if c != "other_account_id")
    conn.execute(f"INSERT INTO transactions ({comunes}) SELECT {comunes} FROM transactions_v2")
    conn.execute("DROP TABLE transactions_v2")

    tablas = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "tr_checks" in tablas:
        if "account_checks" not in tablas:
            conn.executescript(SCHEMA)
        cuenta = conn.execute("SELECT value FROM settings WHERE key = 'envelope_account_id'").fetchone()
        conn.execute("INSERT INTO account_checks (account_id, date, balance, created_at) "
                     "SELECT ?, date, balance, created_at FROM tr_checks",
                     (int(cuenta[0]) if cuenta and cuenta[0] else None,))
        conn.execute("DROP TABLE tr_checks")


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
