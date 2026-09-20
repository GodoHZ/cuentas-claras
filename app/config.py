"""Rutas y zona horaria. Todo se puede cambiar con variables de entorno."""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR.parent / "data"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", BASE_DIR.parent / "backups"))
DB_PATH = DATA_DIR / "finanzas.db"
APP_VERSION = os.environ.get("APP_VERSION", "dev")
try:
    TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Madrid")
except (ValueError, KeyError):             # TZ con formato raro (":/etc/localtime"...)
    TZ = ZoneInfo("Europe/Madrid")
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "30"))
# Fichero que tiene que existir en la carpeta de copias para dar por buena la
# ruta (sirve para no escribir en un punto de montaje sin montar). Vacío = no comprobar.
BACKUP_SENTINEL = os.environ.get("BACKUP_SENTINEL", "")


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()
