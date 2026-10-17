"""State recovery snapshots include live WAL commits and the encryption key."""
import os
import sqlite3
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app import state_backup, storage


@pytest.fixture
def setup(tmp_path, monkeypatch):
    local = tmp_path / "data"
    local.mkdir()
    nas = tmp_path / "NAS"
    nas.mkdir()
    settings = SimpleNamespace(
        db_path=local / "drift.db",
        secret_key_path=local / "secret.key",
        nas_root=nas,
        require_nas_mount=False,
        state_backup_interval_s=0,
    )
    settings.secret_key_path.write_bytes(Fernet.generate_key())
    settings.secret_key_path.chmod(0o600)
    # The unit-test directory represents the mounted NAS. Guard rejection and
    # root escape are tested separately; no production mount check is disabled.
    monkeypatch.setattr(state_backup, "require_storage", lambda root: root.resolve())
    connection = sqlite3.connect(settings.db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE records (value TEXT)")
    connection.commit()
    yield settings, connection, state_backup.StateBackup(settings)
    connection.close()


def values(path):
    with sqlite3.connect(path) as database:
        return database.execute("SELECT value FROM records").fetchall()


def test_snapshot_includes_latest_committed_wal_and_recovery_key(setup):
    settings, connection, backup = setup
    key = settings.secret_key_path.read_bytes()
    encrypted = Fernet(key).encrypt(b"destination credential").decode()
    connection.execute("INSERT INTO records VALUES (?)", (encrypted,))
    connection.commit()
    assert Path(str(settings.db_path) + "-wal").stat().st_size > 0
    # An uncommitted row from another connection must not be included.
    connection.execute("INSERT INTO records VALUES ('uncommitted')")
    database_before = settings.db_path.read_bytes()
    wal_path = Path(str(settings.db_path) + "-wal")
    wal_before = wal_path.read_bytes()

    path = backup.snapshot()

    assert path == settings.nas_root / ".drift/state/drift.db"
    recovered_key = path.with_name("secret.key").read_bytes()
    assert recovered_key == key
    assert Fernet(recovered_key).decrypt(values(path)[0][0].encode()) == b"destination credential"
    assert len(values(path)) == 1
    with sqlite3.connect(path) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.with_name("secret.key").stat().st_mode) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["drift.db", "secret.key"]
    assert values(settings.db_path) == [(encrypted,)]
    assert settings.db_path.read_bytes() == database_before
    assert wal_path.read_bytes() == wal_before


def test_failed_publication_preserves_previous_good_database(setup, monkeypatch):
    settings, connection, backup = setup
    connection.execute("INSERT INTO records VALUES ('first backup')")
    connection.commit()
    path = backup.snapshot()
    first = path.read_bytes()
    connection.execute("INSERT INTO records VALUES ('new committed row')")
    connection.commit()
    original_replace = os.replace

    def fail_publication(source, target):
        if Path(target) == path:
            raise OSError("NAS publication interrupted")
        original_replace(source, target)

    monkeypatch.setattr(state_backup.os, "replace", fail_publication)
    with pytest.raises(OSError, match="publication interrupted"):
        backup.snapshot()
    assert path.read_bytes() == first
    assert values(path) == [("first backup",)]
    assert sorted(p.name for p in path.parent.iterdir()) == ["drift.db", "secret.key"]


def test_failed_nas_stream_preserves_previous_good_database(setup, monkeypatch):
    _settings, connection, backup = setup
    connection.execute("INSERT INTO records VALUES ('previous backup')")
    connection.commit()
    path = backup.snapshot()
    first = path.read_bytes()
    connection.execute("INSERT INTO records VALUES ('latest commit')")
    connection.commit()

    def failed_stream(source, destination, length):
        destination.write(source.read(100))
        raise OSError("NAS stream interrupted")

    monkeypatch.setattr(state_backup.shutil, "copyfileobj", failed_stream)
    with pytest.raises(OSError, match="stream interrupted"):
        backup.snapshot()
    assert path.read_bytes() == first
    assert values(path) == [("previous backup",)]
    assert sorted(p.name for p in path.parent.iterdir()) == ["drift.db", "secret.key"]


def test_missing_mount_guard_fails_before_creating_anything(setup, monkeypatch):
    settings, _connection, backup = setup

    def offline(_root):
        raise RuntimeError("not a mounted NAS")

    monkeypatch.setattr(state_backup, "require_storage", offline)
    with pytest.raises(RuntimeError, match="not a mounted NAS"):
        backup.snapshot()
    assert list(settings.nas_root.iterdir()) == []


def test_real_storage_guard_rejects_an_sd_card_fallback(setup, monkeypatch):
    settings, _connection, backup = setup
    monkeypatch.setattr(state_backup, "require_storage", storage.require_storage)
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(require_nas_mount=True))
    monkeypatch.setattr(storage, "filesystem_type", lambda _root: "ext4")
    with pytest.raises(RuntimeError, match="not a mounted NAS"):
        backup.snapshot()
    assert list(settings.nas_root.iterdir()) == []


def test_staging_refuses_an_sd_card_tmp_directory(setup, monkeypatch):
    settings, _connection, backup = setup
    settings.require_nas_mount = True
    monkeypatch.setattr(state_backup, "filesystem_type", lambda _root: "ext4")
    with pytest.raises(RuntimeError, match="/tmp.*tmpfs"):
        backup.snapshot()
    assert list(settings.nas_root.iterdir()) == []


def test_sqlite_snapshot_is_staged_in_tmp_and_never_opened_on_nas(setup, monkeypatch):
    settings, connection, backup = setup
    settings.require_nas_mount = True
    monkeypatch.setattr(state_backup, "filesystem_type", lambda _root: "tmpfs")
    connection.execute("INSERT INTO records VALUES ('committed')")
    connection.commit()
    original_connect = sqlite3.connect
    opened = []

    def connect(path, *args, **kwargs):
        opened.append(path)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(state_backup.sqlite3, "connect", connect)
    path = backup.snapshot()
    assert len(opened) == 2
    assert opened[0] == settings.db_path.resolve().as_uri() + "?mode=ro"
    staging = Path(opened[1])
    assert staging.is_relative_to(Path("/tmp"))
    assert not staging.is_relative_to(settings.nas_root)
    assert not staging.parent.exists()
    assert values(path) == [("committed",)]


def test_symlink_outside_nas_cannot_create_local_snapshot(setup, tmp_path):
    settings, _connection, backup = setup
    local_fallback = tmp_path / "local-fallback"
    local_fallback.mkdir()
    (settings.nas_root / ".drift").symlink_to(local_fallback, target_is_directory=True)
    with pytest.raises(RuntimeError, match="escapes the NAS"):
        backup.snapshot()
    assert list(local_fallback.iterdir()) == []


def test_key_change_preserves_previous_recoverable_pair(setup):
    settings, connection, backup = setup
    connection.execute("INSERT INTO records VALUES ('recoverable')")
    connection.commit()
    path = backup.snapshot()
    original_key = path.with_name("secret.key").read_bytes()
    original_database = path.read_bytes()
    settings.secret_key_path.write_bytes(Fernet.generate_key())
    with pytest.raises(RuntimeError, match="key differs"):
        backup.snapshot()
    assert path.with_name("secret.key").read_bytes() == original_key
    assert path.read_bytes() == original_database


def test_disabled_service_does_not_start_background_work(setup, monkeypatch):
    _settings, _connection, backup = setup
    calls = []
    monkeypatch.setattr(backup, "snapshot", lambda: calls.append(True))
    backup.start()
    backup.stop()
    assert backup._thread is None
    assert calls == []


def test_service_initial_and_final_snapshots_are_background_work(setup, monkeypatch):
    settings, _connection, backup = setup
    settings.state_backup_interval_s = 900
    first = threading.Event()
    workers = []

    def snapshot():
        workers.append(threading.current_thread().name)
        first.set()

    monkeypatch.setattr(backup, "snapshot", snapshot)
    backup.start()
    assert first.wait(2)
    backup.stop()
    assert workers == ["drift-state-backup", "drift-state-backup"]
    assert not backup._thread.is_alive()
