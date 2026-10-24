"""La API del bot y los Atajos: clave obligatoria, sin PIN y con la misma validación que el formulario."""
import pytest

from app import config, repo

CLAVE = {"Authorization": "Bearer clave-de-prueba"}


@pytest.fixture(autouse=True)
def con_clave(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "clave-de-prueba")


def test_sin_clave_no_entra(client):
    assert client.get("/api/saldo").status_code == 401
    assert client.get("/api/saldo", headers={"Authorization": "Bearer otra"}).status_code == 401


def test_apagada_si_no_hay_clave(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    assert client.get("/api/saldo", headers=CLAVE).status_code == 404


def test_saldo(client):
    r = client.get("/api/saldo", headers=CLAVE)
    assert r.status_code == 200 and {"puedes_gastar", "disponible", "comprometido", "texto"} <= set(r.json())


def test_apuntar_un_gasto(client, db_path):
    from app import db
    categorias = client.get("/api/categorias", headers=CLAVE).json()["gasto"]
    assert categorias
    nombre = categorias[0]
    r = client.post("/api/gastos", headers=CLAVE, json={"importe": "12,50", "categoria": nombre[:4].upper(),
                                                         "concepto": "desde el bot"})
    assert r.status_code == 200, r.text
    conn = db.connect(db_path)
    tx = repo.get_tx(conn, r.json()["id"])
    conn.close()
    assert tx["amount"] == 1250 and tx["category"] == nombre and tx["concept"] == "desde el bot"


def test_categoria_desconocida_y_importe_malo(client):
    r = client.post("/api/gastos", headers=CLAVE, json={"importe": "5", "categoria": "zzz-inventada"})
    assert r.status_code == 400 and r.json()["categorias"]
    cat = client.get("/api/categorias", headers=CLAVE).json()["gasto"][0]
    assert client.post("/api/gastos", headers=CLAVE, json={"importe": "doce", "categoria": cat}).status_code == 400


def test_la_api_no_pide_pin_pero_la_web_si(client, db_path):
    from app import db, pin
    conn = db.connect(db_path)
    with conn:
        repo.set_setting(conn, "pin_hash", pin.hash_pin("1234"))
    conn.close()
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/saldo", headers=CLAVE).status_code == 200
