import datetime as dt
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Destination, Job, MediaItem, UploadedClip, UploadState
from app.transfer_history import TransferEstimator, upload_queue_snapshot


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _queue(**changes):
    return {"durable_bytes": 0, "remaining_bytes": 10000,
            "known_upload_queue": True, "running_uploads": 1,
            "failed_transfers": 0, "estimated_scope": "Current upload queue", **changes}


def test_eta_uses_durable_bytes_and_stops_estimating_when_stalled():
    estimator = TransferEstimator()
    wall = dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc)
    assert estimator.observe(_queue(), now=0)["eta_seconds"] is None
    report = estimator.observe(_queue(durable_bytes=3000), now=30, wall_time=wall)
    assert report["throughput_bytes_per_s"] == 100
    assert report["eta_seconds"] == 100
    assert report["eta_at"] == "2026-10-18T00:01:40+00:00"
    assert estimator.observe(_queue(durable_bytes=3000), now=160)["eta_seconds"] is None


def test_eta_does_not_use_job_percentage_or_invent_data_after_counter_reset():
    estimator = TransferEstimator()
    estimator.observe(_queue(durable_bytes=10000), now=0)
    assert estimator.observe(_queue(durable_bytes=15000), now=30)["eta_seconds"] == 60
    assert estimator.observe(_queue(durable_bytes=10), now=60)["eta_seconds"] is None


def test_new_run_does_not_count_historical_uploaded_bytes_as_new_throughput():
    estimator = TransferEstimator()
    estimator.observe(_queue(durable_bytes=0, running_uploads=0), now=0)
    result = estimator.observe(_queue(durable_bytes=1000000), now=60)
    assert result["eta_seconds"] is None
    assert estimator.observe(_queue(durable_bytes=1003000), now=90)["eta_seconds"] == 100


def test_eta_is_unknown_for_paused_failed_or_incomplete_queue():
    for changes in [{"running_uploads": 0}, {"failed_transfers": 1},
                    {"known_upload_queue": False}]:
        estimator = TransferEstimator()
        estimator.observe(_queue(), now=0)
        report = estimator.observe(_queue(durable_bytes=3000, **changes), now=30)
        assert report["eta_seconds"] is None


def test_remaining_bytes_deduplicates_queue_and_uses_partial_ledger():
    session = _session()
    dest = Destination(name="NAS", type="local", is_default=True, enabled=True)
    session.add(dest)
    session.flush()
    a = MediaItem(path="/camera/a.mp4", filename="a.mp4", size_bytes=1000, checksum="same")
    b = MediaItem(path="/camera/b.mp4", filename="b.mp4", size_bytes=1000, checksum="same")
    complete = MediaItem(path="/camera/c.mp4", filename="c.mp4", size_bytes=500, checksum="done")
    session.add_all([a,b,complete]);session.flush()
    session.add_all([
        Job(kind="upload", status="running", payload=json.dumps({"media_ids":[a.id,b.id,complete.id]})),
        Job(kind="import", status="running"),
        UploadedClip(destination_id=dest.id, checksum="same", filename="a.mp4", size_bytes=1000,
                     status="uploading", bytes_uploaded=300),
        UploadedClip(destination_id=dest.id, checksum="done", filename="c.mp4", size_bytes=500,
                     status="done", bytes_uploaded=500, remote_path="/NAS/c.mp4"),
        UploadState(media_id=a.id, destination_id=dest.id, status="uploading", bytes_uploaded=300),
    ]);session.commit()
    report = upload_queue_snapshot(session)
    assert report["remaining_bytes"] == 700
    assert report["durable_bytes"] == 800
    assert report["known_upload_queue"] is True
    assert "indexing can add more" in report["estimated_scope"]


def test_failed_ledger_wins_over_a_stale_done_upload_state():
    session = _session()
    d = Destination(name="NAS", type="local", is_default=True, enabled=True)
    m = MediaItem(path="/camera/a.mp4", filename="a.mp4", size_bytes=1000, checksum="a")
    session.add_all([d,m]);session.flush()
    session.add_all([
        Job(kind="upload", status="running", payload=json.dumps({"media_ids":[m.id]})),
        UploadedClip(destination_id=d.id, checksum="a", filename="a.mp4", size_bytes=1000,
                     status="error", bytes_uploaded=300),
        UploadState(media_id=m.id, destination_id=d.id, status="done", remote_path="/old", bytes_uploaded=0),
    ]);session.commit()
    result = upload_queue_snapshot(session)
    assert result["failed_transfers"] == 1
    assert result["remaining_bytes"] == 700


def test_missing_media_or_unknown_size_keeps_eta_unknown():
    session = _session()
    d = Destination(name="NAS", type="local", is_default=True, enabled=True)
    session.add(d);session.flush()
    session.add(Job(kind="upload", status="running", payload=json.dumps({"media_ids":[999]})))
    session.commit()
    assert upload_queue_snapshot(session)["known_upload_queue"] is False


def test_verified_corrupt_copy_is_remaining_work_even_if_old_ledger_says_done():
    session = _session()
    d = Destination(name="NAS", type="local", is_default=True, enabled=True)
    m = MediaItem(path="/camera/a.mp4", filename="a.mp4", size_bytes=1000, checksum="a")
    session.add_all([d,m]);session.flush()
    session.add_all([
        Job(kind="upload", status="running", payload=json.dumps({"media_ids":[m.id]})),
        UploadedClip(destination_id=d.id, checksum="a", filename="a.mp4", size_bytes=1000,
                     status="done", bytes_uploaded=1000, remote_path="/old",
                     full_verification_failed=True),
    ]);session.commit()
    result = upload_queue_snapshot(session)
    assert result["failed_transfers"] == 1
    assert result["remaining_bytes"] == 1000
