"""Copias de seguridad de la base de datos.

Usa la API de copia en caliente de SQLite (la misma que `sqlite3 .backup`),
así que se puede copiar con la app funcionando. Cada copia se comprueba con
`PRAGMA integrity_check` antes de darla por buena.

- Diaria:  backups/finanzas-2026-09-19.db       (la hace la app sola)
- Manual:  backups/finanzas-2026-09-19_183005-manual.db
- Se guardan las copias de los últimos 30 días.

Uso manual (dentro del contenedor: docker exec -it finanzas ...):
    python -m app.backup copia                   hace una copia ahora
    python -m app.backup lista                   lista las copias
    python -m app.backup comprobar FICHERO       integridad + resumen del contenido
    python -m app.backup restaurar FICHERO DESTINO   restaura en DESTINO (no pisa nada)
"""
from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import calc, config

NAME_RE = re.compile(r"^finanzas-(\d{4}-\d{2}-\d{2})(?:_(\d{6})-manual)?\.db$")


@dataclass
class BackupFile:
    path: Path
    day: date
    manual: bool
    size: int
    modified: datetime


class BackupError(RuntimeError):
    pass


def dir_problem(backup_dir: Path) -> str | None:
    """None si se puede escribir ahí. Si no, el motivo, en castellano.

    Con BACKUP_SENTINEL puesto (las copias van al RAID), exige que ese fichero
    exista: así, si el RAID no está montado, la app avisa en vez de escribir en
    el punto de montaje y dejar los ficheros escondidos debajo.
    """
    if config.BACKUP_SENTINEL and not (backup_dir / config.BACKUP_SENTINEL).exists():
        return (f"el disco de las copias no parece estar montado (no encuentro «{config.BACKUP_SENTINEL}» "
                f"en {backup_dir}). No escribo nada ahí para no dejar ficheros escondidos bajo el punto de montaje.")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        probe = backup_dir / ".escritura-de-prueba"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return f"no puedo escribir en {backup_dir}: {e}"
    return None


def _copy(src_path: Path, dest_path: Path) -> None:
    """Copia en caliente src -> dest (vía fichero temporal) y comprueba la copia."""
    if not Path(src_path).is_file():
        raise BackupError(f"No existe {src_path}")
    tmp = dest_path.with_name(dest_path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")    # copia autocontenida, sin -wal
    finally:
        dst.close()
        src.close()
    problem = integrity_problem(tmp)
    if problem:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"La copia no ha pasado la comprobación: {problem}")
    tmp.replace(dest_path)


def integrity_problem(path: Path) -> str | None:
    """None si el fichero es una base de datos SQLite sana."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return str(e)
    return None if result == "ok" else result


def make_backup(db_path: Path, backup_dir: Path, now: datetime, manual: bool = False) -> Path:
    problem = dir_problem(backup_dir)
    if problem:
        raise BackupError(f"No he podido hacer la copia: {problem}")
    name = (f"finanzas-{now:%Y-%m-%d_%H%M%S}-manual.db" if manual else f"finanzas-{now:%Y-%m-%d}.db")
    dest = backup_dir / name
    _copy(Path(db_path), dest)
    return dest


def list_backups(backup_dir: Path) -> list[BackupFile]:
    """Copias existentes, la más reciente primero."""
    if not backup_dir.is_dir():
        return []
    found = []
    for p in backup_dir.iterdir():
        m = NAME_RE.match(p.name)
        if m and p.is_file():
            st = p.stat()
            found.append(BackupFile(p, date.fromisoformat(m[1]), bool(m[2]), st.st_size,
                                    datetime.fromtimestamp(st.st_mtime, config.TZ)))
    return sorted(found, key=lambda b: b.modified, reverse=True)


def rotate(backup_dir: Path, today: date, keep_days: int = config.BACKUP_KEEP_DAYS) -> list[Path]:
    """Borra las copias de hace más de `keep_days` días. Devuelve lo borrado."""
    oldest = today - timedelta(days=keep_days - 1)
    removed = []
    for b in list_backups(backup_dir):
        if b.day < oldest:
            b.path.unlink()
            removed.append(b.path)
    return removed


def daily_backup_if_needed(db_path: Path, backup_dir: Path, now: datetime,
                           keep_days: int = config.BACKUP_KEEP_DAYS) -> Path | None:
    """Hace la copia del día si todavía no existe, y rota las viejas."""
    dest = backup_dir / f"finanzas-{now:%Y-%m-%d}.db"
    if dest.exists():
        return None
    problem = dir_problem(backup_dir)
    if problem:
        raise BackupError(f"No he podido hacer la copia del día: {problem}")
    path = make_backup(db_path, backup_dir, now)
    rotate(backup_dir, now.date(), keep_days)
    return path


def summary(path: Path) -> dict:
    """Resumen legible del contenido de una base de datos (para comprobar copias)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("categories", "envelopes", "accounts", "transactions", "loans", "tr_checks")}
        txs = [calc.Tx(date.fromisoformat(r["date"]), r["type"], r["amount"], r["category_id"], r["envelope_id"])
               for r in conn.execute("SELECT * FROM transactions")]
        total = sum(calc.envelope_balances(txs).values())
    finally:
        conn.close()
    return {"tablas": counts, "total_en_sobres_cent": total}


def restore(backup_path: Path, dest_path: Path, overwrite: bool = False) -> None:
    """Restaura una copia en dest_path. Por seguridad no pisa nada salvo overwrite=True."""
    backup_path, dest_path = Path(backup_path), Path(dest_path)
    problem = integrity_problem(backup_path)
    if problem:
        raise BackupError(f"La copia {backup_path.name} está dañada: {problem}")
    if dest_path.exists() and not overwrite:
        raise BackupError(f"{dest_path} ya existe; no lo piso.")
    for suffix in ("-wal", "-shm"):
        Path(str(dest_path) + suffix).unlink(missing_ok=True)
    _copy(backup_path, dest_path)


def main(argv=None) -> int:
    from .money import eur
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "lista"
    if cmd == "copia":
        path = make_backup(config.DB_PATH, config.BACKUP_DIR, config.now(), manual=True)
        print(f"Copia hecha: {path}")
    elif cmd == "lista":
        for b in list_backups(config.BACKUP_DIR):
            print(f"{b.modified:%d/%m/%Y %H:%M}  {b.size / 1024:8.1f} KB  {b.path.name}")
    elif cmd == "comprobar" and len(argv) == 2:
        problem = integrity_problem(Path(argv[1]))
        if problem:
            print(f"DAÑADA: {problem}")
            return 1
        s = summary(Path(argv[1]))
        print("OK, integridad correcta.", s["tablas"], "| total en sobres:", eur(s["total_en_sobres_cent"]))
    elif cmd == "restaurar" and len(argv) == 3:
        restore(Path(argv[1]), Path(argv[2]))
        print(f"Restaurada en {argv[2]}.", summary(Path(argv[2])))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
