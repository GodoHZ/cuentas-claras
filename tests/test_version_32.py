"""Novedades de la 3.2: lo retenido por el banco, repartir la nómina, la evolución de tu dinero, el
calendario del mes y los avisos al momento por Telegram."""
from datetime import date, timedelta

from app import avisos, banco, calc, repo, views
from conftest import HOY, apuntar, ids
from test_banco import BancoFalso, conectar, mov, sincronizar


# ---------------------------------------------------------------- retenido

def test_lo_retenido_se_guarda_y_se_resta_de_puedes_gastar(conn):
    apuntar(conn, "2026-09-01", calc.INGRESO, 100000, categoria="Otros ingresos")
    antes = views.forecast(conn, repo.all_txs(conn), 2026, 9, HOY).left
    falso = BancoFalso(movimientos={"u-corriente": [[
        mov("2026-09-19", "12.50", "HELADERIA", estado="PDNG"),
        mov("2026-09-19", "40.00", "DEVOLUCION", "CRDT", estado="PDNG"),
    ]]})
    r = sincronizar(conn, falso, conectar(conn, falso))
    assert r["retenidos"] == 2 and r["nuevos"] == 0
    retenidos = banco.pending(conn)
    assert sorted(x["amount"] for x in retenidos) == [-1250, 4000]
    assert banco.held(retenidos) == 1250                       # lo que va a entrar no se suma hasta que llegue
    despues = views.forecast(conn, repo.all_txs(conn), 2026, 9, HOY, banco.held(retenidos))
    assert despues.left == antes - 1250 and despues.held == 1250


def test_cuando_el_banco_lo_contabiliza_deja_de_estar_retenido(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-18", "12.50", "HELADERIA", estado="PDNG")]]})
    link = conectar(conn, falso)
    sincronizar(conn, falso, link)
    assert len(banco.pending(conn)) == 1
    falso.movimientos = {"u-corriente": [[mov("2026-09-19", "12.50", "HELADERIA", ref="b1")]]}
    r = sincronizar(conn, falso, link)
    assert r["nuevos"] == 1 and banco.pending(conn) == []


def test_si_viene_a_la_vez_retenido_y_contabilizado_no_cuenta_dos_veces(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[
        mov("2026-09-19", "12.50", "HELADERIA", ref="b1"),
        mov("2026-09-19", "12.50", "HELADERIA", estado="PDNG"),
    ]]})
    sincronizar(conn, falso, conectar(conn, falso))
    assert banco.pending(conn) == []


def test_si_ya_lo_apuntaste_a_mano_no_se_resta_otra_vez(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-19", "12.50", "HELADERIA", estado="PDNG")]]})
    sincronizar(conn, falso, conectar(conn, falso))
    assert len(banco.pending(conn)) == 1
    apuntar(conn, "2026-09-19", calc.GASTO, 1250, categoria="Comida fuera")
    assert banco.pending(conn) == []


def test_lo_retenido_se_ve_en_inicio_y_en_movimientos(client, db_path):
    from app import db
    conn = db.connect(db_path)
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-19", "12.50", "HELADERIA MARCONATA", estado="PDNG")]]})
    sincronizar(conn, falso, conectar(conn, falso))
    conn.close()
    inicio = client.get("/").text
    assert "Retenido en el banco" in inicio and "retenido <b class=\"num\">−12,50 €" in inicio
    assert "pendiente" in client.get("/movimientos").text


def test_quitar_la_conexion_borra_lo_retenido(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-19", "12.50", "HELADERIA", estado="PDNG")]]})
    link = conectar(conn, falso)
    sincronizar(conn, falso, link)
    banco.disconnect(conn, falso, link["id"])
    assert banco.pending(conn) == []


# ---------------------------------------------------------------- repartir la nómina

def test_al_cobrar_toca_repartir_y_se_aparta_en_los_sobres(client, db_path):
    from app import db
    conn = db.connect(db_path)
    apuntar(conn, "2026-09-18", calc.INGRESO, 150000, categoria="Nómina")
    assert views.plan_due(conn, HOY) == date(2026, 9, 18)
    p = views.payday_plan(conn, repo.all_txs(conn), HOY)
    assert p["income"] == 150000
    sugeridos = {s["env"]["name"]: s["suggested"] for s in p["envelopes"]}
    assert sugeridos == {"Colchón": 15000, "Vacaciones": 10000, "Coche": 5000}
    assert p["base"] == 150000                                  # sin pagos fijos: todo es para sobres y día a día
    assert "Ha llegado la nómina" in client.get("/").text
    _, envs, _ = ids(conn)
    pagina = client.get("/reparto").text
    assert "¿Cuánto guardas en cada sobre?" in pagina and "1.200,00 €" in pagina   # 1.500 − 300 sugeridos
    r = client.post("/reparto", data={f"sobre_{envs['Colchón']}": "200", f"sobre_{envs['Vacaciones']}": "",
                                      f"sobre_{envs['Coche']}": "50"}, follow_redirects=False)
    assert r.status_code == 303
    aportes = conn.execute("SELECT envelope_id, amount, concept FROM transactions WHERE type = 'aporte_sobre' "
                           "ORDER BY envelope_id").fetchall()
    assert [(a["envelope_id"], a["amount"]) for a in aportes] == [(envs["Colchón"], 20000), (envs["Coche"], 5000)]
    assert aportes[0]["concept"] == "Reparto de la nómina (18/09/2026)"
    assert views.plan_due(conn, HOY) is None
    assert "Ha llegado la nómina" not in client.get("/").text
    conn.close()


def test_repartir_se_puede_dejar_para_otro_mes(client, db_path):
    from app import db
    conn = db.connect(db_path)
    apuntar(conn, "2026-09-18", calc.INGRESO, 150000, categoria="Nómina")
    client.post("/reparto/omitir")
    assert views.plan_due(conn, HOY) is None
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE type = 'aporte_sobre'").fetchone()[0] == 0
    conn.close()


def test_un_cobro_viejo_ya_no_pide_repartir(conn):
    apuntar(conn, "2026-09-01", calc.INGRESO, 150000, categoria="Nómina")
    assert views.plan_due(conn, HOY) is None                   # hace 18 días


def test_sin_nomina_la_pantalla_lo_explica(client):
    assert "Aún no has cobrado ninguna nómina" in client.get("/reparto").text


# ---------------------------------------------------------------- evolución

def test_la_evolucion_sale_de_los_movimientos(conn):
    apuntar(conn, "2026-09-02", calc.INGRESO, 100000, categoria="Otros ingresos")
    apuntar(conn, "2026-09-10", calc.GASTO, 20000, categoria="Casa")
    apuntar(conn, "2026-09-12", calc.APORTE, 5000, sobre="Colchón", cuenta="Cuenta de ahorro")
    e = views.money_history(conn, repo.all_txs(conn), HOY, date(2026, 9, 1))
    datos = e["chart"]["series"][0]["data"]
    assert datos[0] == 0 and datos[1] == 1000 and datos[-1] == 800
    assert e["now"] == 80000 and e["envelopes"] == 5000 and e["change_all"] == 80000
    assert views.money_history(conn, [], HOY, HOY - timedelta(days=3)) is None


def test_el_resumen_ensena_la_evolucion(client, db_path):
    from app import db
    conn = db.connect(db_path)
    apuntar(conn, "2026-09-02", calc.INGRESO, 100000, categoria="Otros ingresos")
    conn.close()
    pagina = client.get("/resumen").text
    assert "Tu dinero" in pagina and 'data-chart="evolucion"' in pagina


# ---------------------------------------------------------------- calendario

def test_el_calendario_pone_cada_cosa_en_su_dia(client, db_path):
    from app import db
    conn = db.connect(db_path)
    cats, _, _ = ids(conn)
    with conn:
        conn.execute("INSERT INTO loans (name, installment, first_date, n_installments, kind, category_id) "
                     "VALUES ('Gimnasio', 3000, '2026-08-25', NULL, 'suscripcion', ?)", (cats["Ocio"],))
    apuntar(conn, "2026-09-05", calc.GASTO, 4550, categoria="Compra")
    c = views.month_calendar(conn, repo.all_txs(conn), 2026, 9, HOY)
    dias = {d["date"]: d for semana in c["weeks"] for d in semana if d}
    assert c["from"] == date(2026, 9, 1) and date(2026, 9, 30) in dias
    assert c["weeks"][0][:1] == [None]                          # el 1/9/2026 es martes
    assert dias[date(2026, 9, 5)]["spent"] == 4550
    assert [e["name"] for e in dias[date(2026, 9, 25)]["events"]] == ["Gimnasio"]
    assert [e["name"] for _, e in c["upcoming"]] == ["Gimnasio"]
    assert {u.name for u in c["envelopes"]} == {"Colchón", "Vacaciones", "Coche"}
    pagina = client.get("/calendario").text
    assert "Lo que viene" in pagina and "Gimnasio" in pagina and 'aria-current="page">Calendario' in pagina
    conn.close()


# ---------------------------------------------------------------- avisos al momento

def _telegram(monkeypatch):
    enviados = []
    monkeypatch.setattr(avisos, "configurado", lambda: True)
    monkeypatch.setattr(avisos, "enviar", lambda texto: enviados.append(texto) or True)
    return enviados


def test_avisa_de_un_gasto_grande_y_de_un_presupuesto_pasado(conn, monkeypatch):
    enviados = _telegram(monkeypatch)
    apuntar(conn, "2026-09-05", calc.GASTO, 25000, categoria="Compra")       # 250 de 300: aún bien
    falso = BancoFalso(movimientos={"u-corriente": [[
        mov("2026-09-18", "120.00", "COMPRA EN MERCADONA", ref="g1"),
        mov("2026-09-18", "3.20", "CAFE", ref="g2"),
    ]]})
    regla_cats, _, _ = ids(conn)
    with conn:
        conn.execute("INSERT INTO rules (pattern, kind, category_id) VALUES ('MERCADONA', 'gasto', ?)",
                     (regla_cats["Compra"],))
    sincronizar(conn, falso, conectar(conn, falso))
    assert len(enviados) == 1
    texto = enviados[0]
    assert "💸 Gasto de 120,00 €" in texto and "CAFE" not in texto
    assert "⚠️ Compra: 370,00 € de 300,00 € · te has pasado" in texto


def test_avisa_cuando_te_devuelven_lo_que_te_debian(conn, monkeypatch):
    enviados = _telegram(monkeypatch)
    with conn:
        conn.execute("INSERT INTO owed (person, concept, amount, date) VALUES ('Pol', 'Airbnb', 5000, '2026-09-10')")
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-18", "50.00", "BIZUM DE POL", "CRDT", ref="o1")]]})
    sincronizar(conn, falso, conectar(conn, falso))
    assert enviados == ["✅ Pol te ha devuelto 50,00 € (Airbnb)"]


def test_los_avisos_se_pueden_apagar(client, db_path, monkeypatch):
    from app import db
    enviados = _telegram(monkeypatch)
    r = client.post("/ajustes/telegram", data={"grande_desde": "500", "deudas": "1"}, follow_redirects=False)
    assert r.status_code == 303
    conn = db.connect(db_path)
    pref = avisos.preferencias(conn)
    assert pref == {"grande": False, "grande_desde": 50000, "presupuesto": False, "deudas": True,
                    "semanal": False, "cambios": False}
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-18", "900.00", "TIENDA", ref="x1")]]})
    sincronizar(conn, falso, conectar(conn, falso))
    assert enviados == []
    conn.close()


def test_sin_bot_no_se_manda_nada(conn, monkeypatch):
    llamadas = []
    monkeypatch.setattr(avisos, "enviar", lambda texto: llamadas.append(texto))
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-18", "900.00", "TIENDA", ref="x1")]]})
    sincronizar(conn, falso, conectar(conn, falso))
    assert llamadas == []
