"""SQLite database setup using SQLAlchemy 2.0."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_engine = create_engine(
    f"sqlite:///{_settings.db_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    # WAL gives better concurrency between the web request handlers and the
    # background job threads; both run in the same process.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(_engine)
    _run_migrations()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def _run_migrations() -> None:
    db_path = _settings.db_path
    conn = sqlite3.connect(db_path)
    try:
        tables = _table_names(conn)
        if "upload_states" in tables:
            cols = _column_names(conn, "upload_states")
            if "bytes_uploaded" not in cols:
                conn.execute(
                    "ALTER TABLE upload_states ADD COLUMN bytes_uploaded INTEGER DEFAULT 0"
                )
            if "total_bytes" not in cols:
                conn.execute(
                    "ALTER TABLE upload_states ADD COLUMN total_bytes INTEGER DEFAULT 0"
                )
            if "updated_at" not in cols:
                conn.execute(
                    "ALTER TABLE upload_states ADD COLUMN updated_at DATETIME"
                )
                conn.execute(
                    "UPDATE upload_states SET updated_at = COALESCE(uploaded_at, CURRENT_TIMESTAMP)"
                )

        if "uploaded_clips" in tables:
            cols = _column_names(conn, "uploaded_clips")
            if "upload_duration_s" not in cols:
                conn.execute(
                    "ALTER TABLE uploaded_clips ADD COLUMN upload_duration_s FLOAT"
                )
            if "upload_throughput_bps" not in cols:
                conn.execute(
                    "ALTER TABLE uploaded_clips ADD COLUMN upload_throughput_bps FLOAT"
                )
            if "uploaded_at" not in cols:
                conn.execute(
                    "ALTER TABLE uploaded_clips ADD COLUMN uploaded_at DATETIME"
                )
                conn.execute(
                    """
                    UPDATE uploaded_clips
                    SET uploaded_at = CASE
                        WHEN status = 'done' THEN COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
                        ELSE NULL
                    END
                    """
                )

        if "jobs" in tables:
            cols = _column_names(conn, "jobs")
            if "dismissed_at" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN dismissed_at DATETIME")

        if "job_logs" not in tables:
            conn.execute(
                """
                CREATE TABLE job_logs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    level VARCHAR(16) DEFAULT 'INFO' NOT NULL,
                    message TEXT NOT NULL,
                    progress FLOAT,
                    created_at DATETIME,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX ix_job_logs_job_id ON job_logs (job_id)")
            conn.execute("CREATE INDEX ix_job_logs_created_at ON job_logs (created_at)")

        if "app_settings" in tables:
            cols = _column_names(conn, "app_settings")
            if "auto_import_on_connect" not in cols:
                conn.execute(
                    "ALTER TABLE app_settings ADD COLUMN auto_import_on_connect BOOLEAN DEFAULT 0"
                )
            if "auto_upload_on_import" not in cols:
                conn.execute(
                    "ALTER TABLE app_settings ADD COLUMN auto_upload_on_import BOOLEAN DEFAULT 0"
                )
            if "default_destination_ids" not in cols:
                conn.execute(
                    "ALTER TABLE app_settings ADD COLUMN default_destination_ids TEXT DEFAULT ''"
                )
            if "ha_base_url" not in cols:
                conn.execute(
                    "ALTER TABLE app_settings ADD COLUMN ha_base_url VARCHAR(1024)"
                )
            if "ha_token" not in cols:
                conn.execute("ALTER TABLE app_settings ADD COLUMN ha_token TEXT")
            if "ha_entity_prefix" not in cols:
                conn.execute(
                    "ALTER TABLE app_settings ADD COLUMN ha_entity_prefix VARCHAR(128) DEFAULT 'drift_import'"
                )
            if "updated_at" not in cols:
                conn.execute("ALTER TABLE app_settings ADD COLUMN updated_at DATETIME")
                conn.execute("UPDATE app_settings SET updated_at = CURRENT_TIMESTAMP")

        conn.execute(
            """
            INSERT OR IGNORE INTO app_settings
                (id, auto_import_on_connect, auto_upload_on_import, default_destination_ids, ha_entity_prefix, updated_at)
            VALUES
                (1, 0, 0, '', 'drift_import', CURRENT_TIMESTAMP)
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_engine():
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for use inside background jobs / scripts."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
