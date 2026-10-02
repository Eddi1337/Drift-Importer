"""Server-rendered HTML pages."""
from __future__ import annotations

from pathlib import Path
from hashlib import sha256

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Compute once at startup: every deployment gets matching styles and scripts,
# even when a browser still has the previous static files in its cache.
_static = Path(__file__).resolve().parent.parent / "static"
templates.env.globals["asset_version"] = sha256(b"".join(
    (_static / name).read_bytes()
    for name in ("style.css", "app.js", "workflow.js", "favicon.svg")
)).hexdigest()[:12]


def _page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(name, {"request": request, "active": name, **ctx})


@router.get("/", response_class=HTMLResponse)
def gallery(request: Request):
    return _page(request, "dashboard.html")


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return _page(request, "import.html")


@router.get("/library", response_class=HTMLResponse)
def library(request: Request):
    return _page(request, "gallery.html")


@router.get("/destinations", response_class=HTMLResponse)
def destinations(request: Request):
    return _page(request, "destinations.html")


@router.get("/explorer", response_class=HTMLResponse)
def explorer(request: Request):
    return _page(request, "explorer.html")


@router.get("/albums", response_class=HTMLResponse)
def albums(request: Request):
    return _page(request, "trips.html")


@router.get("/trips", response_class=HTMLResponse)
def trips(request: Request):
    return _page(request, "trips.html")


@router.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request):
    return _page(request, "jobs.html")


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return _page(request, "settings.html")


@router.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    return _page(request, "stats.html")
