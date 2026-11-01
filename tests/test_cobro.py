"""El mes de nómina a nómina: empieza el día que entra la nómina y acaba el día antes de la siguiente.

Se llama como el mes que cubre casi entero (del 28/10 al 27/11 es noviembre). La fecha de cada
movimiento sigue siendo la real; lo que cambia es a qué «mes» cuenta.
"""
from datetime import date

from app import calc, db, repo, views
from conftest import apuntar, ids


# ---------------------------------------------------------------- las reglas

def test_los_meses_van_de_cobro_a_cobro():
    inicios = calc.payday_starts([date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 28), date(2026, 11, 27)])
    assert inicios == [date(2026, 9, 29), date(2026, 10, 28), date(2026, 11, 27)]    # el 1/10, pegado al 29/9
    ciclos = calc.cycles(inicios)
    assert [c.label for c in ciclos] == [(2026, 10), (2026, 11), (2026, 12)]
    assert ciclos[1].start == date(2026, 10, 28) and ciclos[1].end == date(2026, 11, 27)
    assert ciclos[-1].end is None                                                    # el mes en curso
    assert calc.period_of(date(2026, 10, 27), ciclos) == (2026, 10)
    assert calc.period_of(date(2026, 10, 28), ciclos) == (2026, 11)
    assert calc.period_of(date(2026, 9, 10), ciclos) == (2026, 9)                    # antes del primer cobro
    assert calc.period_range(2026, 11, ciclos) == (date(2026, 10, 28), date(2026, 11, 27))
    assert calc.period_range(2026, 9, ciclos) == (date(2026, 9, 1), date(2026, 9, 29))


def test_cobrar_a_principios_tambien_vale():
    ciclos = calc.cycles(calc.payday_starts([date(2026, 9, 1), date(2026, 10, 2), date(2026, 10, 31)]))
    assert [c.label for c in ciclos] == [(2026, 9), (2026, 10), (2026, 11)]


def test_dos_cobros_en_el_mismo_mes_no_se_pisan():
    ciclos = calc.cycles([date(2026, 10, 1), date(2026, 10, 24)])
    assert [c.label for c in ciclos] == [(2026, 10), (2026, 11)]


def test_el_proximo_cobro_se_estima():
    ciclos = calc.cycles([date(2026, 10, 28)])
    assert calc.next_payday_estimate(ciclos, date(2026, 11, 3)) == date(2026, 11, 28)
    assert calc.next_payday_estimate(ciclos, date(2026, 12, 1)) == date(2026, 12, 2)   # se retrasa: mañana
    assert calc.next_payday_estimate([], date(2026, 11, 3)) is None


# ---------------------------------------------------------------- con la base de datos

def test_todo_cuenta_de_nomina_a_nomina(conn):
    apuntar(conn, "2026-09-29", calc.INGRESO, 150000, categoria="Nómina")
    apuntar(conn, "2026-10-20", calc.GASTO, 30000, categoria="Casa")
    apuntar(conn, "2026-10-28", calc.INGRESO, 250000, categoria="Nómina")
    apuntar(conn, "2026-10-30", calc.GASTO, 5000, categoria="Casa")
    txs = repo.all_txs(conn)
    assert calc.month_summary(txs, 2026, 10).income == 150000
    assert calc.month_summary(txs, 2026, 10).payroll_spent == 30000
    assert calc.month_summary(txs, 2026, 11).income == 250000
    assert calc.month_summary(txs, 2026, 11).payroll_spent == 5000                 # el gasto del 30/10 ya es de noviembre
    assert [r["amount"] for r in repo.tx_rows(conn, month=(2026, 11), oldest_first=True)] == [250000, 5000]
    assert repo.period_of(conn, date(2026, 10, 30)) == (2026, 11)
    assert views.period_range(conn, 2026, 11)["desde"] == date(2026, 10, 28)


def test_con_meses_normales_todo_va_por_fecha(conn):
    with conn:
        repo.set_setting(conn, "month_start", "dia1")
    apuntar(conn, "2026-10-28", calc.INGRESO, 250000, categoria="Nómina")
    assert calc.month_summary(repo.all_txs(conn), 2026, 10).income == 250000
    assert views.period_range(conn, 2026, 10) is None


def test_el_dia_a_dia_hasta_el_proximo_cobro(conn):
    apuntar(conn, "2026-10-28", calc.INGRESO, 150000, categoria="Nómina")
    hoy = date(2026, 11, 3)
    previsto = views.forecast(conn, repo.all_txs(conn), *repo.period_of(conn, hoy), hoy)
    cobro = views.until_payday(conn, previsto.left, hoy)
    assert cobro["date"] == date(2026, 11, 28) and cobro["days"] == 25
    assert cobro["per_day"] == previsto.left // 25


def test_la_migracion_anade_el_mes(tmp_path):
    ruta = tmp_path / "vieja.db"
    conn = db.connect(ruta)
    conn.executescript(db.SCHEMA.replace("    month       TEXT,\n", ""))
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()
    db.init_db(ruta)
    conn = db.connect(ruta)
    assert "month" in {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    conn.close()


def test_desde_el_formulario_y_ajustes(client, db_path):
    r = client.post("/ajustes/general", data={"nomina": "1500", "mes_inicio": "2026-09", "inicio_mes": "nomina"})
    assert r.status_code in (200, 303)
    conn = db.connect(db_path)
    assert repo.month_mode(conn) == "nomina"
    cats, _, accs = ids(conn)
    conn.close()
    base = {"tipo": "ingreso", "importe": "1500", "categoria_id": cats["Nómina"], "sobre_id": "",
            "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-28", "concepto": "nómina"}
    client.post("/movimientos/nuevo", data=base)
    client.post("/movimientos/nuevo", data={**base, "tipo": "gasto", "categoria_id": cats["Casa"],
                                            "cuenta_mes": "+1", "concepto": "alquiler adelantado"})
    conn = db.connect(db_path)
    meses = {r["concept"]: r["month"] for r in repo.tx_rows(conn)}
    conn.close()
    # la nómina del 28/09 abre «octubre»; el alquiler, adelantado al siguiente: noviembre
    assert meses == {"nómina": None, "alquiler adelantado": "2026-11"}
    assert "¿Cuenta para otro mes?" in client.get("/nuevo").text
    assert "¿Cuándo empieza tu mes?" in client.get("/ajustes").text


# ---------------------------------------------------------------- lo que sobra antes de cobrar

def ciclo(conn, gasto=40000):
    """Cobras 1.500 € el 28/09, gastas y vuelves a cobrar el 28/10."""
    apuntar(conn, "2026-09-28", calc.INGRESO, 150000, categoria="Nómina")
    apuntar(conn, "2026-10-10", calc.GASTO, gasto, categoria="Casa")
    apuntar(conn, "2026-10-28", calc.INGRESO, 150000, categoria="Nómina")


def sin_previsto(conn):
    with conn:
        conn.execute("UPDATE envelopes SET monthly = NULL")
        conn.execute("DELETE FROM loans")


def test_los_dias_de_cobro(conn):
    apuntar(conn, "2026-09-28", calc.INGRESO, 150000, categoria="Nómina")
    apuntar(conn, "2026-10-01", calc.INGRESO, 22000, categoria="Nómina")      # la paga extra, el mismo cobro
    apuntar(conn, "2026-10-28", calc.INGRESO, 150000, categoria="Nómina")
    apuntar(conn, "2026-10-15", calc.INGRESO, 5000, categoria="Otros ingresos")
    assert views.paydays(conn) == [date(2026, 9, 28), date(2026, 10, 28)]


def test_lo_que_queda_antes_de_cobrar_va_al_ahorro(conn):
    sin_previsto(conn)
    ciclo(conn)
    with conn:
        repo.set_setting(conn, "last_sweep_payday", "2026-09-28")             # el ciclo anterior ya cerrado
    txs = repo.all_txs(conn)
    antes = views.forecast(conn, [t for t in txs if t.date < date(2026, 10, 28)], 2026, 10, date(2026, 10, 27)).left
    pending = views.pending_sweep(conn, txs, date(2026, 10, 28))
    assert pending["mode"] == "cobro" and pending["amount"] == antes > 0
    assert pending["due"] is False                                            # el mismo día del cobro, aún no
    pending = views.pending_sweep(conn, txs, date(2026, 10, 29))
    assert pending["due"] is True
    tx_id = views.do_sweep(conn, pending)
    tx = repo.get_tx(conn, tx_id)
    assert (tx["date"], tx["type"], tx["amount"], tx["envelope"]) == ("2026-10-27", calc.APORTE, antes, "Colchón")
    assert views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 30)) is None   # una sola vez
    cierre = views.last_close(conn, date(2026, 10, 30))
    assert cierre["amount"] == antes and cierre["saved"] and cierre["payday"] == date(2026, 10, 28)
    assert views.last_close(conn, date(2026, 11, 20)) is None                # a los diez días ya no se enseña


def test_si_no_queda_nada_se_cierra_sin_apuntar(conn):
    sin_previsto(conn)
    ciclo(conn, gasto=1_000_000)
    with conn:
        repo.set_setting(conn, "last_sweep_payday", "2026-09-28")
    pending = views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 29))
    assert pending["amount"] < 0
    assert views.do_sweep(conn, pending) is None
    assert views.last_close(conn, date(2026, 10, 29))["saved"] is False
    assert views.pending_sweep(conn, repo.all_txs(conn), date(2026, 10, 30)) is None


def test_al_arrancar_no_se_cierran_cobros_pasados(client, db_path):
    conn = db.connect(db_path)
    hecho = repo.setting(conn, "last_sweep_payday")
    conn.close()
    assert hecho                                                              # lo pone el arranque
    r = client.get("/")
    assert r.status_code == 200


def test_la_tarjeta_y_el_ajuste(client, db_path):
    conn = db.connect(db_path)
    with conn:
        repo.set_setting(conn, "month_start", "dia1")                         # con meses normales se puede elegir
    conn.close()
    r = client.post("/ajustes/sobras", data={"modo": "mes", "sobre": "", "dia": "1"})
    assert r.status_code in (200, 303)
    conn = db.connect(db_path)
    assert views.sweep_settings(conn)["mode"] == "mes"
    conn.close()
    assert "Lo que me queda en las cuentas antes de cobrar" in client.get("/ajustes").text


def test_un_ingreso_apartado_en_un_sobre(client, db_path):
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    conn.close()
    r = client.post("/movimientos/nuevo", data={
        "tipo": "ingreso", "importe": "153,47", "categoria_id": cats["Otros ingresos"], "sobre_id": "",
        "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-18", "concepto": "Bizum viaje",
        "sobre_ingreso": envs["Vacaciones"]}, follow_redirects=True)
    assert "apartado en «Vacaciones»" in r.text
    conn = db.connect(db_path)
    txs = repo.all_txs(conn)
    assert calc.envelope_balances(txs)[envs["Vacaciones"]] == 15347
    filas = {r["type"]: r for r in repo.tx_rows(conn)}
    assert filas["ingreso"]["account"] == "Cuenta corriente"                  # el dinero entra en su cuenta
    assert filas["aporte_sobre"]["envelope"] == "Vacaciones"
    conn.close()
