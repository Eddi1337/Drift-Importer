from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import pages


def test_overview_renders_with_current_template_api(monkeypatch):
    monkeypatch.setattr(pages, "get_settings", lambda: SimpleNamespace(processing_role="uploader"))
    app = FastAPI()
    app.include_router(pages.router)
    with TestClient(app) as client:
        response = client.get("/overview")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "</html>" in response.text
