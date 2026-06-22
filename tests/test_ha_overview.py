from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.ha_publish import compute_jobs_overview
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


def test_overview_averages_inflight_progress_and_ignores_terminal_jobs():
    session = _session()
    session.add_all([
        Job(kind="upload", status="running", progress=0.5),
        Job(kind="upload", status="running", progress=0.1),
        Job(kind="upload", status="queued", progress=0.0),
        # Terminal jobs must not drag the overall percent up or down.
        Job(kind="upload", status="done", progress=1.0),
        Job(kind="upload", status="error", progress=0.3),
    ])
    session.commit()

    overview = compute_jobs_overview(session)

    assert overview["active"] == 3
    assert overview["running"] == 2
    assert overview["status"] == "running"
    # mean(0.5, 0.1, 0.0) = 0.2 -> 20%
    assert overview["percent"] == 20


def test_overview_idle_when_nothing_active():
    session = _session()
    session.add(Job(kind="upload", status="done", progress=1.0))
    session.commit()

    overview = compute_jobs_overview(session)

    assert overview["active"] == 0
    assert overview["status"] == "idle"
    assert overview["percent"] == 100
