"""Uploads stay on the Pi; ride jobs and movie bytes use the processor."""
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.security import HTTPBasic
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import jobs, processing
from app.database import Base
from app.models import Job


@pytest.fixture
def services(monkeypatch):
    settings = SimpleNamespace(processing_role="uploader", processing_backend_url="http://192.168.10.223:8080",
        max_concurrent_uploads=1, max_concurrent_ffmpeg=1)
    monkeypatch.setattr(processing, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    security = HTTPBasic(auto_error=False)
    app = FastAPI()
    @app.middleware("http")
    async def route(request, call_next):
        result = await processing.route_request(request, lambda credentials: True, security)
        return result if result is not None else await call_next(request)
    @app.get("/api/roadtrips/processor-input")
    def snapshot():
        return {"source": "Pi metadata"}
    @app.get("/healthz")
    def health():
        return {"status": "ok"}
    return settings, TestClient(app)


def test_movie_ranges_redirect_to_lxc_without_proxying_bytes(services, monkeypatch):
    _, client = services
    monkeypatch.setattr(processing.httpx, "AsyncClient", lambda **kw: pytest.fail("Film must stream directly from wired processor"))
    result = client.get("/api/roadtrips/auto/12/film", headers={"Range": "bytes=100-200"}, follow_redirects=False)
    assert result.status_code == 307
    assert result.headers["location"] == "http://192.168.10.223:8080/api/roadtrips/auto/12/film"
    assert client.get("/api/roadtrips/processor-input").json() == {"source": "Pi metadata"}
    assert client.get("/healthz").status_code == 200


def test_metadata_requests_use_fixed_backend_and_preserve_http_semantics(services, monkeypatch):
    _, client = services
    seen = []
    async def respond(request):
        seen.append(request)
        return httpx.Response(202, json={"job_id": 99}, headers={"ETag": '"v1"'})
    real = httpx.AsyncClient
    monkeypatch.setattr(processing.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(respond), **kw))
    result = client.post("/api/roadtrips/import-ha?url=http://other.invalid", json={"month": "2026-10"})
    assert result.status_code == 202 and result.json() == {"job_id": 99}
    assert result.headers["etag"] == '"v1"'
    assert seen[0].url.host == "192.168.10.223" and seen[0].url.path == "/api/roadtrips/import-ha"
    assert seen[0].read() == b'{"month": "2026-10"}'


def test_processor_rejects_upload_actions_and_missing_backend_is_clear(services):
    settings, client = services
    settings.processing_role = "processor"
    assert client.post("/api/import-device", json={}).status_code == 409
    settings.processing_role, settings.processing_backend_url = "uploader", ""
    assert client.get("/api/roadtrips/auto").status_code == 503


@pytest.mark.parametrize("role,expected", [("processor", "auto_rides_discover"), ("uploader", "upload"), ("standalone", "upload")])
def test_role_claims_only_its_jobs_and_automatic_work_yields_to_uploads(services, monkeypatch, role, expected):
    settings, _ = services
    settings.processing_role = role
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Job(kind="auto_rides_discover", status="queued"), Job(kind="upload", status="queued")])
        session.commit()
        @contextmanager
        def scoped():
            yield session
            session.commit()
        monkeypatch.setattr(jobs, "session_scope", scoped)
        manager = jobs.JobManager()
        claimed = manager._claim_next()
        assert session.get(Job, claimed).kind == expected
        if role == "processor":
            with pytest.raises(ValueError, match="other Drift"):
                manager.enqueue("upload")
