import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import SystemSample, UploadedClip
from app.routers.api import build_system_stats, build_upload_timeline


def test_build_system_stats_has_expected_sections():
    stats = build_system_stats()

    assert "sampled_at" in stats
    assert {"percent", "load_1m", "load_5m", "load_15m", "cpu_count", "history"} <= set(stats["cpu"])
    assert {
        "rx_bytes_total",
        "tx_bytes_total",
        "rx_bytes_per_s",
        "tx_bytes_per_s",
        "rx_history",
        "tx_history",
    } <= set(stats["network"])
    # No DB session -> history series are present but empty.
    assert stats["cpu"]["history"] == []
    assert stats["filesystems"]
    assert {"label", "path", "total_bytes", "used_bytes", "free_bytes", "used_percent"} <= set(
        stats["filesystems"][0]
    )
    assert {"hours", "bucket_minutes", "points"} <= set(stats["upload_timeline"])


def test_build_system_stats_returns_stored_history_window():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session_maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with session_maker() as session:
        # One sample inside the 30-minute window, one well outside it.
        session.add(SystemSample(created_at=now - dt.timedelta(minutes=5), cpu_percent=42.0,
                                 rx_bytes_per_s=1000, tx_bytes_per_s=2000))
        session.add(SystemSample(created_at=now - dt.timedelta(hours=5), cpu_percent=99.0,
                                 rx_bytes_per_s=1, tx_bytes_per_s=1))
        session.commit()

        stats = build_system_stats(timeline_hours=0.5, session=session)

    assert [point["v"] for point in stats["cpu"]["history"]] == [42.0]
    assert [point["v"] for point in stats["network"]["rx_history"]] == [1000]
    assert [point["v"] for point in stats["network"]["tx_history"]] == [2000]


def test_build_upload_timeline_buckets_recent_uploads():
    now = dt.datetime(2026, 6, 19, 12, 0, 0)
    rows = [
        UploadedClip(
            filename="done.mp4",
            checksum="a",
            destination_id=1,
            size_bytes=100,
            bytes_uploaded=100,
            status="done",
            uploaded_at=now - dt.timedelta(minutes=20),
            updated_at=now - dt.timedelta(minutes=20),
        ),
        UploadedClip(
            filename="failed.mp4",
            checksum="b",
            destination_id=1,
            size_bytes=200,
            bytes_uploaded=80,
            status="error",
            updated_at=now - dt.timedelta(minutes=10),
        ),
        UploadedClip(
            filename="old.mp4",
            checksum="c",
            destination_id=1,
            size_bytes=300,
            bytes_uploaded=300,
            status="done",
            uploaded_at=now - dt.timedelta(hours=4),
            updated_at=now - dt.timedelta(hours=4),
        ),
    ]

    timeline = build_upload_timeline(rows, hours=3, now=now)

    assert timeline["hours"] == 3
    assert timeline["bucket_minutes"] == 10
    assert timeline["total_uploaded_bytes"] == 100
    assert timeline["total_error_bytes"] == 80
    assert timeline["total_active_bytes"] == 0
    assert sum(point["clip_count"] for point in timeline["points"]) == 2
