"""Private, explicit GPS imports and advisory trip matching."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import roadtrips as engine
from ..database import get_session, session_scope
from ..models import Job, utcnow
from ..roadtrip_models import RoadtripMatch

router = APIRouter()
_QUEUE_LOCK = threading.Lock()


class SettingsRequest(BaseModel):
    entity_id: str = ""
    timezone: str = "Europe/London"
    home_lat: float | None = Field(None, ge=-90, le=90)
    home_lon: float | None = Field(None, ge=-180, le=180)
    home_radius_m: float = Field(150, ge=20, le=2000)
    max_accuracy_m: float = Field(100, ge=5, le=1000)
    max_gap_s: int = Field(900, ge=30, le=1800)
    max_speed_kmh: float = Field(180, ge=10, le=400)


class HAImportRequest(BaseModel):
    month: str
    end_month: str | None = None
    entity_id: str = ""


class PreviewRequest(BaseModel):
    month: str
    group_key: str
    outing_id: int
    start_time: str | None = None
    clip_offsets: dict[str, float] = Field(default_factory=dict)
    album_id: int | None = None
    media_ids: list[int] | None = Field(None, max_length=2000)
    clip_ids: list[str] | None = Field(None, max_length=2000)


class MatchRequest(PreviewRequest):
    name: str = Field("", max_length=256)
    status: Literal["confirmed", "rejected"] = "confirmed"


def _error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(400, f"Home Assistant returned HTTP {exc.response.status_code}; check its connection and history access")
    if isinstance(exc, httpx.HTTPError):
        return HTTPException(400, "Home Assistant connection failed")
    return HTTPException(400, str(exc))


@router.get("/state")
def state(session: Session = Depends(get_session)):
    return engine.state(session)


@router.get("/entities")
def entities(session: Session = Depends(get_session)):
    try:
        result = engine.discover_entities(session)
        session.commit()
        return result
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        raise _error(exc) from None


@router.post("/settings")
def settings(request: SettingsRequest, session: Session = Depends(get_session)):
    if request.entity_id and not re.fullmatch(r"(?:person|device_tracker)\.[a-zA-Z0-9_]+", request.entity_id):
        raise HTTPException(400, "Choose a Home Assistant person or device tracker")
    try:
        ZoneInfo(request.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(400, "Choose a valid IANA time zone") from None
    if (request.home_lat is None) != (request.home_lon is None):
        raise HTTPException(400, "Provide both home coordinates")
    prefs = engine.preferences(session)
    for key, value in request.model_dump().items():
        if key in ("home_lat", "home_lon") and value is None:
            continue
        setattr(prefs, key, value)
    prefs.updated_at = utcnow()
    session.commit()
    return engine.preferences_dict(prefs)


def _enqueue(kind, payload):
    with _QUEUE_LOCK, session_scope() as session:
        active = session.query(Job).filter(Job.kind.in_(("roadtrip_import_ha", "roadtrip_import_file", "roadtrip_probe")),
            Job.status.in_(("running", "queued", "paused"))).first()
        if active:
            raise HTTPException(409, "A location import is already active; wait for it to finish")
        row = Job(kind=kind, status="queued", description=f"Index private location history for {payload['month']}", payload=json.dumps(payload))
        session.add(row)
        session.flush()
        return {"job_id": row.id}


@router.post("/import-ha")
def import_ha(request: HAImportRequest):
    try:
        engine.months_between(request.month, request.end_month)
        if request.entity_id and not re.fullmatch(r"(?:person|device_tracker)\.[a-zA-Z0-9_]+", request.entity_id):
            raise ValueError("Choose a person or device tracker")
        # Mount must be live before accepting work; never create an SD fallback.
        engine._cache_root()
        return _enqueue("roadtrip_import_ha", request.model_dump())
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None


@router.post("/import-file")
async def import_file(month: str = Form(...), file: UploadFile = File(...)):
    path = None
    try:
        engine.month_bounds(month)
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".gpx", ".json"):
            raise ValueError("Choose a GPX or Google Timeline JSON export")
        root = engine._cache_root() / "imports"
        root.mkdir(mode=0o700, exist_ok=True)
        if not root.resolve().is_relative_to(engine._cache_root()) or root.is_symlink():
            raise ValueError("Unsafe location import directory")
        descriptor, value = tempfile.mkstemp(suffix=suffix, prefix="location-", dir=root)
        path = Path(value)
        total = 0
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := await file.read(65536):
                total += len(chunk)
                if total > engine.MAX_IMPORT_BYTES:
                    raise ValueError("Use a monthly location export smaller than 16 MB")
                handle.write(chunk)
        # Stable logical source: a reimport replaces that month's export instead
        # of multiplying outings because the upload had a different filename.
        result = _enqueue("roadtrip_import_file", {"path": str(path), "month": month, "source": "file:timeline"})
        path = None
        return result
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None
    finally:
        if path:
            path.unlink(missing_ok=True)
        await file.close()


@router.get("/candidates")
def candidates(month: str, album_id: int | None = None, media_ids: str = "", clip_ids: str = "", session: Session = Depends(get_session)):
    try:
        ids = [int(value) for value in media_ids.split(",") if value] if media_ids else None
        if ids and len(ids) > 2000:
            raise ValueError("Choose at most 2,000 clips")
        selected_clips = clip_ids.split(",") if clip_ids else None
        if selected_clips and len(selected_clips) > 2000:
            raise ValueError("Choose at most 2,000 clips")
        return engine.candidates(session, month, album_id, ids, selected_clips)
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None


@router.post("/preview")
def preview(request: PreviewRequest, session: Session = Depends(get_session)):
    try:
        return engine.preview(session, **request.model_dump())
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None


@router.post("/matches")
def save_match(request: MatchRequest, session: Session = Depends(get_session)):
    try:
        result = engine.save_match(session, request)
        session.commit()
        return result
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None


@router.get("/matches/{match_id}")
def match(match_id: int, session: Session = Depends(get_session)):
    row = session.get(RoadtripMatch, match_id)
    if row is None:
        raise HTTPException(404, "Road trip not found")
    try:
        mapping = json.loads(row.mapping)
        return engine.preview(session, row.month, row.group_key, row.outing_id,
            mapping.get("start_time"), mapping.get("clip_offsets"), mapping.get("group"), row.status == "confirmed") | {"match": engine.match_dict(row)}
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None


@router.post("/probe")
@router.post("/probe-clips")
def probe_clips(request: HAImportRequest):
    try:
        engine.month_bounds(request.month)
        engine._cache_root()
        return _enqueue("roadtrip_probe", {"month": request.month})
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None
