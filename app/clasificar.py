"""Adivinar la categoría de un gasto que la app apunta sola.

Sirve para los movimientos que llegan del banco: traen un concepto («REPSOL E.S.
1234 TOLEDO») y hay que decidir en qué categoría van. De más fiable a menos:

1. Lo que hiciste tú la última vez en ese mismo comercio: aprende.
2. Una lista de comercios conocidos (Repsol → «Gasolina»), si tienes una
   categoría con ese nombre.
3. La categoría «por si acaso» que elijas en Ajustes.

Si sale de 2 o de 3, el movimiento queda «por revisar». En la práctica: la
primera vez que pagas en un sitio te pide que lo mires; las siguientes ya lo sabe.
"""
from __future__ import annotations

import re
import unicodedata

from . import repo

# palabras que no dicen nada del comercio
GENERIC = {"el", "la", "los", "las", "de", "del", "y", "the", "and", "sl", "sa", "slu", "sau",
           "cb", "es", "tienda", "shop", "store",
           # lo que añaden los bancos alrededor del comercio
           "compra", "compras", "pago", "pagos", "tarjeta", "tarj", "con", "en", "por", "recibo",
           "adeudo", "cargo", "abono", "transferencia", "transf", "inmediata", "recibida", "emitida",
           "operacion", "num", "ref", "fecha", "debito", "credito", "visa", "mastercard"}
# pasarelas de pago: el comercio de verdad viene detrás («PAYPAL *SPOTIFY»)
INTERMEDIARIES = {"paypal", "sumup", "stripe", "zettle", "izettle", "square", "redsys", "bizum"}

# comercios conocidos -> nombres de categoría por orden de preferencia. Solo se
# usa una categoría si la tienes creada con ese nombre (sin mirar mayúsculas ni tildes).
HINTS = [
    (("Gasolina", "Combustible", "Transporte"),
     ("repsol", "cepsa", "moeve", "galp", "bp", "shell", "petronor", "ballenoil", "plenoil",
      "petroprix", "gasolinera", "estacion de servicio")),
    (("Compra", "Supermercado", "Alimentación"),
     ("mercadona", "carrefour", "lidl", "aldi", "dia", "alcampo", "eroski", "consum", "ahorramas",
      "hipercor", "supercor", "bonpreu", "condis", "gadis", "froiz", "coviran", "spar", "masymas",
      "caprabo", "bm supermercados")),
    (("Comida fuera", "Restaurantes", "Comer fuera"),
     ("mcdonalds", "mc donalds", "burger king", "kfc", "telepizza", "dominos", "five guys",
      "starbucks", "glovo", "just eat", "uber eats", "rodilla", "vips", "goiko", "montaditos",
      "taco bell", "foster s hollywood", "restaurante", "cafeteria", "bar")),
    (("Ropa",),
     ("zara", "pull bear", "bershka", "stradivarius", "primark", "h m", "mango", "springfield",
      "lefties", "massimo dutti", "kiabi", "uniqlo", "decathlon")),
    (("Suscripciones",),
     ("netflix", "spotify", "hbo", "disney", "prime video", "apple com bill", "youtube", "dazn",
      "audible")),
    (("Salud",),
     ("farmacia", "clinica", "dentista", "dental", "optica", "fisioterapia")),
    (("Parking y peajes", "Parking", "Transporte"),
     ("parking", "aparcamiento", "empark", "saba", "telpark", "autopista", "peaje", "via t")),
    (("Transporte", "Viajes"),
     ("renfe", "metro", "emt", "cabify", "uber", "bolt", "blablacar", "alsa", "taxi")),
    (("Tecnología",),
     ("media markt", "mediamarkt", "pccomponentes", "fnac", "apple store")),
    (("Ocio",),
     ("cine", "cinesa", "yelmo", "steam", "playstation", "nintendo", "xbox", "ticketmaster")),
    (("Viajes",),
     ("booking", "airbnb", "ryanair", "vueling", "iberia", "easyjet", "hotel")),
]
# los nombres más largos primero: «uber eats» antes que «uber», «burger king» antes que «bar»
_HINT_WORDS = sorted(((kw, names) for names, kws in HINTS for kw in kws), key=lambda h: -len(h[0]))


def normalize(text) -> str:
    """'REPSOL E.S. 1234 Toledo' -> 'repsol e s toledo': sin tildes, números ni signos."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z]+", " ", t).split())


def merchant_key(merchant) -> str:
    """La palabra que identifica al comercio: 'REPSOL E.S. 1234 TOLEDO' -> 'repsol'.

    Así la gasolinera de Toledo y la de Madrid son el mismo sitio.
    """
    todas = [w for w in normalize(merchant).split() if set(w) != {"x"}]   # fuera «XXXXXXXX1234»
    palabras = [w for w in todas if len(w) >= 3 and w not in GENERIC]
    while len(palabras) > 1 and palabras[0] in INTERMEDIARIES:
        palabras = palabras[1:]
    if palabras:
        return palabras[0]
    return " ".join(todas)


def learned_category(conn, merchant, kind: str = "gasto") -> int | None:
    """La categoría que le pusiste la última vez a un gasto en ese comercio.

    Solo cuenta lo que has apuntado o revisado tú: una suposición sin revisar
    no enseña nada.
    """
    key = merchant_key(merchant)
    if not key:
        return None
    for row in conn.execute("SELECT concept, category_id FROM transactions "
                            "WHERE type = ? AND review = 0 AND concept <> '' "
                            "ORDER BY date DESC, id DESC LIMIT 3000", (kind,)):
        if merchant_key(row["concept"]) == key:
            return row["category_id"]
    return None


def hinted_category(conn, merchant) -> int | None:
    """La categoría de la lista de comercios conocidos, si existe en tu app."""
    texto = normalize(merchant)
    if not texto:
        return None
    mias = {normalize(c["name"]): c["id"] for c in repo.categories(conn, active=True, kind="gasto")}
    for palabra, nombres in _HINT_WORDS:
        if re.search(rf"\b{re.escape(palabra)}\b", texto):
            for nombre in nombres:
                if normalize(nombre) in mias:
                    return mias[normalize(nombre)]
    return None


def fallback_category(conn, kind: str = "gasto") -> int | None:
    """La categoría «por si acaso»: la elegida en Ajustes, o «Otros gastos/ingresos», o la primera."""
    activas = repo.categories(conn, active=True, kind=kind)
    if kind == "gasto":
        elegida = repo.int_setting(conn, "auto_fallback_category_id")
        if any(c["id"] == elegida for c in activas):
            return elegida
    for c in activas:
        if normalize(c["name"]) == ("otros gastos" if kind == "gasto" else "otros ingresos"):
            return c["id"]
    return activas[0]["id"] if activas else None


def guess(conn, merchant, kind: str = "gasto") -> tuple[int | None, bool]:
    """(categoría, hay que revisarla): lo aprendido manda; si no, una suposición."""
    aprendida = learned_category(conn, merchant, kind) if merchant else None
    if aprendida is not None:
        return aprendida, False
    pista = hinted_category(conn, merchant) if kind == "gasto" else None
    return pista or fallback_category(conn, kind), True
