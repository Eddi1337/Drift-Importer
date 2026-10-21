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
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import roadtrips as engine
from .. import roadtrip_collections as collections_engine
from ..database import get_session, session_scope
from ..models import Job, utcnow
from ..roadtrip_models import RoadtripCollection, RoadtripCollectionClip, RoadtripMatch

router = APIRouter()
_QUEUE_LOCK = threading.Lock()


@router.get("/processor-input")
def processor_input(request: Request, session: Session = Depends(get_session)):
    from ..processing_sync import export_snapshot
    from ..config import get_settings
    if get_settings().processing_role == "processor":
        raise HTTPException(404, "The input snapshot belongs to the camera uploader")
    snapshot = export_snapshot(session)
    etag = '"' + snapshot["digest"] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(snapshot, headers={"ETag": etag, "Cache-Control": "private, no-cache"})


@router.get("/jobs/{job_id}")
def processing_job(job_id: int, session: Session = Depends(get_session)):
    from ..jobs import PROCESSOR_KINDS
    from .api import get_job
    row = session.get(Job, job_id)
    if not row or row.kind not in PROCESSOR_KINDS:
        raise HTTPException(404, "Location processing job not found")
    return get_job(job_id, session)


@router.get("/auto")
def automatic_rides(session: Session = Depends(get_session)):
    from ..auto_ride_jobs import list_payload
    return list_payload(session)


@router.post("/auto/discover")
def discover_automatic_rides():
    from ..auto_ride_jobs import enqueue_discovery
    return enqueue_discovery(backfill=True)


@router.get("/covers/{key}")
def trip_cover(key: str, session: Session = Depends(get_session)):
    from ..auto_ride_models import AutoRide, AutoRideDiscovery
    from ..auto_rides import _json
    from ..trip_history import cover_directory
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HTTPException(404, "Trip picture not found")
    state = session.get(AutoRideDiscovery, 1)
    groups = _json(state.report if state else None, {}).get('recording_groups', [])
    allowed = any(g.get('cover_key') == key for g in groups) or any(
        _json(r.payload, {}).get('cover_key') == key for r in session.query(AutoRide).filter_by(active=True, status='ready'))
    if not allowed:
        raise HTTPException(404, "Trip picture is no longer current")
    try:
        path = cover_directory() / (key + '.jpg')
        if path.is_symlink() or not path.is_file():
            raise HTTPException(404, "Trip picture is being prepared")
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control':'private, max-age=86400'})
    except (OSError, RuntimeError):
        raise HTTPException(503, "Trip picture storage is unavailable") from None


@router.get("/recordings/{group_id}")
def recorded_trip(group_id: str, session: Session = Depends(get_session)):
    from ..auto_ride_models import AutoRideDiscovery
    from ..auto_rides import _json, _hash, eligible_clips
    state = session.get(AutoRideDiscovery, 1)
    group = next((g for g in _json(state.report if state else None, {}).get('recording_groups', []) if g['id'] == group_id), None)
    if not group:
        raise HTTPException(404, "Recording group not found")
    current = {c['archive_id']:c for c in eligible_clips(session)[0]}
    clips = [current[i] for i in group['clip_ids'] if i in current]
    if len(clips) != len(group['clip_ids']) or _hash([c['input_signature'] for c in clips]) != group['input_signature']:
        raise HTTPException(409, "Recording dates changed; refresh trip history")
    return {'group':group, 'clips':clips}


@router.get("/auto/{ride_id}")
def automatic_ride(ride_id: int, session: Session = Depends(get_session)):
    from ..auto_rides import ride_detail
    from ..auto_ride_models import AutoRide
    from ..auto_ride_jobs import render_summary
    try:
        detail = ride_detail(session, ride_id)
        detail["ride"]["render"] = render_summary(session, session.get(AutoRide, ride_id))
        return detail
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None


@router.post("/auto/{ride_id}/render")
def render_automatic_ride(ride_id: int):
    from ..auto_ride_jobs import enqueue_render
    try:
        return enqueue_render(ride_id, force=True)
    except ValueError as exc:
        raise _error(exc) from None


def _ride_asset(session, ride_id, filename):
    from ..auto_ride_jobs import AutoRideRender
    from ..auto_ride_models import AutoRide
    from ..auto_rides import ride_clips
    from ..config import get_settings
    from ..ride_render import job_directory
    row = session.get(AutoRide, ride_id)
    index = session.get(AutoRideRender, ride_id)
    if not row or not row.active or not index or index.input_signature != row.input_signature:
        raise HTTPException(404, "This ride film is not current")
    try:
        ride_clips(session, row)  # Metadata-only revalidation, including GPS revisions.
        state = json.loads(index.state_json)
        allowed = {"done"} if filename == "ride.mp4" else {"rendering", "done"}
        if state.get("status") not in allowed:
            raise HTTPException(404, "The ride film is still being prepared")
        directory = job_directory(get_settings().nas_root, index.signature)
        path = directory / filename
        if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            raise HTTPException(404, "Ride asset not found")
        return path
    except (ValueError, RuntimeError, OSError) as exc:
        raise _error(exc) from None


@router.get("/auto/{ride_id}/film")
def automatic_ride_film(ride_id: int, request: Request, session: Session = Depends(get_session)):
    from ..streaming import stream_file
    return stream_file(request, _ride_asset(session, ride_id, "ride.mp4"))


@router.get("/auto/{ride_id}/snapshot/{index}")
def automatic_ride_snapshot(ride_id: int, index: int, session: Session = Depends(get_session)):
    if not 1 <= index <= 6:
        raise HTTPException(404, "Ride snapshot not found")
    return FileResponse(_ride_asset(session, ride_id, f"snapshot-{index}.jpg"), media_type="image/jpeg")


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


class CollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    clip_ids: list[str] = Field(..., min_length=1, max_length=10000)
    match_ids: list[int] | None = Field(None, max_length=1000)


def _error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(400, f"Home Assistant returned HTTP {exc.response.status_code}; check its connection and history access")
    if isinstance(exc, httpx.HTTPError):
        return HTTPException(400, "Home Assistant connection failed")
    return HTTPException(400, str(exc))


@router.get("/state")
def state(session: Session = Depends(get_session)):
    return engine.state(session)


@router.get("/files")
@router.get("/library")
def collection_files(month: str | None = None, offset: int = 0, limit: int = 1000,
        session: Session = Depends(get_session)):
    try:
        return collections_engine.library(session, month, offset, limit)
    except ValueError as exc:
        raise _error(exc) from None


@router.get("/collections")
def collections(session: Session = Depends(get_session)):
    return collections_engine.list_collections(session)


@router.post("/collections")
def create_collection(request: CollectionRequest, session: Session = Depends(get_session)):
    try:
        result = collections_engine.save_collection(session, **request.model_dump())
        session.commit()
        return result
    except ValueError as exc:
        raise _error(exc) from None


@router.get("/collections/{collection_id}")
def collection(collection_id: int, session: Session = Depends(get_session)):
    row = session.get(RoadtripCollection, collection_id)
    if row is None:
        raise HTTPException(404, "Trip collection not found")
    return collections_engine.collection_detail(session, row)


@router.get("/collections/{collection_id}/map")
def collection_map(collection_id: int, session: Session = Depends(get_session)):
    from ..roadtrip_collection_map import collection_map as build_map
    row = session.get(RoadtripCollection, collection_id)
    if row is None:
        raise HTTPException(404, "Trip collection not found")
    return build_map(session, row)


@router.post("/collections/{collection_id}")
@router.patch("/collections/{collection_id}")
def update_collection(collection_id: int, request: CollectionRequest, session: Session = Depends(get_session)):
    row = session.get(RoadtripCollection, collection_id)
    if row is None:
        raise HTTPException(404, "Trip collection not found")
    try:
        result = collections_engine.save_collection(session, row=row, **request.model_dump())
        session.commit()
        return result
    except ValueError as exc:
        raise _error(exc) from None


@router.delete("/collections/{collection_id}")
def delete_collection(collection_id: int, session: Session = Depends(get_session)):
    row = session.get(RoadtripCollection, collection_id)
    if row is None:
        raise HTTPException(404, "Trip collection not found")
    # Membership and label only: no media, archive, ledger or GPS rows are removed.
    session.query(RoadtripCollectionClip).filter_by(collection_id=collection_id).delete()
    session.delete(row)
    session.commit()
    return {"deleted": True, "id": collection_id}


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
