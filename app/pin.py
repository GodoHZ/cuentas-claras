"""PIN opcional. Si no hay PIN guardado, la app entra directamente.

El PIN se guarda con PBKDF2 (nunca en claro). La cookie de sesión es un HMAC
del hash del PIN: al cambiar o quitar el PIN, las sesiones viejas dejan de valer.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

ITERATIONS = 200_000
COOKIE = "finanzas_pin"
MAX_FAILS = 5
LOCK_SECONDS = 300

_fails = {"count": 0, "until": 0.0}


def hash_pin(pin: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2${ITERATIONS}${salt}${digest}"


def check_pin(pin: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        test = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), int(iterations)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(test, digest)


def token(secret: str, pin_hash: str) -> str:
    return hmac.new(secret.encode(), pin_hash.encode(), hashlib.sha256).hexdigest()


def valid_token(value: str | None, secret: str, pin_hash: str) -> bool:
    return bool(value) and hmac.compare_digest(value, token(secret, pin_hash))


def locked_seconds() -> int:
    return max(0, int(_fails["until"] - time.monotonic()))


def register_attempt(ok: bool) -> None:
    """Tras 5 fallos seguidos, bloquea 5 minutos."""
    if ok:
        _fails.update(count=0, until=0.0)
        return
    _fails["count"] += 1
    if _fails["count"] >= MAX_FAILS:
        _fails.update(count=0, until=time.monotonic() + LOCK_SECONDS)
