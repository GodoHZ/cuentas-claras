"""Las pantallas: que responden, que guardan y que enseñan las cifras buenas."""
from datetime import date

from app import calc, db, repo
from conftest import apuntar, ids


def get(client, url, code=200):
    r = client.get(url)
    assert r.status_code == code, f"{url} -> {r.status_code}"
    return r


def test_todas_las_pantallas_responden(client):
    for url in ["/", "/movimientos", "/movimientos?mes=todos", "/sobres", "/presupuesto",
                "/resumen", "/deudas", "/mas", "/ajustes", "/nuevo", "/ajustes/importar",
                "/salud", "/manifest.webmanifest", "/sw.js", "/offline"]:
        get(client, url)


def test_panel_ensena_las_cifras(client, db_path):
    conn = db.connect(db_path)
    apuntar(conn, "2026-09-01", calc.INGRESO, 150000, categoria="Nómina")
    apuntar(conn, "2026-09-02", calc.GASTO, 50000, categoria="Casa")
    apuntar(conn, "2026-09-03", calc.APORTE, 30000, sobre="Colchón", cuenta="Cuenta de ahorro")
    with conn:
        repo.add_check(conn, date(2026, 9, 19), 30000)
    conn.close()
    html = get(client, "/").text
    assert "Libre" in html
    assert "700,00 €" in html          # 1.500 − 500 − 300
    assert "300,00 €" in html          # total en sobres
    assert "Todo apuntado" in html                 # el cuadre coincide


def test_sobres_y_detalle(client, db_path):
    conn = db.connect(db_path)
    envs = ids(conn)[1]
    apuntar(conn, "2026-09-02", calc.APORTE, 120000, sobre="Vacaciones", cuenta="Cuenta de ahorro")
    conn.close()
    html = get(client, "/sobres").text
    assert "¡Conseguido!" in html and "1.200,00 €" in html
    detalle = get(client, f"/sobres/{envs['Coche']}").text
    assert "Seguro, revisiones" in detalle and "Historial" in detalle
    get(client, "/sobres/9999", 404)


def test_apuntar_editar_y_borrar(client, db_path):
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    conn.close()

    r = client.post("/movimientos/nuevo", data={
        "tipo": "gasto", "importe": "25,50", "categoria_id": cats["Ocio"], "sobre_id": "",
        "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-19", "concepto": "Cena"})
    assert r.status_code in (200, 303)
    conn = db.connect(db_path)
    fila = repo.tx_rows(conn, month=(2026, 9))[0]
    conn.close()
    assert (fila["amount"], fila["category"], fila["concept"]) == (2550, "Ocio", "Cena")

    r = client.post(f"/movimientos/{fila['id']}", data={
        "tipo": "gasto", "importe": "30", "categoria_id": cats["Ocio"], "sobre_id": "",
        "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-19", "concepto": "Cena y copas"})
    assert r.status_code in (200, 303)
    conn = db.connect(db_path)
    assert repo.get_tx(conn, fila["id"])["amount"] == 3000
    conn.close()

    client.post(f"/movimientos/{fila['id']}/borrar")
    conn = db.connect(db_path)
    assert repo.get_tx(conn, fila["id"]) is None
    conn.close()


def test_errores_del_formulario(client, db_path):
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    conn.close()
    r = client.post("/movimientos/nuevo", data={
        "tipo": "gasto", "importe": "", "categoria_id": "", "sobre_id": "",
        "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-19", "concepto": ""})
    assert r.status_code == 200
    assert "Escribe el importe." in r.text and "Elige una categoría." in r.text

    r = client.post("/movimientos/nuevo", data={
        "tipo": "aporte_sobre", "importe": "10", "sobre_id": "", "cuenta_id": accs["Cuenta de ahorro"],
        "fecha": "2026-09-19"})
    assert "Elige el sobre." in r.text

    r = client.post("/movimientos/nuevo", data={
        "tipo": "gasto", "importe": "doce", "categoria_id": cats["Ocio"],
        "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-19"})
    assert "El importe no es válido" in r.text
    conn = db.connect(db_path)
    assert repo.all_txs(conn) == []                    # no se ha guardado nada
    conn.close()


def test_apuntar_desde_la_hoja_del_boton_mas(client, db_path):
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    conn.close()
    r = client.post("/movimientos/nuevo", headers={"HX-Request": "true"}, data={
        "origen": "dialogo", "tipo": "aporte_sobre", "importe": "100", "sobre_id": envs["Colchón"],
        "cuenta_id": accs["Cuenta de ahorro"], "fecha": "2026-09-20", "concepto": ""})
    assert r.status_code == 200
    assert "txGuardado" in r.headers.get("HX-Trigger", "")
    assert "Colch" in r.headers["HX-Trigger"] or "Colch" in r.headers["HX-Trigger"].encode().decode("unicode_escape")
    assert '<form id="txform-qa"' in r.text          # vuelve el formulario en blanco


def test_cuadre(client, db_path):
    conn = db.connect(db_path)
    apuntar(conn, "2026-09-02", calc.APORTE, 30000, sobre="Colchón", cuenta="Cuenta de ahorro")
    conn.close()
    r = client.post("/cuadre", headers={"HX-Request": "true"}, data={"saldo": "301,50", "fecha": "2026-09-20"})
    assert r.status_code == 200
    assert "intereses" in r.text                     # 1,50 € de más
    r = client.post("/cuadre", headers={"HX-Request": "true"}, data={"saldo": "tres mil"})
    assert "no es válido" in r.text


def test_presupuesto_y_resumen(client, db_path):
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    with conn:
        repo.insert_tx(conn, date="2026-09-10", type="gasto", category_id=cats["Ocio"],
                       envelope_id=None, account_id=accs["Cuenta corriente"], concept="Cine", amount=13000)
    conn.close()
    html = get(client, "/presupuesto?mes=2026-09").text
    assert "Ocio" in html and "Cerca del límite" in html      # 130 de 150 = 87 %
    resumen = get(client, "/resumen").text
    assert "sep 26" in resumen and "chart-resumen" in resumen


def test_exportar(client):
    j = get(client, "/exportar/json")
    assert j.json()["ajustes"]["salary"] == "150000"
    assert "pin_hash" not in j.json()["ajustes"]
    z = get(client, "/exportar/csv")
    assert z.content[:2] == b"PK"


def test_importar_csv_por_la_web(client):
    csv = ("Fecha,Tipo,Categoría,Sobre,Cuenta,Concepto,Importe (€)\n"
           "25/09/2026,Gasto,Ocio,,Cuenta corriente,Concierto,\"40,00 €\"\n")
    r = client.post("/ajustes/importar", files={"archivo": ("mov.csv", csv, "text/csv")})
    assert r.status_code == 200 and "Vista previa" in r.text and "Concierto" in r.text
    import re
    payload = re.search(r'name="payload" value="([^"]+)"', r.text)[1]
    r = client.post("/ajustes/importar/confirmar", data={"payload": payload}, follow_redirects=True)
    assert "Concierto" in get(client, "/movimientos?mes=2026-09").text


def test_ajustes_guardan(client, db_path):
    client.post("/ajustes/general", data={"nomina": "1.600", "mes_inicio": "2026-10",
                                          "categoria_amortizacion": "", "cuenta_general": "1", "cuenta_sobres": "2"})
    conn = db.connect(db_path)
    assert repo.salary(conn) == 160000 and repo.start_month(conn) == (2026, 10)

    client.post("/ajustes/lista/categorias", data={"nombre": "Mascotas", "tipo": "gasto", "presupuesto": "40"})
    nueva = [c for c in repo.categories(conn) if c["name"] == "Mascotas"][0]
    assert nueva["budget"] == 4000
    client.post(f"/ajustes/lista/categorias/{nueva['id']}", data={"nombre": "Perro", "tipo": "gasto", "presupuesto": "45"})
    assert repo.get_row(conn, "categories", nueva["id"])["name"] == "Perro"
    client.post(f"/ajustes/lista/categorias/{nueva['id']}/archivar")
    assert repo.get_row(conn, "categories", nueva["id"])["active"] == 0

    client.post("/ajustes/lista/sobres", data={"nombre": "Viaje a Japón", "objetivo": "1.200", "aporte": "100", "nota": "Verano"})
    sobre = [e for e in repo.envelopes(conn) if e["name"] == "Viaje a Japón"][0]
    assert (sobre["target"], sobre["monthly"], sobre["note"]) == (120000, 10000, "Verano")

    client.post("/ajustes/prestamos", data={"nombre": "Sofá", "cuota": "50", "primera": "2026-11-01", "cuotas": "10"})
    assert any(loan["name"] == "Sofá" for loan in repo.loans(conn))
    conn.close()


def test_ordenar_categorias(client, db_path):
    conn = db.connect(db_path)
    orden = [c["id"] for c in repo.categories(conn)]
    client.post(f"/ajustes/lista/categorias/{orden[1]}/mover", data={"dir": "arriba"})
    nuevo = [c["id"] for c in repo.categories(conn)]
    assert nuevo[0] == orden[1] and nuevo[1] == orden[0]
    conn.close()


def test_nombres_repetidos(client, db_path):
    r = client.post("/ajustes/lista/categorias", data={"nombre": "Ocio", "tipo": "gasto", "presupuesto": ""},
                    follow_redirects=True)
    assert "Ya existe" in r.text


def test_copia_manual_desde_ajustes(client, tmp_path):
    r = client.post("/ajustes/copia", follow_redirects=True)
    assert "Copia hecha" in r.text
    assert "manual" in get(client, "/ajustes?abierto=copias").text


def test_pin(client, db_path):
    # sin PIN, la app entra directamente
    assert client.get("/", follow_redirects=False).status_code == 200
    client.post("/ajustes/pin", data={"pin": "1234", "pin2": "1234"})
    conn = db.connect(db_path)
    assert repo.setting(conn, "pin_hash").startswith("pbkdf2$")
    conn.close()

    sin_cookie = type(client)(client.app)                     # cliente nuevo, sin la cookie
    r = sin_cookie.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/pin")
    assert sin_cookie.get("/pin").status_code == 200
    assert "PIN incorrecto" in sin_cookie.post("/pin", data={"pin": "9999", "next": "/"}).text
    r = sin_cookie.post("/pin", data={"pin": "1234", "next": "/movimientos"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/movimientos"
    assert sin_cookie.get("/", follow_redirects=False).status_code == 200

    client.post("/ajustes/pin/quitar")
    assert type(client)(client.app).get("/", follow_redirects=False).status_code == 200


def test_pin_no_tapa_los_estaticos_ni_la_salud(client):
    client.post("/ajustes/pin", data={"pin": "4321", "pin2": "4321"})
    otro = type(client)(client.app)
    assert otro.get("/salud").status_code == 200
    assert otro.get("/static/css/app.css").status_code == 200
    assert otro.get("/manifest.webmanifest").status_code == 200
    client.post("/ajustes/pin/quitar")


def test_redireccion_de_pin_solo_a_rutas_internas(client):
    r = client.get("/pin?next=https://malo.example.com")
    assert 'value="/"' in r.text


# ---------------------------------------------------------------- configurable

def test_presupuesto_se_edita_desde_su_pantalla(client, db_path):
    """Se podían cambiar, pero solo desde Ajustes y abriendo cada categoría."""
    html = get(client, "/presupuesto").text
    assert "/presupuesto/editar" in html          # el botón está a la vista

    conn = db.connect(db_path)
    cats = ids(conn)[0]
    conn.close()
    editar = get(client, "/presupuesto/editar?mes=2026-09").text
    assert f'name="presupuesto_{cats["Ocio"]}"' in editar
    assert 'name="nueva_categoria"' in editar

    r = client.post("/presupuesto/editar", data={
        "mes": "2026-09",
        f"presupuesto_{cats['Ocio']}": "222,50",
        f"presupuesto_{cats['Transporte']}": "",          # vaciar = sin tope
        "nueva_categoria": "Mascotas", "nuevo_presupuesto": "40"},
        follow_redirects=True)
    assert r.status_code == 200
    conn = db.connect(db_path)
    porNombre = {c["name"]: c for c in repo.categories(conn)}
    assert porNombre["Ocio"]["budget"] == 22250
    assert porNombre["Transporte"]["budget"] is None
    assert porNombre["Mascotas"]["budget"] == 4000
    conn.close()


def test_presupuesto_editar_avisa_si_el_importe_esta_mal(client, db_path):
    conn = db.connect(db_path)
    cats = ids(conn)[0]
    antes = repo.get_row(conn, "categories", cats["Ocio"])["budget"]
    conn.close()
    r = client.post("/presupuesto/editar", data={
        "mes": "2026-09", f"presupuesto_{cats['Ocio']}": "mucho dinero"}, follow_redirects=True)
    assert "no es un importe válido" in r.text
    conn = db.connect(db_path)
    assert repo.get_row(conn, "categories", cats["Ocio"])["budget"] == antes   # no toca nada
    conn.close()


def test_reglas_configurables(client, db_path):
    conn = db.connect(db_path)
    cats, _, accs = ids(conn)
    with conn:                                   # 100 € de 150 € = 67 %
        repo.insert_tx(conn, date="2026-09-10", type="gasto", category_id=cats["Ocio"],
                       envelope_id=None, account_id=accs["Cuenta corriente"], concept="Ocio", amount=10000)
    conn.close()
    assert "Cerca del límite" not in get(client, "/presupuesto?mes=2026-09").text

    client.post("/ajustes/reglas", data={"budget_warn_pct": "60", "installments_limit_pct": "15",
                                         "reconcile_small_cents": "1", "backup_keep_days": "7"})
    html = get(client, "/presupuesto?mes=2026-09").text
    assert "Cerca del límite" in html            # con el aviso al 60 %, ahora sí
    assert "Te avisa por encima del 15" in get(client, "/").text
    conn = db.connect(db_path)
    assert repo.rule(conn, "backup_keep_days") == 7
    conn.close()

    r = client.post("/ajustes/reglas", data={"budget_warn_pct": "500", "installments_limit_pct": "35",
                                             "reconcile_small_cents": "20", "backup_keep_days": "30"},
                    follow_redirects=True)
    assert "tiene que ser un número entre" in r.text

    client.post("/ajustes/restablecer-reglas")
    conn = db.connect(db_path)
    assert repo.rule(conn, "budget_warn_pct") == 80 and repo.rule(conn, "backup_keep_days") == 30
    conn.close()


def test_empezar_de_cero(client, db_path, tmp_path):
    conn = db.connect(db_path)
    apuntar(conn, "2026-09-01", calc.INGRESO, 150000, categoria="Nómina")
    apuntar(conn, "2026-09-02", calc.APORTE, 20000, sobre="Colchón", cuenta="Cuenta de ahorro")
    assert len(repo.all_txs(conn)) == 2
    conn.close()

    r = client.post("/ajustes/empezar-de-cero", data={"alcance": "movimientos", "confirmacion": "vale"},
                    follow_redirects=True)
    assert "hay que escribir BORRAR" in r.text
    conn = db.connect(db_path)
    assert len(repo.all_txs(conn)) == 2          # sigue todo
    conn.close()

    r = client.post("/ajustes/empezar-de-cero", data={"alcance": "movimientos", "confirmacion": "borrar"},
                    follow_redirects=True)
    conn = db.connect(db_path)
    assert repo.all_txs(conn) == [] and repo.last_check(conn) is None
    assert len(repo.categories(conn)) == 14      # las categorías se quedan
    conn.close()
    assert list((tmp_path / "backups").glob("*-manual.db"))   # copia antes de borrar

    client.post("/ajustes/empezar-de-cero", data={"alcance": "todo", "confirmacion": "BORRAR"})
    conn = db.connect(db_path)
    assert repo.categories(conn) == [] and repo.envelopes(conn) == [] and repo.loans(conn) == []
    cuentas = repo.accounts(conn)
    assert len(cuentas) == 1 and cuentas[0]["name"] == "Cuenta principal"
    assert repo.int_setting(conn, "default_account_id") == cuentas[0]["id"]
    conn.close()
    get(client, "/")                             # la app sigue funcionando vacía
    get(client, "/presupuesto")
    get(client, "/nuevo")


def test_nombre_de_la_cuenta_de_sobres_no_esta_fijo(client, db_path):
    assert "Cuadre con Cuenta de ahorro" in get(client, "/").text
    conn = db.connect(db_path)
    envelope_account = repo.int_setting(conn, "envelope_account_id")
    conn.close()
    client.post(f"/ajustes/lista/cuentas/{envelope_account}", data={"nombre": "MyInvestor"})
    html = get(client, "/").text
    assert "Cuadre con MyInvestor" in html and "Cuenta de ahorro" not in html


def test_archivar_y_borrar_desde_ajustes(client, db_path):
    """El botón de archivar estaba dentro del formulario de editar y no hacía nada."""
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    conn.close()

    # un sobre recién creado, sin movimientos: se puede archivar y borrar
    client.post("/ajustes/lista/sobres", data={"nombre": "Sobre de prueba", "objetivo": "", "aporte": "", "nota": ""})
    conn = db.connect(db_path)
    prueba = [e for e in repo.envelopes(conn) if e["name"] == "Sobre de prueba"][0]
    conn.close()

    client.post(f"/ajustes/lista/sobres/{prueba['id']}/archivar")
    conn = db.connect(db_path)
    assert repo.get_row(conn, "envelopes", prueba["id"])["active"] == 0
    conn.close()
    client.post(f"/ajustes/lista/sobres/{prueba['id']}/archivar")          # y desarchivar
    conn = db.connect(db_path)
    assert repo.get_row(conn, "envelopes", prueba["id"])["active"] == 1
    conn.close()

    r = client.post(f"/ajustes/lista/sobres/{prueba['id']}/borrar", follow_redirects=True)
    assert "borrado" in r.text
    conn = db.connect(db_path)
    assert repo.get_row(conn, "envelopes", prueba["id"]) is None
    conn.close()


def test_no_se_borra_lo_que_tiene_historial(client, db_path):
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    apuntar(conn, "2026-09-02", calc.APORTE, 5000, sobre="Vacaciones", cuenta="Cuenta de ahorro")
    conn.close()
    # «Vacaciones» ya tiene un movimiento
    r = client.post(f"/ajustes/lista/sobres/{envs['Vacaciones']}/borrar", follow_redirects=True)
    assert "no se puede borrar" in r.text
    # el «Colchón» está elegido para las sobras del mes
    r = client.post(f"/ajustes/lista/sobres/{envs['Colchón']}/borrar", follow_redirects=True)
    assert "no se puede borrar" in r.text
    # y las cuentas por defecto tampoco
    r = client.post(f"/ajustes/lista/cuentas/{accs['Cuenta corriente']}/borrar", follow_redirects=True)
    assert "no se puede borrar" in r.text
    conn = db.connect(db_path)
    assert len(repo.envelopes(conn)) == 3 and len(repo.accounts(conn)) == 2
    conn.close()


def test_ajustes_no_tiene_formularios_anidados(client):
    """Un <form> dentro de otro lo descarta el navegador: los botones dejan de funcionar."""
    import re
    html = get(client, "/ajustes").text
    profundidad = 0
    for etiqueta in re.findall(r"<form\b|</form>", html):
        profundidad += 1 if etiqueta == "<form" else -1
        assert profundidad in (0, 1), "hay un formulario dentro de otro en Ajustes"


def test_reenviar_lo_apuntado_sin_conexion_no_duplica(client, db_path):
    """El móvil reenvía lo que guardó sin red; el mismo uid no entra dos veces."""
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    conn.close()
    datos = {"origen": "dialogo", "uid": "abc-123", "tipo": "gasto", "importe": "12,34",
             "categoria_id": cats["Ocio"], "sobre_id": "", "cuenta_id": accs["Cuenta corriente"],
             "fecha": "2026-09-19", "concepto": "Cena"}

    primera = client.post("/movimientos/nuevo", headers={"HX-Request": "true"}, data=datos)
    segunda = client.post("/movimientos/nuevo", headers={"HX-Request": "true"}, data=datos)
    assert primera.status_code == 200 and segunda.status_code == 200
    assert "txGuardado" in segunda.headers.get("HX-Trigger", "")      # el móvil lo da por enviado

    conn = db.connect(db_path)
    movimientos = repo.tx_rows(conn, month=(2026, 9))
    assert len(movimientos) == 1 and movimientos[0]["amount"] == 1234
    assert movimientos[0]["client_uid"] == "abc-123"
    conn.close()


def test_sin_uid_se_pueden_repetir_movimientos_iguales(client, db_path):
    """Dos cafés iguales el mismo día son dos movimientos, no un duplicado."""
    conn = db.connect(db_path)
    cats, envs, accs = ids(conn)
    conn.close()
    datos = {"tipo": "gasto", "importe": "1,60", "categoria_id": cats["Ocio"], "sobre_id": "",
             "cuenta_id": accs["Cuenta corriente"], "fecha": "2026-09-19", "concepto": "Café"}
    client.post("/movimientos/nuevo", data=datos)
    client.post("/movimientos/nuevo", data=datos)
    conn = db.connect(db_path)
    assert len(repo.tx_rows(conn, month=(2026, 9))) == 2
    conn.close()


def test_con_pin_el_movil_no_guarda_copias_de_las_pantallas(client, db_path):
    """Si no, al abrir la app sin conexión se verían sin pedir el PIN."""
    assert "X-Sin-Copia" not in get(client, "/").headers
    client.post("/ajustes/pin", data={"pin": "1234", "pin2": "1234"})
    assert get(client, "/").headers.get("X-Sin-Copia") == "1"
    client.post("/ajustes/pin/quitar")
    assert "X-Sin-Copia" not in get(client, "/").headers


def test_se_puede_elegir_si_el_movil_guarda_copias(client, db_path):
    assert "X-Sin-Copia" not in get(client, "/").headers          # sin PIN, guarda
    client.post("/ajustes/pin", data={"pin": "1234", "pin2": "1234"})
    assert get(client, "/").headers.get("X-Sin-Copia") == "1"     # con PIN, por defecto no

    client.post("/ajustes/sin-conexion", data={"activo": "1"})    # pero se puede activar
    assert "X-Sin-Copia" not in get(client, "/").headers
    client.post("/ajustes/sin-conexion", data={})                 # y volver a quitar
    assert get(client, "/").headers.get("X-Sin-Copia") == "1"
    client.post("/ajustes/pin/quitar")
