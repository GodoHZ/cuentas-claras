"""3.1: a este ritmo, me deben, buscador, reglas propias, en qué se va y el cierre por Telegram."""
from datetime import date

from app import avisos, calc, db, repo, views
from conftest import apuntar, ids


def test_a_este_ritmo(conn):
    apuntar(conn, "2026-08-01", calc.GASTO, 5000, categoria="Ocio")             # fuera de los últimos 30 días
    apuntar(conn, "2026-09-05", calc.GASTO, 20000, categoria="Ocio")
    apuntar(conn, "2026-09-20", calc.GASTO, 10000, categoria="Ocio")            # 300 € en 30 días: 10 €/día
    hoy = date(2026, 9, 25)
    cobro = {"days": 20, "date": date(2026, 10, 15)}
    r = views.pace(conn, repo.all_txs(conn), 50000, hoy, cobro)
    assert r["per_day"] == 1000 and r["end"] == 50000 - 1000 * 20


def test_sin_historia_no_hay_ritmo(conn):
    apuntar(conn, "2026-09-05", calc.GASTO, 20000, categoria="Ocio")
    assert views.pace(conn, repo.all_txs(conn), 50000, date(2026, 9, 10), {"days": 20, "date": date(2026, 9, 30)}) is None


def test_me_deben_se_cobra_solo_al_llegar_el_bizum(client, db_path):
    client.post("/me-deben", data={"persona": "Pol", "importe": "153,47", "concepto": "Airbnb", "fecha": "2026-09-10"})
    client.post("/me-deben", data={"persona": "Ana", "importe": "153,47", "concepto": "Airbnb", "fecha": "2026-09-10"})
    conn = db.connect(db_path)
    cats, _, accs = ids(conn)
    conn.close()
    r = client.post("/movimientos/nuevo", data={"tipo": "ingreso", "importe": "153,47", "categoria_id": cats["Otros ingresos"],
                                                "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-12",
                                                "concepto": "Bizum de Pol"}, follow_redirects=True)
    assert "cobrado lo que te debía Pol" in r.text
    conn = db.connect(db_path)
    assert [o["person"] for o in repo.owed(conn, pending=True)] == ["Ana"]
    conn.close()
    assert "Te deben 153,47" in client.get("/").text


def test_buscar_en_todos_los_meses(client, db_path):
    conn = db.connect(db_path)
    apuntar(conn, "2026-08-02", calc.GASTO, 3000, categoria="Ocio", concepto="Cine Yelmo")
    apuntar(conn, "2026-09-02", calc.GASTO, 2000, categoria="Ocio", concepto="cine con Ana")
    apuntar(conn, "2026-09-03", calc.GASTO, 999, categoria="Casa", concepto="Mercadona")
    conn.close()
    html = client.get("/movimientos?q=cine").text
    lista = html.split('id="lista"', 1)[1]
    assert "2 resultados" in html and "−50,00" in html and "Mercadona" not in lista


def test_una_regla_manda_y_limpia_lo_que_estaba_por_revisar(client, db_path):
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    with conn:
        tx = repo.insert_tx(conn, date="2026-09-10", type="gasto", category_id=cats["Otros gastos"], envelope_id=None,
                            account_id=accs["Cuenta corriente"], amount=3499, source="banco", review=1,
                            concept="ADEUDO // Basic-Fit Spain")
    conn.close()
    r = client.post("/ajustes/reglas-banco", data={"texto": "basic fit", "tipo": "gasto", "categoria_id": cats["Ocio"]},
                    follow_redirects=True)
    assert "Aplicada a 1" in r.text
    conn = db.connect(db_path)
    fila = repo.get_tx(conn, tx)
    assert (fila["category"], fila["review"]) == ("Ocio", 0)
    assert repo.matching_rule(conn, "PAGO // BASIC-FIT SPAIN SAU", "gasto")["pattern"] == "basic fit"
    assert repo.matching_rule(conn, "PAGO // BASIC-FIT", "ingreso") is None
    conn.close()


def test_en_que_se_va_y_el_mensaje_del_cierre(conn):
    for mes in ("06", "07", "08"):
        apuntar(conn, f"2026-{mes}-05", calc.GASTO, 10000, categoria="Ocio")
    apuntar(conn, "2026-09-05", calc.GASTO, 20000, categoria="Ocio")
    with conn:
        repo.set_setting(conn, "month_start", "dia1")
    filas = views.category_trends(repo.all_txs(conn), 2026, 9, repo.categories(conn))
    ocio = next(f for f in filas if f["name"] == "Ocio")
    assert ocio["average"] == 10000 and round(ocio["change"], 2) == 1.0
    texto = views.close_message(conn, date(2026, 10, 1), 4250, "Colchón", True)
    assert "Septiembre de 2026 cerrado" in texto and "Te sobraron 42,50" in texto and "Ocio: +100%" in texto
    assert avisos.enviar("hola") is False                                   # sin bot configurado, no hace nada
