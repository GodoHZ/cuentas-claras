import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, main, repo, seed  # noqa: E402

HOY = date(2026, 9, 19)          # un "hoy" fijo, para que los tests no dependan del calendario


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "finanzas.db"
    db.init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path)
    seed.seed(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(tmp_path, db_path):
    from fastapi.testclient import TestClient
    app = main.create_app(db_path=db_path, backup_dir=tmp_path / "backups",
                          backups=False, today_fn=lambda: HOY)
    with TestClient(app) as test_client:
        yield test_client


def apuntar(conn, dia, tipo, importe, categoria=None, sobre=None, cuenta="Cuenta corriente", concepto=""):
    """Apunta un movimiento de prueba usando los nombres del esqueleto inicial."""
    cats, envs, accs = ids(conn)
    with conn:
        return repo.insert_tx(conn, date=dia, type=tipo, category_id=cats.get(categoria),
                              envelope_id=envs.get(sobre), account_id=accs[cuenta],
                              concept=concepto, amount=importe)


def ids(conn):
    """Atajo: nombres -> id de categorías, sobres y cuentas."""
    return (
        {r["name"]: r["id"] for r in repo.categories(conn)},
        {r["name"]: r["id"] for r in repo.envelopes(conn)},
        {r["name"]: r["id"] for r in repo.accounts(conn)},
    )
