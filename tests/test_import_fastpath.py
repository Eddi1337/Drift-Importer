"""Re-importing an already-indexed card must not re-probe/re-hash every clip,
and auto-upload must start new clips' uploads while the import is still running."""
import json
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tasks
from app.database import Base
from app.models import Job, MediaItem


class StubContext:
    def __init__(self):
        self.events = []

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


def _wire_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(tasks, "session_scope", _session_scope_factory(maker))
    return maker


def _stub_media_ops(monkeypatch):
    """Replace ffprobe/hashing with counters so tests can assert call volume."""
    calls = {"probe": 0, "checksum": 0}

    def fake_probe(_path):
        calls["probe"] += 1
        return {
            "duration_s": 12.5,
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "capture_time": None,
        }

    def fake_checksum(path, sample_bytes=None):
        calls["checksum"] += 1
        return f"cs-{Path(path).name}"

    monkeypatch.setattr(tasks, "probe", fake_probe)
    monkeypatch.setattr(tasks, "checksum", fake_checksum)
    return calls


def test_reimport_of_unchanged_file_skips_probe_and_checksum(tmp_path, monkeypatch):
    maker = _wire_db(monkeypatch)
    calls = _stub_media_ops(monkeypatch)
    clip = tmp_path / "DVR0001.MP4"
    clip.write_bytes(b"video-bytes" * 100)

    with maker() as s:
        tasks.import_one(s, clip, source="device")
        s.commit()
    assert calls == {"probe": 1, "checksum": 1}

    # Reconnect: same path, same size. Must be a stat-only fast path.
    with maker() as s:
        item = tasks.import_one(s, clip, source="device")
        s.commit()
    assert calls == {"probe": 1, "checksum": 1}
    assert item.duration_s == 12.5

    # A changed file (different size) must be re-probed.
    clip.write_bytes(b"video-bytes" * 200)
    with maker() as s:
        tasks.import_one(s, clip, source="device")
        s.commit()
    assert calls["probe"] == 2


def test_reimport_still_probes_when_metadata_is_missing(tmp_path, monkeypatch):
    maker = _wire_db(monkeypatch)
    calls = _stub_media_ops(monkeypatch)
    clip = tmp_path / "DVR0002.MP4"
    clip.write_bytes(b"x" * 64)

    with maker() as s:
        s.add(
            MediaItem(
                path=str(clip),
                filename=clip.name,
                kind="video",
                size_bytes=clip.stat().st_size,
                checksum="cs-old",
            )
        )
        s.commit()

    # duration/codec were never captured (probe failed originally) → refresh.
    with maker() as s:
        item = tasks.import_one(s, clip, source="device")
        s.commit()
    assert calls["probe"] == 1
    assert item.codec == "h264"


def test_auto_upload_enqueues_new_clips_before_import_finishes(tmp_path, monkeypatch):
    maker = _wire_db(monkeypatch)
    _stub_media_ops(monkeypatch)

    old_clip = tmp_path / "DVR0001.MP4"
    old_clip.write_bytes(b"old" * 50)
    new_clip = tmp_path / "DVR0002.MP4"
    new_clip.write_bytes(b"new" * 50)

    with maker() as s:
        s.add(
            MediaItem(
                path=str(old_clip),
                filename=old_clip.name,
                kind="video",
                size_bytes=old_clip.stat().st_size,
                duration_s=1.0,
                codec="h264",
                checksum="cs-old-clip",
                thumbnail="/thumbs/1.jpg",
            )
        )
        s.commit()
        old_id = s.query(MediaItem.id).filter(MediaItem.path == str(old_clip)).scalar()

    enqueued: list[tuple[str, dict]] = []

    def fake_enqueue(kind, payload, description=""):
        with maker() as s:
            job = Job(kind=kind, status="queued", description=description,
                      payload=json.dumps(payload))
            s.add(job)
            s.commit()
            jid = job.id
        enqueued.append((kind, payload))
        return jid

    monkeypatch.setattr(tasks, "get_manager_enqueue", fake_enqueue)
    monkeypatch.setattr(tasks, "_media_needing_thumbnails", lambda ids: [])

    tasks.handle_import(
        1,
        {
            "paths": [str(old_clip), str(new_clip)],
            "source": "device",
            "auto_upload": True,
            "destination_ids": [7],
        },
        StubContext(),
    )

    uploads = [payload for kind, payload in enqueued if kind == "upload"]
    assert len(uploads) == 2
    with maker() as s:
        new_id = s.query(MediaItem.id).filter(MediaItem.path == str(new_clip)).scalar()
    # The new clip's upload is enqueued first (inline, during the import loop);
    # the already-known clip's re-verification upload queues after it.
    assert uploads[0]["media_ids"] == [new_id]
    assert uploads[1]["media_ids"] == [old_id]
    assert all(u["destination_ids"] == [7] for u in uploads)
