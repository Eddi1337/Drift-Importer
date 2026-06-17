"""JSON / form API endpoints."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..crypto import encrypt
from ..database import get_session
from ..destinations import get_backend
from ..devices import detect_devices, scan_media_files
from ..media import classify
from ..jobs import get_manager
from ..models import (
    Album,
    AlbumItem,
    AppSettings,
    Destination,
    Job,
    MediaItem,
    Tag,
    UploadedClip,
    UploadState,
)
from ..settings_store import app_settings_dict, encode_destination_ids, get_app_settings, touch_settings
from ..streaming import stream_file

router = APIRouter()


# --- serialization helpers --------------------------------------------------

def _round_or_none(value: Optional[float], digits: int = 3) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _build_upload_totals(rows: list[UploadedClip]) -> dict:
    done_rows = [row for row in rows if row.status == "done"]
    total_bytes = sum(int(row.size_bytes or 0) for row in done_rows)
    durations = [float(row.upload_duration_s) for row in done_rows if row.upload_duration_s]
    throughputs = [float(row.upload_throughput_bps) for row in done_rows if row.upload_throughput_bps]
    return {
        "uploaded_clip_count": len(done_rows),
        "uploaded_bytes": total_bytes,
        "average_upload_duration_s": _round_or_none(
            (sum(durations) / len(durations)) if durations else None
        ),
        "average_throughput_bps": _round_or_none(
            (sum(throughputs) / len(throughputs)) if throughputs else None
        ),
    }


def _destination_storage_payload(
    destination: Destination,
    rows: list[UploadedClip],
) -> dict:
    uploaded_bytes = sum(int(row.size_bytes or 0) for row in rows if row.status == "done")
    try:
        backend_storage = get_backend(destination).storage_info()
    except Exception as exc:  # noqa: BLE001
        backend_storage = {
            "free_bytes": None,
            "total_bytes": None,
            "error": str(exc),
        }
    return {
        "free_bytes": backend_storage.get("free_bytes"),
        "total_bytes": backend_storage.get("total_bytes"),
        "used_bytes": backend_storage.get("used_bytes"),
        "bytes_uploaded_by_app": uploaded_bytes,
        **({"error": backend_storage["error"]} if backend_storage.get("error") else {}),
    }


def build_upload_stats(session: Session) -> dict:
    destinations = session.query(Destination).order_by(Destination.name).all()
    rows = session.query(UploadedClip).all()
    rows_by_destination: dict[int, list[UploadedClip]] = {}
    for row in rows:
        rows_by_destination.setdefault(row.destination_id, []).append(row)

    return {
        "overview": _build_upload_totals(rows),
        "destinations": [
            {
                **dest_dict(destination),
                **_build_upload_totals(rows_by_destination.get(destination.id, [])),
                "storage": _destination_storage_payload(
                    destination,
                    rows_by_destination.get(destination.id, []),
                ),
            }
            for destination in destinations
        ],
    }

def media_dict(m: MediaItem) -> dict:
    return {
        "id": m.id,
        "filename": m.filename,
        "kind": m.kind,
        "size_bytes": m.size_bytes,
        "duration_s": m.duration_s,
        "codec": m.codec,
        "width": m.width,
        "height": m.height,
        "capture_time": m.capture_time.isoformat() if m.capture_time else None,
        "year": m.year,
        "month": m.month,
        "source": m.source,
        "derived": m.derived,
        "checksum": m.checksum,
        "has_thumb": bool(m.thumbnail),
        "tags": [t.name for t in m.tags],
        "uploads": [
            {
                "destination_id": u.destination_id,
                "status": u.status,
                "error": u.error,
                "bytes_uploaded": u.bytes_uploaded,
                "total_bytes": u.total_bytes,
                "progress": round((u.bytes_uploaded / u.total_bytes), 4) if u.total_bytes else 0,
            }
            for u in m.upload_states
        ],
    }


# --- devices ----------------------------------------------------------------

@router.get("/devices")
def list_devices():
    devices = detect_devices()
    return [
        {
            "path": str(d.path),
            "label": d.label,
            "dcim_path": str(d.dcim_path) if d.dcim_path else None,
            "free_bytes": d.free_bytes,
            "total_bytes": d.total_bytes,
            "file_count": len(scan_media_files(d.dcim_path)) if d.dcim_path else 0,
        }
        for d in devices
    ]


class ImportDeviceReq(BaseModel):
    dcim_path: str
    auto_upload: Optional[bool] = None
    destination_ids: Optional[List[int]] = None
    paths: Optional[List[str]] = None


@router.get("/device-files")
def list_device_files(dcim_path: str):
    root = Path(dcim_path)
    if not root.exists():
        raise HTTPException(400, "DCIM path does not exist")
    files = []
    for path in scan_media_files(root):
        kind = classify(path)
        if kind != "video":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "path": str(path),
                "filename": path.name,
                "relative_path": str(path.relative_to(root)),
                "size_bytes": stat.st_size,
                "modified_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return {"dcim_path": str(root), "files": files}


@router.post("/import-device")
def import_device(req: ImportDeviceReq, session: Session = Depends(get_session)):
    """Import every clip from a connected DCIM device.

    With auto_upload this is the 'Upload Everything' flow.
    """
    prefs = get_app_settings(session)
    root = Path(req.dcim_path)
    if not root.exists():
        raise HTTPException(400, "DCIM path does not exist")
    paths = [str(p) for p in scan_media_files(root)]
    if req.paths:
        requested = {str(Path(p)) for p in req.paths}
        paths = [p for p in paths if p in requested and Path(p).is_relative_to(root)]
    if not paths:
        raise HTTPException(400, "No media files found on device")
    auto_upload = prefs.auto_upload_on_import if req.auto_upload is None else req.auto_upload
    destination_ids = req.destination_ids
    if auto_upload and destination_ids is None:
        destination_ids = app_settings_dict(prefs)["default_destination_ids"]
    job_id = get_manager().enqueue(
        "import",
        description=f"Import {len(paths)} files from {root.name}",
        payload={
            "paths": paths,
            "source": "device",
            "auto_upload": auto_upload,
            "destination_ids": destination_ids,
        },
    )
    return {
        "job_id": job_id,
        "file_count": len(paths),
        "auto_upload": auto_upload,
        "destination_ids": destination_ids,
    }


# --- media listing & filtering ---------------------------------------------

@router.get("/media")
def list_media(
    year: Optional[int] = None,
    month: Optional[int] = None,
    tag: Optional[str] = None,
    album_id: Optional[int] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = session.query(MediaItem)
    if year is not None:
        q = q.filter(func.strftime("%Y", MediaItem.capture_time) == f"{year:04d}")
    if month is not None:
        q = q.filter(func.strftime("%m", MediaItem.capture_time) == f"{month:02d}")
    if tag:
        q = q.join(MediaItem.tags).filter(Tag.name == tag)
    if album_id is not None:
        q = q.join(AlbumItem, AlbumItem.media_id == MediaItem.id).filter(
            AlbumItem.album_id == album_id
        )
    q = q.order_by(MediaItem.capture_time.desc().nullslast())
    items = q.all()
    if status:
        items = [m for m in items if any(u.status == status for u in m.upload_states)]
    return [media_dict(m) for m in items]


@router.get("/media/months")
def media_months(session: Session = Depends(get_session)):
    """Distinct Year/Month buckets for the filter UI."""
    rows = (
        session.query(
            func.strftime("%Y", MediaItem.capture_time).label("y"),
            func.strftime("%m", MediaItem.capture_time).label("m"),
            func.count(MediaItem.id),
        )
        .filter(MediaItem.capture_time.isnot(None))
        .group_by("y", "m")
        .order_by("y", "m")
        .all()
    )
    return [
        {"year": int(y), "month": int(mo), "count": c}
        for y, mo, c in rows
        if y and mo
    ]


@router.get("/media/{media_id}/stream")
def stream_media(media_id: int, request: Request, session: Session = Depends(get_session)):
    m = session.get(MediaItem, media_id)
    if not m:
        raise HTTPException(404, "Not found")
    return stream_file(request, Path(m.path))


@router.get("/media/{media_id}/thumb")
def media_thumb(media_id: int, session: Session = Depends(get_session)):
    m = session.get(MediaItem, media_id)
    if not m or not m.thumbnail or not Path(m.thumbnail).exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(m.thumbnail, media_type="image/jpeg")


class RenameReq(BaseModel):
    filename: str


@router.post("/media/{media_id}/rename")
def rename_media(media_id: int, req: RenameReq, session: Session = Depends(get_session)):
    m = session.get(MediaItem, media_id)
    if not m:
        raise HTTPException(404, "Not found")
    old = Path(m.path)
    new = old.with_name(req.filename)
    if new.exists():
        raise HTTPException(409, "Target filename already exists")
    if old.exists():
        old.rename(new)
    m.path = str(new)
    m.filename = new.name
    session.commit()
    return media_dict(m)


@router.delete("/media/{media_id}")
def delete_media(media_id: int, delete_file: bool = False, session: Session = Depends(get_session)):
    m = session.get(MediaItem, media_id)
    if not m:
        raise HTTPException(404, "Not found")
    if delete_file and Path(m.path).exists():
        Path(m.path).unlink()
    if m.thumbnail and Path(m.thumbnail).exists():
        Path(m.thumbnail).unlink()
    session.delete(m)
    session.commit()
    return {"deleted": media_id}


# --- tags -------------------------------------------------------------------

class TagAssignReq(BaseModel):
    media_ids: List[int]
    tags: List[str]


@router.post("/tags/assign")
def assign_tags(req: TagAssignReq, session: Session = Depends(get_session)):
    tag_objs = []
    for name in req.tags:
        name = name.strip()
        if not name:
            continue
        t = session.query(Tag).filter(Tag.name == name).first()
        if not t:
            t = Tag(name=name)
            session.add(t)
            session.flush()
        tag_objs.append(t)
    for mid in req.media_ids:
        m = session.get(MediaItem, mid)
        if not m:
            continue
        for t in tag_objs:
            if t not in m.tags:
                m.tags.append(t)
    session.commit()
    return {"updated": len(req.media_ids)}


@router.get("/tags")
def list_tags(session: Session = Depends(get_session)):
    return [{"id": t.id, "name": t.name} for t in session.query(Tag).order_by(Tag.name).all()]


# --- albums -----------------------------------------------------------------

class AlbumReq(BaseModel):
    name: str
    description: Optional[str] = None


@router.post("/albums")
def create_album(req: AlbumReq, session: Session = Depends(get_session)):
    if session.query(Album).filter(Album.name == req.name).first():
        raise HTTPException(409, "Album exists")
    a = Album(name=req.name, description=req.description)
    session.add(a)
    session.commit()
    return {"id": a.id, "name": a.name}


@router.get("/albums")
def list_albums(session: Session = Depends(get_session)):
    albums = session.query(Album).order_by(Album.name).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "description": a.description,
            "item_ids": [it.media_id for it in a.items],
        }
        for a in albums
    ]


class AlbumItemsReq(BaseModel):
    media_ids: List[int]  # ordered


@router.post("/albums/{album_id}/items")
def set_album_items(album_id: int, req: AlbumItemsReq, session: Session = Depends(get_session)):
    """Replace album membership with the given ordered list (order = merge order)."""
    a = session.get(Album, album_id)
    if not a:
        raise HTTPException(404, "Album not found")
    for it in list(a.items):
        session.delete(it)
    session.flush()
    for pos, mid in enumerate(req.media_ids):
        session.add(AlbumItem(album_id=album_id, media_id=mid, position=pos))
    session.commit()
    return {"album_id": album_id, "count": len(req.media_ids)}


@router.delete("/albums/{album_id}")
def delete_album(album_id: int, session: Session = Depends(get_session)):
    a = session.get(Album, album_id)
    if not a:
        raise HTTPException(404, "Not found")
    session.delete(a)
    session.commit()
    return {"deleted": album_id}


# --- destinations -----------------------------------------------------------

class DestinationReq(BaseModel):
    name: str
    type: str  # local|nfs|smb|nextcloud|sftp|rsync
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    secret: Optional[str] = None  # plaintext in; stored encrypted
    base_path: str = "/"
    path_template: str = "{year}/{month:02d}"
    is_default: bool = False
    enabled: bool = True


def dest_dict(d: Destination) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "type": d.type,
        "host": d.host,
        "port": d.port,
        "username": d.username,
        "base_path": d.base_path,
        "path_template": d.path_template,
        "is_default": d.is_default,
        "enabled": d.enabled,
        "has_secret": bool(d.secret_enc),
    }


def uploaded_clip_dict(r: UploadedClip) -> dict:
    return {
        "id": r.id,
        "destination_id": r.destination_id,
        "source_media_id": r.source_media_id,
        "checksum": r.checksum,
        "filename": r.filename,
        "size_bytes": r.size_bytes,
        "remote_path": r.remote_path,
        "temp_remote_path": r.temp_remote_path,
        "status": r.status,
        "bytes_uploaded": r.bytes_uploaded,
        "upload_duration_s": _round_or_none(r.upload_duration_s),
        "upload_throughput_bps": _round_or_none(r.upload_throughput_bps),
        "last_error": r.last_error,
        "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("/destinations")
def list_destinations(session: Session = Depends(get_session)):
    rows = session.query(UploadedClip).all()
    rows_by_destination: dict[int, list[UploadedClip]] = {}
    for row in rows:
        rows_by_destination.setdefault(row.destination_id, []).append(row)
    return [
        {
            **dest_dict(destination),
            "storage": _destination_storage_payload(
                destination,
                rows_by_destination.get(destination.id, []),
            ),
        }
        for destination in session.query(Destination).order_by(Destination.name).all()
    ]


@router.post("/destinations")
def create_destination(req: DestinationReq, session: Session = Depends(get_session)):
    if req.type not in ("nextcloud", "sftp", "local", "nfs", "smb", "rsync"):
        raise HTTPException(400, "Invalid destination type")
    d = Destination(
        name=req.name,
        type=req.type,
        host=req.host,
        port=req.port,
        username=req.username,
        secret_enc=encrypt(req.secret) if req.secret else None,
        base_path=req.base_path,
        path_template=req.path_template,
        is_default=req.is_default,
        enabled=req.enabled,
    )
    session.add(d)
    session.commit()
    return dest_dict(d)


@router.put("/destinations/{dest_id}")
def update_destination(dest_id: int, req: DestinationReq, session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    if req.type not in ("nextcloud", "sftp", "local", "nfs", "smb", "rsync"):
        raise HTTPException(400, "Invalid destination type")
    d.name = req.name
    d.type = req.type
    d.host = req.host
    d.port = req.port
    d.username = req.username
    if req.secret:  # only overwrite when a new secret is supplied
        d.secret_enc = encrypt(req.secret)
    d.base_path = req.base_path
    d.path_template = req.path_template
    d.is_default = req.is_default
    d.enabled = req.enabled
    session.commit()
    return dest_dict(d)


@router.delete("/destinations/{dest_id}")
def delete_destination(dest_id: int, session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    session.delete(d)
    session.commit()
    return {"deleted": dest_id}


@router.post("/destinations/{dest_id}/test")
def test_destination(dest_id: int, session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    try:
        get_backend(d).test_connection()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@router.get("/destinations/{dest_id}/folders")
def browse_destination(dest_id: int, path: str = "", session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    try:
        folders = get_backend(d).list_directories(path)
        return {"destination_id": dest_id, "path": path, "folders": folders}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


# --- settings ---------------------------------------------------------------

class AppSettingsReq(BaseModel):
    auto_import_on_connect: bool = False
    auto_upload_on_import: bool = False
    default_destination_ids: List[int] = []
    ha_base_url: str = ""
    ha_token: str = ""
    ha_entity_prefix: str = "drift_import"


@router.get("/settings")
def read_settings(session: Session = Depends(get_session)):
    settings = get_app_settings(session)
    return app_settings_dict(settings)


@router.put("/settings")
def update_settings(req: AppSettingsReq, session: Session = Depends(get_session)):
    settings = get_app_settings(session)
    settings.auto_import_on_connect = req.auto_import_on_connect
    settings.auto_upload_on_import = req.auto_upload_on_import
    settings.default_destination_ids = encode_destination_ids(req.default_destination_ids)
    settings.ha_base_url = req.ha_base_url.strip() or None
    settings.ha_token = req.ha_token.strip() or None
    settings.ha_entity_prefix = req.ha_entity_prefix.strip() or "drift_import"
    touch_settings(settings)
    session.commit()
    return app_settings_dict(settings)


@router.get("/uploaded-clips")
def list_uploaded_clips(limit: int = 200, session: Session = Depends(get_session)):
    rows = session.query(UploadedClip).order_by(UploadedClip.updated_at.desc()).limit(limit).all()
    return [uploaded_clip_dict(r) for r in rows]


@router.get("/stats/uploads")
def upload_stats(session: Session = Depends(get_session)):
    return build_upload_stats(session)


@router.get("/stats")
def stats(session: Session = Depends(get_session)):
    return build_upload_stats(session)


# --- actions: upload / timestamp / merge ------------------------------------

class UploadReq(BaseModel):
    media_ids: List[int]
    destination_ids: Optional[List[int]] = None  # None -> defaults


@router.post("/upload")
def start_upload(req: UploadReq):
    if not req.media_ids:
        raise HTTPException(400, "No media selected")
    job_id = get_manager().enqueue(
        "upload",
        description=f"Upload {len(req.media_ids)} item(s)",
        payload={"media_ids": req.media_ids, "destination_ids": req.destination_ids},
    )
    return {"job_id": job_id}


class TimestampReq(BaseModel):
    media_ids: List[int]
    mode: str = "shift"  # shift|set
    absolute: Optional[str] = None  # ISO datetime for mode=set
    days: int = 0
    hours: int = 0
    minutes: int = 0
    seconds: int = 0
    write_metadata: bool = True


@router.post("/timestamp")
def start_timestamp(req: TimestampReq):
    if req.mode == "set" and not req.absolute:
        raise HTTPException(400, "mode=set requires 'absolute'")
    job_id = get_manager().enqueue(
        "timestamp",
        description=f"Adjust timestamps for {len(req.media_ids)} item(s)",
        payload=req.model_dump(),
    )
    return {"job_id": job_id}


class MergeReq(BaseModel):
    media_ids: List[int]  # ordered
    album_id: Optional[int] = None
    output_name: Optional[str] = None
    order: str = "selected"  # selected|date|sequence


def _sequence_key(filename: str) -> tuple[int, str]:
    numbers = [int(n) for n in re.findall(r"\d+", filename)]
    return (numbers[-1] if numbers else 0, filename.lower())


@router.post("/merge")
def start_merge(req: MergeReq, session: Session = Depends(get_session)):
    media_ids = req.media_ids
    if req.album_id is not None:
        a = session.get(Album, req.album_id)
        if not a:
            raise HTTPException(404, "Album not found")
        media_ids = [it.media_id for it in a.items]
    if req.order in ("date", "sequence"):
        items = [session.get(MediaItem, mid) for mid in media_ids]
        items = [item for item in items if item]
        if req.order == "date":
            items.sort(key=lambda item: (item.capture_time or dt.datetime.min, item.filename.lower()))
        else:
            items.sort(key=lambda item: _sequence_key(item.filename))
        media_ids = [item.id for item in items]
    if len(media_ids) < 2:
        raise HTTPException(400, "Need at least two clips to merge")
    job_id = get_manager().enqueue(
        "merge",
        description=f"Merge {len(media_ids)} clips",
        payload={"media_ids": media_ids, "output_name": req.output_name},
    )
    return {"job_id": job_id}


# --- jobs -------------------------------------------------------------------

def job_dict(j: Job) -> dict:
    return {
        "id": j.id,
        "kind": j.kind,
        "description": j.description,
        "status": j.status,
        "progress": round(j.progress, 4),
        "detail": j.detail,
        "error": j.error,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


@router.get("/jobs")
def list_jobs(limit: int = 50, session: Session = Depends(get_session)):
    jobs = session.query(Job).order_by(Job.created_at.desc()).limit(limit).all()
    return [job_dict(j) for j in jobs]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    get_manager().request_cancel(job_id)
    return {"cancelled": job_id}
