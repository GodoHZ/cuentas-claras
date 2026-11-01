"""Los nombres que enseña la app para lo que trae el banco."""
import pytest

from app.nombres import limpio


@pytest.mark.parametrize("banco, nombre", [
    ("ADEUDO A SU CARGO // PAGO DE ADEUDO DIRECTO SEPA // N 2026296000031121 Basic-Fit Spain S.A.U (BNP)", "Basic-Fit"),
    ("PAGO CON TARJETA EN GASOLINERAS // PAGO CON TARJETA // PASEO CASTELLANA MADRID ES", "Paseo Castellana Madrid"),
    ("PAGO CON TARJETA EN DISCOS, LIBROS, FOTOS Y PC'S // PAGO CON TARJETA // ANTHROPIC* CLAUDE SUB", "Anthropic Claude Sub"),
    ("***** · BIZUM // OTROS // RECIBIDO: Pago AirBnB viaje Valencia", "Bizum: Pago AirBnB viaje Valencia"),
    ("***** · BIZUM // OTROS // RECIBIDO: Bizum de POL", "Bizum de POL"),
    ("***** · BIZUM // OTROS // ENVIADO: Sin concepto", "Bizum enviado"),
    ("***** · BIZUM // OTROS // RECIBIDO: 67676767", "Bizum recibido"),
    ("ABONO DE NOMINA POR TRANSFERENCIA // INGRESO POR NÓMINA O PENSIÓN // HABERES GRUPO GRANSOLAR S.L.",
     "Nómina · Grupo Gransolar"),
    ("ADEUDO A SU CARGO // PAGO DE ADEUDO DIRECTO SEPA", ""),           # no dice quién
    ("Cena con amigos", "Cena con amigos"),                              # lo escrito a mano, tal cual
    ("", ""),
])
def test_nombres_limpios(banco, nombre):
    assert limpio(banco) == nombre
