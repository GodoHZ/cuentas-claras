from app import calc, importer, repo
from conftest import apuntar, ids

CSV = """Fecha,Tipo,Categoría,Sobre,Cuenta,Concepto,Importe (€)
19/09/2026,Gasto,Ocio,,Cuenta corriente,Cena,"25,50 €"
19/09/2026,Ingreso,Nómina,,Cuenta corriente,Nómina de septiembre,"1.423,00 €"
20/09/2026,Aporte a sobre,,Coche,Cuenta de ahorro,Aporte mensual,"100,00 €"
21/09/2026,Gasto,Transporte,Coche,Cuenta de ahorro,Gasoil,"60,00 €"
22/09/2026,Gasto,Ocio,,Cuenta corriente,Fila de ejemplo (ejemplo),"10,00 €"
23/09/2026,Gasto,Ocio,,Cuenta corriente,Sin importe,
31/08/2026,Aporte a sobre,,Vacaciones,Cuenta de ahorro,Ahorro de agosto,"2.000,00 €"
24/09/2026,Gasto,Mascotas,,Cuenta corriente,Pienso,"30,00 €"
25/09/2026,Gasto,Ocio,,Cuenta corriente,Fecha rara,"5,00 €"
"""


def preview(conn, text=CSV):
    return importer.parse(conn, text)


def ya_apuntado(conn):
    """El movimiento que en el CSV tiene que salir como «ya está en la app»."""
    apuntar(conn, "2026-08-31", calc.APORTE, 200000, sobre="Vacaciones",
            cuenta="Cuenta de ahorro", concepto="Ahorro de agosto")


def test_vista_previa_clasifica_las_filas(conn):
    ya_apuntado(conn)
    p = preview(conn)
    estados = {r.line: r.status for r in p.rows}
    assert estados[2] == "ok"                       # gasto normal
    assert estados[3] == "ok"
    assert estados[4] == "ok"
    assert estados[5] == "ok"                       # gasto pagado desde un sobre
    assert estados[6] == "ignorada"                 # (ejemplo)
    assert estados[7] == "ignorada"                 # sin importe
    assert estados[8] == "duplicada"                # ese ya estaba apuntado
    assert estados[9] == "falta"                    # la categoría Mascotas no existe
    assert p.missing_categories == {"Mascotas"}
    assert p.count("ok") == 5


def test_importar_sin_crear_lo_que_falta(conn):
    ya_apuntado(conn)
    p = preview(conn)
    antes = len(repo.all_txs(conn))
    añadidos = importer.apply(conn, p)
    assert añadidos == 5
    assert len(repo.all_txs(conn)) == antes + 5
    assert not any(c["name"] == "Mascotas" for c in repo.categories(conn))
    # reimportar el mismo CSV no duplica nada
    assert importer.apply(conn, preview(conn)) == 0


def test_importar_creando_lo_que_falta(conn):
    ya_apuntado(conn)
    añadidos = importer.apply(conn, preview(conn), create_missing=True)
    assert añadidos == 6
    nuevas = {c["name"]: c["kind"] for c in repo.categories(conn)}
    assert nuevas["Mascotas"] == "gasto"


def test_importe_y_cuenta_por_defecto(conn):
    cats, envs, accs = ids(conn)
    texto = ("Fecha;Tipo;Categoría;Sobre;Cuenta;Concepto;Importe\n"
             "01/10/2026;aporte_sobre;;Colchón;;Lo que sobra;50\n")
    p = importer.parse(conn, texto)
    assert p.rows[0].status == "ok"
    assert p.rows[0].amount == 5000
    assert p.rows[0].account == "Cuenta de ahorro"      # cuenta por defecto de los sobres
    importer.apply(conn, p)
    fila = repo.tx_rows(conn, month=(2026, 10))[0]
    assert (fila["envelope"], fila["account"], fila["amount"]) == ("Colchón", "Cuenta de ahorro", 5000)


def test_errores_de_fila(conn):
    texto = ("Fecha,Tipo,Categoría,Sobre,Cuenta,Concepto,Importe (€)\n"
             "32/13/2026,Gasto,Ocio,,Cuenta corriente,Fecha imposible,\"5,00 €\"\n"
             "01/10/2026,Regalo,Ocio,,Cuenta corriente,Tipo inventado,\"5,00 €\"\n"
             "01/10/2026,Aporte a sobre,,,Cuenta de ahorro,Sin sobre,\"5,00 €\"\n"
             "01/10/2026,Gasto,Ocio,,Cuenta corriente,Importe raro,cinco euros\n")
    p = importer.parse(conn, texto)
    assert [r.status for r in p.rows] == ["error"] * 4
    assert "no válida" in p.rows[0].reason
    assert "desconocido" in p.rows[1].reason
    assert "Elige el sobre." in p.rows[2].reason
    assert "no es un importe válido" in p.rows[3].reason


def test_columnas_que_no_cuadran(conn):
    p = importer.parse(conn, "Una,Dos,Tres\n1,2,3\n")
    assert "No encuentro la columna" in p.error
    assert importer.parse(conn, "").error


def test_el_csv_exportado_se_vuelve_a_importar(conn):
    ya_apuntado(conn)
    apuntar(conn, "2026-09-05", calc.GASTO, 2550, categoria="Ocio", concepto="Cena")
    import io
    import zipfile

    from app import export
    zip_bytes = export.to_csv_zip(conn)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        movimientos = z.read("movimientos.csv").decode("utf-8-sig")
    p = importer.parse(conn, movimientos)
    assert p.count("duplicada") == 2 and p.count("ok") == 0     # todo estaba ya apuntado
