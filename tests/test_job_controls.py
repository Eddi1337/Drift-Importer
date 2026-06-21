import json
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import jobs as jobs_module
from app.database import Base
from app.jobs import JobManager
from app.models import Job


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
    monkeypatch.setattr(jobs_module, "session_scope", _session_scope_factory(session_maker))
    return session_maker


def test_pause_resume_and_stop_all_jobs(monkeypatch):
    session_maker = _session_maker(monkeypatch)
    with session_maker() as session:
        session.add_all(
            [
                Job(kind="upload", description="Queued upload", status="queued"),
                Job(kind="merge", description="Running merge", status="running"),
            ]
        )
        session.commit()

    manager = JobManager(worker_count=0)

    assert manager.pause_all() == {"paused": 1, "running": 1}
    with session_maker() as session:
        statuses = sorted(job.status for job in session.query(Job).all())
    assert statuses == ["paused", "running"]
    assert manager._claim_next() is None

    assert manager.resume_all() == {"resumed": 1}
    with session_maker() as session:
        statuses = sorted(job.status for job in session.query(Job).all())
    assert statuses == ["queued", "running"]

    result = manager.stop_all()
    assert result == {"cancel_requested": 2, "cancelled": 1, "running": 1}
    with session_maker() as session:
        by_status = {job.description: job.status for job in session.query(Job).all()}
        cancel_flags = {job.description: job.cancel_requested for job in session.query(Job).all()}
    assert by_status == {"Queued upload": "cancelled", "Running merge": "running"}
    assert cancel_flags == {"Queued upload": True, "Running merge": True}


def test_retry_clones_upload_job_payload(monkeypatch):
    session_maker = _session_maker(monkeypatch)
    payload = {"media_ids": [1, 2], "destination_ids": [3]}
    with session_maker() as session:
        original = Job(
            kind="upload",
            description="Auto-upload June",
            status="error",
            payload=json.dumps(payload),
        )
        session.add(original)
        session.commit()
        original_id = original.id

    manager = JobManager(worker_count=0)
    retry_id = manager.retry(original_id)

    with session_maker() as session:
        retry = session.get(Job, retry_id)
    assert retry.kind == "upload"
    assert retry.status == "queued"
    assert retry.description == "Retry Auto-upload June"
    assert json.loads(retry.payload) == payload
