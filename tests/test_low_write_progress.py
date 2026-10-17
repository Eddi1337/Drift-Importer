"""Sparse UI checkpoints must preserve cancellation and recoverable backups."""
import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import jobs, tasks
from app.database import Base
from app.destinations import local
from app.jobs import JobCancelled, JobContext, JobManager
from app.media import checksum
from app.models import Destination, Job, JobLog, MediaItem, UploadedClip, UploadState


@pytest.fixture
def setup_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        with maker() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(jobs, "session_scope", scope)
    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(
        jobs, "get_settings", lambda: SimpleNamespace(
            job_progress_interval_s=15, max_concurrent_uploads=1,
            max_concurrent_ffmpeg=1,
        )
    )
    monkeypatch.setattr(
        tasks, "get_settings", lambda: SimpleNamespace(upload_progress_interval_s=30)
    )
    clock = [100.0]
    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock[0])
    return maker, clock


def new_job(maker, kind="import"):
    with maker() as session:
        job = Job(kind=kind, status="running")
        session.add(job)
        session.commit()
        return job.id


def test_sparse_progress_reduces_detail_logs_and_always_saves_completion(setup_db):
    maker, clock = setup_db
    job_id = new_job(maker)
    ctx = JobContext(JobManager(worker_count=0), job_id)
    ctx.set_progress(0.1, "Indexed first file")
    for index in range(1, 100):
        clock[0] = 100 + index / 10
        ctx.set_progress(0.1 + index / 200, f"Indexed file {index}")
    with maker() as session:
        assert session.get(Job, job_id).progress == 0.1
        assert session.query(JobLog).count() == 1

    clock[0] = 115
    ctx.set_progress(0.9, "Nearly finished")
    clock[0] = 115.1
    ctx.set_progress(1, "All files indexed")
    with maker() as session:
        job = session.get(Job, job_id)
        assert job.progress == 1
        assert job.detail == "All files indexed"
        assert session.query(JobLog).count() == 3


def test_cancellation_does_not_wait_for_progress_interval(setup_db):
    maker, clock = setup_db
    job_id = new_job(maker)
    manager = JobManager(worker_count=0)
    ctx = JobContext(manager, job_id)
    ctx.set_progress(0.1, "First file")
    manager.request_cancel(job_id)
    clock[0] += 0.01
    with pytest.raises(JobCancelled):
        ctx.set_progress(0.2, "Second file")
    with maker() as session:
        assert session.get(Job, job_id).progress == 0.1


def test_job_error_is_persisted_inside_suppressed_progress_window(setup_db, monkeypatch):
    maker, clock = setup_db
    job_id = new_job(maker)

    def fail(_job_id, _payload, ctx):
        ctx.set_progress(0.1, "Started indexing")
        clock[0] += 1
        ctx.set_progress(0.9, "Still indexing")
        raise OSError("Camera disconnected")

    monkeypatch.setitem(jobs._HANDLERS, "import", fail)
    JobManager(worker_count=0)._run_job(job_id)
    with maker() as session:
        job = session.get(Job, job_id)
        assert job.status == "error"
        assert job.error == "Camera disconnected"
        assert job.finished_at is not None
        assert session.query(JobLog).filter_by(level="ERROR").count() == 1


def upload_fixture(tmp_path, maker, monkeypatch):
    source = tmp_path / "DVR00001.MP4"
    source.write_bytes(bytes(range(128)))
    root = tmp_path / "nas"
    root.mkdir()
    monkeypatch.setattr(local, "require_storage", lambda root: root)
    monkeypatch.setattr(
        local, "get_settings", lambda: SimpleNamespace(upload_chunk_bytes=4)
    )
    with maker() as session:
        job = Job(kind="upload", status="running")
        item = MediaItem(
            path=str(source), filename=source.name, source="library", kind="video",
            size_bytes=source.stat().st_size, duration_s=1, codec="h264",
            capture_time=dt.datetime(2026, 10, 1), checksum=checksum(source),
        )
        dest = Destination(
            name="NAS", type="local", base_path=str(root), path_template="backups",
            enabled=True, is_default=True,
        )
        session.add_all([job, item, dest])
        session.commit()
        ids = job.id, item.id, dest.id
    target = root / "backups" / source.name
    target.parent.mkdir()
    return source, target, ids


def test_resume_uses_real_partial_size_and_persists_final_ledger(
    setup_db, tmp_path, monkeypatch
):
    maker, clock = setup_db
    source, target, (job_id, mid, did) = upload_fixture(tmp_path, maker, monkeypatch)
    partial = target.with_suffix(".MP4.part")
    partial.write_bytes(source.read_bytes()[:20])
    # Simulate stale progress after a power cut: the last persisted checkpoint
    # says 4 bytes, while the NAS already has 20 recoverable bytes.
    with maker() as session:
        session.add(UploadState(media_id=mid, destination_id=did, status="uploading", bytes_uploaded=4))
        session.add(UploadedClip(
            destination_id=did, source_media_id=mid, checksum=checksum(source),
            filename=source.name, size_bytes=128, bytes_uploaded=4, status="uploading",
        ))
        session.commit()
    checkpoints = []
    mark_progress = tasks._mark_upload_progress

    def record_checkpoint(*args, **kwargs):
        checkpoints.append(args[3])
        return mark_progress(*args, **kwargs)

    monkeypatch.setattr(tasks, "_mark_upload_progress", record_checkpoint)
    ctx = JobContext(JobManager(worker_count=0), job_id)
    tasks.handle_upload(job_id, {"media_ids": [mid], "destination_ids": [did]}, ctx)
    assert checkpoints == [20, 128]
    assert target.read_bytes() == source.read_bytes()
    assert not partial.exists()
    with maker() as session:
        state = session.query(UploadState).one()
        ledger = session.query(UploadedClip).one()
        assert state.status == ledger.status == "done"
        assert state.bytes_uploaded == ledger.bytes_uploaded == 128
        assert state.uploaded_at is not None and ledger.uploaded_at is not None
        assert session.get(Job, job_id).progress == 1


def test_upload_error_and_cancellation_survive_sparse_checkpoints(
    setup_db, tmp_path, monkeypatch
):
    maker, clock = setup_db
    source, target, (job_id, mid, did) = upload_fixture(tmp_path, maker, monkeypatch)
    manager = JobManager(worker_count=0)
    partial = target.with_suffix(".MP4.part")

    def interrupted(_backend, _path, _dir, _name, progress, **kwargs):
        partial.write_bytes(source.read_bytes()[:16])
        clock[0] += 1
        progress(16, 128)
        raise OSError("NAS went offline")

    monkeypatch.setattr(local.LocalBackend, "upload", interrupted)
    tasks.handle_upload(
        job_id, {"media_ids": [mid], "destination_ids": [did]},
        JobContext(manager, job_id),
    )
    with maker() as session:
        state = session.query(UploadState).one()
        ledger = session.query(UploadedClip).one()
        assert state.status == ledger.status == "error"
        assert state.error == ledger.last_error == "NAS went offline"
        assert state.bytes_uploaded == ledger.bytes_uploaded == 0
    assert partial.stat().st_size == 16

    def cancelled(_backend, _path, _dir, _name, progress, **kwargs):
        assert kwargs["start_offset"] == 16
        manager.request_cancel(job_id)
        clock[0] += 1
        progress(20, 128)
        pytest.fail("Cancellation must be observed before another checkpoint is due")

    monkeypatch.setattr(local.LocalBackend, "upload", cancelled)
    with pytest.raises(JobCancelled):
        tasks.handle_upload(
            job_id, {"media_ids": [mid], "destination_ids": [did]},
            JobContext(manager, job_id),
        )
    assert partial.stat().st_size == 16
