import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Job
from app.routers.api import list_jobs


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_list_jobs_surfaces_running_job_despite_large_queue():
    session = _session()
    base = dt.datetime(2026, 6, 22, 12, 0, 0)
    # The worker runs the OLDEST queued job, so the running one is created early
    # and buried under 150 newer queued jobs. Newest-first + limit would hide it.
    session.add(Job(kind="upload", status="running", progress=0.4, created_at=base))
    for i in range(150):
        session.add(Job(kind="upload", status="queued", progress=0.0,
                        created_at=base + dt.timedelta(seconds=i + 1)))
    session.add(Job(kind="upload", status="done", progress=1.0,
                    created_at=base + dt.timedelta(hours=1)))
    session.commit()

    rows = list_jobs(limit=100, include_dismissed=False, session=session)
    statuses = [r["status"] for r in rows]

    assert rows[0]["status"] == "running"      # active jobs first
    assert "done" in statuses                  # recent finished jobs still back-filled
    assert len(rows) <= 100
