"""Pagos fijos (préstamos, seguros, suscripciones), presupuesto sin duplicados y sobres puntuales."""
from datetime import date

from app import calc, db, repo, views
from conftest import apuntar, ids


def test_una_suscripcion_no_termina_y_entra_en_lo_previsto(conn):
    cats = ids(conn)[0]
    with conn:
        conn.execute("INSERT INTO loans (name, installment, first_date, n_installments, category_id, kind) "
                     "VALUES ('Gimnasio', 3499, '2026-09-22', NULL, ?, 'suscripcion')", (cats["Ocio"],))
    hoy = date(2026, 10, 5)
    st = calc.loan_status(3499, date(2026, 9, 22), None, hoy)
    assert not st.finished and st.pending is None
    assert calc.next_installment(date(2026, 9, 22), None, hoy) == date(2026, 10, 22)
    assert calc.installment_in_month(date(2026, 9, 22), None, 2030, 1) == date(2030, 1, 22)
    previsto = views.forecast(conn, repo.all_txs(conn), *repo.period_of(conn, hoy), hoy)
    assert "Gimnasio" in [u.name for u in previsto.lines]


def test_la_pantalla_sugiere_lo_que_se_repite(client, db_path):
    conn = db.connect(db_path)
    apuntar(conn, "2026-08-05", calc.GASTO, 1049, categoria="Ocio", concepto="Youtube")
    apuntar(conn, "2026-09-05", calc.GASTO, 1049, categoria="Ocio", concepto="Youtube")
    conn.close()
    html = client.get("/fijos").text
    assert "¿Estos también son fijos?" in html and "Youtube" in html
    r = client.post("/fijos", data={"nombre": "Youtube", "tipo": "suscripcion", "cuota": "10,49",
                                    "primera": "2026-10-05", "sin_fin": "1", "categoria_id": ""}, follow_redirects=True)
    assert "Añadido: Youtube" in r.text
    conn = db.connect(db_path)
    fila = conn.execute("SELECT * FROM loans WHERE name = 'Youtube'").fetchone()
    assert fila["n_installments"] is None and fila["kind"] == "suscripcion"
    conn.close()
    assert client.get("/deudas", follow_redirects=False).status_code == 303


def test_el_presupuesto_no_cuenta_dos_veces_los_pagos_fijos(conn):
    cats = ids(conn)[0]
    with conn:
        conn.execute("UPDATE categories SET budget = 50000 WHERE id = ?", (cats["Casa"],))
        conn.execute("INSERT INTO loans (name, installment, first_date, n_installments, category_id) "
                     "VALUES ('Hipoteca', 50000, '2026-01-01', 300, ?)", (cats["Casa"],))
    b = views.budget(conn, repo.all_txs(conn), 2026, 9)
    assert "Casa" not in [l["cat"]["name"] for l in b["lines"]]
    assert [f["loan"]["name"] for f in b["fixed"]] == ["Hipoteca"] and b["fixed_total"] == 50000


def test_la_migracion_convierte_prestamos_en_pagos_fijos(tmp_path):
    ruta = tmp_path / "vieja.db"
    conn = db.connect(ruta)
    viejo = db.SCHEMA.replace("    n_installments INTEGER CHECK (n_installments IS NULL OR n_installments > 0),",
                              "    n_installments INTEGER NOT NULL CHECK (n_installments > 0),")
    i = viejo.index("    -- qué es: un préstamo")
    viejo = viejo[:i] + viejo[viejo.index("\n", viejo.index("kind           TEXT", i)) + 1:]
    conn.executescript(viejo)
    conn.execute("INSERT INTO loans (name, installment, first_date, n_installments) VALUES ('Seguro coche', 25000, '2026-03-10', 2)")
    conn.execute("INSERT INTO loans (name, installment, first_date, n_installments) VALUES ('Coche', 19950, '2026-01-15', 96)")
    conn.commit()
    conn.close()
    db.init_db(ruta)
    conn = db.connect(ruta)
    assert {r["name"]: r["kind"] for r in conn.execute("SELECT * FROM loans")} == {"Seguro coche": "seguro", "Coche": "prestamo"}
    conn.close()


def test_un_sobre_puntual_se_crea_y_se_archiva_desde_sobres(client, db_path):
    r = client.post("/ajustes/lista/sobres", data={"nombre": "Viaje", "objetivo": "", "aporte": "", "nota": "",
                                                   "cuenta": "", "pregunta_temporal": "1", "temporal": "1",
                                                   "volver": "/sobres"}, follow_redirects=False)
    assert r.headers["location"].startswith("/sobres")
    conn = db.connect(db_path)
    env = conn.execute("SELECT * FROM envelopes WHERE name = 'Viaje'").fetchone()
    assert env["temporary"] == 1
    apuntar(conn, "2026-09-01", calc.APORTE, 5000, sobre="Viaje")
    apuntar(conn, "2026-09-02", calc.GASTO, 5000, categoria="Ocio", sobre="Viaje")
    conn.close()
    assert "Ya lo has gastado" in client.get(f"/sobres/{env['id']}").text
    assert "Nuevo sobre" in client.get("/sobres").text


def test_varios_pagos_fijos_en_la_misma_categoria_no_se_cubren_con_el_mismo_gasto(conn):
    cats = ids(conn)[0]
    with conn:
        for nombre, cuota in (("Gimnasio", 3499), ("YouTube", 1049)):
            conn.execute("INSERT INTO loans (name, installment, first_date, n_installments, category_id, kind) "
                         "VALUES (?, ?, '2026-09-05', NULL, ?, 'suscripcion')", (nombre, cuota, cats["Ocio"]))
    apuntar(conn, "2026-09-06", calc.GASTO, 3499, categoria="Ocio", concepto="Basic-Fit")
    hoy = date(2026, 9, 10)
    previsto = views.forecast(conn, repo.all_txs(conn), *repo.period_of(conn, hoy), hoy)
    estado = {u.name: u.paid for u in previsto.lines}
    assert estado["Gimnasio"] is True and estado["YouTube"] is False


def test_el_presupuesto_ensena_lo_guardado_en_sobres(conn):
    envs = ids(conn)[1]
    with conn:
        conn.execute("UPDATE envelopes SET monthly = NULL")
        conn.execute("UPDATE envelopes SET monthly = 30000 WHERE id = ?", (envs["Coche"],))
    apuntar(conn, "2026-09-10", calc.APORTE, 30000, sobre="Coche", cuenta="Cuenta de ahorro")
    b = views.budget(conn, repo.all_txs(conn), 2026, 9)
    assert [(s["env"]["name"], s["done"]) for s in b["savings"]] == [("Coche", 30000)]
