"""Gallery reads stay independent of NAS and stale render files stay private."""
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import auto_ride_jobs as jobs
from app import auto_rides, config, ride_render
from app.auto_ride_models import AutoRide, AutoRideDiscovery
from app.database import Base, get_session
from app.models import Job
from app.roadtrips import timestamp
from app.routers.roadtrips import router
from app.processing_sync import ProcessingSyncState


@pytest.fixture
def data(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    nas = tmp_path / "NAS"
    nas.mkdir()
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(nas_root=nas))
    monkeypatch.setattr(ride_render, "require_storage", lambda path: path)
    monkeypatch.setattr(auto_rides, "ride_clips", lambda *args: [])
    with Session(engine) as session:
        ride = AutoRide(signature="a" * 64, input_signature="input-v1", source="ha:phone", outing_id=1,
            name="Sunday ride", start_time=timestamp("2026-10-18T12:00:00Z"), end_time=timestamp("2026-10-18T12:10:00Z"),
            clip_count=2, duration_s=600, distance_m=1000, confidence=.95, active=True, status="ready")
        session.add(ride)
        session.flush()
        app = FastAPI()
        app.include_router(router, prefix="/api/roadtrips")
        app.dependency_overrides[get_session] = lambda: session
        yield session, ride, nas, TestClient(app)


def cache(session, ride, state):
    row = jobs.AutoRideRender(ride_id=ride.id, signature="b" * 64, input_signature=ride.input_signature,
        state_json=json.dumps(state))
    session.add(row)
    session.flush()
    return row


def test_gallery_never_waits_for_nas_or_exposes_spool_paths(data, monkeypatch):
    session, ride, _, client = data
    cache(session, ride, {"status": "done", "progress": 1, "output": "../../secret", "snapshots": [
        {"filename": "snapshot-1.jpg", "time": "2026-10-18T12:00:00Z"}, {"filename": "../../secret"}]})
    monkeypatch.setattr(Path, "stat", lambda *a, **k: pytest.fail("Gallery must use cached metadata only"))
    payload = client.get("/api/roadtrips/auto").json()
    render = payload["rides"][0]["render"]
    assert render["video_url"] == f"/api/roadtrips/auto/{ride.id}/film"
    assert render["snapshots"] == [{"url": f"/api/roadtrips/auto/{ride.id}/snapshot/1", "time": "2026-10-18T12:00:00Z"}]
    assert "secret" not in json.dumps(payload)


def test_gallery_reports_pending_or_failed_prepare_without_an_index(data):
    session, ride, _, _ = data
    job = Job(kind="auto_ride_prepare", payload=json.dumps({"ride_id": ride.id}), status="queued")
    session.add(job)
    session.flush()
    assert jobs.render_summary(session, ride)["status"] == "queued"
    job.status, job.error = "error", "NAS unavailable"
    session.flush()
    assert jobs.render_summary(session, ride)["error"] == "NAS unavailable"


def test_changed_ride_cannot_offer_or_stream_the_old_film(data):
    session, ride, _, client = data
    cache(session, ride, {"status": "done", "progress": 1})
    ride.input_signature = "changed-video-or-gps"
    session.flush()
    assert jobs.render_summary(session, ride)["video_url"] is None
    assert client.get(f"/api/roadtrips/auto/{ride.id}/film").status_code == 404


def test_film_ranges_and_early_snapshots_use_fixed_safe_assets(data):
    session, ride, nas, client = data
    row = cache(session, ride, {"status": "rendering", "snapshots": [{"filename": "snapshot-1.jpg"}]})
    directory = ride_render.job_directory(nas, row.signature, create=True)
    (directory / "snapshot-1.jpg").write_bytes(b"jpeg-preview")
    (directory / "ride.mp4").write_bytes(b"0123456789")
    assert client.get(f"/api/roadtrips/auto/{ride.id}/snapshot/1").content == b"jpeg-preview"
    assert client.get(f"/api/roadtrips/auto/{ride.id}/film").status_code == 404
    row.state_json = json.dumps({"status": "done"})
    session.flush()
    response = client.get(f"/api/roadtrips/auto/{ride.id}/film", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert client.get(f"/api/roadtrips/auto/{ride.id}/snapshot/7").status_code == 404
    (directory / "snapshot-2.jpg").symlink_to(directory / "snapshot-1.jpg")
    assert client.get(f"/api/roadtrips/auto/{ride.id}/snapshot/2").status_code == 404


def test_deferred_discovery_retries_after_nas_recovers_without_new_input(data, monkeypatch):
    session, _, _, _ = data
    session.add(AutoRideDiscovery(id=1, input_signature="same", report=json.dumps({"deferred_months": ["2026-10"]})))
    session.flush()
    @contextmanager
    def scoped():
        yield session
    monkeypatch.setattr(jobs, "session_scope", scoped)
    monkeypatch.setattr(auto_rides, "input_fingerprint", lambda s: "same")
    calls = []
    monkeypatch.setattr(jobs, "enqueue_discovery", lambda: calls.append("retry"))
    jobs.AutoRideMonitor().tick()
    assert calls == ["retry"]


def test_processor_waits_for_initial_metadata_before_discovery(data, monkeypatch):
    session, _, nas, _ = data
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(processing_role="processor", nas_root=nas))
    @contextmanager
    def scoped():
        yield session
    monkeypatch.setattr(jobs, "session_scope", scoped)
    calls = []
    monkeypatch.setattr(jobs, "enqueue_discovery", lambda: calls.append("discover"))
    monitor = jobs.AutoRideMonitor()
    monitor.tick()
    assert calls == []
    session.add(ProcessingSyncState(id=1, seeded=True))
    session.flush()
    monitor.tick()
    assert calls == ["discover"]
