"""Novedades de la 3.3: el ritmo del mes frente a otros meses, el resumen del domingo, las subidas de
precio y cobros dobles, cuántos meses aguantas, el patrimonio, tu año, dividir un gasto, las etiquetas,
los tickets de Paperless y las metas con fecha en los sobres."""
from datetime import date, datetime

from app import avisos, calc, db, paperless, repo, views
from conftest import HOY, apuntar, ids
from test_banco import BancoFalso, conectar, mov, sincronizar


def _telegram(monkeypatch):
    enviados = []
    monkeypatch.setattr(avisos, "configurado", lambda: True)
    monkeypatch.setattr(avisos, "enviar", lambda texto: enviados.append(_sin_nbsp(texto)) or True)
    return enviados


def _sin_nbsp(texto: str) -> str:
    """Los importes llevan un espacio duro antes del €: aquí, uno normal, para leer mejor los tests."""
    return texto.replace("\xa0", " ")


def _ver(client, url: str) -> str:
    return _sin_nbsp(client.get(url).text)


def _pago_fijo(conn, nombre, cuota, primera, cuotas=None, tipo="suscripcion", categoria=None):
    cats, _, _ = ids(conn)
    with conn:
        conn.execute("INSERT INTO loans (name, installment, first_date, n_installments, kind, category_id) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (nombre, cuota, primera, cuotas, tipo, cats.get(categoria)))


# ---------------------------------------------------------------- el ritmo del mes

def _meses_de_compra(conn):
    apuntar(conn, "2026-07-01", calc.GASTO, 5000, categoria="Compra")
    apuntar(conn, "2026-07-10", calc.GASTO, 5000, categoria="Compra")
    apuntar(conn, "2026-08-05", calc.GASTO, 10000, categoria="Compra")
    apuntar(conn, "2026-08-25", calc.GASTO, 99900, categoria="Compra")       # después del día 19: no cuenta
    apuntar(conn, "2026-09-03", calc.GASTO, 30000, categoria="Compra")
    apuntar(conn, "2026-09-15", calc.GASTO, 5000, categoria="Ocio")


def test_el_ritmo_compara_con_lo_que_llevabas_otros_meses_el_mismo_dia(conn):
    _meses_de_compra(conn)
    _pago_fijo(conn, "Netflix", 1299, "2026-07-10", categoria="Suscripciones")
    apuntar(conn, "2026-09-10", calc.GASTO, 1299, categoria="Suscripciones")  # pago fijo: no es ritmo
    apuntar(conn, "2026-09-11", calc.GASTO, 20000, categoria="Ocio", sobre="Vacaciones")   # desde un sobre: tampoco
    r = views.month_pace(conn, repo.all_txs(conn), HOY)
    assert r["day"] == 19 and r["spent"] == 35000 and r["average"] == 10000 and r["months"] == 2
    assert r["diff"] == 25000 and r["warn"]


def test_sin_meses_con_datos_no_hay_ritmo(conn):
    apuntar(conn, "2026-09-03", calc.GASTO, 3000, categoria="Compra")
    assert views.month_pace(conn, repo.all_txs(conn), HOY) is None
    assert views.month_pace(conn, repo.all_txs(conn), date(2026, 9, 2)) is None


def test_el_ritmo_sale_en_inicio(client, db_path):
    conn = db.connect(db_path)
    _meses_de_compra(conn)
    conn.close()
    pagina = _ver(client, "/")
    assert "Vas 250,00 € por encima de otros meses" in pagina
    assert "A día 19, en el día a día llevas" in pagina


# ---------------------------------------------------------------- resumen del domingo

def test_el_resumen_de_la_semana(conn):
    apuntar(conn, "2026-09-01", calc.INGRESO, 150000, categoria="Otros ingresos")
    apuntar(conn, "2026-09-08", calc.GASTO, 2000, categoria="Ocio")
    apuntar(conn, "2026-09-15", calc.GASTO, 5000, categoria="Compra")
    apuntar(conn, "2026-09-16", calc.GASTO, 1000, categoria="Ocio")
    texto = _sin_nbsp(views.weekly_message(conn, HOY))
    assert texto.startswith("📅 Tu semana (13/09 – 19/09)")
    assert "Gastaste 60,00 € (40,00 € más que la anterior)" in texto
    assert "Lo que más: Compra 50,00 €, Ocio 10,00 €" in texto
    assert "Puedes gastar" in texto


def test_el_resumen_sale_los_domingos_por_la_tarde_y_una_vez(conn, monkeypatch):
    enviados = _telegram(monkeypatch)
    domingo = datetime(2026, 9, 20, 20, 15)
    assert not views.weekly_due(conn, datetime(2026, 9, 19, 21, 0))          # sábado
    assert not views.weekly_due(conn, datetime(2026, 9, 20, 19, 0))          # aún no son las 20:00
    assert avisos.resumen_semanal(conn, domingo)
    assert not avisos.resumen_semanal(conn, domingo.replace(hour=21))
    assert len(enviados) == 1 and enviados[0].startswith("📅 Tu semana")
    with conn:
        repo.set_setting(conn, "alert_weekly", "0")
    assert not views.weekly_due(conn, datetime(2026, 9, 27, 20, 30))


# ---------------------------------------------------------------- subidas y cobros dobles

def test_detecta_subidas_y_cobros_dobles_en_lo_periodico():
    d = date.fromisoformat
    cargos = [
        (d("2026-07-05"), "netflix", "Netflix", 1299, 1), (d("2026-08-05"), "netflix", "Netflix", 1299, 1),
        (d("2026-09-05"), "netflix", "Netflix", 1399, 1),
        (d("2026-07-10"), "spotify", "Spotify", 999, 1), (d("2026-08-10"), "spotify", "Spotify", 999, 1),
        (d("2026-09-10"), "spotify", "Spotify", 999, 1), (d("2026-09-11"), "spotify", "Spotify", 999, 1),
        (d("2026-07-01"), "gym", "Gym", 3000, 2), (d("2026-08-01"), "gym", "Gym", 3000, 2),
        (d("2026-09-01"), "gym", "Gym", 3000, 2),
        (d("2025-09-15"), "seguro", "Seguro coche", 30000, 3), (d("2026-09-15"), "seguro", "Seguro coche", 32000, 3),
    ] + [(d(f"2026-0{m}-{dia:02d}"), "mercadona", "Mercadona", 4000 + dia, 4) for m in (7, 8, 9) for dia in (2, 9, 16)]
    cambios = {c.key: c for c in calc.price_changes(cargos, HOY)}
    assert set(cambios) == {"netflix", "spotify", "seguro"}
    assert cambios["netflix"].kind == "subida" and (cambios["netflix"].before, cambios["netflix"].now) == (1299, 1399)
    assert cambios["spotify"].kind == "doble" and cambios["spotify"].now == 999
    assert cambios["seguro"].diff == 2000


def test_las_subidas_salen_en_inicio_hasta_que_dices_vale(client, db_path):
    conn = db.connect(db_path)
    for dia in ("2026-07-05", "2026-08-05"):
        apuntar(conn, dia, calc.GASTO, 1299, categoria="Suscripciones", concepto="Netflix")
    apuntar(conn, "2026-09-05", calc.GASTO, 1399, categoria="Suscripciones", concepto="Netflix")
    [cambio] = views.price_alerts(conn, HOY)
    assert _sin_nbsp(views.change_text(cambio)) == "Netflix ha subido 1,00 €: ahora 13,99 € (antes 12,99 €)"
    assert "Netflix ha subido 1,00 €" in _ver(client, "/")
    assert "Subidas y cobros repetidos" in _ver(client, "/fijos")
    client.post("/avisos/cambio/visto", data={"id": cambio.id})
    assert views.price_alerts(conn, HOY) == []
    assert "Netflix ha subido" not in _ver(client, "/")
    assert "Subidas y cobros repetidos" in _ver(client, "/fijos")          # en Pagos fijos se sigue viendo
    conn.close()


def test_avisa_por_telegram_de_una_subida_que_trae_el_banco(conn, monkeypatch):
    enviados = _telegram(monkeypatch)
    for dia in ("2026-07-18", "2026-08-18"):
        apuntar(conn, dia, calc.GASTO, 1299, categoria="Suscripciones", concepto="NETFLIX")
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-18", "13.99", "NETFLIX", ref="n1")]]})
    sincronizar(conn, falso, conectar(conn, falso))
    assert enviados == ["📈 NETFLIX ha subido 1,00 €: ahora 13,99 € (antes 12,99 €)"]


# ---------------------------------------------------------------- patrimonio y cuánto aguantas

def _patrimonio(conn):
    apuntar(conn, "2026-08-01", calc.INGRESO, 200000, categoria="Otros ingresos")
    apuntar(conn, "2026-09-10", calc.GASTO, 50000, categoria="Casa", concepto="Mercadona")
    with conn:
        conn.execute("INSERT INTO owed (person, concept, amount, date) VALUES ('Pol', 'Cena', 3000, '2026-09-12')")
    _pago_fijo(conn, "Préstamo coche", 10000, "2026-01-05", cuotas=12, tipo="prestamo")


def test_el_patrimonio_es_lo_que_tienes_menos_lo_que_debes(conn):
    _patrimonio(conn)
    p = views.net_worth(conn, repo.all_txs(conn), HOY)
    assert (p["accounts"], p["owed"], p["debt"]) == (150000, 3000, 30000)    # quedan 3 cuotas de 100 €
    assert p["total"] == 123000
    assert p["points"] == [(date(2026, 8, 31), 160000), (HOY, 123000)]
    assert p["change_month"] == -37000 and p["chart"]["labels"] == ["ago 26", "hoy"]


def test_cuantos_meses_aguantas(conn):
    _patrimonio(conn)
    a = views.runway(conn, repo.all_txs(conn), HOY)
    assert a.money == 150000 and a.monthly_spend == 30417 and a.monthly_fixed == 10000
    assert round(a.months, 1) == 4.9 and a.months_fixed == 15


def test_el_resumen_ensena_patrimonio_aguante_y_tu_ano(client, db_path):
    conn = db.connect(db_path)
    _patrimonio(conn)
    conn.close()
    pagina = _ver(client, "/resumen")
    assert "Patrimonio" in pagina and "1.230,00 €" in pagina
    assert "Si dejaras de cobrar" in pagina and "4,9" in pagina
    assert "/resumen/anual?ano=2026" in pagina


def test_tu_ano(client, db_path):
    conn = db.connect(db_path)
    _patrimonio(conn)
    r = views.year_review(conn, repo.all_txs(conn), 2026)
    assert r["n_months"] == 2 and r["total"].income == 200000 and r["spent_all"] == 50000
    assert (r["best"].year, r["best"].month) == (2026, 8)
    assert r["biggest"]["amount"] == 50000 and r["merchants"][0]["name"] == "Mercadona"
    assert views.year_review(conn, repo.all_txs(conn), 2020) is None
    conn.close()
    pagina = _ver(client, "/resumen/anual?ano=2026")
    assert "Tu mejor mes: agosto de 2026" in pagina and "Dónde se fue" in pagina
    assert views.year_review_due(date(2026, 12, 20)) == 2026 and views.year_review_due(date(2027, 1, 10)) == 2026
    assert views.year_review_due(HOY) is None


# ---------------------------------------------------------------- dividir un gasto

def test_dividir_un_gasto_entre_varios(client, db_path):
    r = client.post("/me-deben", data={"dividir": "1", "persona": "Pol, Ana, Marc", "importe": "80",
                                       "yo_tambien": "1", "concepto": "Cena", "fecha": "2026-09-18"},
                    follow_redirects=False)
    assert r.status_code == 303
    conn = db.connect(db_path)
    filas = conn.execute("SELECT person, amount, concept FROM owed ORDER BY id").fetchall()
    assert [(f["person"], f["amount"], f["concept"]) for f in filas] == [
        ("Pol", 2000, "Cena"), ("Ana", 2000, "Cena"), ("Marc", 2000, "Cena")]
    client.post("/me-deben", data={"dividir": "1", "persona": "Eva, Leo, Sol", "importe": "80"})
    assert conn.execute("SELECT amount FROM owed WHERE person = 'Eva'").fetchone()[0] == 2666
    conn.close()
    assert views.split_amount(1000, 1, True) == 500


def test_sin_dividir_sigue_siendo_una_sola_persona(client, db_path):
    client.post("/me-deben", data={"persona": "Pol, el de la cena", "importe": "15"})
    conn = db.connect(db_path)
    assert [tuple(f) for f in conn.execute("SELECT person, amount FROM owed")] == [("Pol, el de la cena", 1500)]
    conn.close()


# ---------------------------------------------------------------- etiquetas

def _form_gasto(conn, importe="25", etiquetas=""):
    cats, _, accs = ids(conn)
    return {"tipo": "gasto", "importe": importe, "categoria_id": cats["Comida fuera"],
            "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-12", "concepto": "Trattoria",
            "pregunta_etiquetas": "1", "etiquetas": etiquetas}


def test_las_etiquetas_se_ponen_desde_el_movimiento(client, db_path):
    conn = db.connect(db_path)
    tx = apuntar(conn, "2026-09-12", calc.GASTO, 2500, categoria="Comida fuera", concepto="Trattoria")
    client.post(f"/movimientos/{tx}", data=_form_gasto(conn, etiquetas="Roma 2026, viajes , Roma 2026"))
    assert repo.get_tx(conn, tx)["tags"] in ("Roma 2026, viajes", "viajes, Roma 2026")
    [roma] = [t for t in repo.tags(conn) if t["name"] == "Roma 2026"]
    assert roma["n"] == 1 and roma["spent"] == 2500
    assert "Roma 2026" in _ver(client, f"/movimientos/{tx}")
    assert "Trattoria" in _ver(client, f"/movimientos?etiqueta={roma['id']}")
    client.post(f"/movimientos/{tx}", data=_form_gasto(conn, etiquetas=""))
    assert repo.tags(conn) == []                                   # sin movimientos, la etiqueta se va
    conn.close()


def test_etiquetar_los_gastos_de_unas_fechas(client, db_path):
    conn = db.connect(db_path)
    a = apuntar(conn, "2026-09-10", calc.GASTO, 4000, categoria="Transporte", concepto="Vuelo")
    b = apuntar(conn, "2026-09-12", calc.GASTO, 2500, categoria="Comida fuera", concepto="Trattoria")
    apuntar(conn, "2026-09-12", calc.INGRESO, 1000, categoria="Otros ingresos", concepto="Bizum de Ana")
    apuntar(conn, "2026-09-16", calc.GASTO, 900, categoria="Compra")
    r = client.post("/etiquetas", data={"nombre": "Roma 2026", "desde": "2026-09-10", "hasta": "2026-09-12"},
                    follow_redirects=False)
    tag_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert {x["id"] for x in repo.tx_rows(conn, tag_id=tag_id)} == {a, b}
    pagina = _ver(client, f"/etiquetas/{tag_id}")
    assert "65,00 €" in pagina and "Transporte" in pagina and "3 días" in pagina
    assert "Roma 2026" in _ver(client, "/etiquetas")
    client.post(f"/etiquetas/{tag_id}/quitar/{a}")
    assert {x["id"] for x in repo.tx_rows(conn, tag_id=tag_id)} == {b}
    client.post(f"/etiquetas/{tag_id}/nombre", data={"nombre": "Italia"})
    assert repo.get_tag(conn, tag_id)["name"] == "Italia"
    client.post(f"/etiquetas/{tag_id}/borrar")
    assert repo.get_tag(conn, tag_id) is None and repo.get_tx(conn, b) is not None
    r = client.post("/etiquetas", data={"nombre": "Nada", "desde": "2026-01-01"}, follow_redirects=False)
    assert repo.tags(conn) == []
    conn.close()


# ---------------------------------------------------------------- tickets y Paperless

def test_enlazar_un_documento_a_un_movimiento(client, db_path):
    conn = db.connect(db_path)
    tx = apuntar(conn, "2026-09-12", calc.GASTO, 1250, categoria="Compra")
    pagina = _ver(client, f"/movimientos/{tx}")
    assert "Factura o ticket" in pagina and "Buscar «" not in pagina
    with conn:
        repo.set_setting(conn, "paperless_url", "https://papeles.example")
    assert "https://papeles.example/documents?query=12%2C50" in _ver(client, f"/movimientos/{tx}")
    client.post(f"/movimientos/{tx}/documentos", data={"url": "https://papeles.example/documents/42/details"})
    client.post(f"/movimientos/{tx}/documentos", data={"url": "no es un enlace"})
    [doc] = repo.tx_docs(conn, tx)
    assert doc["paperless_id"] == 42
    client.post(f"/movimientos/{tx}/documentos/{doc['id']}/quitar")
    assert repo.tx_docs(conn, tx) == []
    conn.close()


def test_con_la_api_de_paperless_propone_y_anota(client, db_path, monkeypatch):
    conn = db.connect(db_path)
    tx = apuntar(conn, "2026-09-12", calc.GASTO, 1250, categoria="Compra")
    with conn:
        repo.set_setting(conn, "paperless_url", "https://papeles.example")
    llamadas = []

    def api_falsa(_conn, path, data=None):
        llamadas.append((path, data))
        if path.startswith("/api/documents/?"):
            return {"results": [{"id": 7, "title": "Ticket Mercadona", "created": "2026-09-12T10:00:00Z"}]}
        return {}
    monkeypatch.setenv("PAPERLESS_TOKEN", "x")
    monkeypatch.setattr(paperless, "_api", api_falsa)
    assert "Buscando en Paperless" in _ver(client, f"/movimientos/{tx}")
    assert "Ticket Mercadona" in _ver(client, f"/movimientos/{tx}/paperless")
    assert "query=%2212%2C50%22" in llamadas[0][0]
    client.post(f"/movimientos/{tx}/documentos", data={"paperless_id": "7", "titulo": "Ticket Mercadona"})
    [doc] = repo.tx_docs(conn, tx)
    assert doc["url"] == "https://papeles.example/documents/7/details" and doc["title"] == "Ticket Mercadona"
    nota = llamadas[-1]
    assert nota[0] == "/api/documents/7/notes/" and f"/movimientos/{tx}" in nota[1]["note"]
    assert "Ticket Mercadona" not in _ver(client, f"/movimientos/{tx}/paperless")    # ya enlazado
    conn.close()


# ---------------------------------------------------------------- metas con fecha

def test_una_meta_con_fecha_dice_cuanto_guardar_al_mes():
    st = calc.envelope_status(30000, 120000, 10000, "2026-12", HOY)
    assert st.deadline == (2026, 12) and st.months_left == 4 and st.needed == 22500
    assert st.on_track is False and st.eta == (2027, 5)
    assert calc.envelope_status(30000, 120000, 30000, "2026-12", HOY).on_track is True
    assert calc.envelope_status(120000, 120000, 10000, "2026-12", HOY).needed is None
    assert calc.envelope_status(0, 120000, None, "2026-08", HOY).months_left == 1        # ya pasó: todo este mes
    assert calc.parse_ym("2026-13") is None and calc.parse_ym("julio") is None


def test_la_fecha_de_la_meta_se_guarda_y_se_ajusta_el_aporte(client, db_path):
    conn = db.connect(db_path)
    _, envs, accs = ids(conn)
    sobre = envs["Vacaciones"]
    datos = {"nombre": "Vacaciones", "objetivo": "1200", "aporte": "100", "cuenta": accs["Cuenta de ahorro"],
             "pregunta_fecha": "1", "fecha_meta": "2026-12", "volver": f"/sobres/{sobre}"}
    client.post(f"/ajustes/lista/sobres/{sobre}", data=datos)
    assert repo.get_row(conn, "envelopes", sobre)["target_date"] == "2026-12"
    pagina = _ver(client, f"/sobres/{sobre}")
    assert "300,00 € al mes" in pagina and "Guardar 300,00 € al mes" in pagina
    assert "guarda 300,00 €/mes" in _ver(client, "/sobres")
    client.post(f"/sobres/{sobre}/aporte-meta")
    assert repo.get_row(conn, "envelopes", sobre)["monthly"] == 30000
    assert "llegas a tiempo" in _ver(client, f"/sobres/{sobre}")
    client.post(f"/ajustes/lista/sobres/{sobre}", data={**datos, "fecha_meta": "2026-13"})
    client.post(f"/ajustes/lista/sobres/{sobre}", data={**datos, "objetivo": ""})
    assert repo.get_row(conn, "envelopes", sobre)["target_date"] == "2026-12"           # los dos fallan
    conn.close()


# ---------------------------------------------------------------- base de datos y pantallas

def test_una_base_de_la_3_2_pasa_a_la_3_3(tmp_path):
    path = tmp_path / "vieja.db"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("ALTER TABLE envelopes DROP COLUMN target_date")
    conn.executescript("DROP TABLE tx_tags; DROP TABLE tags; DROP TABLE tx_docs; PRAGMA user_version = 11;")
    conn.close()
    db.init_db(path)
    conn = db.connect(path)
    assert "target_date" in {r["name"] for r in conn.execute("PRAGMA table_info(envelopes)")}
    tablas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"tags", "tx_tags", "tx_docs"} <= tablas
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 12
    conn.close()


def test_las_pantallas_nuevas_se_abren(client, db_path):
    conn = db.connect(db_path)
    _patrimonio(conn)
    tx = apuntar(conn, "2026-09-12", calc.GASTO, 1250, categoria="Compra")
    conn.close()
    for url in ("/", "/resumen", "/resumen/anual", "/resumen/anual?ano=1999", "/etiquetas", "/fijos",
                "/ajustes", "/sobres", f"/movimientos/{tx}", f"/me-deben?desde={tx}", "/nuevo", "/reparto"):
        r = client.get(url)
        assert r.status_code == 200, url
    medeben = _ver(client, f"/me-deben?desde={tx}")
    assert medeben.count("Dividir un gasto entre varios") == 1 and medeben.count("<form") < 10
    ajustes = _ver(client, "/ajustes")
    assert "Paperless" in ajustes and "El resumen de la semana" in ajustes
