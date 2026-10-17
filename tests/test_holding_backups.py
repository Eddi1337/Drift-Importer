"""Date uncertainty must never prevent an original camera backup."""

import datetime as dt
import hashlib
import json
import struct
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import date_review as dates, media, tasks, timestamps, workflow
from app.database import Base
from app.media import checksum
from app.models import CorrectedArchive, DateCorrection, Destination, Job, MediaItem, RecordingDate, UploadedClip, UploadState


NOW = dt.datetime(2026, 10, 5, 10, tzinfo=dt.timezone.utc)
CAMERA_TIME = dt.datetime(2026, 9, 20, 14, 45, 55)
CORRECTED_TIME = dt.datetime(2026, 10, 4, 17)


class Context:
    def __init__(self):
        self.ffmpeg_semaphore = threading.Semaphore(1)
        self.upload_semaphore = threading.Semaphore(1)

    def is_cancelled(self):
        return False

    def set_progress(self, *args):
        pass

    def log(self, *args, **kwargs):
        pass


def movie(path, when=CAMERA_TIME, payload=b"original video packets"):
    """Tiny MP4 header fixture; media decoding is mocked in these tests."""
    def atom(name, body):
        return struct.pack(">I4s", len(body) + 8, name) + body

    seconds = int((when - dt.datetime(1904, 1, 1)).total_seconds())
    header = b"\0\0\0\0" + struct.pack(">IIII", seconds, seconds, 1000, 300000)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(atom(b"ftyp", b"isom0000") + atom(b"mdat", payload) + atom(b"moov", atom(b"mvhd", header)))
    return path


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

    for module in (dates, tasks, workflow):
        monkeypatch.setattr(module, "session_scope", scope)
    camera, nas = tmp_path / "drift-camera", tmp_path / "NAS"
    camera.mkdir()
    nas.mkdir()
    monkeypatch.setattr(workflow, "get_device_monitor", lambda: SimpleNamespace(get_devices=lambda: [
        {"path": str(camera), "dcim_path": str(camera / "DCIM")},
    ]))
    monkeypatch.setattr(dates, "utcnow", lambda: NOW)
    monkeypatch.setattr(dates, "queue_metadata", lambda *args: 99)
    monkeypatch.setattr(tasks, "_media_needing_thumbnails", lambda ids: [])

    def probe(path):
        when, duration = dates.mp4_clock(Path(path))
        return {"capture_time": when, "duration_s": duration, "codec": "h264", "width": 1920,
                "height": 1080, "stream_signature": [("video", "h264")]}

    def metadata_copy(source, output, when):
        data = Path(source).read_bytes()
        # Rebuild only the header while preserving the synthetic video payload.
        start = data.index(b"mdat") + 4
        end = data.index(b"moov") - 4
        movie(Path(output), when, data[start:end])

    monkeypatch.setattr(tasks, "probe", probe)
    monkeypatch.setattr(media, "probe", probe)
    monkeypatch.setattr(timestamps, "metadata_copy", metadata_copy)
    with scope() as session:
        session.add(Destination(id=1, name="NAS", type="local", base_path=str(nas), is_default=True))
        session.add(Job(id=1, kind="date_scan"))
    return maker, camera, nas


def index(setup, relative="DCIM/107MEDIA/DVR00908.MP4", *, scan=True, payload=b"original video packets"):
    maker, camera, _ = setup
    path = movie(camera / relative, payload=payload)
    if scan:
        dates.scan_job(1, {"camera_root": str(camera), "connected_at": NOW.isoformat()}, Context())
    with maker() as session:
        item = tasks.import_one(session, path, source="device", verify_checksum=True)
        session.commit()
        return item.id, path


@pytest.mark.parametrize("scanned", [True, False])
def test_uncertain_and_unchecked_camera_dates_are_backed_up_in_holding(setup, scanned):
    maker, camera, nas = setup
    media_id, source = index(setup, scan=scanned)
    before, fingerprint = source.read_bytes(), dates.fingerprint(source)

    tasks.handle_upload(2, {"media_ids": [media_id], "destination_ids": [1]}, Context())

    expected = nas / "Holding" / camera.name / "DCIM/107MEDIA" / f"{source.stem}_{checksum(source)[:16]}.MP4"
    assert expected.read_bytes() == before
    assert source.read_bytes() == before and dates.fingerprint(source) == fingerprint
    assert not list(nas.rglob("*.part"))
    with maker() as session:
        state = session.query(UploadState).one()
        ledger = session.query(UploadedClip).one()
        assert state.status == ledger.status == "done"
        assert state.remote_path == ledger.remote_path == str(expected)
        assert state.bytes_uploaded == ledger.bytes_uploaded == len(before)
        assert state.error is None and ledger.last_error is None
        if scanned:
            assert session.query(RecordingDate).one().status == "review"
        # A successful holding backup is complete; polling/reconnect must not
        # transfer the same original repeatedly while awaiting date review.
        assert tasks._media_ids_needing_upload([media_id], [1]) == []


def test_holding_keeps_camera_relative_folders_for_reused_filenames(setup):
    maker, camera, nas = setup
    a, source_a = index(setup, "DCIM/107MEDIA/DVR00001.MP4", payload=b"first recording")
    b, source_b = index(setup, "DCIM/108MEDIA/DVR00001.MP4", payload=b"later recording")
    tasks.handle_upload(2, {"media_ids": [a, b], "destination_ids": [1]}, Context())
    with maker() as session:
        ledgers = session.query(UploadedClip).order_by(UploadedClip.source_media_id).all()
        assert len(ledgers) == 2 and all(row.status == "done" for row in ledgers)
        assert [Path(row.remote_path).relative_to(nas / "Holding" / camera.name).parent.as_posix()
                for row in ledgers] == ["DCIM/107MEDIA", "DCIM/108MEDIA"]
        assert Path(ledgers[0].remote_path).read_bytes() == source_a.read_bytes()
        assert Path(ledgers[1].remote_path).read_bytes() == source_b.read_bytes()


@pytest.mark.parametrize("content_names", [False, True])
def test_approved_recording_uses_confirmed_date_folder(setup, content_names):
    maker, _, nas = setup
    media_id, source = index(setup)
    with maker() as session:
        recording = session.query(RecordingDate).one()
        recording.status, recording.corrected_time = "confirmed", CORRECTED_TIME
        session.commit()
    tasks.handle_upload(2, {"media_ids": [media_id], "destination_ids": [1], "content_names": content_names}, Context())
    filename = f"{source.stem}_{checksum(source)[:16]}.MP4"
    expected = nas / "2026/10" / filename
    assert expected.read_bytes() == source.read_bytes()
    assert not (nas / "Holding").exists()
    with maker() as session:
        assert session.query(UploadState).one().remote_path == str(expected)


@pytest.mark.parametrize("group_by_month", [False, True])
def test_every_camera_clip_is_imported_and_queued_before_date_confirmation(setup, monkeypatch, group_by_month):
    maker, camera, _ = setup
    dcim = movie(camera / "DCIM/107MEDIA/DVR00908.MP4")
    event = movie(camera / "EVENT/E_DVR00909.MP4", payload=b"event packets")
    dates.scan_job(1, {"camera_root": str(camera), "connected_at": NOW.isoformat()}, Context())
    queued = []
    monkeypatch.setattr(tasks, "get_manager_enqueue", lambda kind, payload, description="": queued.append((kind, payload)) or len(queued) + 10)

    job_id, count = tasks.enqueue_device_import(camera, auto_upload=True, destination_ids=[1],
                                               group_uploads_by_month=group_by_month, fingerprint_on_import=True)
    assert job_id is not None and count == 2
    kind, payload = queued.pop(0)
    assert kind == "import" and set(payload["paths"]) == {str(dcim), str(event)}
    tasks.handle_import(job_id, payload, Context())

    with maker() as session:
        ids = {row.id for row in session.query(MediaItem)}
        assert len(ids) == 2
        assert all(row.status == "review" for row in session.query(RecordingDate))
    assert {mid for kind, data in queued if kind == "upload" for mid in data["media_ids"]} == ids
    assert all(data["content_names"] for kind, data in queued if kind == "upload")


@pytest.mark.parametrize("camera_connected", [True, False])
def test_confirming_held_backup_moves_original_and_publishes_corrected_metadata(setup, camera_connected):
    maker, camera, nas = setup
    media_id, source = index(setup)
    before, fingerprint = source.read_bytes(), dates.fingerprint(source)
    tasks.handle_upload(2, {"media_ids": [media_id], "destination_ids": [1]}, Context())
    with maker() as session:
        recording_id = session.query(RecordingDate).one().id
        held_path = Path(session.query(UploadedClip).one().remote_path)
    if not camera_connected:
        source.unlink()

    with maker() as session:
        preview = dates.make_preview(session, str(camera), [recording_id], recording_id,
                                     "2026-10-04T18:00:00", False, False, 1, True, False)
        session.commit()
        job_id = dates.confirm_plan(session, preview["plan_id"])["job_id"]
    dates.correction_job(job_id, {"plan_id": preview["plan_id"], "camera_root": str(camera)}, Context())

    archived = nas / "2026/10" / held_path.name
    assert not held_path.exists() and archived.read_bytes() == before
    if camera_connected:
        assert source.read_bytes() == before and dates.fingerprint(source) == fingerprint
    audit = json.loads(archived.with_name(archived.name + ".dates.json").read_text())
    assert audit["original_bytes_preserved"] is True
    with maker() as session:
        assert session.get(DateCorrection, preview["plan_id"]).status == "done"
        recording = session.get(RecordingDate, recording_id)
        assert recording.status == "confirmed" and recording.corrected_time == CORRECTED_TIME
        assert session.get(MediaItem, media_id).capture_time == CORRECTED_TIME
        assert session.query(UploadedClip).one().remote_path == str(archived)
        assert session.query(UploadState).one().remote_path == str(archived)
        derivative = session.query(CorrectedArchive).one()
        corrected = Path(derivative.corrected_path)
        assert corrected.is_relative_to(nas / "Corrected/2026/10")
        assert dates.mp4_clock(corrected) == (CORRECTED_TIME, 300)
        assert hashlib.sha256(corrected.read_bytes()).digest() != hashlib.sha256(before).digest()
        assert derivative.original_path == str(archived)
    assert not list(nas.rglob("*.part"))


def test_offline_review_rejects_a_reused_or_corrupt_same_size_backup(setup):
    maker, camera, _ = setup
    media_id, source = index(setup)
    tasks.handle_upload(2, {"media_ids": [media_id], "destination_ids": [1]}, Context())
    with maker() as session:
        recording_id = session.query(RecordingDate).one().id
        backup = Path(session.query(UploadedClip).one().remote_path)
    before = backup.read_bytes()
    # Identical filename and size are insufficient to identify an original.
    movie(backup, CAMERA_TIME - dt.timedelta(days=1))
    assert backup.stat().st_size == len(before)
    changed = backup.read_bytes()
    source.unlink()
    with maker() as session:
        with pytest.raises((ValueError, RuntimeError), match="(?i)(changed|unavailable|match|backup|source|archive)"):
            dates.make_preview(session, str(camera), [recording_id], recording_id,
                               "2026-10-04T18:00:00", False, False, 1, True, False)
    assert backup.read_bytes() == changed


def test_saved_holding_review_and_range_playback_work_without_camera(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import get_session
    from app.routers.api import router

    maker, camera, _ = setup
    media_id, source = index(setup)
    expected = source.read_bytes()
    tasks.handle_upload(2, {"media_ids": [media_id], "destination_ids": [1]}, Context())
    source.unlink()
    monkeypatch.setattr(workflow, 'get_device_monitor', lambda: SimpleNamespace(get_devices=lambda: []))
    def sessions():
        with maker() as session:
            yield session
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        cameras = client.get('/api/recording-dates/cameras').json()
        assert len(cameras) == 1 and cameras[0]['connected'] is False
        assert cameras[0]['pending_review'] == 1
        response = client.get('/api/recording-dates', params={'camera_root': str(camera), 'destination_id': 1})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['camera_available'] is False
        file = data['files'][0]
        assert file['backed_up'] and '/Holding/' in file['holding_path']
        assert file['suggested_time'] and file['suggestion_confidence']
        response = client.get('/api/recording-dates/file', params={'recording_id': file['id'], 'destination_id': 1}, headers={'Range': 'bytes=0-15'})
        assert response.status_code == 206 and response.content == expected[:16]
        response = client.post('/api/recording-dates/preview', json={
            'camera_root': str(camera), 'recording_ids': [file['id']], 'anchor_id': file['id'],
            'anchor_time': '2026-10-04T18:00:00', 'anchor_end': False,
            'destination_id': 1, 'metadata_copy': True,
        })
        assert response.status_code == 200, response.text
        assert '/Holding/' in response.json()['entries'][0]['moves'][0]['old']
