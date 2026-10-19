from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tasks
from app.database import Base
from app.models import Destination, Job, MediaItem, UploadedClip


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
    monkeypatch.setattr(tasks, "session_scope", _session_scope_factory(session_maker))
    return session_maker


def test_enqueue_upload_jobs_skips_media_done_for_default_destination(monkeypatch):
    session_maker = _session_maker(monkeypatch)
    with session_maker() as session:
        dest = Destination(name="NAS", type="local", base_path="/mnt/NAS", is_default=True)
        done = MediaItem(path="/media/done.mp4", filename="done.mp4", checksum="aaa")
        missing = MediaItem(path="/media/missing.mp4", filename="missing.mp4", checksum="bbb")
        session.add_all([dest, done, missing])
        session.flush()
        session.add(
            UploadedClip(
                destination_id=dest.id,
                source_media_id=done.id,
                checksum=done.checksum,
                filename=done.filename,
                status="done",
                remote_path="/mnt/NAS/2026/06/done.mp4",
            )
        )
        session.commit()
        done_id = done.id
        missing_id = missing.id

    enqueued = []
    monkeypatch.setattr(
        tasks,
        "get_manager_enqueue",
        lambda kind, payload, description="": enqueued.append((kind, payload, description)) or 42,
    )

    assert tasks.enqueue_upload_jobs([done_id, missing_id]) == [42]
    assert enqueued == [
        (
            "upload",
            {"media_ids": [missing_id], "destination_ids": None},
            "Upload missing.mp4",
        )
    ]


def test_enqueue_upload_jobs_returns_empty_when_every_target_is_done(monkeypatch):
    session_maker = _session_maker(monkeypatch)
    with session_maker() as session:
        dest = Destination(name="NAS", type="local", base_path="/mnt/NAS", is_default=True)
        item = MediaItem(path="/media/done.mp4", filename="done.mp4", checksum="aaa")
        session.add_all([dest, item])
        session.flush()
        session.add(
            UploadedClip(
                destination_id=dest.id,
                source_media_id=item.id,
                checksum=item.checksum,
                filename=item.filename,
                status="done",
                remote_path="/mnt/NAS/2026/06/done.mp4",
            )
        )
        session.commit()
        item_id = item.id

    monkeypatch.setattr(tasks, "get_manager_enqueue", lambda *_args, **_kwargs: 42)

    assert tasks.enqueue_upload_jobs([item_id]) == []


def _queue_setup(monkeypatch):
    maker = _session_maker(monkeypatch)
    with maker() as session:
        session.add_all([
            Destination(id=1, name="NAS", type="local", is_default=True),
            Destination(id=2, name="Second NAS", type="local", is_default=True),
            MediaItem(id=1, path="/camera/one.mp4", filename="one.mp4", checksum="one",
                      capture_time=dt.datetime(2026, 10, 1)),
            MediaItem(id=2, path="/camera/two.mp4", filename="two.mp4", checksum="two",
                      capture_time=dt.datetime(2026, 10, 1)),
        ])
        session.commit()

    def enqueue(kind, payload, description=""):
        with maker() as session:
            job = Job(kind=kind, payload=json.dumps(payload), description=description)
            session.add(job)
            session.commit()
            return job.id
    monkeypatch.setattr(tasks, "get_manager_enqueue", enqueue)
    return maker


@pytest.mark.parametrize("status", ["queued", "running", "paused"])
def test_reimport_keeps_existing_active_pairs_and_enqueues_other_targets(monkeypatch, status):
    maker = _queue_setup(monkeypatch)
    with maker() as session:
        session.add(Job(kind="upload", status=status,
                        payload=json.dumps({"media_ids": [1, 2], "destination_ids": [1]})))
        session.commit()
    ids = tasks.enqueue_upload_jobs([1, 2], [1, 2], content_names=True)
    with maker() as session:
        assert [json.loads(session.get(Job, jid).payload) for jid in ids] == [
            {"media_ids": [1], "destination_ids": [2], "content_names": True},
            {"media_ids": [2], "destination_ids": [2], "content_names": True},
        ]
    assert tasks.enqueue_upload_jobs([1, 2], [1, 2]) == []


def test_default_targets_are_covered_by_active_default_jobs(monkeypatch):
    maker = _queue_setup(monkeypatch)
    with maker() as session:
        session.add(Job(kind="upload", payload=json.dumps({"media_ids": [1], "destination_ids": None})))
        session.commit()
    assert tasks.enqueue_upload_jobs([1]) == []
    assert tasks.enqueue_upload_jobs([1], [2]) == []


@pytest.mark.parametrize("status,cancelling", [("error", False), ("cancelled", False), ("running", True)])
def test_error_and_cancelled_jobs_allow_a_new_attempt(monkeypatch, status, cancelling):
    maker = _queue_setup(monkeypatch)
    with maker() as session:
        session.add(Job(kind="upload", status=status, cancel_requested=cancelling,
                        payload=json.dumps({"media_ids": [1], "destination_ids": [1]})))
        session.commit()
    assert len(tasks.enqueue_upload_jobs([1], [1])) == 1


def test_done_target_and_other_active_target_need_no_extra_job(monkeypatch):
    maker = _queue_setup(monkeypatch)
    with maker() as session:
        session.add(UploadedClip(destination_id=1, checksum="one", filename="one.mp4",
                                 status="done", remote_path="/mnt/NAS/one.mp4"))
        session.add(Job(kind="upload", payload=json.dumps({"media_ids": [1], "destination_ids": [2]})))
        session.commit()
    assert tasks.enqueue_upload_jobs([1], [1, 2]) == []


def test_month_grouping_preserves_each_clips_uncovered_targets(monkeypatch):
    maker = _queue_setup(monkeypatch)
    tasks.enqueue_upload_jobs([1], [1])
    ids = tasks.enqueue_upload_jobs_by_month([1, 2], [1, 2])
    with maker() as session:
        payloads = [json.loads(session.get(Job, jid).payload) for jid in ids]
    assert payloads == [
        {"media_ids": [1], "destination_ids": [2]},
        {"media_ids": [2], "destination_ids": [1, 2]},
    ]
    assert tasks.enqueue_upload_jobs_by_month([1, 2], [1, 2]) == []


def test_simultaneous_reimport_requests_only_queue_one_transfer(monkeypatch):
    maker = _queue_setup(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tasks.enqueue_upload_jobs([1], [1]), range(2)))
    assert sorted(len(ids) for ids in results) == [0, 1]
    with maker() as session:
        assert session.query(Job).filter(Job.kind == "upload").count() == 1


def test_malformed_old_payload_does_not_prevent_a_new_backup(monkeypatch):
    maker = _queue_setup(monkeypatch)
    with maker() as session:
        session.add_all([Job(kind="upload", payload="not json"), Job(kind="upload", payload="[]")])
        session.commit()
    assert len(tasks.enqueue_upload_jobs([1], [1])) == 1
