from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tasks
from app.database import Base
from app.models import Destination, MediaItem, UploadedClip


def _session_scope_factory(session_maker):
    @contextmanager
    def scope():
        session = session_maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return scope


def _session_maker(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(tasks, "session_scope", _session_scope_factory(session_maker))
    return session_maker


def test_enqueue_upload_jobs_skips_media_done_for_default_destination(monkeypatch):
    session_maker = _session_maker(monkeypatch)
    with session_maker() as session:
        dest = Destination(name="NAS", type="local", base_path="/mnt/NAS", is_default=True)
        done = MediaItem(path="/media/done.mp4", filename="done.mp4", checksum="aaa")
        missing = MediaItem(path="/media/missing.mp4", filename="missing.mp4", checksum="bbb")
        session.add_all([dest, done, missing])
        session.flush()
        session.add(
            UploadedClip(
                destination_id=dest.id,
                source_media_id=done.id,
                checksum=done.checksum,
                filename=done.filename,
                status="done",
                remote_path="/mnt/NAS/2026/06/done.mp4",
            )
        )
        session.commit()
        done_id = done.id
        missing_id = missing.id

    enqueued = []
    monkeypatch.setattr(
        tasks,
        "get_manager_enqueue",
        lambda kind, payload, description="": enqueued.append((kind, payload, description)) or 42,
    )

    assert tasks.enqueue_upload_jobs([done_id, missing_id]) == [42]
    assert enqueued == [
        (
            "upload",
            {"media_ids": [missing_id], "destination_ids": None},
            "Upload missing.mp4",
        )
    ]


def test_enqueue_upload_jobs_returns_empty_when_every_target_is_done(monkeypatch):
    session_maker = _session_maker(monkeypatch)
    with session_maker() as session:
        dest = Destination(name="NAS", type="local", base_path="/mnt/NAS", is_default=True)
        item = MediaItem(path="/media/done.mp4", filename="done.mp4", checksum="aaa")
        session.add_all([dest, item])
        session.flush()
        session.add(
            UploadedClip(
                destination_id=dest.id,
                source_media_id=item.id,
                checksum=item.checksum,
                filename=item.filename,
                status="done",
                remote_path="/mnt/NAS/2026/06/done.mp4",
            )
        )
        session.commit()
        item_id = item.id

    monkeypatch.setattr(tasks, "get_manager_enqueue", lambda *_args, **_kwargs: 42)

    assert tasks.enqueue_upload_jobs([item_id]) == []
