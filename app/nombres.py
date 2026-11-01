"""Nombres legibles para lo que trae el banco.

El banco escribe «PAGO CON TARJETA EN GASOLINERAS // PAGO CON TARJETA // PASEO CASTELLANA MADRID ES»;
en la lista basta con «Paseo Castellana Madrid». Solo cambia cómo se enseña: el concepto guardado
sigue siendo el del banco (de él aprende la app las categorías).
"""
from __future__ import annotations

import re

SUFIJOS_EMPRESA = re.compile(r"(\b(S\.?\s?A\.?\s?U?\.?|S\.?\s?L\.?\s?U?\.?|SPAIN|ESPA[ÑN]A)|\([A-Z]{2,5}\))\s*$", re.I)
PAIS_AL_FINAL = re.compile(r"\s+(ES|ESP)$")
REFERENCIA = re.compile(r"^(N\s*)?\d{6,}\s*")
# lo que el banco pone cuando no dice quién: no es un nombre («PAGO DE ADEUDO DIRECTO SEPA»)
GENERICO = re.compile(r"^(PAGO|ADEUDO|ABONO|TRANSFERENCIA|RECIBO|COMPRA|CARGO|OPERACI[OÓ]N)\b")
MINUSCULAS = {"de", "del", "la", "las", "el", "los", "y", "en", "a"}


def _titulo(texto: str) -> str:
    """«PASEO CASTELLANA MADRID» -> «Paseo Castellana Madrid»; lo que ya tiene minúsculas se respeta."""
    if any(c.islower() for c in texto):
        return texto
    palabras = []
    for i, p in enumerate(texto.lower().split()):
        palabras.append(p if (i and p in MINUSCULAS) else p[:1].upper() + p[1:])
    return " ".join(palabras)


def limpio(concepto: str | None) -> str:
    """El concepto, sin la paja del banco. Si no es del banco, tal cual; si no dice quién, vacío."""
    concepto = (concepto or "").strip()
    if "//" not in concepto:
        return concepto
    partes = [p.strip(" ·*") for p in concepto.split("//") if p.strip(" ·*")]
    ultimo = partes[-1] if partes else concepto

    if "BIZUM" in concepto.upper():
        detalle = re.sub(r"^(RECIBIDO|ENVIADO):\s*", "", ultimo, flags=re.I).strip()
        sentido = "de" if "RECIBIDO" in concepto.upper() else "a"
        if not detalle or detalle.lower() == "sin concepto" or detalle.isdigit():
            return f"Bizum {'recibido' if sentido == 'de' else 'enviado'}"
        return detalle if detalle.lower().startswith("bizum") else f"Bizum: {detalle}"
    if "NOMINA" in concepto.upper() or "NÓMINA" in concepto.upper():
        empresa = re.sub(r"^HABERES\s+", "", ultimo, flags=re.I)
        return f"Nómina · {_titulo(SUFIJOS_EMPRESA.sub('', empresa).strip(' .,'))}"

    if GENERICO.match(ultimo):
        return ""                      # sin nombre: mejor la categoría que «Pago de adeudo directo»
    nombre = REFERENCIA.sub("", ultimo)
    nombre = PAIS_AL_FINAL.sub("", nombre)
    for _ in range(3):
        nombre = SUFIJOS_EMPRESA.sub("", nombre).strip(" .,")
    nombre = nombre.replace("*", " ").strip()
    nombre = re.sub(r"\s{2,}", " ", nombre)
    return _titulo(nombre) or concepto
