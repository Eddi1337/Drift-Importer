import datetime as dt
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tasks
from app.database import Base
from app.destinations.local import LocalBackend
from app.media import checksum
from app.models import Destination, MediaItem, UploadedClip, UploadState


class StubContext:
    def __init__(self):
        self.events = []
        self.upload_semaphore = threading.Semaphore(1)

    def log(self, message, level="INFO", progress=None):
        self.events.append((level, message, progress))

    def set_progress(self, value, detail=None):
        self.events.append(("PROGRESS", detail, value))


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


def test_upload_recovers_failed_ledger_when_remote_file_already_verifies(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(tasks, "session_scope", _session_scope_factory(session_maker))
    monkeypatch.setattr(
        tasks,
        "probe",
        lambda _path: {
            "duration_s": None,
            "codec": None,
            "width": None,
            "height": None,
            "capture_time": None,
        },
    )

    source = tmp_path / "DVR00001.MP4"
    source.write_bytes(b"clip-data" * 1024)
    checksum_value = checksum(source)
    remote_root = tmp_path / "nas"
    remote_file = remote_root / "2026" / "06" / source.name
    remote_file.parent.mkdir(parents=True)
    remote_file.write_bytes(source.read_bytes())

    with session_maker() as session:
        dest = Destination(
            name="Mounted NAS",
            type="local",
            base_path=str(remote_root),
            path_template="{year}/{month:02d}",
            is_default=True,
            enabled=True,
        )
        item = MediaItem(
            path=str(source),
            filename=source.name,
            kind="video",
            size_bytes=source.stat().st_size,
            capture_time=dt.datetime(2026, 6, 1, 12, 0, 0),
            checksum=checksum_value,
        )
        session.add_all([dest, item])
        session.flush()
        session.add(UploadState(media_id=item.id, destination_id=dest.id, status="error"))
        session.add(
            UploadedClip(
                destination_id=dest.id,
                source_media_id=item.id,
                checksum=checksum_value,
                filename=source.name,
                size_bytes=source.stat().st_size,
                status="error",
                bytes_uploaded=source.stat().st_size,
                last_error="Remote verification failed after upload",
            )
        )
        session.commit()
        media_id = item.id
        dest_id = dest.id

    def fail_upload(*_args, **_kwargs):
        raise AssertionError("existing verified remote file should not be re-uploaded")

    monkeypatch.setattr(LocalBackend, "upload", fail_upload)

    ctx = StubContext()
    tasks.handle_upload(123, {"media_ids": [media_id], "destination_ids": [dest_id]}, ctx)

    with session_maker() as session:
        clip = session.query(UploadedClip).one()
        state = session.query(UploadState).one()

    assert clip.status == "done"
    assert clip.remote_path == str(remote_file)
    assert clip.last_error is None
    assert state.status == "done"
    assert state.remote_path == str(remote_file)
    assert any(event[1] == "Verified existing DVR00001.MP4" for event in ctx.events)
