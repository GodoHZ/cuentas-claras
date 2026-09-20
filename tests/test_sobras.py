"""Las sobras del mes se guardan solas en el colchón."""
from datetime import date

from app import calc, main, repo, views
from conftest import ids


def mes_con_sobras(conn, ingreso=150000, gasto=20000, aporte=10000):
    """Septiembre de 2026 con un resto claro: 1.500 − 200 − 100 = 1.200 €."""
    cats, envs, accs = ids(conn)
    with conn:
        repo.insert_tx(conn, date="2026-09-01", type=calc.INGRESO, category_id=cats["Nómina"],
                       envelope_id=None, account_id=accs["Cuenta corriente"], concept="Nómina", amount=ingreso)
        repo.insert_tx(conn, date="2026-09-02", type=calc.GASTO, category_id=cats["Casa"],
                       envelope_id=None, account_id=accs["Cuenta corriente"], concept="Casa", amount=gasto)
        repo.insert_tx(conn, date="2026-09-03", type=calc.APORTE, category_id=None,
                       envelope_id=envs["Coche"], account_id=accs["Cuenta de ahorro"],
                       concept="Aporte", amount=aporte)
    return ingreso - gasto - aporte


def test_calcula_lo_que_sobro_del_mes_cerrado(conn):
    sobra = mes_con_sobras(conn)
    hoy = date(2026, 10, 1)
    pending = views.pending_sweep(conn, repo.all_txs(conn), hoy)
    assert pending["amount"] == sobra == 120000
    assert (pending["year"], pending["month"]) == (2026, 9)
    assert pending["envelope"]["name"] == "Colchón"
    assert pending["due"] is True                       # el día 1 toca


def test_guardar_las_sobras_deja_el_mes_a_cero(conn):
    mes_con_sobras(conn)
    pending = views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 1))
    views.do_sweep(conn, pending)

    txs = repo.all_txs(conn)
    septiembre = calc.month_summary(txs, 2026, 9)
    assert septiembre.free == 0                         # ya no sobra nada: está guardado
    assert septiembre.saved == 10000 + 120000
    saldos = calc.envelope_balances(txs)
    envs = ids(conn)[1]
    assert saldos[envs["Colchón"]] == 120000
    fila = repo.tx_rows(conn, month=(2026, 9))[0]
    assert fila["date"] == "2026-09-30"                 # último día del mes barrido
    assert fila["concept"] == "Sobras de septiembre de 2026"
    assert fila["account"] == "Cuenta de ahorro"


def test_no_se_repite_ni_se_adelanta(conn):
    mes_con_sobras(conn)
    hoy = date(2026, 10, 1)
    views.do_sweep(conn, views.pending_sweep(conn, repo.all_txs(conn), hoy))
    assert views.pending_sweep(conn, repo.all_txs(conn), hoy) is None          # ya está hecho
    assert views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 20)) is None
    # y dentro del propio mes en curso no se toca nada
    assert views.pending_sweep(conn, repo.all_txs(conn), date(2026, 9, 25)) is None


def test_si_no_sobra_nada_no_hace_nada(conn):
    cats, envs, accs = ids(conn)
    with conn:                                          # mes en números rojos
        repo.insert_tx(conn, date="2026-09-01", type=calc.INGRESO, category_id=cats["Nómina"],
                       envelope_id=None, account_id=accs["Cuenta corriente"], concept="Nómina", amount=100000)
        repo.insert_tx(conn, date="2026-09-05", type=calc.GASTO, category_id=cats["Ocio"],
                       envelope_id=None, account_id=accs["Cuenta corriente"], concept="Fiesta", amount=120000)
    assert views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 1)) is None


def test_el_dia_elegido_manda(conn):
    mes_con_sobras(conn)
    with conn:
        repo.set_setting(conn, "sweep_day", 5)
    assert views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 2))["due"] is False
    assert views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 5))["due"] is True


def test_desactivado_no_se_guarda_solo_pero_se_puede_a_mano(conn, db_path, tmp_path):
    mes_con_sobras(conn)
    with conn:
        repo.set_setting(conn, "sweep_enabled", "0")
    pending = views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 1))
    assert pending["enabled"] is False and pending["due"] is False
    assert main.run_pending_sweep(db_path, date(2026, 10, 1)) is None          # la tarea no lo hace
    views.do_sweep(conn, pending)                                             # a mano sí
    assert calc.month_summary(repo.all_txs(conn), 2026, 9).free == 0


def test_la_tarea_de_fondo_lo_hace_sola(conn, db_path):
    mes_con_sobras(conn)
    conn.close()
    assert "sobras de septiembre" in main.run_pending_sweep(db_path, date(2026, 10, 1))
    assert main.run_pending_sweep(db_path, date(2026, 10, 1)) is None          # solo una vez


def test_desde_la_web(client, db_path):
    from app import db as database
    conn = database.connect(db_path)
    mes_con_sobras(conn)
    conn.close()
    html = client.get("/?mes=2026-10").text
    assert "Sobras de septiembre" not in html          # hoy es 19/09 en los tests: aún no ha cerrado

    r = client.post("/sobras", follow_redirects=True)
    assert "No hay sobras pendientes" in r.text        # no se puede barrer el mes en curso
