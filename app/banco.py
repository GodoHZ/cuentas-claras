"""Traer los movimientos del banco, con Enable Banking.

Cómo funciona, de principio a fin:

1. Te das de alta en Enable Banking y registras una aplicación. Te dan un
   identificador (App ID) y una clave privada (.pem), que se suben en la app y se
   guardan en SECRETS_DIR, fuera de la base de datos.
2. «Conectar un banco» te lleva a la web de tu banco para dar permiso. Al volver,
   la app guarda la sesión y las cuentas que has autorizado.
3. Cada pocas horas (o al pulsar «Sincronizar») se descargan los movimientos
   apuntados del banco y se convierten en gastos e ingresos de la app:
   - Lo que ya hayas apuntado a mano se reconoce (misma cuenta, mismo importe,
     fecha cercana) y no se duplica: se queda lo tuyo.
   - Un movimiento visto no vuelve a entrar nunca, aunque lo borres.
   - Si sale dinero de una cuenta conectada y entra en otra, es un traspaso y se
     apunta como tal, no como un gasto y un ingreso.
   - La categoría la adivina clasificar.py, y lo que no sabe queda «por revisar».
   - El saldo que dice el banco se apunta como comprobación de la cuenta, así que
     el «¿cuadra con el banco?» se rellena solo.

El permiso caduca cada pocos meses (lo decide el banco, normalmente 180 días):
entonces hay que volver a conectar, y no se duplica nada.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from . import calc, clasificar, config, repo

SOURCE = "banco"
log = logging.getLogger("finanzas")
KEY_FILE = "enablebanking.pem"
MAX_CONSENT = timedelta(days=180)
OVERLAP = timedelta(days=30)        # cada vez se vuelve a mirar el último mes: el banco a veces apunta tarde
MATCH_DAYS = 4                      # un movimiento tuyo y el del banco son el mismo si se separan hasta 4 días
TRANSFER_DAYS = 3                   # lo que sale de una cuenta y entra en otra en 3 días es un traspaso
# La ley (RTS de PSD2, art. 36.5.b) solo deja consultar 4 veces al día cuando tú no
# estás delante: cada 6 horas. Cuando abres la app sí estás, y eso no cuenta.
AUTO_SYNC_EVERY = timedelta(hours=6)
PRESENT_SYNC_EVERY = timedelta(minutes=15)
WARN_BEFORE = timedelta(days=14)    # avisar de que el permiso caduca


class BankError(RuntimeError):
    pass


# ---------------------------------------------------------------- configuración

def key_path() -> Path:
    return config.SECRETS_DIR / KEY_FILE


def save_key(pem: bytes) -> None:
    """Guarda la clave privada (comprobando antes que lo es) solo legible por la app."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    try:
        load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as e:
        raise BankError("Ese fichero no es una clave privada .pem válida.") from e
    config.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.SECRETS_DIR, 0o700)
    destino = key_path()
    tmp = destino.with_suffix(".tmp")
    tmp.write_bytes(pem)
    os.chmod(tmp, 0o600)
    tmp.replace(destino)


def configured(conn) -> bool:
    return bool(repo.setting(conn, "bank_app_id")) and key_path().is_file()


# ---------------------------------------------------------------- cliente de la API

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id: str, pem: bytes, now: float | None = None) -> str:
    """El token firmado (RS256) que pide Enable Banking en cada llamada."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    iat = int(now if now is not None else time.time())
    cabecera = {"typ": "JWT", "alg": "RS256", "kid": app_id}
    cuerpo = {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": iat, "exp": iat + 3600}
    firmado = f"{_b64(json.dumps(cabecera).encode())}.{_b64(json.dumps(cuerpo).encode())}"
    clave = load_pem_private_key(pem, password=None)
    firma = clave.sign(firmado.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{firmado}.{_b64(firma)}"


class Client:
    """Las llamadas a Enable Banking que usa la app. En los tests se cambia por uno falso."""

    def __init__(self, app_id: str, pem: bytes, base: str = config.BANK_API, psu: dict | None = None):
        self.app_id, self.pem, self.base = app_id, pem, base.rstrip("/")
        self.psu = psu or {}

    def with_psu(self, headers: dict) -> "Client":
        """Una copia que le dice al banco que estás delante (cabeceras Psu-*).

        Así la consulta no cuenta para el límite de 4 al día.
        """
        return Client(self.app_id, self.pem, self.base, headers)

    @classmethod
    def from_settings(cls, conn) -> "Client":
        if not configured(conn):
            raise BankError("Falta configurar la conexión con el banco (App ID y clave).")
        return cls(repo.setting(conn, "bank_app_id"), key_path().read_bytes())

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        datos = json.dumps(body).encode() if body is not None else None
        cabeceras = {"Authorization": f"Bearer {make_jwt(self.app_id, self.pem)}",
                     "Content-Type": "application/json", "Accept": "application/json"}
        if path.startswith("/accounts/"):            # solo las consultas de datos llevan las Psu-*
            cabeceras.update({k: v for k, v in self.psu.items() if v})
        peticion = urllib.request.Request(url, data=datos, method=method, headers=cabeceras)
        try:
            with urllib.request.urlopen(peticion, timeout=40) as respuesta:
                texto = respuesta.read().decode() or "{}"
        except urllib.error.HTTPError as e:
            detalle = e.read().decode(errors="replace")[:300]
            try:
                info = json.loads(detalle)
                detalle = info.get("message") or info.get("detail") or info.get("error") or detalle
            except ValueError:
                pass
            raise BankError(f"El banco ha respondido con un error {e.code}: {detalle}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BankError(f"No he podido hablar con Enable Banking: {e}") from e
        return json.loads(texto)

    # una función por llamada, con los nombres de la documentación
    def aspsps(self, country: str) -> list[dict]:
        return self.request("GET", "/aspsps", query={"country": country, "psu_type": "personal"}).get("aspsps", [])

    def start_auth(self, name: str, country: str, valid_until: datetime, state: str, redirect_url: str) -> str:
        r = self.request("POST", "/auth", body={
            "access": {"valid_until": valid_until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "aspsp": {"name": name, "country": country}, "state": state,
            "redirect_url": redirect_url, "psu_type": "personal"})
        return r["url"]

    def create_session(self, code: str) -> dict:
        return self.request("POST", "/sessions", body={"code": code})

    def delete_session(self, session_id: str) -> None:
        self.request("DELETE", f"/sessions/{session_id}")

    def transactions(self, uid: str, date_from: date, continuation_key: str | None = None) -> dict:
        return self.request("GET", f"/accounts/{uid}/transactions", query={
            "date_from": date_from.isoformat(), "continuation_key": continuation_key})

    def balances(self, uid: str) -> list[dict]:
        return self.request("GET", f"/accounts/{uid}/balances").get("balances", [])


# ---------------------------------------------------------------- conectar

def links(conn) -> list:
    return conn.execute("SELECT * FROM bank_links ORDER BY aspsp_name").fetchall()


def accounts_of(conn, link_id: int) -> list:
    return conn.execute("SELECT * FROM bank_accounts WHERE link_id = ? ORDER BY name", (link_id,)).fetchall()


def start_connection(conn, client, name: str, country: str, redirect_url: str,
                     max_seconds: int | None = None, now: datetime | None = None) -> str:
    """Prepara la conexión y devuelve la dirección del banco a la que hay que ir."""
    now = now or datetime.now(timezone.utc)
    duracion = MAX_CONSENT if not max_seconds else min(MAX_CONSENT, timedelta(seconds=int(max_seconds)))
    state = secrets.token_urlsafe(24)
    with conn:
        conn.execute("INSERT INTO bank_links (aspsp_name, aspsp_country, state) VALUES (?, ?, ?) "
                     "ON CONFLICT(aspsp_name, aspsp_country) DO UPDATE SET state = excluded.state",
                     (name, country, state))
    return client.start_auth(name, country, now + duracion - timedelta(minutes=5), state, redirect_url)


def finish_connection(conn, client, code: str, state: str, today: date) -> dict:
    """La vuelta desde el banco: guarda la sesión y las cuentas. Devuelve el enlace."""
    link = conn.execute("SELECT * FROM bank_links WHERE state = ?", (state or "",)).fetchone()
    if not link or not state:
        raise BankError("No reconozco esta vuelta del banco (¿la empezaste desde otro navegador?). "
                        "Vuelve a darle a «Conectar».")
    sesion = client.create_session(code)
    valid_until = (sesion.get("access") or {}).get("valid_until")
    mias = repo.accounts(conn, active=True)
    with conn:
        conn.execute("UPDATE bank_links SET session_id = ?, valid_until = ?, state = NULL, last_error = NULL "
                     "WHERE id = ?", (sesion["session_id"], valid_until, link["id"]))
        for cuenta in sesion.get("accounts", []):
            ident = cuenta.get("identification_hash") or cuenta.get("uid")
            iban = ((cuenta.get("account_id") or {}).get("iban") or "")
            nombre = cuenta.get("name") or cuenta.get("product") or iban or link["aspsp_name"]
            existe = conn.execute("SELECT 1 FROM bank_accounts WHERE ident = ?", (ident,)).fetchone()
            if existe:                     # renovar el permiso: misma cuenta, uid nuevo
                conn.execute("UPDATE bank_accounts SET uid = ?, link_id = ?, name = ?, iban = ? WHERE ident = ?",
                             (cuenta.get("uid"), link["id"], nombre, iban, ident))
            else:
                conn.execute("INSERT INTO bank_accounts (ident, link_id, uid, name, iban, account_id, since) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (ident, link["id"], cuenta.get("uid"), nombre, iban,
                              _guess_account(mias, link["aspsp_name"], nombre), today.isoformat()))
    return conn.execute("SELECT * FROM bank_links WHERE id = ?", (link["id"],)).fetchone()


def _guess_account(mias, banco: str, nombre: str) -> int | None:
    """La cuenta de la app que se llama como el banco («Mi Banco» → Mi Banco). Si no, ninguna."""
    for texto in (banco, nombre):
        t = clasificar.normalize(texto)
        for a in mias:
            n = clasificar.normalize(a["name"])
            if n and t and (n in t or t in n):
                return a["id"]
    return None


def disconnect(conn, client, link_id: int) -> None:
    """Quita la conexión. Los movimientos ya traídos se quedan, y no se duplicarán si vuelves."""
    link = conn.execute("SELECT * FROM bank_links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        return
    if link["session_id"] and client is not None:
        try:
            client.delete_session(link["session_id"])
        except BankError:
            pass                           # si ya había caducado, da igual
    with conn:
        conn.execute("DELETE FROM bank_links WHERE id = ?", (link_id,))


# ---------------------------------------------------------------- sincronizar

def _cents(importe) -> int | None:
    try:
        valor = Decimal(str((importe or {}).get("amount")))
    except (InvalidOperation, TypeError):
        return None
    return abs(int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _texto(tx: dict) -> str:
    """El concepto: con quién (si lo dice) y la descripción del banco."""
    contraparte = (tx.get("creditor") if tx.get("credit_debit_indicator") == "DBIT" else tx.get("debtor")) or {}
    partes = [contraparte.get("name") or ""] + list(tx.get("remittance_information") or [])
    vistas, limpio = set(), []
    for p in partes:
        p = " ".join(str(p).split())
        if p and p.lower() not in vistas:
            vistas.add(p.lower())
            limpio.append(p)
    return " · ".join(limpio)[:200]


def _refs(cuenta_ident: str, transacciones: list[dict]) -> list[str]:
    """Un identificador estable por movimiento.

    El del banco si lo da (entry_reference); si no, una huella de fecha, importe y
    concepto, numerada por si ese mismo día hay dos movimientos idénticos.
    """
    vistos: dict[str, int] = {}
    refs = []
    for tx in transacciones:
        if tx.get("entry_reference"):
            refs.append(f"{cuenta_ident}:{tx['entry_reference']}")
            continue
        huella = hashlib.sha256(json.dumps([
            tx.get("booking_date"), (tx.get("transaction_amount") or {}).get("amount"),
            tx.get("credit_debit_indicator"), _texto(tx)]).encode()).hexdigest()[:24]
        n = vistos.get(huella, 0)
        vistos[huella] = n + 1
        refs.append(f"{cuenta_ident}:h{huella}:{n}")
    return refs


def _manual_match(conn, account_id: int, signed: int, day: date):
    """Un movimiento tuyo (no del banco) que mueve lo mismo en esa cuenta y en fechas cercanas."""
    desde, hasta = day - timedelta(days=MATCH_DAYS), day + timedelta(days=MATCH_DAYS)
    candidatos = conn.execute(
        "SELECT * FROM transactions t WHERE source IS NULL AND date BETWEEN ? AND ? "
        "AND (account_id = ? OR other_account_id = ?) "
        "AND NOT EXISTS (SELECT 1 FROM bank_refs r WHERE r.tx_id = t.id AND r.account_id = ?)",
        (desde.isoformat(), hasta.isoformat(), account_id, account_id, account_id)).fetchall()
    mejor = None
    for fila in candidatos:
        if calc.account_moves(_tx(fila)).get(account_id) == signed:
            distancia = abs((date.fromisoformat(fila["date"]) - day).days)
            if mejor is None or distancia < mejor[0]:
                mejor = (distancia, fila)
    return mejor[1] if mejor else None


def _tx(fila) -> calc.Tx:
    return calc.Tx(date.fromisoformat(fila["date"]), fila["type"], fila["amount"], fila["category_id"],
                   fila["envelope_id"], fila["account_id"], fila["other_account_id"])


def import_account(conn, client, cuenta, today: date) -> dict:
    """Trae los movimientos de una cuenta del banco. Devuelve cuántos de cada tipo."""
    cuenta_app = cuenta["account_id"]
    cuenta_info = {"nuevos": 0, "ya_estaban": 0, "por_revisar": 0, "otra_moneda": 0}
    since = date.fromisoformat(cuenta["since"])
    desde = max(since, today - OVERLAP)
    if not conn.execute("SELECT 1 FROM bank_refs WHERE ref LIKE ? LIMIT 1", (cuenta["ident"] + ":%",)).fetchone():
        desde = since                       # la primera vez, desde la fecha elegida
    transacciones, clave = [], None
    for _ in range(50):                      # páginas
        pagina = client.transactions(cuenta["uid"], desde, clave)
        transacciones += pagina.get("transactions", [])
        clave = pagina.get("continuation_key")
        if not clave:
            break
    apuntadas = [t for t in transacciones if t.get("status", "BOOK") == "BOOK"]
    for tx, ref in zip(apuntadas, _refs(cuenta["ident"], apuntadas)):
        if conn.execute("SELECT 1 FROM bank_refs WHERE ref = ?", (ref,)).fetchone():
            continue
        dia_txt = tx.get("booking_date") or tx.get("value_date") or tx.get("transaction_date")
        cents = _cents(tx.get("transaction_amount"))
        if not dia_txt or not cents:
            continue
        dia = date.fromisoformat(dia_txt)
        if dia < since:
            continue
        if (tx.get("transaction_amount") or {}).get("currency", "EUR") != "EUR":
            cuenta_info["otra_moneda"] += 1
            continue
        entra = tx.get("credit_debit_indicator") == "CRDT"
        signed = cents if entra else -cents
        with conn:
            tuyo = _manual_match(conn, cuenta_app, signed, dia)
            if tuyo is not None:
                conn.execute("INSERT INTO bank_refs (ref, tx_id, account_id) VALUES (?, ?, ?)",
                             (ref, tuyo["id"], cuenta_app))
                cuenta_info["ya_estaban"] += 1
                continue
            tipo = calc.INGRESO if entra else calc.GASTO
            concepto = _texto(tx) or ("Ingreso" if entra else "Gasto") + " del banco"
            categoria, revisar = clasificar.guess(conn, concepto, tipo)
            if categoria is None:
                continue                     # sin categorías de ese tipo no se puede apuntar
            tx_id = repo.insert_tx(conn, date=dia.isoformat(), type=tipo, category_id=categoria,
                                   envelope_id=None, account_id=cuenta_app, concept=concepto, amount=cents,
                                   source=SOURCE, review=1 if revisar else 0)
            conn.execute("INSERT INTO bank_refs (ref, tx_id, account_id) VALUES (?, ?, ?)",
                         (ref, tx_id, cuenta_app))
            cuenta_info["nuevos"] += 1
            cuenta_info["por_revisar"] += 1 if revisar else 0
    return cuenta_info


def pair_transfers(conn) -> int:
    """Junta en un traspaso lo que salió de una cuenta y entró en otra (los dos del banco, sin tocar)."""
    juntados = 0
    ingresos = conn.execute("SELECT * FROM transactions WHERE source = ? AND type = 'ingreso' AND review = 1 "
                            "ORDER BY date, id", (SOURCE,)).fetchall()
    for entra in ingresos:
        dia = date.fromisoformat(entra["date"])
        sale = conn.execute(
            "SELECT * FROM transactions WHERE source = ? AND type = 'gasto' AND review = 1 AND amount = ? "
            "AND account_id <> ? AND date BETWEEN ? AND ? ORDER BY ABS(julianday(date) - julianday(?)), id LIMIT 1",
            (SOURCE, entra["amount"], entra["account_id"], (dia - timedelta(days=TRANSFER_DAYS)).isoformat(),
             (dia + timedelta(days=TRANSFER_DAYS)).isoformat(), entra["date"])).fetchone()
        if not sale:
            continue
        origen = repo.get_row(conn, "accounts", sale["account_id"])
        with conn:
            conn.execute("UPDATE transactions SET type = 'traspaso', category_id = NULL, other_account_id = ?, "
                         "concept = ?, review = 1 WHERE id = ?",
                         (sale["account_id"], f"Traspaso desde {origen['name'] if origen else 'otra cuenta'}",
                          entra["id"]))
            conn.execute("UPDATE bank_refs SET tx_id = ? WHERE tx_id = ?", (entra["id"], sale["id"]))
            conn.execute("DELETE FROM transactions WHERE id = ?", (sale["id"],))
        juntados += 1
    return juntados


def record_balance(conn, client, cuenta, today: date) -> int | None:
    """Apunta el saldo que dice el banco como comprobación de hoy (una por día)."""
    preferidos = ("ITBD", "CLBD", "XPCD", "ITAV", "CLAV")
    saldos = {b.get("balance_type"): b for b in client.balances(cuenta["uid"])}
    elegido = next((saldos[t] for t in preferidos if t in saldos), next(iter(saldos.values()), None))
    if not elegido:
        return None
    importe = (elegido.get("balance_amount") or {})
    try:
        cents = int((Decimal(str(importe.get("amount"))) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError):
        return None
    with conn:
        conn.execute("DELETE FROM account_checks WHERE account_id = ? AND date = ?",
                     (cuenta["account_id"], today.isoformat()))
        repo.add_check(conn, cuenta["account_id"], today, cents)
    return cents


def sync_link(conn, client, link, today: date, now: datetime | None = None) -> dict:
    """Sincroniza una conexión entera. Nunca lanza: el error queda guardado en la conexión."""
    now = now or datetime.now(timezone.utc)
    total = {"nuevos": 0, "ya_estaban": 0, "por_revisar": 0, "otra_moneda": 0, "traspasos": 0}
    try:
        if not link["session_id"]:
            raise BankError("Esta conexión no llegó a terminarse: vuelve a darle a «Conectar».")
        if link["valid_until"] and _parse_dt(link["valid_until"]) <= now:
            raise BankError("El permiso del banco ha caducado: vuelve a conectar para renovarlo.")
        for cuenta in accounts_of(conn, link["id"]):
            if not cuenta["account_id"] or not cuenta["uid"]:
                continue
            for k, v in import_account(conn, client, cuenta, today).items():
                total[k] += v
            record_balance(conn, client, cuenta, today)
        total["traspasos"] = pair_transfers(conn)
        error = None
    except BankError as e:
        error = str(e)
    with conn:
        conn.execute("UPDATE bank_links SET last_sync = ?, last_error = ? WHERE id = ?",
                     (now.strftime("%Y-%m-%dT%H:%M:%SZ"), error, link["id"]))
    total["error"] = error
    return total


def sync_all(conn, client, today: date, now: datetime | None = None) -> list[tuple[str, dict]]:
    return [(link["aspsp_name"], sync_link(conn, client, link, today, now))
            for link in links(conn) if link["session_id"]]


def summary_text(r: dict) -> str:
    if r.get("error"):
        return r["error"]
    partes = [f"{r['nuevos']} movimiento{'s' if r['nuevos'] != 1 else ''} nuevo{'s' if r['nuevos'] != 1 else ''}"]
    if r["ya_estaban"]:
        partes.append(f"{r['ya_estaban']} ya los tenías apuntados")
    if r["traspasos"]:
        partes.append(f"{r['traspasos']} traspaso{'s' if r['traspasos'] != 1 else ''} entre tus cuentas")
    if r["por_revisar"]:
        partes.append(f"{r['por_revisar']} por revisar")
    if r["otra_moneda"]:
        partes.append(f"{r['otra_moneda']} en otra moneda que no he apuntado")
    return ", ".join(partes)


def _parse_dt(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def expiring(conn, now: datetime | None = None) -> list[dict]:
    """Conexiones cuyo permiso caduca pronto (o ya ha caducado), para avisar en el panel."""
    now = now or datetime.now(timezone.utc)
    avisos = []
    for link in links(conn):
        if link["session_id"] and link["valid_until"]:
            hasta = _parse_dt(link["valid_until"])
            if hasta - now <= WARN_BEFORE:
                avisos.append({"name": link["aspsp_name"], "until": hasta.astimezone(config.TZ).date(),
                               "expired": hasta <= now})
    return avisos


def present_sync_due(conn, now: datetime | None = None) -> bool:
    """¿Toca mirar el banco al abrir la app? Si se miró hace menos de 15 minutos, no."""
    now = now or datetime.now(timezone.utc)
    conectadas = [link for link in links(conn) if link["session_id"]]
    if not conectadas:
        return False
    ultimas = [_parse_dt(link["last_sync"]) for link in conectadas if link["last_sync"]]
    return len(ultimas) < len(conectadas) or now - min(ultimas) >= PRESENT_SYNC_EVERY


def sync_all_present(conn, client, psu: dict, today: date, now: datetime | None = None):
    """Sincroniza estando tú delante. Si un banco no acepta las cabeceras, lo intenta sin ellas."""
    resultados = []
    for link in links(conn):
        if not link["session_id"]:
            continue
        r = sync_link(conn, client.with_psu(psu), link, today, now)
        if r.get("error") and "PSU" in r["error"].upper():
            log.warning("%s no acepta el modo «estoy delante» (%s): repito en modo normal",
                        link["aspsp_name"], r["error"])
            r = sync_link(conn, client, link, today, now)
        else:
            log.info("%s, estando delante: %s", link["aspsp_name"], summary_text(r))
        resultados.append((link["aspsp_name"], r))
    return resultados


def auto_sync_due(conn, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    ultima = repo.setting(conn, "bank_last_auto_sync")
    return not ultima or now - _parse_dt(ultima) >= AUTO_SYNC_EVERY
