"""Recoverable SQLite snapshots on the NAS; the live WAL database stays local."""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from .config import get_settings
from .storage import require_storage

log = logging.getLogger("drift.state_backup")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StateBackup:
    """Periodically publish a standalone database and its credential key.

    Nothing is restored automatically. Both files in ``.drift/state`` are
    needed to recover app state after replacing the Pi's SD card. The key is
    deliberately never included in logs or in a public application endpoint.
    """

    def __init__(self, settings=None):
        self.settings = settings if settings is not None else get_settings()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        interval = self.settings.state_backup_interval_s
        if interval <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="drift-state-backup", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Request a final snapshot without making shutdown wait on offline NFS."""
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                log.warning("NAS state snapshot is still finishing at shutdown")

    def _try_snapshot(self) -> None:
        try:
            self.snapshot()
        except Exception as exc:  # NAS failure must not stop imports or startup.
            log.warning("NAS state snapshot failed: %s", exc)

    def _run(self) -> None:
        # The first snapshot is asynchronous so boot does not wait for the NAS.
        while not self._stop.is_set():
            self._try_snapshot()
            if self._stop.wait(self.settings.state_backup_interval_s):
                break
        self._try_snapshot()

    def snapshot(self) -> Path:
        """Atomically replace the NAS snapshot only after a consistent backup.

        SQLite's backup API includes committed WAL data without copying or
        checkpointing the live database. Temporary files are created on the
        guarded NAS itself, so an unavailable mount cannot fill the SD card.
        """
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> Path:
        root = require_storage(self.settings.nas_root)
        state_dir = root / ".drift" / "state"
        if not state_dir.resolve().is_relative_to(root):
            raise RuntimeError("State snapshot directory escapes the NAS")

        # Read the key before creating anything: a database without its key is
        # not a usable recovery copy of encrypted destination credentials.
        key = self.settings.secret_key_path.read_bytes()
        if not key:
            raise RuntimeError("Cannot snapshot app state without its secret key")
        if not self.settings.db_path.is_file():
            raise RuntimeError("Cannot snapshot app state without its database")

        # Recheck immediately before writes, including on later periodic runs.
        root = require_storage(self.settings.nas_root)
        for directory in (root / ".drift", state_dir):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        _fsync_directory(root)
        _fsync_directory(root / ".drift")

        key_path = state_dir / "secret.key"
        if key_path.is_symlink():
            raise RuntimeError("State snapshot key must be a regular private file")
        if key_path.exists() and key_path.read_bytes() != key:
            # A rotated key needs an explicit paired migration. Never replace
            # the old key and leave the previous good DB undecryptable.
            raise RuntimeError("Snapshot key differs from the app key; recovery copy preserved")

        fd, name = tempfile.mkstemp(prefix=".drift.db-", suffix=".tmp", dir=state_dir)
        temporary = Path(name)
        os.close(fd)
        key_temporary: Path | None = None
        try:
            deadline = time.monotonic() + 120.0

            def progress(_status, _remaining, _total):
                if time.monotonic() > deadline:
                    raise TimeoutError("SQLite state snapshot exceeded its time limit")
                # Yield between small page batches on the Pi while uploads run.
                time.sleep(0.01)

            source_uri = self.settings.db_path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as source:
                with closing(sqlite3.connect(temporary, timeout=5.0)) as destination:
                    destination.execute("PRAGMA synchronous=FULL")
                    source.backup(destination, pages=64, progress=progress, sleep=0.05)
                    destination.execute("PRAGMA journal_mode=DELETE")
                    if destination.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise RuntimeError("SQLite state snapshot failed its integrity check")
            temporary.chmod(0o600)
            with temporary.open("rb") as snapshot_file:
                os.fsync(snapshot_file.fileno())

            if self.settings.secret_key_path.read_bytes() != key:
                raise RuntimeError("App key changed during snapshot; recovery copy preserved")
            # Check again before publishing; mounting failures leave the old
            # complete snapshot in place rather than creating a local fallback.
            require_storage(self.settings.nas_root)
            if not key_path.exists():
                key_fd, key_name = tempfile.mkstemp(prefix=".secret.key-", dir=state_dir)
                key_temporary = Path(key_name)
                with os.fdopen(key_fd, "wb") as key_file:
                    key_file.write(key)
                    key_file.flush()
                    os.fsync(key_file.fileno())
                os.replace(key_temporary, key_path)
                _fsync_directory(state_dir)
            key_path.chmod(0o600)
            snapshot_path = state_dir / "drift.db"
            os.replace(temporary, snapshot_path)
            _fsync_directory(state_dir)
            log.info("App state snapshot saved on the NAS")
            return snapshot_path
        finally:
            for candidate in (temporary, key_temporary):
                if candidate is not None:
                    candidate.unlink(missing_ok=True)
            # SQLite should use a standalone DELETE-journal DB, but remove any
            # unpublished sidecars after an interrupted backup too.
            for suffix in ("-journal", "-wal", "-shm"):
                Path(str(temporary) + suffix).unlink(missing_ok=True)


@lru_cache
def get_state_backup() -> StateBackup:
    return StateBackup()
