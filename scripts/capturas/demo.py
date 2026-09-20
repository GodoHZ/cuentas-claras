"""Crea una base de datos de ejemplo (nunca la de verdad) para las capturas."""
import os, sys
sys.path.insert(0, "/app")
os.environ["DATA_DIR"] = "/demo"
from app import db, repo, seed, calc

RUTA = "/demo/finanzas.db"
db.init_db(RUTA)
conn = db.connect(RUTA)
seed.seed(conn)
with conn:
    repo.set_setting(conn, "start_month", "2026-08-01")   # para que se vean las sobras de agosto
cats = {r["name"]: r["id"] for r in repo.categories(conn)}
envs = {r["name"]: r["id"] for r in repo.envelopes(conn)}
accs = {r["name"]: r["id"] for r in repo.accounts(conn)}
MOV = [
    ("2026-09-01", "ingreso", "Nómina", None, "Cuenta corriente", "Nómina de septiembre", 150000),
    ("2026-09-02", "gasto", "Casa", None, "Cuenta corriente", "Para casa", 20000),
    ("2026-09-03", "aporte_sobre", None, "Coche", "Cuenta de ahorro", "Aporte del mes", 10000),
    ("2026-09-03", "aporte_sobre", None, "Colchón", "Cuenta de ahorro", "Lo que sobró de agosto", 25000),
    ("2026-09-05", "gasto", "Transporte", None, "Cuenta corriente", "Repsol", 6500),
    ("2026-09-07", "gasto", "Ocio", None, "Cuenta corriente", "Cena con amigos", 3450),
    ("2026-09-11", "gasto", "Ocio", None, "Cuenta corriente", "Cine", 2400),
    ("2026-09-12", "gasto", "Suscripciones", None, "Cuenta corriente", "Spotify y iCloud", 1899),
    ("2026-09-14", "gasto", "Ocio", None, "Cuenta corriente", "Concierto", 7000),
    ("2026-09-15", "gasto", "Otros gastos", "Coche", "Cuenta de ahorro", "Cambio de aceite", 8900),
    ("2026-09-18", "gasto", "Transporte", None, "Cuenta corriente", "Cepsa", 4200),
    ("2026-08-01", "ingreso", "Nómina", None, "Cuenta corriente", "Nómina de agosto", 150000),
    ("2026-08-04", "gasto", "Casa", None, "Cuenta corriente", "Para casa", 20000),
    ("2026-08-09", "gasto", "Ocio", None, "Cuenta corriente", "Escapada", 32000),
    ("2026-08-20", "aporte_sobre", None, "Vacaciones", "Cuenta de ahorro", "Aporte", 30000),
    ("2026-10-01", "ingreso", "Nómina", None, "Cuenta corriente", "Nómina de octubre", 150000),
    ("2026-10-02", "gasto", "Casa", None, "Cuenta corriente", "Alquiler", 50000),
    ("2026-10-03", "aporte_sobre", None, "Coche", "Cuenta de ahorro", "Aporte del mes", 10000),
    ("2026-10-05", "gasto", "Ocio", None, "Cuenta corriente", "Escape room", 9000),
]
with conn:
    for fecha, tipo, cat, env, acc, concepto, importe in MOV:
        repo.insert_tx(conn, date=fecha, type=tipo, category_id=cats.get(cat), envelope_id=envs.get(env),
                       account_id=accs[acc], concept=concepto, amount=importe)
    repo.add_check(conn, __import__("datetime").date(2026, 9, 19), 336470)
print("demo lista:", sum(calc.envelope_balances(repo.all_txs(conn)).values()))
conn.close()
