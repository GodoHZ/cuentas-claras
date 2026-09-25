"""Las reglas de cálculo de punta a punta: base de datos, vistas y cifras."""
from datetime import date

import pytest

from app import calc, repo, seed, views
from conftest import HOY, ids


def apuntar(conn, dia, tipo, importe, categoria=None, sobre=None, cuenta="Cuenta corriente", concepto=""):
    cats, envs, accs = ids(conn)
    with conn:
        return repo.insert_tx(conn, date=dia, type=tipo, category_id=cats.get(categoria),
                              envelope_id=envs.get(sobre), account_id=accs[cuenta],
                              concept=concepto, amount=importe)


def test_instalacion_nueva(conn):
    """Una base de datos recién creada: un esqueleto usable y sin movimientos."""
    cats, envs, accs = ids(conn)
    assert len(cats) == 14 and len(envs) == 3 and len(accs) == 2
    assert repo.all_txs(conn) == []
    assert repo.salary(conn) == 150000
    assert set(accs) == {"Cuenta corriente", "Cuenta de ahorro"}
    assert views.sweep_settings(conn)["envelope"]["name"] == "Colchón"
    assert views.envelope_account_name(conn) == "Cuenta de ahorro"
    assert views.envelopes_overview(conn, repo.all_txs(conn))[1] == 0


def test_un_mes_de_verdad(conn):
    """1.500 € de nómina, 500 de gastos y 200 guardados -> libre 800."""
    apuntar(conn, "2026-09-01", calc.INGRESO, 150000, categoria="Nómina", concepto="Nómina")
    apuntar(conn, "2026-09-02", calc.GASTO, 50000, categoria="Casa", concepto="Alquiler")
    apuntar(conn, "2026-09-03", calc.APORTE, 20000, sobre="Colchón", cuenta="Cuenta de ahorro")

    m = calc.month_summary(repo.all_txs(conn), 2026, 9)
    assert (m.income, m.payroll_spent, m.saved) == (150000, 50000, 20000)
    assert m.free == 80000
    assert m.saved_ratio == pytest.approx(20000 / 150000)

    cards, total = views.envelopes_overview(conn, repo.all_txs(conn))
    saldos = {c["env"]["name"]: c["st"] for c in cards}
    assert saldos["Colchón"].balance == 20000 and total == 20000
    assert saldos["Colchón"].months == 19            # faltan 2.800 € a 150 €/mes


def test_gasto_desde_sobre_no_cambia_el_libre(conn):
    """Lo pagado con dinero ya guardado no vuelve a restar del mes."""
    apuntar(conn, "2026-09-01", calc.INGRESO, 150000, categoria="Nómina")
    apuntar(conn, "2026-09-02", calc.APORTE, 40000, sobre="Coche", cuenta="Cuenta de ahorro")
    antes = calc.month_summary(repo.all_txs(conn), 2026, 9)

    apuntar(conn, "2026-09-20", calc.GASTO, 30000, categoria="Otros gastos", sobre="Coche",
            cuenta="Cuenta de ahorro", concepto="Revisión")

    txs = repo.all_txs(conn)
    despues = calc.month_summary(txs, 2026, 9)
    assert despues.free == antes.free                # el libre no se mueve
    assert despues.envelope_spent == 30000           # pero sí lo gastado desde sobres
    cats, envs, _ = ids(conn)
    assert calc.envelope_balances(txs)[envs["Coche"]] == 10000
    assert calc.spent_by_category(txs, 2026, 9)[cats["Otros gastos"]] == 30000   # y el presupuesto sí lo cuenta


def test_cuadre_de_una_cuenta(conn):
    """El saldo lo calcula la app; el cuadre solo comprueba si se te ha escapado algo."""
    cuentas = ids(conn)[2]
    ahorro = cuentas["Cuenta de ahorro"]
    apuntar(conn, "2026-09-01", calc.APORTE, 20000, sobre="Colchón", cuenta="Cuenta de ahorro")
    tarjetas, _ = views.accounts_overview(conn, repo.all_txs(conn))
    saldo = next(t["balance"] for t in tarjetas if t["account"]["id"] == ahorro)
    with conn:
        repo.add_check(conn, ahorro, date(2026, 9, 19), saldo)
    assert views.reconciliation(conn, ahorro, saldo)["r"].message == "Cuadra con el banco"

    with conn:                                       # 5 € de intereses sin apuntar
        repo.add_check(conn, ahorro, date(2026, 9, 20), saldo + 500)
    cuadre = views.reconciliation(conn, ahorro, saldo)["r"]
    assert cuadre.level == "sobra" and "intereses" in cuadre.message


def test_prestamo(conn):
    with conn:
        conn.execute("INSERT INTO loans (name, installment, first_date, n_installments) VALUES (?, ?, ?, ?)",
                     ("Préstamo del coche", 25000, "2026-10-02", 120))
    d = views.debts(conn, repo.all_txs(conn), HOY)
    st = d["loans"][0]["st"]
    assert st.last == date(2036, 9, 2)               # 1.ª cuota + 119 meses
    assert st.paid == 0 and st.pending == 120        # hoy es 19/09/2026: todavía no ha empezado
    assert st.pending_total == 120 * 25000
    assert d["ratio"] == pytest.approx(25000 / 150000, abs=1e-5)
    assert d["ratio"] < d["limit"]


def test_seed_es_idempotente(conn):
    antes = (len(repo.categories(conn)), len(repo.envelopes(conn)), len(repo.all_txs(conn)))
    assert seed.seed(conn).get("omitido") is True          # con la marca puesta no hace nada
    forzado = seed.seed(conn, force=True)                  # forzado: tampoco duplica
    assert sum(v for v in forzado.values() if isinstance(v, int)) == 0
    assert (len(repo.categories(conn)), len(repo.envelopes(conn)), len(repo.all_txs(conn))) == antes
