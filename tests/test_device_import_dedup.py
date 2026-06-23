import json
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tasks
from app.database import Base
from app.models import Job, MediaItem


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


def _make_dcim(tmp_path) -> Path:
    dcim = tmp_path / "Drift" / "DCIM" / "100MEDIA"
    dcim.mkdir(parents=True)
    (dcim / "DVR0001.MP4").write_bytes(b"x")
    (dcim / "DVR0002.MP4").write_bytes(b"x")
    return tmp_path / "Drift" / "DCIM"


def test_enqueue_device_import_dedupes_reconnect(tmp_path, monkeypatch):
    maker = _wire_db(monkeypatch)
    dcim = _make_dcim(tmp_path)

    enqueued: list[dict] = []

    def fake_enqueue(kind, payload, description=""):
        with maker() as s:
            job = Job(kind=kind, status="queued", description=description,
                      payload=json.dumps(payload))
            s.add(job)
            s.commit()
            jid = job.id
        enqueued.append(payload)
        return jid

    monkeypatch.setattr(tasks, "get_manager_enqueue", fake_enqueue)

    job_id, count = tasks.enqueue_device_import(dcim, auto_upload=False)
    assert job_id is not None
    assert count == 2
    assert enqueued[0]["dcim_root"] == str(dcim)

    # A second detection of the same still-connected device must not re-enqueue.
    job_id2, count2 = tasks.enqueue_device_import(dcim, auto_upload=False)
    assert job_id2 is None
    assert count2 == 2
    assert len(enqueued) == 1


def test_enqueue_device_import_honours_explicit_selection(tmp_path, monkeypatch):
    maker = _wire_db(monkeypatch)
    dcim = _make_dcim(tmp_path)
    one = str(dcim / "100MEDIA" / "DVR0001.MP4")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        tasks, "get_manager_enqueue",
        lambda kind, payload, description="": (calls.append(payload["paths"]) or 1),
    )

    # Even with a pending whole-device import, an explicit file pick (dedup off)
    # still goes through with just the chosen file.
    tasks.enqueue_device_import(dcim, auto_upload=False)
    job_id, count = tasks.enqueue_device_import(dcim, paths=[one], dedup=False)
    assert job_id == 1
    assert count == 1
    assert calls[-1] == [one]


def test_media_needing_thumbnails_skips_existing(tmp_path, monkeypatch):
    maker = _wire_db(monkeypatch)
    thumb = tmp_path / "1.jpg"
    thumb.write_bytes(b"jpg")
    with maker() as s:
        s.add(MediaItem(id=1, path="/a.mp4", filename="a.mp4", thumbnail=str(thumb)))
        s.add(MediaItem(id=2, path="/b.mp4", filename="b.mp4", thumbnail=None))
        s.commit()

    # id=1 already has a thumbnail on disk; id=2 needs one.
    monkeypatch.setattr(tasks, "_thumb_path_for", lambda mid: tmp_path / f"missing-{mid}.jpg")
    need = tasks._media_needing_thumbnails([1, 2])
    assert need == [2]


class _Ctx:
    def __init__(self):
        self.ffmpeg_semaphore = __import__("threading").Semaphore(1)

    def log(self, *a, **k):
        pass

    def set_progress(self, *a, **k):
        pass


def test_handle_thumbnail_skips_when_file_exists(tmp_path, monkeypatch):
    maker = _wire_db(monkeypatch)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    with maker() as s:
        s.add(MediaItem(id=1, path=str(media), filename="clip.mp4", thumbnail=None))
        s.commit()

    out = tmp_path / "1.jpg"
    out.write_bytes(b"jpg")  # thumbnail already on disk
    monkeypatch.setattr(tasks, "_thumb_path_for", lambda mid: out)

    called = {"n": 0}
    monkeypatch.setattr(tasks, "make_thumbnail", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True)

    tasks.handle_thumbnail(1, {"media_ids": [1]}, _Ctx())

    assert called["n"] == 0  # ffmpeg never invoked
    with maker() as s:
        assert s.get(MediaItem, 1).thumbnail == str(out)  # row backfilled
