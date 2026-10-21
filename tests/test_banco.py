"""Traer los movimientos del banco sin duplicar nada (con un banco de mentira)."""
import base64
import json
import os
from datetime import date, datetime, timezone

import pytest

from app import banco, calc, config, db, repo
from conftest import HOY, ids

AHORA = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class BancoFalso:
    """Responde como Enable Banking, con lo que le digamos."""

    def __init__(self, movimientos=None, saldos=None, sesion=None):
        self.movimientos = movimientos or {}      # uid -> lista de páginas
        self.saldos = saldos or {}
        self.sesion = sesion or SESION
        self.llamadas = []

    def aspsps(self, country):
        return [{"name": "Mi Banco", "country": country, "maximum_consent_validity": 15552000}]

    def start_auth(self, name, country, valid_until, state, redirect_url):
        self.llamadas.append(("auth", name, country, valid_until, state, redirect_url))
        return "https://banco.example/permiso"

    def create_session(self, code):
        self.llamadas.append(("sesion", code))
        return self.sesion

    def delete_session(self, session_id):
        self.llamadas.append(("borrar", session_id))

    def transactions(self, uid, date_from, continuation_key=None):
        self.llamadas.append(("movimientos", uid, date_from, continuation_key))
        paginas = self.movimientos.get(uid, [[]])
        i = int(continuation_key or 0)
        return {"transactions": paginas[i], "continuation_key": str(i + 1) if i + 1 < len(paginas) else None}

    def balances(self, uid):
        return self.saldos.get(uid, [])


SESION = {
    "session_id": "sesion-1",
    "access": {"valid_until": "2027-03-01T00:00:00Z"},
    "accounts": [
        {"uid": "u-corriente", "identification_hash": "h-corriente", "name": "Cuenta corriente",
         "account_id": {"iban": "ES7600000000000000001234"}, "currency": "EUR"},
        {"uid": "u-ahorro", "identification_hash": "h-ahorro", "name": "Cuenta de ahorro", "currency": "EUR"},
    ],
}


def mov(dia, importe, texto, sentido="DBIT", ref=None, estado="BOOK", moneda="EUR"):
    m = {"booking_date": dia, "transaction_amount": {"amount": importe, "currency": moneda},
         "credit_debit_indicator": sentido, "status": estado, "remittance_information": [texto]}
    if ref:
        m["entry_reference"] = ref
    return m


def conectar(conn, falso, desde="2026-09-01"):
    banco.start_connection(conn, falso, "Mi Banco", "ES", "https://app.example/banco/vuelta", now=AHORA)
    state = falso.llamadas[-1][4]
    link = banco.finish_connection(conn, falso, "codigo", state, HOY)
    with conn:
        conn.execute("UPDATE bank_accounts SET since = ?", (desde,))
    return conn.execute("SELECT * FROM bank_links WHERE id = ?", (link["id"],)).fetchone()


def sincronizar(conn, falso, link):
    return banco.sync_link(conn, falso, link, HOY, now=AHORA)


def filas(conn):
    return repo.tx_rows(conn, oldest_first=True)


# ---------------------------------------------------------------- firma

def test_el_token_va_firmado_como_pide_enable_banking():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = clave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
    token = banco.make_jwt("app-123", pem, now=1_800_000_000)
    cabecera, cuerpo, firma = token.split(".")
    leer = lambda t: json.loads(base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)))   # noqa: E731
    assert leer(cabecera) == {"typ": "JWT", "alg": "RS256", "kid": "app-123"}
    assert leer(cuerpo) == {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
                            "iat": 1_800_000_000, "exp": 1_800_003_600}
    clave.public_key().verify(base64.urlsafe_b64decode(firma + "=" * (-len(firma) % 4)),
                              f"{cabecera}.{cuerpo}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_la_clave_se_guarda_aparte_y_solo_para_la_app(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    banco.save_key(pem)
    assert banco.key_path().read_bytes() == pem
    assert banco.key_path().parent == config.SECRETS_DIR
    assert oct(os.stat(banco.key_path()).st_mode & 0o777) == "0o600"
    with pytest.raises(banco.BankError):
        banco.save_key(b"esto no es una clave")


# ---------------------------------------------------------------- conectar

def test_conectar_guarda_la_sesion_y_reconoce_las_cuentas(conn):
    falso = BancoFalso()
    link = conectar(conn, falso)
    assert link["session_id"] == "sesion-1" and link["state"] is None
    _, _, accs = ids(conn)
    cuentas = {c["ident"]: c for c in banco.accounts_of(conn, link["id"])}
    assert cuentas["h-corriente"]["account_id"] == accs["Cuenta corriente"]     # por el nombre
    assert cuentas["h-ahorro"]["account_id"] == accs["Cuenta de ahorro"]
    assert cuentas["h-corriente"]["iban"].endswith("1234")
    # el permiso se pide por el máximo que admita el banco (180 días), un pelín menos
    hasta = falso.llamadas[0][3]
    assert 179 <= (hasta - AHORA).days <= 180


def test_una_vuelta_que_no_es_nuestra_no_vale(conn):
    with pytest.raises(banco.BankError):
        banco.finish_connection(conn, BancoFalso(), "codigo", "otro-state", HOY)


def test_renovar_el_permiso_no_duplica_las_cuentas(conn):
    falso = BancoFalso()
    conectar(conn, falso)
    nueva = dict(SESION, session_id="sesion-2",
                 accounts=[dict(a, uid=a["uid"] + "-2") for a in SESION["accounts"]])
    falso.sesion = nueva
    conectar(conn, falso)
    assert conn.execute("SELECT COUNT(*) FROM bank_accounts").fetchone()[0] == 2
    assert conn.execute("SELECT uid FROM bank_accounts WHERE ident = 'h-corriente'").fetchone()[0] == "u-corriente-2"


# ---------------------------------------------------------------- sincronizar

def test_trae_gastos_e_ingresos_y_supone_la_categoria(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[
        mov("2026-09-10", "23.45", "COMPRA EN MERCADONA, CON LA TARJETA : 5402XXXXXXXX1234", ref="a1"),
        mov("2026-09-11", "1500.00", "NOMINA EMPRESA SA", "CRDT", ref="a2"),
        mov("2026-09-12", "9.99", "PENDIENTE", estado="PDNG", ref="a3"),          # sin apuntar aún
        mov("2026-09-12", "20.00", "TIENDA EN NUEVA YORK", moneda="USD", ref="a4"),
    ]]})
    r = sincronizar(conn, falso, conectar(conn, falso))
    assert r["error"] is None and r["nuevos"] == 2 and r["otra_moneda"] == 1
    gasto, ingreso = filas(conn)
    assert (gasto["type"], gasto["amount"], gasto["category"]) == ("gasto", 2345, "Compra")
    assert gasto["source"] == "banco" and gasto["review"] == 1 and gasto["account"] == "Cuenta corriente"
    assert (ingreso["type"], ingreso["amount"], ingreso["category"]) == ("ingreso", 150000, "Otros ingresos")
    assert "2 por revisar" in banco.summary_text(r)


def test_lo_que_ya_ha_entrado_no_vuelve_a_entrar_ni_aunque_lo_borres(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-10", "23.45", "MERCADONA", ref="a1")]]})
    link = conectar(conn, falso)
    sincronizar(conn, falso, link)
    sincronizar(conn, falso, link)
    assert len(filas(conn)) == 1
    with conn:
        repo.delete_tx(conn, filas(conn)[0]["id"])
    sincronizar(conn, falso, link)
    assert filas(conn) == []


def test_lo_apuntado_a_mano_se_reconoce_y_se_queda_lo_tuyo(conn):
    cats, _, accs = ids(conn)
    with conn:
        mio = repo.insert_tx(conn, date="2026-09-08", type="gasto", category_id=cats["Casa"], envelope_id=None,
                             account_id=accs["Cuenta corriente"], concept="La compra de la semana", amount=2345)
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-10", "23.45", "MERCADONA", ref="a1")]]})
    r = sincronizar(conn, falso, conectar(conn, falso))
    assert r["ya_estaban"] == 1 and r["nuevos"] == 0
    assert [f["id"] for f in filas(conn)] == [mio]
    assert filas(conn)[0]["concept"] == "La compra de la semana"


def test_un_traspaso_entre_tus_cuentas_es_un_traspaso(conn):
    falso = BancoFalso(movimientos={
        "u-corriente": [[mov("2026-09-10", "300.00", "TRANSFERENCIA A AHORRO", ref="c1")]],
        "u-ahorro": [[mov("2026-09-11", "300.00", "TRANSFERENCIA RECIBIDA", "CRDT", ref="h1")]],
    })
    link = conectar(conn, falso)
    r = sincronizar(conn, falso, link)
    assert r["traspasos"] == 1
    (t,) = filas(conn)
    _, _, accs = ids(conn)
    assert t["type"] == "traspaso" and t["amount"] == 30000
    assert (t["other_account_id"], t["account_id"]) == (accs["Cuenta corriente"], accs["Cuenta de ahorro"])
    saldos = calc.account_balances(repo.all_txs(conn), repo.accounts(conn))
    assert saldos[accs["Cuenta corriente"]] == -30000 and saldos[accs["Cuenta de ahorro"]] == 30000
    sincronizar(conn, falso, link)                                    # y no se rehace
    assert len(filas(conn)) == 1


def test_un_traspaso_que_ya_apuntaste_cubre_los_dos_lados(conn):
    _, _, accs = ids(conn)
    with conn:
        repo.insert_tx(conn, date="2026-09-10", type="traspaso", category_id=None, envelope_id=None,
                       account_id=accs["Cuenta de ahorro"], other_account_id=accs["Cuenta corriente"],
                       concept="Al ahorro", amount=30000)
    falso = BancoFalso(movimientos={
        "u-corriente": [[mov("2026-09-10", "300.00", "TRANSFERENCIA", ref="c1")]],
        "u-ahorro": [[mov("2026-09-11", "300.00", "TRANSFERENCIA", "CRDT", ref="h1")]],
    })
    r = sincronizar(conn, falso, conectar(conn, falso))
    assert r["ya_estaban"] == 2 and r["nuevos"] == 0 and len(filas(conn)) == 1


def test_solo_desde_la_fecha_elegida(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[
        mov("2026-08-30", "5.00", "ANTIGUO", ref="v1"), mov("2026-09-02", "6.00", "NUEVO", ref="n1")]]})
    sincronizar(conn, falso, conectar(conn, falso, desde="2026-09-01"))
    assert [f["concept"] for f in filas(conn)] == ["NUEVO"]
    pedido = next(c for c in falso.llamadas if c[0] == "movimientos")
    assert pedido[2] == date(2026, 9, 1)                    # la primera vez, desde la fecha elegida


def test_lee_todas_las_paginas(conn):
    falso = BancoFalso(movimientos={"u-corriente": [
        [mov("2026-09-02", "1.00", "UNO", ref="p1")], [mov("2026-09-03", "2.00", "DOS", ref="p2")]]})
    sincronizar(conn, falso, conectar(conn, falso))
    assert [f["concept"] for f in filas(conn)] == ["UNO", "DOS"]


def test_sin_identificador_del_banco_tampoco_duplica(conn):
    cafe = mov("2026-09-05", "1.50", "CAFETERIA SOL")
    falso = BancoFalso(movimientos={"u-corriente": [[cafe, dict(cafe)]]})          # dos cafés iguales
    link = conectar(conn, falso)
    sincronizar(conn, falso, link)
    sincronizar(conn, falso, link)
    assert len(filas(conn)) == 2


def test_el_saldo_del_banco_rellena_el_cuadre(conn):
    falso = BancoFalso(saldos={"u-corriente": [
        {"balance_type": "ITAV", "balance_amount": {"amount": "999.00", "currency": "EUR"}},
        {"balance_type": "ITBD", "balance_amount": {"amount": "1234.56", "currency": "EUR"}}]})
    link = conectar(conn, falso)
    sincronizar(conn, falso, link)
    sincronizar(conn, falso, link)                                    # una comprobación por día
    _, _, accs = ids(conn)
    checks = repo.checks(conn, accs["Cuenta corriente"])
    assert len(checks) == 1 and checks[0]["balance"] == 123456 and checks[0]["date"] == HOY.isoformat()


def test_un_permiso_caducado_no_llama_al_banco_y_lo_dice(conn):
    falso = BancoFalso()
    link = conectar(conn, falso)
    with conn:
        conn.execute("UPDATE bank_links SET valid_until = '2026-09-01T00:00:00Z'")
    link = conn.execute("SELECT * FROM bank_links").fetchone()
    r = sincronizar(conn, falso, link)
    assert "caducado" in r["error"]
    assert not any(c[0] == "movimientos" for c in falso.llamadas)
    assert conn.execute("SELECT last_error FROM bank_links").fetchone()[0] == r["error"]
    assert banco.expiring(conn, now=AHORA)[0]["expired"] is True


def test_una_cuenta_sin_destino_no_se_trae(conn):
    falso = BancoFalso(movimientos={"u-ahorro": [[mov("2026-09-05", "8.00", "ALGO", ref="z1")]]})
    link = conectar(conn, falso)
    with conn:
        conn.execute("UPDATE bank_accounts SET account_id = NULL WHERE ident = 'h-ahorro'")
    sincronizar(conn, falso, link)
    assert filas(conn) == []


def test_quitar_la_conexion_deja_lo_traido(conn):
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-05", "8.00", "ALGO", ref="z1")]]})
    link = conectar(conn, falso)
    sincronizar(conn, falso, link)
    banco.disconnect(conn, falso, link["id"])
    assert ("borrar", "sesion-1") in falso.llamadas
    assert banco.links(conn) == [] and len(filas(conn)) == 1


# ---------------------------------------------------------------- por la web

def test_la_pantalla_de_bancos(client, db_path, monkeypatch):
    html = client.get("/banco").text
    assert "Tu aplicación de Enable Banking" in html and "/banco/vuelta" in html
    r = client.post("/banco/config", data={"app_id": "app-1"},
                    files={"clave": ("mala.pem", b"no es una clave", "text/plain")}, follow_redirects=True)
    assert "no es una clave privada" in r.text


def test_conectar_y_volver_por_la_web(client, db_path, monkeypatch):
    falso = BancoFalso(movimientos={"u-corriente": [[mov("2026-09-18", "4.20", "CAFETERIA SOL", ref="w1")]]})
    monkeypatch.setattr(banco.Client, "from_settings", classmethod(lambda cls, conn: falso))
    monkeypatch.setattr(banco, "configured", lambda conn: True)
    r = client.post("/banco/conectar", data={"nombre": "Mi Banco", "pais": "ES"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://banco.example/permiso"
    state = falso.llamadas[-1][4]
    assert falso.llamadas[-1][5].endswith("/banco/vuelta")
    r = client.get(f"/banco/vuelta?code=abc&state={state}", follow_redirects=True)
    assert "Mi Banco conectado" in r.text
    conn = db.connect(db_path)
    assert conn.execute("SELECT session_id FROM bank_links").fetchone()[0] == "sesion-1"
    conn.close()


def test_si_el_banco_dice_que_no(client):
    r = client.get("/banco/vuelta?error=access_denied&error_description=Cancelled%20by%20user&state=x",
                   follow_redirects=True)
    assert "No se ha conectado: Cancelled by user" in r.text


def test_el_panel_avisa_antes_de_que_caduque(client, db_path):
    conn = db.connect(db_path)
    with conn:
        conn.execute("INSERT INTO bank_links (aspsp_name, aspsp_country, session_id, valid_until) "
                     "VALUES ('Mi Banco', 'ES', 's', ?)",
                     ((datetime.now(timezone.utc).replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    conn.close()
    assert "La conexión con Mi Banco" in client.get("/").text
