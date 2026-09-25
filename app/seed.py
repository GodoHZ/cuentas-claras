"""Datos iniciales de una instalación nueva: un esqueleto de ejemplo.

Son categorías y sobres corrientes para que la app no arranque vacía; se cambian
todos desde Ajustes. **No hay ningún movimiento**: el historial lo escribes tú.

Idempotente: se puede ejecutar las veces que se quiera sin duplicar nada.
- La app lo lanza sola al crear una base de datos nueva.
- Deja la marca `seed_version` en settings; mientras exista, no vuelve a tocar
  nada (así, si renombras "Colchón", no reaparece el nombre viejo).
- Aun forzándolo (--forzar), cada elemento se busca antes de insertarlo.

Uso manual:  python -m app.seed [--forzar]
"""

from __future__ import annotations

import sys

from . import config, db, repo

SEED_VERSION = "1"

SETTINGS = {
    "salary": 150000,              # nómina de referencia de ejemplo: 1.500 €
}

# nombre | tipo | presupuesto mensual en céntimos (None = sin tope)
CATEGORIES = [
    ("Casa", "gasto", 50000),
    ("Compra", "gasto", 30000),
    ("Transporte", "gasto", 10000),
    ("Comida fuera", "gasto", 10000),
    ("Ocio", "gasto", 15000),
    ("Suscripciones", "gasto", 2000),
    ("Salud", "gasto", None),
    ("Ropa", "gasto", None),
    ("Regalos", "gasto", None),
    ("Amortización préstamo", "gasto", None),
    ("Otros gastos", "gasto", None),
    ("Nómina", "ingreso", None),
    ("Intereses", "ingreso", None),
    ("Otros ingresos", "ingreso", None),
]

# nombre | objetivo | aporte mensual previsto | nota
ENVELOPES = [
    ("Colchón", 300000, 15000, "Para imprevistos. Aquí van las sobras de cada mes"),
    ("Vacaciones", 120000, 10000, ""),
    ("Coche", None, 5000, "Seguro, revisiones y averías"),
]

ACCOUNTS = ["Cuenta corriente", "Cuenta de ahorro"]

LOANS = []          # los préstamos se añaden en Ajustes
TRANSACTIONS = []   # sin movimientos: el historial es tuyo
TR_CHECKS = []


def _id(conn, table: str, name: str | None) -> int | None:
    if name is None:
        return None
    row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def seed(conn, force: bool = False) -> dict:
    """Carga los datos iniciales. Devuelve cuántas cosas ha insertado."""
    if repo.setting(conn, "seed_version") and not force:
        return {"omitido": True}
    added = {"ajustes": 0, "categorías": 0, "sobres": 0, "cuentas": 0,
             "préstamos": 0, "movimientos": 0, "cuadres": 0}
    with conn:
        arranque = dict(SETTINGS, start_month=config.today().replace(day=1).isoformat())
        for key, value in arranque.items():
            if repo.setting(conn, key) is None:
                repo.set_setting(conn, key, value)
                added["ajustes"] += 1

        for pos, (name, kind, budget) in enumerate(CATEGORIES):
            if _id(conn, "categories", name) is None:
                conn.execute("INSERT INTO categories (name, kind, budget, position) VALUES (?, ?, ?, ?)",
                             (name, kind, budget, pos))
                added["categorías"] += 1

        for pos, name in enumerate(ACCOUNTS):
            if _id(conn, "accounts", name) is None:
                conn.execute("INSERT INTO accounts (name, position) VALUES (?, ?)", (name, pos))
                added["cuentas"] += 1

        cuenta_ahorro = _id(conn, "accounts", ACCOUNTS[-1])
        for pos, (name, target, monthly, note) in enumerate(ENVELOPES):
            if _id(conn, "envelopes", name) is None:
                conn.execute("INSERT INTO envelopes (name, target, monthly, note, account_id, position) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (name, target, monthly, note, cuenta_ahorro, pos))
                added["sobres"] += 1

        for pos, (name, installment, first, count) in enumerate(LOANS):
            if _id(conn, "loans", name) is None:
                conn.execute("INSERT INTO loans (name, installment, first_date, n_installments, position) "
                             "VALUES (?, ?, ?, ?, ?)", (name, installment, first, count, pos))
                added["préstamos"] += 1

        # referencias por id que la app necesita (se pueden cambiar en Ajustes)
        defaults = {
            "amortization_category_id": _id(conn, "categories", "Amortización préstamo"),
            "sweep_envelope_id": _id(conn, "envelopes", "Colchón"),        # ahí van las sobras del mes
            "default_account_id": _id(conn, "accounts", "Cuenta corriente"),
            "envelope_account_id": _id(conn, "accounts", "Cuenta de ahorro"),
        }
        for key, value in defaults.items():
            if repo.setting(conn, key) is None and value is not None:
                repo.set_setting(conn, key, value)
                added["ajustes"] += 1

        for day, type_, cat, env, acc, concept, amount in TRANSACTIONS:
            fields = dict(date=day, type=type_, category_id=_id(conn, "categories", cat),
                          envelope_id=_id(conn, "envelopes", env), account_id=_id(conn, "accounts", acc),
                          concept=concept, amount=amount)
            exists = conn.execute(
                "SELECT 1 FROM transactions WHERE date = ? AND type = ? AND amount = ? AND concept = ? "
                "AND envelope_id IS ?", (day, type_, amount, concept, fields["envelope_id"])).fetchone()
            if not exists:
                repo.insert_tx(conn, **fields)
                added["movimientos"] += 1

        for day, balance in TR_CHECKS:
            if not conn.execute("SELECT 1 FROM account_checks WHERE date = ? AND balance = ?",
                                (day, balance)).fetchone():
                conn.execute("INSERT INTO account_checks (date, balance) VALUES (?, ?)", (day, balance))
                added["cuadres"] += 1

        repo.set_setting(conn, "seed_version", SEED_VERSION)
    return added


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    db.init_db(config.DB_PATH)
    conn = db.connect(config.DB_PATH)
    try:
        result = seed(conn, force="--forzar" in argv)
    finally:
        conn.close()
    if result.get("omitido"):
        print("La base de datos ya tenía los datos iniciales: no se ha tocado nada. "
              "(Usa --forzar para añadir lo que falte sin duplicar.)")
    else:
        print("Datos iniciales cargados:", ", ".join(f"{k} {v}" for k, v in result.items()))


if __name__ == "__main__":
    main()
