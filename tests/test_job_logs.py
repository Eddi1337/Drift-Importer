import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Job, JobLog
from app.routers.api import job_log_dict


def test_job_logs_are_related_and_serialized():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    job = Job(kind="upload", description="Upload January")
    session.add(job)
    session.flush()
    row = JobLog(
        job_id=job.id,
        level="INFO",
        message="Uploading clip.mp4",
        progress=0.5,
        created_at=dt.datetime(2026, 6, 19, 12, 0, 0),
    )
    session.add(row)
    session.commit()

    loaded = session.get(Job, job.id)

    assert loaded.logs[0].message == "Uploading clip.mp4"
    assert job_log_dict(row) == {
        "id": row.id,
        "job_id": job.id,
        "level": "INFO",
        "message": "Uploading clip.mp4",
        "progress": 0.5,
        "created_at": "2026-06-19T12:00:00",
    }
