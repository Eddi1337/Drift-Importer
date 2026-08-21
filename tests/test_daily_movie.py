import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Album, MediaItem
from app.routers import api


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_daily_movie_groups_only_the_latest_day_and_excludes_derived(monkeypatch, tmp_path):
    session = _session()
    today = dt.datetime.utcnow().replace(microsecond=0, hour=9, minute=0, second=0)
    yesterday = today - dt.timedelta(days=1)
    old, first, second, derived = [tmp_path / name for name in ("old.mp4", "a.mp4", "b.mp4", "day.mp4")]
    for path in (old, first, second, derived):
        path.write_bytes(b"video")
    session.add_all([
        MediaItem(path=str(old), filename="old.mp4", kind="video", checksum="old", capture_time=yesterday),
        MediaItem(path=str(first), filename="a.mp4", kind="video", checksum="a", capture_time=today),
        MediaItem(path=str(second), filename="b.mp4", kind="video", checksum="b", capture_time=today + dt.timedelta(minutes=5)),
        MediaItem(path=str(derived), filename="day.mp4", kind="video", checksum="derived", capture_time=today, derived=True),
    ])
    session.commit()

    queued = []
    monkeypatch.setattr(api, "get_manager", lambda: type("Manager", (), {
        "enqueue": lambda _self, kind, description, payload: queued.append((kind, description, payload)) or 17
    })())

    result = api.start_daily_movie(session)

    day = today.date().isoformat()
    assert result == {"job_id": 17, "group_id": 1, "group_name": f"Day {day}", "file_count": 2}
    group = session.get(Album, 1)
    assert [item.media_id for item in group.items] == [2, 3]
    assert queued == [(
        "merge",
        f"Make {day} movie from 2 clips",
        {"media_ids": [2, 3], "album_id": 1, "output_name": f"day_{today.strftime('%Y_%m_%d')}.mp4"},
    )]
