from datetime import datetime, timedelta

from app import backup, calc, config, repo
from conftest import apuntar


def ahora(day="2026-09-19", hora=3):
    return datetime(*[int(p) for p in day.split("-")], hora, 30, tzinfo=config.TZ)


def test_copia_y_restauracion(tmp_path, conn, db_path):
    apuntar(conn, "2026-09-01", calc.APORTE, 20000, sobre="Colchón", cuenta="Cuenta de ahorro")
    apuntar(conn, "2026-09-02", calc.APORTE, 10037, sobre="Coche", cuenta="Cuenta de ahorro")
    backups = tmp_path / "backups"
    path = backup.make_backup(db_path, backups, ahora())
    assert path.name == "finanzas-2026-09-19.db"
    assert backup.integrity_problem(path) is None
    assert backup.summary(path)["total_en_sobres_cent"] == 30037

    # restaurar en una base de datos de prueba y comprobar que está todo
    destino = tmp_path / "prueba.db"
    backup.restore(path, destino)
    prueba = __import__("app.db", fromlist=["db"]).connect(destino)
    try:
        assert len(repo.all_txs(prueba)) == 2
        assert repo.salary(prueba) == 150000
        assert sum(calc.envelope_balances(repo.all_txs(prueba)).values()) == 30037
    finally:
        prueba.close()


def test_no_pisa_un_fichero_existente(tmp_path, conn, db_path):
    path = backup.make_backup(db_path, tmp_path / "backups", ahora())
    destino = tmp_path / "ya-existe.db"
    destino.write_text("no me pises")
    try:
        backup.restore(path, destino)
        assert False, "tendría que haber fallado"
    except backup.BackupError:
        pass
    assert destino.read_text() == "no me pises"


def test_copia_diaria_una_sola_vez_al_dia(tmp_path, conn, db_path):
    backups = tmp_path / "backups"
    assert backup.daily_backup_if_needed(db_path, backups, ahora()) is not None
    assert backup.daily_backup_if_needed(db_path, backups, ahora(hora=20)) is None
    assert backup.daily_backup_if_needed(db_path, backups, ahora("2026-09-20")) is not None
    assert len(backup.list_backups(backups)) == 2


def test_rotacion_30_dias(tmp_path, conn, db_path):
    backups = tmp_path / "backups"
    hoy = ahora("2026-09-19")
    for dias in (0, 1, 29, 30, 40):
        backup.make_backup(db_path, backups, hoy - timedelta(days=dias))
    assert len(backup.list_backups(backups)) == 5
    borradas = backup.rotate(backups, hoy.date(), keep_days=30)
    assert len(borradas) == 2                                  # las de hace 30 y 40 días
    dias_guardados = sorted(b.day.isoformat() for b in backup.list_backups(backups))
    assert dias_guardados == ["2026-08-21", "2026-09-18", "2026-09-19"]


def test_copia_manual_lleva_hora_en_el_nombre(tmp_path, conn, db_path):
    path = backup.make_backup(db_path, tmp_path / "backups", ahora(), manual=True)
    assert path.name == "finanzas-2026-09-19_033000-manual.db"
    assert backup.list_backups(tmp_path / "backups")[0].manual is True


def test_no_escribe_si_el_disco_no_esta_montado(tmp_path, conn, db_path, monkeypatch):
    """Protección para no repetir lo de los 98 GB escondidos bajo el punto de montaje."""
    from app import config as cfg
    backups = tmp_path / "raid" / "backups"
    backups.mkdir(parents=True)
    monkeypatch.setattr(cfg, "BACKUP_SENTINEL", ".esta-en-el-raid")

    problema = backup.dir_problem(backups)
    assert problema and "no parece estar montado" in problema
    try:
        backup.make_backup(db_path, backups, ahora())
        assert False, "tendría que haberse negado"
    except backup.BackupError as e:
        assert "no parece estar montado" in str(e)
    assert list(backups.iterdir()) == []                  # no ha dejado nada

    (backups / ".esta-en-el-raid").write_text("estoy en el raid")
    assert backup.dir_problem(backups) is None
    assert backup.make_backup(db_path, backups, ahora()).exists()


def test_sin_marca_configurada_funciona_igual(tmp_path, conn, db_path):
    """Quien no use un disco aparte no tiene que configurar nada."""
    backups = tmp_path / "copias"
    assert backup.dir_problem(backups) is None
    assert backup.make_backup(db_path, backups, ahora()).exists()
