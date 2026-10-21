"""Adelantar lo que ya sabes que va a salir: las cuotas y los aportes del mes.

A principio de mes la nómina está entera y las cuotas todavía no han pasado por
el banco, así que el «libre» engaña. Esto lo descuenta antes de que te lo gastes.
"""
from datetime import date

from app import calc, repo, views
from conftest import ids


# ---------------------------------------------------------------- las reglas

def test_la_cuota_que_toca_cada_mes():
    primera = date(2026, 10, 15)
    assert calc.installment_in_month(primera, 3, 2026, 9) is None        # aún no ha empezado
    assert calc.installment_in_month(primera, 3, 2026, 10) == date(2026, 10, 15)
    assert calc.installment_in_month(primera, 3, 2026, 12) == date(2026, 12, 15)
    assert calc.installment_in_month(primera, 3, 2027, 1) is None        # ya se terminó
    # si el día no existe en ese mes, el último (igual que el resto de la app)
    assert calc.installment_in_month(date(2026, 1, 31), 3, 2026, 2) == date(2026, 2, 28)


def test_lo_que_queda_por_apuntar_nunca_es_negativo():
    entera = calc.Upcoming(calc.CUOTA, "Moto", 18000, 0, 15)
    assert entera.pending == 18000 and not entera.paid and not entera.partial
    a_medias = calc.Upcoming(calc.CUOTA, "Moto", 18000, 10000, 15)
    assert a_medias.pending == 8000 and a_medias.partial and not a_medias.paid
    de_mas = calc.Upcoming(calc.CUOTA, "Moto", 18000, 30000, 15)
    assert de_mas.pending == 0 and de_mas.paid        # pagar de más no da crédito


def test_lo_que_tienes_menos_lo_previsto():
    f = calc.Forecast(100000, (calc.Upcoming(calc.CUOTA, "Moto", 18000, 0, 15),
                               calc.Upcoming(calc.APORTE_MENSUAL, "Ahorro", 20000, 20000)))
    assert f.pending_total == 18000                  # el aporte ya está hecho
    assert f.left == 100000 - 18000
    assert f.expected_total == 38000
    assert [u.name for u in f.pending] == ["Moto"]


def test_los_aportes_del_mes_se_cuentan_por_sobre():
    txs = [calc.Tx(date(2026, 10, 2), calc.APORTE, 10000, None, 7, 1),
           calc.Tx(date(2026, 10, 9), calc.APORTE, 5000, None, 7, 1),
           calc.Tx(date(2026, 10, 9), calc.RETIRO, 3000, None, 7, 1),       # un retiro no descuenta
           calc.Tx(date(2026, 11, 2), calc.APORTE, 9000, None, 7, 1)]       # ni otro mes
    assert calc.contributed_by_envelope(txs, 2026, 10) == {7: 15000}


# ---------------------------------------------------------------- con datos

def escenario(conn):
    """Octubre: 300 € de antes, nómina de 1.500 €, una cuota el día 15 y 200 € al colchón."""
    cats, envs, accs = ids(conn)
    with conn:
        conn.execute("UPDATE accounts SET initial_balance = 30000 WHERE id = ?",
                     (accs["Cuenta corriente"],))                           # lo que traías de antes
        conn.execute("UPDATE envelopes SET monthly = NULL")                 # solo uno con aporte
        conn.execute("UPDATE envelopes SET monthly = 20000 WHERE id = ?", (envs["Colchón"],))
        conn.execute("INSERT INTO categories (name, kind) VALUES ('Préstamo moto', 'gasto')")
        conn.execute("INSERT INTO loans (name, installment, first_date, n_installments, category_id) "
                     "VALUES ('Moto', 18000, '2026-10-15', 48, "
                     "        (SELECT id FROM categories WHERE name = 'Préstamo moto'))")
        repo.insert_tx(conn, date="2026-10-01", type=calc.INGRESO, category_id=cats["Nómina"],
                       envelope_id=None, account_id=accs["Cuenta corriente"],
                       concept="Nómina", amount=150000)


def previsto(conn, dia=1):
    return views.forecast(conn, repo.all_txs(conn), 2026, 10, date(2026, 10, dia))


def test_se_parte_del_dinero_que_tienes_no_de_la_nomina(conn):
    escenario(conn)
    f = previsto(conn)
    # 300 € de antes + 1.500 € de nómina: el dinero que tienes, no lo que has cobrado
    assert f.available == 180000
    assert calc.month_summary(repo.all_txs(conn), 2026, 10).free == 150000     # eso es otra cosa
    assert f.pending_total == 18000 + 20000
    assert f.left == 180000 - 38000                         # con esto se puede contar de verdad
    assert [(u.name, u.pending, u.day) for u in f.pending] == [("Moto", 18000, 15), ("Colchón", 20000, None)]


def test_lo_apartado_en_sobres_no_se_puede_gastar(conn):
    escenario(conn)
    _, envs, accs = ids(conn)
    with conn:                                              # 500 € de la corriente al sobre
        repo.insert_tx(conn, date="2026-10-02", type=calc.APORTE, category_id=None,
                       envelope_id=envs["Vacaciones"], account_id=accs["Cuenta de ahorro"],
                       other_account_id=accs["Cuenta corriente"], concept="Para el verano", amount=50000)
    f = previsto(conn, dia=2)
    assert f.available == 180000 - 50000                    # el dinero sigue siendo tuyo, pero tiene nombre
    assert views.available_money(conn, repo.all_txs(conn)) == 130000


def test_una_cuenta_puede_quedarse_fuera_de_la_suma(conn):
    """Una tarjeta de comida es dinero tuyo, pero no es para el día a día."""
    escenario(conn)
    with conn:
        conn.execute("INSERT INTO accounts (name, initial_balance, spendable) VALUES ('Comida', 10000, 0)")
    assert views.available_money(conn, repo.all_txs(conn)) == 180000       # esos 100 € no suman
    assert previsto(conn).available == 180000
    with conn:
        conn.execute("UPDATE accounts SET spendable = 1 WHERE name = 'Comida'")
    assert views.available_money(conn, repo.all_txs(conn)) == 190000       # y si la cuentas, sí


def test_un_sobre_de_una_cuenta_que_no_suma_no_resta(conn):
    """Si la cuenta no entra en la suma, lo que tenga apartado tampoco debe salir de ella."""
    escenario(conn)
    with conn:
        conn.execute("INSERT INTO accounts (name, initial_balance, spendable) VALUES ('Comida', 10000, 0)")
        comida = conn.execute("SELECT id FROM accounts WHERE name = 'Comida'").fetchone()[0]
        conn.execute("INSERT INTO envelopes (name, account_id) VALUES ('Dietas', ?)", (comida,))
        sobre = conn.execute("SELECT id FROM envelopes WHERE name = 'Dietas'").fetchone()[0]
        repo.insert_tx(conn, date="2026-10-02", type=calc.APORTE, category_id=None, envelope_id=sobre,
                       account_id=comida, concept="Lo de las dietas", amount=5000)
    assert views.available_money(conn, repo.all_txs(conn)) == 180000       # ni suma ni resta


def test_al_apuntar_la_cuota_deja_de_estar_prevista(conn):
    escenario(conn)
    cats, _, accs = ids(conn)
    with conn:
        repo.insert_tx(conn, date="2026-10-15", type=calc.GASTO, category_id=cats["Préstamo moto"],
                       envelope_id=None, account_id=accs["Cuenta corriente"],
                       concept="Cuota de la moto", amount=18000)
    f = previsto(conn, dia=15)
    assert f.pending_total == 20000                         # solo queda el aporte
    cuota = next(u for u in f.lines if u.name == "Moto")
    assert cuota.paid and cuota.done == 18000
    # la cuota ya ha salido de la cuenta: no se descuenta dos veces
    assert f.available == 180000 - 18000
    assert f.left == 180000 - 18000 - 20000


def test_un_aporte_a_medias_solo_deja_pendiente_lo_que_falta(conn):
    escenario(conn)
    _, envs, accs = ids(conn)
    with conn:
        repo.insert_tx(conn, date="2026-10-05", type=calc.APORTE, category_id=None,
                       envelope_id=envs["Colchón"], account_id=accs["Cuenta de ahorro"],
                       concept="A cuenta", amount=5000)
    colchon = next(u for u in previsto(conn, dia=5).lines if u.name == "Colchón")
    assert colchon.pending == 15000 and colchon.partial


def test_sin_categoria_la_cuota_se_da_por_pagada_al_pasar_su_dia(conn):
    escenario(conn)
    with conn:
        conn.execute("UPDATE loans SET category_id = NULL")
    assert next(u for u in previsto(conn, dia=15).lines if u.name == "Moto").pending == 18000
    assert next(u for u in previsto(conn, dia=16).lines if u.name == "Moto").paid


def test_un_mes_sin_cuota_no_se_la_inventa(conn):
    escenario(conn)
    septiembre = views.forecast(conn, repo.all_txs(conn), 2026, 9, date(2026, 9, 30))
    assert [u.name for u in septiembre.lines] == ["Colchón"]      # el préstamo empieza en octubre


def test_deudas_avisa_de_la_cuota_sin_apuntar(conn):
    escenario(conn)
    d = views.debts(conn, repo.all_txs(conn), date(2026, 10, 1))
    assert d["month_total"] == 18000 and d["month_pending"] == 18000
    assert d["loans"][0]["month"].day == 15
    assert d["loans"][0]["category"]["name"] == "Préstamo moto"
    # en septiembre ese préstamo todavía no existía
    assert views.debts(conn, repo.all_txs(conn), date(2026, 9, 30))["month_total"] == 0


def test_el_panel_ensena_lo_que_quedara(db_path, tmp_path):
    from fastapi.testclient import TestClient

    from app import db as database
    from app import main
    from app import seed as semilla
    conn = database.connect(db_path)
    semilla.seed(conn)
    escenario(conn)
    conn.close()
    app = main.create_app(db_path=db_path, backup_dir=tmp_path / "backups", backups=False,
                          today_fn=lambda: date(2026, 10, 1))
    with TestClient(app) as client:
        html = client.get("/").text
    assert "Previsto este mes" in html
    assert "Puedes gastar" in html
    assert "1.420,00" in html                       # 1.800 que tienes − 180 − 200
    assert "se carga el día 15" in html
    assert "Libre de este mes" in html and "1.500,00" in html      # el mes, aparte y sin protagonismo
    assert "Cuenta corriente" in html and "no suma" not in html
