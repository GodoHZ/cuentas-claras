"""Adivinar y aprender la categoría de lo que la app apunta sola."""
from app import clasificar, repo
from conftest import ids


def test_el_mismo_comercio_aunque_cambie_la_tienda():
    assert clasificar.merchant_key("REPSOL E.S. 1234 TOLEDO") == "repsol"
    assert clasificar.merchant_key("Repsol Madrid") == "repsol"
    assert clasificar.merchant_key("MERCADONA S.A.") == "mercadona"
    assert clasificar.merchant_key("El Corte Inglés") == "corte"
    assert clasificar.merchant_key("PAYPAL *SPOTIFY") == "spotify"      # la pasarela no es el comercio
    assert clasificar.merchant_key("H&M") == "h m"                      # nombres cortos: enteros


def test_la_primera_vez_supone_y_pide_revisar(conn):
    cats = ids(conn)[0]
    assert clasificar.guess(conn, "MERCADONA 1234 TOLEDO") == (cats["Compra"], True)
    assert clasificar.guess(conn, "Ferretería Paco") == (cats["Otros gastos"], True)
    with conn:                                           # la de «por si acaso» se puede elegir
        repo.set_setting(conn, "auto_fallback_category_id", cats["Casa"])
    assert clasificar.guess(conn, "Droguería Pepa") == (cats["Casa"], True)


def test_aprende_de_lo_que_apuntas_o_revisas_tu(conn):
    cats, _, accs = ids(conn)
    with conn:
        repo.insert_tx(conn, date="2026-09-01", type="gasto", category_id=cats["Salud"], envelope_id=None,
                       account_id=accs["Cuenta corriente"], concept="Gimnasio", amount=3499)
        # una suposición sin revisar no enseña nada
        repo.insert_tx(conn, date="2026-09-02", type="gasto", category_id=cats["Ocio"], envelope_id=None,
                       account_id=accs["Cuenta corriente"], concept="MERCADONA", amount=1000, review=1)
    assert clasificar.guess(conn, "GIMNASIO CENTRO") == (cats["Salud"], False)
    assert clasificar.guess(conn, "Mercadona Madrid") == (cats["Compra"], True)


def test_guardarlo_es_darlo_por_revisado(conn):
    cats, _, accs = ids(conn)
    with conn:
        tx = repo.insert_tx(conn, date="2026-09-02", type="gasto", category_id=cats["Otros gastos"],
                            envelope_id=None, account_id=accs["Cuenta corriente"], concept="MERCADONA",
                            amount=1000, source="banco", review=1)
    assert repo.review_count(conn) == 1
    with conn:
        repo.update_tx(conn, tx, date="2026-09-02", type="gasto", category_id=cats["Casa"], envelope_id=None,
                       account_id=accs["Cuenta corriente"], other_account_id=None, concept="MERCADONA",
                       amount=1000)
    assert repo.review_count(conn) == 0
    assert clasificar.guess(conn, "MERCADONA S.A.") == (cats["Casa"], False)    # y ya lo sabe


def test_lo_que_falta_por_revisar_se_ve(client, db_path):
    from app import db
    conn = db.connect(db_path)
    cats, _, accs = ids(conn)
    with conn:
        repo.insert_tx(conn, date="2026-09-10", type="gasto", category_id=cats["Otros gastos"], envelope_id=None,
                       account_id=accs["Cuenta corriente"], concept="Ferretería Paco", amount=900,
                       source="banco", review=1)
    conn.close()
    assert "1 gasto por revisar" in client.get("/").text
    lista = client.get("/movimientos?mes=todos&revisar=1").text
    assert "Ferretería Paco" in lista and "badge-revisar" in lista


def test_ya_no_queda_nada_de_apple_pay(client):
    assert client.post("/api/iphone/pago", json={"importe": 1}).status_code in (303, 404, 405)
    assert "Apuntar desde el iPhone" not in client.get("/ajustes").text
