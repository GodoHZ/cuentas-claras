"""Base de datos SQLite: conexión y esquema.

Importes en céntimos. Categorías, sobres y cuentas se referencian por id,
así que renombrarlos no rompe el historial.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 12

SCHEMA = """
-- Reglas propias para lo que trae el banco: si el concepto contiene «pattern», va a esa
-- categoría (y, si se elige, a ese sobre). Mandan sobre lo que la app aprende sola.
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY,
    pattern     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'gasto' CHECK (kind IN ('gasto', 'ingreso')),
    category_id INTEGER REFERENCES categories(id),
    envelope_id INTEGER REFERENCES envelopes(id),
    position    INTEGER NOT NULL DEFAULT 0
);

-- Lo que te deben: pagaste tú (un Airbnb, una cena) y alguien te lo devolverá. Cuando llega un
-- ingreso por ese importe, se da por cobrado (settled_tx_id es ese ingreso).
CREATE TABLE IF NOT EXISTS owed (
    id            INTEGER PRIMARY KEY,
    person        TEXT NOT NULL,
    concept       TEXT NOT NULL DEFAULT '',
    amount        INTEGER NOT NULL CHECK (amount > 0),
    date          TEXT NOT NULL,
    settled_tx_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    settled_date  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Lo que el banco tiene retenido y aún no ha contabilizado (un pago con tarjeta de hace un rato). Se
-- rehace entero en cada sincronización: cuando el banco lo contabiliza, deja de estar aquí y llega
-- como un movimiento normal. No se exporta: es solo lo que dijo el banco la última vez.
CREATE TABLE IF NOT EXISTS bank_pending (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,
    amount     INTEGER NOT NULL,              -- con signo: negativo, sale dinero
    concept    TEXT NOT NULL DEFAULT '',
    seen_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Etiquetas libres para juntar gastos de varias categorías: un viaje («Roma 2026»), la Navidad...
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS tx_tags (
    tx_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (tx_id, tag_id)
);
CREATE INDEX IF NOT EXISTS ix_tx_tags_tag ON tx_tags(tag_id);

-- La factura o el ticket de un movimiento, en Paperless (o en cualquier otro sitio: es un enlace)
CREATE TABLE IF NOT EXISTS tx_docs (
    id          INTEGER PRIMARY KEY,
    tx_id       INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    paperless_id INTEGER,                 -- el número del documento, si es de Paperless
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (tx_id, url)
);

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
    -- 1 = para algo puntual (un viaje): cuando se gasta, la app ofrece archivarlo
    temporary INTEGER NOT NULL DEFAULT 0,
    -- para cuándo quieres llegar a la meta ('2027-07'); vacío = sin fecha
    target_date TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    -- lo que había en la cuenta el día que la creaste: el saldo se calcula
    -- sumando a esto todos los movimientos
    initial_balance INTEGER NOT NULL DEFAULT 0,
    -- ¿su saldo cuenta en el «puedes gastar» del panel? Se apaga en las cuentas
    -- que no son para el día a día (una tarjeta de comida, la de la empresa...)
    spendable       INTEGER NOT NULL DEFAULT 1,
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
    -- de dónde viene: vacío = a mano; 'banco' = lo trajo la conexión con el banco
    source      TEXT,
    -- 1 = la categoría es una suposición y conviene mirarla. Se apaga al
    -- guardar el movimiento desde la app
    review      INTEGER NOT NULL DEFAULT 0,
    -- el mes al que cuenta ('2026-11'), si no es el de su fecha: la nómina que llega
    -- el 28 es para el mes siguiente. Vacío = el de la fecha
    month       TEXT,
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
    -- cuántas cuotas tiene; vacío = sin fin (una suscripción, un seguro que se renueva...)
    n_installments INTEGER CHECK (n_installments IS NULL OR n_installments > 0),
    -- qué es: un préstamo (cuenta para «cuotas / nómina»), un seguro, una suscripción u otro pago fijo
    kind           TEXT NOT NULL DEFAULT 'prestamo' CHECK (kind IN ('prestamo', 'seguro', 'suscripcion', 'otro')),
    -- con qué categoría apuntas la cuota: sirve para saber si la del mes ya
    -- está apuntada y descontarla de lo previsto
    category_id    INTEGER REFERENCES categories(id),
    position       INTEGER NOT NULL DEFAULT 0
);

-- conexiones con bancos (Enable Banking). Una por banco; se renueva cada pocos
-- meses porque el banco obliga a volver a dar permiso
CREATE TABLE IF NOT EXISTS bank_links (
    id            INTEGER PRIMARY KEY,
    aspsp_name    TEXT NOT NULL,
    aspsp_country TEXT NOT NULL,
    session_id    TEXT,                  -- vacío mientras el banco no ha dicho que sí
    state         TEXT,                  -- para reconocer la vuelta desde el banco
    valid_until   TEXT,                  -- hasta cuándo vale el permiso
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_sync     TEXT,
    last_error    TEXT,
    UNIQUE (aspsp_name, aspsp_country)
);

-- cada cuenta del banco y en qué cuenta de la app se apunta
CREATE TABLE IF NOT EXISTS bank_accounts (
    ident      TEXT PRIMARY KEY,         -- identification_hash: no cambia al renovar el permiso
    link_id    INTEGER NOT NULL REFERENCES bank_links(id) ON DELETE CASCADE,
    uid        TEXT,                     -- el identificador de la sesión actual (cambia al renovar)
    name       TEXT NOT NULL DEFAULT '',
    iban       TEXT NOT NULL DEFAULT '',
    account_id INTEGER REFERENCES accounts(id),   -- vacío = no importar esta cuenta
    since      TEXT NOT NULL             -- desde qué fecha se traen movimientos
);

-- movimientos del banco ya vistos. Sobrevive al borrado del movimiento (tx_id
-- pasa a vacío): si borras algo que vino del banco, no vuelve a aparecer
CREATE TABLE IF NOT EXISTS bank_refs (
    ref        TEXT PRIMARY KEY,
    tx_id      INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    account_id INTEGER REFERENCES accounts(id),
    seen_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_bank_refs_tx ON bank_refs(tx_id);

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

    columnas_prestamos = {r["name"] for r in conn.execute("PRAGMA table_info(loans)")}
    if columnas_prestamos and "category_id" not in columnas_prestamos:    # versión 3 -> 4
        conn.execute("ALTER TABLE loans ADD COLUMN category_id INTEGER REFERENCES categories(id)")

    columnas_cuentas = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if columnas_cuentas and "spendable" not in columnas_cuentas:          # versión 4 -> 5
        conn.execute("ALTER TABLE accounts ADD COLUMN spendable INTEGER NOT NULL DEFAULT 1")

    columnas = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "source" not in columnas:                                          # versión 5 -> 6
        conn.execute("ALTER TABLE transactions ADD COLUMN source TEXT")
    if "review" not in columnas:
        conn.execute("ALTER TABLE transactions ADD COLUMN review INTEGER NOT NULL DEFAULT 0")
    if "month" not in columnas:                                           # versión 7 -> 8
        conn.execute("ALTER TABLE transactions ADD COLUMN month TEXT")

    columnas_prestamos = {r["name"] for r in conn.execute("PRAGMA table_info(loans)")}
    if columnas_prestamos and "kind" not in columnas_prestamos:            # versión 8 -> 9
        _a_la_version_9(conn)
    columnas_sobres = {r["name"] for r in conn.execute("PRAGMA table_info(envelopes)")}
    if columnas_sobres and "temporary" not in columnas_sobres:
        conn.execute("ALTER TABLE envelopes ADD COLUMN temporary INTEGER NOT NULL DEFAULT 0")
    if columnas_sobres and "target_date" not in columnas_sobres:           # versión 11 -> 12
        conn.execute("ALTER TABLE envelopes ADD COLUMN target_date TEXT")


def _a_la_version_9(conn) -> None:
    """Los préstamos pasan a ser «pagos fijos»: con tipo y, si no tienen fin, sin número de cuotas.

    Como en la versión 3, hay que rehacer la tabla (el número de cuotas era obligatorio y SQLite no
    sabe quitar un NOT NULL). Los que se llaman «seguro…» pasan a ser seguros.
    """
    conn.execute("ALTER TABLE loans RENAME TO loans_v8")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO loans (id, name, installment, first_date, n_installments, category_id, position) "
                 "SELECT id, name, installment, first_date, n_installments, category_id, position FROM loans_v8")
    conn.execute("UPDATE loans SET kind = 'seguro' WHERE name LIKE 'seguro%' COLLATE NOCASE")
    conn.execute("DROP TABLE loans_v8")


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
