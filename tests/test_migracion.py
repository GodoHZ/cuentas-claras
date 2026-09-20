"""Que una base de datos vieja siga abriendo después de cambiar el esquema."""
import sqlite3

from app import db, repo, seed

ESQUEMA_V1 = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'gasto', budget INTEGER, position INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE envelopes (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    target INTEGER, monthly INTEGER, note TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    position INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE transactions (id INTEGER PRIMARY KEY, date TEXT NOT NULL, type TEXT NOT NULL,
    category_id INTEGER, envelope_id INTEGER, account_id INTEGER NOT NULL,
    concept TEXT NOT NULL DEFAULT '', amount INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
CREATE TABLE loans (id INTEGER PRIMARY KEY, name TEXT NOT NULL, installment INTEGER NOT NULL,
    first_date TEXT NOT NULL, n_installments INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE tr_checks (id INTEGER PRIMARY KEY, date TEXT NOT NULL, balance INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
PRAGMA user_version = 1;
"""


def test_una_base_de_datos_de_la_version_1_se_actualiza_sola(tmp_path):
    ruta = tmp_path / "vieja.db"
    vieja = sqlite3.connect(ruta)
    vieja.executescript(ESQUEMA_V1)
    vieja.execute("INSERT INTO accounts (name) VALUES ('Cuenta corriente')")
    vieja.execute("INSERT INTO categories (name) VALUES ('Ocio')")
    vieja.execute("INSERT INTO transactions (date, type, category_id, account_id, concept, amount) "
                  "VALUES ('2026-09-01', 'gasto', 1, 1, 'Cena de antes', 2550)")
    vieja.commit()
    vieja.close()

    db.init_db(ruta)                                     # esto es lo que hace la app al arrancar

    conn = db.connect(ruta)
    try:
        columnas = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
        assert "client_uid" in columnas
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert len(repo.all_txs(conn)) == 1              # lo de antes sigue ahí
        assert repo.tx_rows(conn)[0]["concept"] == "Cena de antes"
        seed.seed(conn)                                  # y la app puede seguir trabajando
        repo.insert_tx(conn, date="2026-09-20", type="gasto", category_id=1, envelope_id=None,
                       account_id=1, concept="Cena de ahora", amount=1000, client_uid="nuevo")
        conn.commit()
        assert repo.tx_by_uid(conn, "nuevo")["concept"] == "Cena de ahora"
    finally:
        conn.close()
