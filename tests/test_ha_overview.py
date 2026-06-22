import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.ha_publish import compute_jobs_overview  # alias of jobs.jobs_overview
from app.models import Job


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_overview_progress_is_count_based_over_the_current_run():
    session = _session()
    base = dt.datetime(2026, 6, 22, 12, 0, 0)
    session.add_all([
        # Historical completed job from an earlier run — must NOT count.
        Job(kind="upload", status="done", progress=1.0, created_at=base - dt.timedelta(hours=2)),
        # Current run: created from the oldest active job onwards.
        Job(kind="upload", status="running", progress=0.5, created_at=base),
        Job(kind="upload", status="running", progress=0.1, created_at=base + dt.timedelta(seconds=1)),
        Job(kind="upload", status="queued", progress=0.0, created_at=base + dt.timedelta(seconds=2)),
        Job(kind="upload", status="done", progress=1.0, created_at=base + dt.timedelta(seconds=3)),
        Job(kind="upload", status="error", progress=0.3, created_at=base + dt.timedelta(seconds=4)),
    ])
    session.commit()

    o = compute_jobs_overview(session)

    assert o["active"] == 3 and o["running"] == 2 and o["queued"] == 1
    assert o["done"] == 2 and o["error"] == 1
    assert o["status"] == "running"
    assert o["completed_in_run"] == 1  # only the in-run done job, not the historical one
    assert o["total_in_run"] == 4      # 3 active + 1 completed-in-run
    # (1 done + (0.5 + 0.1) running progress) / 4 = 0.4
    assert o["percent"] == 40


def test_overview_idle_when_nothing_active():
    session = _session()
    session.add(Job(kind="upload", status="done", progress=1.0))
    session.commit()

    o = compute_jobs_overview(session)

    assert o["active"] == 0
    assert o["status"] == "idle"
    assert o["percent"] == 100
