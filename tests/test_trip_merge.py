import threading
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tasks
from app.database import Base
from app.models import Album, AlbumItem, MediaItem


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


class DummyContext:
    ffmpeg_semaphore = threading.Semaphore(1)

    def set_progress(self, *_args, **_kwargs):
        pass


def test_merge_job_adds_combined_clip_to_trip(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(tasks, "session_scope", _session_scope_factory(session_maker))
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(working_dir=tmp_path))
    monkeypatch.setattr(tasks, "merge_clips", lambda _paths, output: output.write_bytes(b"merged"))
    monkeypatch.setattr(tasks, "get_manager_enqueue", lambda *_args, **_kwargs: 1)

    def fake_import_one(session, output, source, derived=False):
        item = MediaItem(
            path=str(output),
            filename=output.name,
            kind="video",
            size_bytes=output.stat().st_size,
            checksum="merged",
            source=source,
            derived=derived,
        )
        session.add(item)
        session.flush()
        return item

    monkeypatch.setattr(tasks, "import_one", fake_import_one)

    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    clip_a.write_bytes(b"a")
    clip_b.write_bytes(b"b")
    with session_maker() as session:
        a = MediaItem(path=str(clip_a), filename="a.mp4", kind="video", checksum="a")
        b = MediaItem(path=str(clip_b), filename="b.mp4", kind="video", checksum="b")
        trip = Album(name="Summer")
        session.add_all([a, b, trip])
        session.flush()
        session.add_all(
            [
                AlbumItem(album_id=trip.id, media_id=a.id, position=0),
                AlbumItem(album_id=trip.id, media_id=b.id, position=1),
            ]
        )
        session.commit()
        media_ids = [a.id, b.id]
        trip_id = trip.id

    tasks.handle_merge(
        1,
        {"media_ids": media_ids, "album_id": trip_id, "output_name": "trip.mp4"},
        DummyContext(),
    )

    with session_maker() as session:
        trip = session.get(Album, trip_id)
        assert [item.position for item in trip.items] == [0, 1, 2]
        assert len(trip.items) == 3
        merged = session.get(MediaItem, trip.items[-1].media_id)
        assert merged.filename == "trip.mp4"
        assert merged.derived is True
