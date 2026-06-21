import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.destinations.local import LocalBackend
from app.models import Destination, UploadedClip
from app.routers import api


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_build_upload_stats_aggregates_persistent_metrics(monkeypatch):
    session = _session()
    destination = Destination(name="Archive", type="local", base_path="/tmp/archive")
    session.add(destination)
    session.flush()
    session.add_all(
        [
            UploadedClip(
                destination_id=destination.id,
                checksum="aaa",
                filename="clip-a.mp4",
                size_bytes=100,
                status="done",
                bytes_uploaded=100,
                upload_duration_s=4.0,
                upload_throughput_bps=25.0,
                uploaded_at=dt.datetime(2026, 6, 17, 12, 0, 0),
            ),
            UploadedClip(
                destination_id=destination.id,
                checksum="bbb",
                filename="clip-b.mp4",
                size_bytes=200,
                status="done",
                bytes_uploaded=200,
                upload_duration_s=5.0,
                upload_throughput_bps=40.0,
                uploaded_at=dt.datetime(2026, 6, 17, 12, 5, 0),
            ),
            UploadedClip(
                destination_id=destination.id,
                checksum="ccc",
                filename="clip-c.mp4",
                size_bytes=300,
                status="error",
                bytes_uploaded=120,
            ),
        ]
    )
    session.commit()

    class StubBackend:
        def storage_info(self):
            return {"free_bytes": 700, "total_bytes": 1000, "used_bytes": 300}

    monkeypatch.setattr(api, "get_backend", lambda dest: StubBackend())

    stats = api.build_upload_stats(session)

    assert stats["overview"] == {
        "uploaded_clip_count": 2,
        "error_clip_count": 1,
        "uploading_clip_count": 0,
        "pending_clip_count": 0,
        "uploaded_bytes": 300,
        "average_upload_duration_s": 4.5,
        "average_throughput_bps": 32.5,
    }
    assert len(stats["destinations"]) == 1
    destination_stats = stats["destinations"][0]
    upload_timeline = destination_stats.pop("upload_timeline")
    assert destination_stats == {
        "id": destination.id,
        "name": "Archive",
        "type": "local",
        "host": None,
        "port": None,
        "username": None,
        "base_path": "/tmp/archive",
        "path_template": "{year}/{month:02d}",
        "is_default": False,
        "enabled": True,
        "rank": 100,
        "has_secret": False,
        "uploaded_clip_count": 2,
        "error_clip_count": 1,
        "uploading_clip_count": 0,
        "pending_clip_count": 0,
        "uploaded_bytes": 300,
        "average_upload_duration_s": 4.5,
        "average_throughput_bps": 32.5,
        "storage": {
            "free_bytes": 700,
            "total_bytes": 1000,
            "used_bytes": 300,
            "bytes_uploaded_by_app": 300,
        },
    }
    assert {"hours", "bucket_minutes", "points"} <= set(upload_timeline)


def test_local_backend_reports_storage_info(tmp_path):
    backend = LocalBackend(Destination(name="NAS", type="local", base_path=str(tmp_path)))

    info = backend.storage_info()

    assert isinstance(info["free_bytes"], int)
    assert isinstance(info["total_bytes"], int)
    assert info["total_bytes"] >= info["free_bytes"] >= 0
