import datetime as dt
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import storage, tasks, workflow
from app.database import Base
from app.jobs import JobCancelled
from app.media import checksum
from app.models import Album, Destination, Job, MediaItem, TripMovie, UploadedClip


class Context:
    ffmpeg_semaphore = threading.Semaphore(1)
    upload_semaphore = threading.Semaphore(1)

    def set_progress(self, *args):
        pass

    def log(self, *args, **kwargs):
        pass

    def is_cancelled(self):
        return False


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        with maker() as session:
            yield session
            session.commit()

    monkeypatch.setattr(workflow, "session_scope", scope)
    monkeypatch.setattr(tasks, "session_scope", scope)
    camera = tmp_path / "camera"
    nas = tmp_path / "NAS"
    camera.mkdir()
    nas.mkdir()
    monkeypatch.setattr(workflow, "get_device_monitor", lambda: SimpleNamespace(get_devices=lambda: [
        {"path": str(camera), "dcim_path": str(camera / "DCIM")},
        {"path": str(camera / "DCIM"), "dcim_path": str(camera / "DCIM" / "100MEDIA")},
        {"path": str(camera), "dcim_path": str(camera / "EVENT")},
    ]))
    with scope() as session:
        session.add(Destination(id=1, name="NAS", type="local", base_path=str(nas), enabled=True))
        session.add(Job(id=1, kind="verify"))
    return maker, camera, nas


def add_clip(maker, camera, nas, name="one.mp4", day="2026-09-29", hour=9, content=b"video", archived=True):
    source = camera / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    target = nas / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if archived:
        target.write_bytes(content)
    with maker() as session:
        item = MediaItem(path=str(source), filename=source.name, kind="video", checksum=checksum(source),
                         size_bytes=len(content), capture_time=dt.datetime.fromisoformat(day).replace(hour=hour),
                         duration_s=300, codec="h264", source="device")
        session.add(item)
        session.flush()
        if archived:
            session.add(UploadedClip(destination_id=1, source_media_id=item.id, checksum=item.checksum,
                                     filename=source.name, size_bytes=len(content), status="done", remote_path=str(target)))
        session.commit()
        return item.id, source, target


def test_verification_covers_dcim_and_event_and_collapses_nested_devices(setup):
    maker, camera, nas = setup
    add_clip(maker, camera, nas, "DCIM/one.mp4", content=b"one")
    add_clip(maker, camera, nas, "EVENT/two.MP4", content=b"two")
    assert workflow.camera_roots() == [camera]
    workflow.handle_verify(1, {"camera_root": str(camera), "destination_id": 1}, Context())
    with maker() as session:
        report = json.loads(session.get(Job, 1).result)
        assert report["ok"] is True
        assert report["total"] == report["verified"] == 2
        assert {row["filename"] for row in report["files"]} == {"DCIM/one.mp4", "EVENT/two.MP4"}


def test_full_verify_detects_middle_corruption_that_sampled_hash_misses(setup):
    maker, camera, nas = setup
    _, source, target = add_clip(maker, camera, nas, content=b"a" * (10 * 1024 * 1024))
    with target.open("r+b") as handle:
        handle.seek(5 * 1024 * 1024)
        handle.write(b"b")
    assert checksum(source) == checksum(target)
    workflow.handle_verify(1, {"camera_root": str(camera), "destination_id": 1}, Context())
    with maker() as session:
        report = json.loads(session.get(Job, 1).result)
        assert report["ok"] is False
        assert report["mismatch"] == 1
        ledger = session.query(UploadedClip).one()
        assert ledger.status == "error"
        assert ledger.full_verification_failed is True


def test_missing_nas_copy_is_not_verified_from_done_ledger(setup):
    maker, camera, nas = setup
    _, _, target = add_clip(maker, camera, nas)
    target.unlink()
    workflow.handle_verify(1, {"camera_root": str(camera), "destination_id": 1}, Context())
    with maker() as session:
        report = json.loads(session.get(Job, 1).result)
        assert report["missing"] == 1
        assert report["ok"] is False


def test_import_repairs_a_copy_that_failed_full_verification(setup, monkeypatch):
    maker, camera, nas = setup
    mid, source, target = add_clip(maker, camera, nas, content=b"a" * (10 * 1024 * 1024))
    with target.open("r+b") as handle:
        handle.seek(5 * 1024 * 1024)
        handle.write(b"b")
    workflow.handle_verify(1, {"camera_root": str(camera), "destination_id": 1}, Context())
    tasks.handle_upload(2, {"media_ids": [mid], "destination_ids": [1]}, Context())
    # Existing destination layout is year/month, so the repaired ledger points
    # at a complete new copy rather than accepting the corrupted sampled hash.
    with maker() as session:
        ledger = session.query(UploadedClip).one()
        assert Path(ledger.remote_path).read_bytes() == source.read_bytes()
        assert ledger.status == "done"
        assert ledger.full_verification_failed is False


@pytest.mark.parametrize("mode", ["empty", "disconnect", "unreadable"])
def test_incomplete_camera_scan_cannot_return_all_clear(setup, monkeypatch, mode):
    maker, camera, nas = setup
    if mode != "empty":
        add_clip(maker, camera, nas)
    if mode == "disconnect":
        def disconnected(path, ctx):
            path.unlink()
            return "hash"
        monkeypatch.setattr(workflow, "_full_hash", disconnected)
    if mode == "unreadable":
        monkeypatch.setattr(workflow.os, "walk", lambda *a, **kw: kw["onerror"](PermissionError("unreadable")))
    with pytest.raises((RuntimeError, OSError)):
        workflow.handle_verify(1, {"camera_root": str(camera), "destination_id": 1}, Context())
    with maker() as session:
        assert session.get(Job, 1).result is None


def test_mount_guard_rejects_an_existing_sd_fallback_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "get_settings", lambda: SimpleNamespace(require_nas_mount=True))
    monkeypatch.setattr(storage, "filesystem_type", lambda p: "ext4")
    with pytest.raises(RuntimeError, match="SD card"):
        storage.require_storage(tmp_path)
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(storage, "filesystem_type", lambda p: "nfs4")
    assert storage.require_storage(tmp_path) == tmp_path


def test_suggestions_include_all_days_and_exclude_derived_videos(setup):
    maker, camera, nas = setup
    add_clip(maker, camera, nas, "a.mp4", content=b"a")
    add_clip(maker, camera, nas, "b.mp4", hour=10, content=b"b")
    add_clip(maker, camera, nas, "c.mp4", day="2026-09-30", content=b"c", archived=False)
    with maker() as session:
        session.add(MediaItem(path="/derived.mp4", filename="derived.mp4", kind="video", derived=True,
                              capture_time=dt.datetime(2026, 9, 29)))
        session.commit()
        days = workflow.trip_suggestions(session, 1)
        assert [d["day"] for d in days] == ["2026-09-30", "2026-09-29"]
        assert days[0]["ready"] is False
        assert days[1]["clip_count"] == 2
        assert days[1]["ready"] is True


def test_multiday_trip_uses_archived_copies_after_camera_removed(setup, monkeypatch):
    maker, camera, nas = setup
    a, source_a, archived_a = add_clip(maker, camera, nas, "a.mp4", day="2026-09-29", content=b"a")
    b, source_b, archived_b = add_clip(maker, camera, nas, "b.mp4", day="2026-09-30", content=b"b")
    source_a.unlink()
    source_b.unlink()
    with maker() as session:
        assert all(day["ready"] for day in workflow.trip_suggestions(session, 1))
    class Manager:
        def enqueue(self, kind, description, payload):
            with maker() as session:
                job = Job(kind=kind, description=description, payload=json.dumps(payload))
                session.add(job)
                session.commit()
                return job.id
    monkeypatch.setattr(workflow, "get_manager", Manager)
    with maker() as session:
        result = workflow.queue_trip(session, ["2026-09-30", "2026-09-29"], 1, "Autumn ride")
        duplicate = workflow.queue_trip(session, ["2026-09-29", "2026-09-30"], 1)
        assert duplicate["job_id"] == result["job_id"]
        payload = json.loads(session.get(Job, result["job_id"]).payload)
        assert payload["media_ids"] == [a, b]
    def merge(paths, output, progress=None, creation_time=None):
        assert creation_time == dt.datetime(2026, 9, 29, 9)
        assert paths == [archived_a, archived_b]
        assert output.is_relative_to(nas / ".drift/tmp")
        output.write_bytes(b"ab")
    monkeypatch.setattr(workflow, "merge_clips", merge)
    from app import media
    monkeypatch.setattr(media, "probe", lambda path: {"codec": "h264", "duration_s": 600})
    def index(session, path, source, derived):
        item = MediaItem(path=str(path), filename=path.name, size_bytes=path.stat().st_size,
                         kind="video", source=source, derived=derived, checksum="movie")
        session.add(item)
        session.flush()
        return item
    monkeypatch.setattr(tasks, "import_one", index)
    monkeypatch.setattr(tasks, "get_manager_enqueue", lambda *a, **kw: 99)
    workflow.handle_trip(result["job_id"], payload, Context())
    with maker() as session:
        movie = session.query(TripMovie).one()
        item = session.get(MediaItem, movie.media_id)
        assert Path(item.path).is_relative_to(nas / "Trips/2026")
        assert Path(item.path).read_bytes() == b"ab"
        assert item.capture_time == dt.datetime(2026, 9, 29, 9)
        assert list((nas / ".drift/tmp").iterdir()) == []
        assert archived_a.read_bytes() == b"a"
        assert archived_b.read_bytes() == b"b"


def test_new_workflow_pages_render():
    from app.main import app
    client = TestClient(app)
    for path, title in [("/", "Your footage, in order."), ("/import", "Import &amp; verify"), ("/trips", "Suggested recording days")]:
        response = client.get(path)
        assert response.status_code == 200
        assert title in response.text


def test_archive_path_cannot_escape_nas(tmp_path):
    assert storage.archive_path(tmp_path, "../outside.mp4") is None
    assert storage.archive_path(tmp_path, "/etc/passwd") is None


def test_two_camera_folders_with_same_filename_do_not_overwrite_each_other(setup):
    maker, camera, nas = setup
    first, source_a, _ = add_clip(maker, camera, nas, "DCIM/same.mp4", content=b"aaa", archived=False)
    second, source_b, _ = add_clip(maker, camera, nas, "EVENT/same.mp4", content=b"bbb", archived=False)
    tasks.handle_upload(2, {"media_ids": [first, second], "destination_ids": [1], "content_names": True}, Context())
    with maker() as session:
        copies = session.query(UploadedClip).all()
        assert len(copies) == 2
        assert len({copy.remote_path for copy in copies}) == 2
        assert {Path(copy.remote_path).read_bytes() for copy in copies} == {b"aaa", b"bbb"}


def test_reused_camera_path_keeps_old_recording_identity(setup, monkeypatch):
    maker, camera, nas = setup
    old_id, source, _ = add_clip(maker, camera, nas, content=b"old")
    source.write_bytes(b"new")
    monkeypatch.setattr(tasks, "probe", lambda path: {"codec": "h264", "width": 1920, "height": 1080,
                                                     "capture_time": dt.datetime(2026, 9, 30), "duration_s": 300})
    with maker() as session:
        new = tasks.import_one(session, source, source="device", verify_checksum=True)
        old = session.get(MediaItem, old_id)
        assert new.id != old_id
        assert old.capture_time.date().isoformat() == "2026-09-29"
        assert new.capture_time.date().isoformat() == "2026-09-30"
        assert old.checksum != new.checksum
        assert old.path != new.path


def test_stream_copy_rejects_mismatched_audio_tracks(tmp_path, monkeypatch):
    from app import merge
    paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for path in paths:
        path.write_bytes(b"video")
    monkeypatch.setattr(merge, "probe", lambda path: {"codec": "h264", "width": 1920, "height": 1080,
                                                     "stream_signature": [("h264",), ("aac", "44100" if path == paths[0] else "48000")]})
    with pytest.raises(merge.MergeError, match="mismatched"):
        merge.check_compatible(paths)
