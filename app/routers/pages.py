"""Server-rendered HTML pages."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(name, {"request": request, "active": name, **ctx})


@router.get("/", response_class=HTMLResponse)
def gallery(request: Request):
    return _page(request, "gallery.html")


@router.get("/destinations", response_class=HTMLResponse)
def destinations(request: Request):
    return _page(request, "destinations.html")


@router.get("/albums", response_class=HTMLResponse)
def albums(request: Request):
    return _page(request, "albums.html")


@router.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request):
    return _page(request, "jobs.html")


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    return _page(request, "settings.html")


@router.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    return _page(request, "stats.html")
