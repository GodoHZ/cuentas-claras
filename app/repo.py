"""Consultas a la base de datos. Nada de reglas de negocio: eso está en calc.py."""
from __future__ import annotations

import sqlite3
from datetime import date

from . import calc
from .money import to_date

# tablas que se pueden ordenar y archivar desde Ajustes
ORDERED_TABLES = {"categories", "envelopes", "accounts", "loans", "rules"}


# ---------------------------------------------------------------- ajustes

def settings(conn) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn, key: str, value) -> None:
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def int_setting(conn, key: str) -> int | None:
    value = setting(conn, key)
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


# reglas que se pueden cambiar en Ajustes (valor de fábrica a la derecha)
RULES = {
    "budget_warn_pct": 80,          # % del presupuesto a partir del cual avisa
    "installments_limit_pct": 35,   # % de la nómina en cuotas que se considera demasiado
    "reconcile_small_cents": 2000,  # diferencia de cuadre que se achaca a intereses
    "backup_keep_days": 30,         # días de copias que se guardan
}


def rule(conn, key: str) -> int:
    """Valor de una regla: el que haya puesto el usuario o el de fábrica."""
    value = int_setting(conn, key)
    return RULES[key] if value is None else value


def offline_cache(conn) -> bool:
    """¿Puede el móvil guardar copias de las pantallas para verlas sin conexión?

    Sin PIN, sí (no hay nada que proteger más allá de la propia red). Con PIN,
    solo si se activa a propósito: esas copias se ven sin pedir el PIN.
    """
    elegido = setting(conn, "offline_cache")
    if elegido is not None:
        return elegido == "1"
    return not setting(conn, "pin_hash")


def salary(conn) -> int | None:
    return int_setting(conn, "salary")


def start_month(conn) -> tuple[int, int]:
    d = to_date(setting(conn, "start_month", "2026-09-01"))
    return d.year, d.month


# ---------------------------------------------------------------- catálogos

def categories(conn, active: bool | None = None, kind: str | None = None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM categories WHERE 1=1", []
    if active is not None:
        sql += " AND active = ?"
        args.append(int(active))
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    return conn.execute(sql + " ORDER BY position, name", args).fetchall()


def envelopes(conn, active: bool | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM envelopes"
    if active is not None:
        sql += f" WHERE active = {int(active)}"
    return conn.execute(sql + " ORDER BY position, name").fetchall()


def accounts(conn, active: bool | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM accounts"
    if active is not None:
        sql += f" WHERE active = {int(active)}"
    return conn.execute(sql + " ORDER BY position, name").fetchall()


def loans(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM loans ORDER BY position, id").fetchall()


def get_row(conn, table: str, row_id: int) -> sqlite3.Row | None:
    assert table in ORDERED_TABLES
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


def next_position(conn, table: str) -> int:
    assert table in ORDERED_TABLES
    return conn.execute(f"SELECT COALESCE(MAX(position), -1) + 1 FROM {table}").fetchone()[0]


def move(conn, table: str, row_id: int, direction: int) -> None:
    """Sube (-1) o baja (+1) un elemento en la lista, renumerando posiciones."""
    assert table in ORDERED_TABLES
    ids = [r["id"] for r in conn.execute(f"SELECT id FROM {table} ORDER BY position, id")]
    if row_id not in ids:
        return
    i = ids.index(row_id)
    j = i + direction
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for pos, rid in enumerate(ids):
        conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, rid))


# ---------------------------------------------------------------- movimientos

def all_txs(conn) -> list[calc.Tx]:
    """Todos los movimientos, en el formato que usan las reglas de calc.py.

    Cada uno lleva el «mes» al que cuenta: el que se le haya puesto a mano o, si no, el del cobro
    de nómina en que cae su fecha (o su mes natural). Así todo lo que va por meses (resumen,
    presupuesto, previsto, sobras) va de nómina a nómina sin tener que saberlo.
    """
    ciclos = cycles(conn)
    txs = []
    for r in conn.execute("SELECT date, type, amount, category_id, envelope_id, account_id, "
                          "other_account_id, month FROM transactions ORDER BY date, id"):
        dia = date.fromisoformat(r["date"])
        mes = r["month"] or (period_key(*calc.period_of(dia, ciclos)) if ciclos else None)
        txs.append(calc.Tx(dia, r["type"], r["amount"], r["category_id"], r["envelope_id"],
                           r["account_id"], r["other_account_id"], mes))
    return txs


TX_SELECT = """
SELECT t.*, c.name AS category, e.name AS envelope, a.name AS account, o.name AS other_account,
       (SELECT group_concat(g.name, ', ') FROM tx_tags x JOIN tags g ON g.id = x.tag_id WHERE x.tx_id = t.id) AS tags
FROM transactions t
LEFT JOIN categories c ON c.id = t.category_id
LEFT JOIN envelopes e ON e.id = t.envelope_id
LEFT JOIN accounts a ON a.id = t.account_id
LEFT JOIN accounts o ON o.id = t.other_account_id
"""


def tx_rows(conn, month: tuple[int, int] | None = None, type_: str | None = None,
            category_id: int | None = None, envelope_id: int | None = None,
            account_id: int | None = None, limit: int | None = None,
            oldest_first: bool = False, review: bool = False, search: str | None = None,
            tag_id: int | None = None) -> list[sqlite3.Row]:
    sql, args = TX_SELECT + " WHERE 1=1", []
    if tag_id:
        sql += " AND t.id IN (SELECT tx_id FROM tx_tags WHERE tag_id = ?)"
        args.append(tag_id)
    if search:
        # en el concepto, la categoría o el sobre; cada palabra tiene que estar en alguno
        for palabra in search.split():
            sql += " AND (t.concept LIKE ? OR c.name LIKE ? OR e.name LIKE ?)"
            args += [f"%{palabra}%"] * 3
    if review:
        sql += " AND t.review = 1"
    if month:
        # el «mes» al que cuenta: el puesto a mano, o el de las fechas de ese mes (de nómina a nómina)
        desde, hasta = calc.period_range(*month, cycles(conn))
        sql += " AND (t.month = ? OR (t.month IS NULL AND t.date >= ? AND t.date < ?))"
        args += [period_key(*month), desde.isoformat(), hasta.isoformat() if hasta else "9999-12-31"]
    if type_:
        sql += " AND t.type = ?"
        args.append(type_)
    if category_id:
        sql += " AND t.category_id = ?"
        args.append(category_id)
    if envelope_id:
        sql += " AND t.envelope_id = ?"
        args.append(envelope_id)
    if account_id:
        sql += " AND (t.account_id = ? OR t.other_account_id = ?)"
        args += [account_id, account_id]
    sql += " ORDER BY t.date, t.id" if oldest_first else " ORDER BY t.date DESC, t.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def get_tx(conn, tx_id: int) -> sqlite3.Row | None:
    return conn.execute(TX_SELECT + " WHERE t.id = ?", (tx_id,)).fetchone()


TX_FIELDS = ("date", "type", "category_id", "envelope_id", "account_id", "other_account_id",
             "concept", "amount", "month")
TX_INSERT_FIELDS = TX_FIELDS + ("client_uid", "source", "review")


def payroll_category_id(conn) -> int | None:
    """La categoría de la nómina: la elegida en Ajustes o, si no, la que se llame «Nómina»."""
    elegida = int_setting(conn, "payroll_category_id")
    if elegida:
        return elegida
    fila = conn.execute("SELECT id FROM categories WHERE name = 'Nómina' COLLATE NOCASE AND kind = 'ingreso'").fetchone()
    return fila[0] if fila else None


# Las partes de la app que se pueden apagar en Ajustes › Qué usas. Apagada, no se ve en ningún sitio y
# no cuenta en lo previsto; sus datos se quedan donde estaban, por si vuelves a encenderla.
MODULES = {"sobres": "Sobres", "presupuesto": "Presupuesto", "fijos": "Pagos fijos", "bancos": "Bancos"}


def modules(conn) -> dict[str, bool]:
    apagados = set((setting(conn, "modules_off", "") or "").split(","))
    return {m: m not in apagados for m in MODULES}


MONTH_MODES = ("nomina", "dia1")


def month_mode(conn) -> str:
    """Cuándo empieza el «mes»: «nomina» (de cobro a cobro, el de fábrica) o «dia1» (el mes natural)."""
    modo = setting(conn, "month_start", "nomina")
    return modo if modo in MONTH_MODES else "nomina"


def cycles(conn) -> list[calc.Cycle]:
    """Los «meses» de nómina a nómina (vacío si se cuenta por meses naturales o aún no hay cobros)."""
    cat = payroll_category_id(conn)
    if month_mode(conn) != "nomina" or not cat:
        return []
    fechas = [date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT date FROM transactions WHERE type = ? AND category_id = ?", (calc.INGRESO, cat))]
    return calc.cycles(calc.payday_starts(fechas))


def period_of(conn, day: date) -> tuple[int, int]:
    return calc.period_of(day, cycles(conn))


def period_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def insert_tx(conn, **fields) -> int:
    cur = conn.execute(
        f"INSERT INTO transactions ({', '.join(TX_INSERT_FIELDS)}) "
        f"VALUES ({', '.join('?' * len(TX_INSERT_FIELDS))})",
        [fields.get(f, 0 if f == "review" else None) for f in TX_INSERT_FIELDS])
    return cur.lastrowid


def tx_by_uid(conn, uid: str):
    """El movimiento que ya se guardó con ese identificador, si existe."""
    return conn.execute(TX_SELECT + " WHERE t.client_uid = ?", (uid,)).fetchone()


def update_tx(conn, tx_id: int, **fields) -> None:
    """Guardar un movimiento desde la app es darlo por revisado."""
    conn.execute(f"UPDATE transactions SET {', '.join(f + ' = ?' for f in TX_FIELDS)}, review = 0 WHERE id = ?",
                 [fields.get(f) if f == "month" else fields[f] for f in TX_FIELDS] + [tx_id])


def delete_tx(conn, tx_id: int) -> None:
    conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))


def usage_counts(conn) -> tuple[dict[int, int], dict[int, int]]:
    """Cuántas veces se ha usado cada categoría y cada sobre (para ordenar chips).

    Los últimos 120 días pesan más que el historial antiguo.
    """
    cats = {r[0]: r[1] for r in conn.execute(
        "SELECT category_id, SUM(CASE WHEN date >= date('now', '-120 days') THEN 10 ELSE 1 END) "
        "FROM transactions WHERE category_id IS NOT NULL GROUP BY category_id")}
    envs = {r[0]: r[1] for r in conn.execute(
        "SELECT envelope_id, SUM(CASE WHEN date >= date('now', '-120 days') THEN 10 ELSE 1 END) "
        "FROM transactions WHERE envelope_id IS NOT NULL GROUP BY envelope_id")}
    return cats, envs


# ---------------------------------------------------------------- cuadre

def last_check(conn, account_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM account_checks WHERE account_id = ? "
                        "ORDER BY date DESC, id DESC LIMIT 1", (account_id,)).fetchone()


def checks(conn, account_id: int | None = None, limit: int = 6) -> list[sqlite3.Row]:
    sql = "SELECT k.*, a.name AS account FROM account_checks k LEFT JOIN accounts a ON a.id = k.account_id"
    args: list = []
    if account_id:
        sql += " WHERE k.account_id = ?"
        args.append(account_id)
    return conn.execute(sql + " ORDER BY k.date DESC, k.id DESC LIMIT ?", args + [limit]).fetchall()


def add_check(conn, account_id: int, day: date, balance: int) -> None:
    conn.execute("INSERT INTO account_checks (account_id, date, balance) VALUES (?, ?, ?)",
                 (account_id, day.isoformat(), balance))


def review_count(conn) -> int:
    """Cuántos movimientos tienen la categoría por confirmar."""
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE review = 1").fetchone()[0]


# ---------------------------------------------------------------- reglas propias

def rules(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.*, c.name AS category, e.name AS envelope FROM rules r "
        "LEFT JOIN categories c ON c.id = r.category_id LEFT JOIN envelopes e ON e.id = r.envelope_id "
        "ORDER BY r.position, r.id").fetchall()


def matching_rule(conn, concept: str | None, kind: str):
    """La primera regla (en su orden) cuyo texto aparece en el concepto, sin mirar mayúsculas ni tildes."""
    from .clasificar import normalize
    texto = normalize(concept or "")
    for regla in rules(conn):
        if regla["kind"] == kind and normalize(regla["pattern"]) and normalize(regla["pattern"]) in texto:
            return regla
    return None


# ---------------------------------------------------------------- lo que te deben

def owed(conn, pending: bool | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT o.*, t.date AS settled_on, t.concept AS settled_concept FROM owed o "
           "LEFT JOIN transactions t ON t.id = o.settled_tx_id")
    if pending is True:
        sql += " WHERE o.settled_tx_id IS NULL AND o.settled_date IS NULL"
    elif pending is False:
        sql += " WHERE o.settled_tx_id IS NOT NULL OR o.settled_date IS NOT NULL"
    return conn.execute(sql + " ORDER BY o.date DESC, o.id DESC").fetchall()


def settle_owed(conn, tx_id: int, amount: int, concept: str | None, day: str):
    """Si este ingreso es lo que te debía alguien, lo da por cobrado. Devuelve esa deuda o None.

    Vale si el importe es exactamente el que te deben y, de las que coinciden, o solo hay una o el
    concepto lleva el nombre de la persona («Bizum de Pol»).
    """
    from .clasificar import normalize
    candidatas = [o for o in owed(conn, pending=True) if o["amount"] == amount and o["date"] <= day]
    texto = normalize(concept or "")
    por_nombre = [o for o in candidatas if normalize(o["person"]) and normalize(o["person"]) in texto]
    elegida = por_nombre[0] if por_nombre else (candidatas[0] if len(candidatas) == 1 else None)
    if elegida:
        conn.execute("UPDATE owed SET settled_tx_id = ?, settled_date = ? WHERE id = ?", (tx_id, day, elegida["id"]))
    return elegida



# ---------------------------------------------------------------- etiquetas

def parse_tag_names(text: str | None) -> list[str]:
    """«Roma 2026, Navidad» -> ['Roma 2026', 'Navidad'] (sin repetidas ni vacías, como mucho 5)."""
    nombres, vistos = [], set()
    for trozo in (text or "").split(","):
        nombre = " ".join(trozo.split())[:40]
        if nombre and nombre.lower() not in vistos:
            vistos.add(nombre.lower())
            nombres.append(nombre)
    return nombres[:5]


def tag_id_for(conn, name: str) -> int:
    fila = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if fila:
        return fila[0]
    return conn.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid


def set_tx_tags(conn, tx_id: int, names: list[str]) -> None:
    """Deja el movimiento con esas etiquetas (crea las que no existan). Las que se quedan sin uso, fuera."""
    conn.execute("DELETE FROM tx_tags WHERE tx_id = ?", (tx_id,))
    for name in names:
        conn.execute("INSERT OR IGNORE INTO tx_tags (tx_id, tag_id) VALUES (?, ?)", (tx_id, tag_id_for(conn, name)))
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM tx_tags)")


def tags(conn) -> list[sqlite3.Row]:
    """Cada etiqueta con cuántos movimientos tiene, lo gastado (menos lo que te devolvieron) y sus fechas."""
    return conn.execute(
        "SELECT g.id, g.name, COUNT(t.id) AS n, "
        "COALESCE(SUM(CASE WHEN t.type = 'gasto' THEN t.amount WHEN t.type = 'ingreso' THEN -t.amount ELSE 0 END), 0) AS spent, "
        "MIN(t.date) AS first, MAX(t.date) AS last "
        "FROM tags g LEFT JOIN tx_tags x ON x.tag_id = g.id LEFT JOIN transactions t ON t.id = x.tx_id "
        "GROUP BY g.id ORDER BY MAX(t.date) DESC, g.name").fetchall()


def get_tag(conn, tag_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()


def tag_range(conn, tag_id: int, desde: str, hasta: str) -> int:
    """Pone la etiqueta a todos los gastos entre dos fechas (los de un viaje). Devuelve cuántos ha añadido."""
    return conn.execute(
        "INSERT OR IGNORE INTO tx_tags (tx_id, tag_id) SELECT id, ? FROM transactions "
        "WHERE type = 'gasto' AND date >= ? AND date <= ?", (tag_id, desde, hasta)).rowcount


# ---------------------------------------------------------------- facturas y tickets

def tx_docs(conn, tx_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tx_docs WHERE tx_id = ? ORDER BY id", (tx_id,)).fetchall()
