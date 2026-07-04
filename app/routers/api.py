"""JSON / form API endpoints."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from ..crypto import encrypt
from ..config import get_settings
from ..database import get_session
from ..destinations import get_backend
from ..devices import scan_media_files
from ..devicescan import get_device_monitor
from ..media import classify, make_thumbnail
from ..jobs import get_manager, jobs_overview
from ..models import (
    Album,
    AlbumItem,
    AppSettings,
    Destination,
    Job,
    JobLog,
    MediaItem,
    SystemSample,
    Tag,
    UploadedClip,
    UploadState,
)
from ..settings_store import app_settings_dict, encode_destination_ids, get_app_settings, touch_settings
from ..streaming import stream_file
from ..sysmon import get_monitor
from ..tasks import enqueue_device_import, enqueue_upload_jobs

router = APIRouter()

# Default stats window: last 30 minutes (in hours, since the API is hours-based).
DEFAULT_TIMELINE_HOURS = 0.5


# --- serialization helpers --------------------------------------------------

def _round_or_none(value: Optional[float], digits: int = 3) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _build_upload_totals(rows: list[UploadedClip]) -> dict:
    done_rows = [row for row in rows if row.status == "done"]
    error_rows = [row for row in rows if row.status == "error"]
    uploading_rows = [row for row in rows if row.status == "uploading"]
    pending_rows = [row for row in rows if row.status == "pending"]
    total_bytes = sum(int(row.size_bytes or 0) for row in done_rows)
    durations = [float(row.upload_duration_s) for row in done_rows if row.upload_duration_s]
    throughputs = [float(row.upload_throughput_bps) for row in done_rows if row.upload_throughput_bps]
    return {
        "uploaded_clip_count": len(done_rows),
        "error_clip_count": len(error_rows),
        "uploading_clip_count": len(uploading_rows),
        "pending_clip_count": len(pending_rows),
        "uploaded_bytes": total_bytes,
        "average_upload_duration_s": _round_or_none(
            (sum(durations) / len(durations)) if durations else None
        ),
        "average_throughput_bps": _round_or_none(
            (sum(throughputs) / len(throughputs)) if throughputs else None
        ),
    }


def _build_upload_totals_query(session: Session, destination_id: int | None = None) -> dict:
    query = session.query(
        func.sum(case((UploadedClip.status == "done", 1), else_=0)).label("done_count"),
        func.sum(case((UploadedClip.status == "error", 1), else_=0)).label("error_count"),
        func.sum(case((UploadedClip.status == "uploading", 1), else_=0)).label("uploading_count"),
        func.sum(case((UploadedClip.status == "pending", 1), else_=0)).label("pending_count"),
        func.sum(case((UploadedClip.status == "done", UploadedClip.size_bytes), else_=0)).label("uploaded_bytes"),
        func.avg(
            case((UploadedClip.status == "done", UploadedClip.upload_duration_s), else_=None)
        ).label("avg_duration"),
        func.avg(
            case((UploadedClip.status == "done", UploadedClip.upload_throughput_bps), else_=None)
        ).label("avg_throughput"),
    )
    if destination_id is not None:
        query = query.filter(UploadedClip.destination_id == destination_id)
    row = query.one()
    return {
        "uploaded_clip_count": int(row.done_count or 0),
        "error_clip_count": int(row.error_count or 0),
        "uploading_clip_count": int(row.uploading_count or 0),
        "pending_clip_count": int(row.pending_count or 0),
        "uploaded_bytes": int(row.uploaded_bytes or 0),
        "average_upload_duration_s": _round_or_none(row.avg_duration),
        "average_throughput_bps": _round_or_none(row.avg_throughput),
    }


def _destination_storage_payload(
    destination: Destination,
    uploaded_bytes: int,
) -> dict:
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


def _cpu_payload() -> dict:
    # The instantaneous percent comes from the background monitor's latest
    # tick (a smoothed interval average), so it no longer depends on how often
    # the client happens to poll. Load averages are cheap to read on demand.
    snap = get_monitor().snapshot()
    load = None
    if hasattr(os, "getloadavg"):
        try:
            load = os.getloadavg()
        except OSError:
            load = None
    return {
        "percent": snap.get("cpu_percent"),
        "load_1m": _round_or_none(load[0]) if load else None,
        "load_5m": _round_or_none(load[1]) if load else None,
        "load_15m": _round_or_none(load[2]) if load else None,
        "cpu_count": os.cpu_count(),
    }


def _network_payload() -> dict:
    snap = get_monitor().snapshot()
    return {
        "rx_bytes_total": snap.get("rx_bytes_total"),
        "tx_bytes_total": snap.get("tx_bytes_total"),
        "rx_bytes_per_s": int(snap.get("rx_bytes_per_s") or 0),
        "tx_bytes_per_s": int(snap.get("tx_bytes_per_s") or 0),
    }


def _downsample(rows: list, max_points: int) -> list:
    """Stride a list down to at most max_points, keeping order (and the ends)."""
    if len(rows) <= max_points:
        return rows
    step = (len(rows) + max_points - 1) // max_points
    sampled = rows[::step]
    if sampled and sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])
    return sampled


def _system_history(session: Session, hours: float) -> tuple[list, list, list]:
    """Return (cpu, rx, tx) history series within the window, as [{t, v}, ...]."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    since = now - dt.timedelta(hours=max(0.0, float(hours or 0)))
    rows = (
        session.query(SystemSample)
        .filter(SystemSample.created_at >= since)
        .order_by(SystemSample.created_at)
        .all()
    )
    rows = _downsample(rows, 300)
    cpu, rx, tx = [], [], []
    for row in rows:
        t = row.created_at.isoformat()
        cpu.append({"t": t, "v": row.cpu_percent if row.cpu_percent is not None else 0})
        rx.append({"t": t, "v": int(row.rx_bytes_per_s or 0)})
        tx.append({"t": t, "v": int(row.tx_bytes_per_s or 0)})
    return cpu, rx, tx


def _filesystem_payload() -> list[dict]:
    settings = get_settings()
    paths = [
        ("Root", Path("/")),
        ("Data", settings.data_dir),
        ("Working", settings.working_dir),
        ("Thumbnails", settings.thumbnail_dir),
    ]
    seen: set[str] = set()
    rows = []
    for label, path in paths:
        try:
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            usage = shutil.disk_usage(path)
            used = int(usage.total - usage.free)
            rows.append(
                {
                    "label": label,
                    "path": resolved,
                    "total_bytes": int(usage.total),
                    "used_bytes": used,
                    "free_bytes": int(usage.free),
                    "used_percent": round((used / usage.total) * 100, 1) if usage.total else None,
                }
            )
        except OSError as exc:
            rows.append(
                {
                    "label": label,
                    "path": str(path),
                    "total_bytes": None,
                    "used_bytes": None,
                    "free_bytes": None,
                    "used_percent": None,
                    "error": str(exc),
                }
            )
    return rows


def _timeline_bucket_minutes(hours: float) -> int:
    if hours <= 1:
        return 5
    if hours <= 3:
        return 10
    if hours <= 6:
        return 15
    if hours <= 12:
        return 30
    return 60


def build_upload_timeline(
    rows: list[UploadedClip],
    hours: float = DEFAULT_TIMELINE_HOURS,
    now: dt.datetime | None = None,
) -> dict:
    hours = max(0.25, min(float(hours or DEFAULT_TIMELINE_HOURS), 72.0))
    bucket_minutes = _timeline_bucket_minutes(hours)
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    since = now - dt.timedelta(hours=hours)
    bucket_delta = dt.timedelta(minutes=bucket_minutes)
    bucket_count = max(1, int((dt.timedelta(hours=hours) / bucket_delta) + 0.999))
    start = now - (bucket_delta * bucket_count)
    buckets = [
        {
            "start": (start + bucket_delta * i),
            "end": (start + bucket_delta * (i + 1)),
            "uploaded_bytes": 0,
            "error_bytes": 0,
            "active_bytes": 0,
            "clip_count": 0,
        }
        for i in range(bucket_count)
    ]
    for row in rows:
        event_time = row.uploaded_at if row.status == "done" else row.updated_at
        if event_time is None or event_time < since or event_time > now:
            continue
        idx = int((event_time - start) / bucket_delta)
        if idx < 0 or idx >= len(buckets):
            continue
        bytes_value = int(row.size_bytes or 0) if row.status == "done" else int(row.bytes_uploaded or 0)
        buckets[idx]["clip_count"] += 1
        if row.status == "done":
            buckets[idx]["uploaded_bytes"] += bytes_value
        elif row.status == "error":
            buckets[idx]["error_bytes"] += bytes_value
        elif row.status == "uploading":
            buckets[idx]["active_bytes"] += bytes_value
    points = [
        {
            "start": bucket["start"].isoformat(),
            "end": bucket["end"].isoformat(),
            "uploaded_bytes": bucket["uploaded_bytes"],
            "error_bytes": bucket["error_bytes"],
            "active_bytes": bucket["active_bytes"],
            "clip_count": bucket["clip_count"],
        }
        for bucket in buckets
    ]
    return {
        "hours": hours,
        "bucket_minutes": bucket_minutes,
        "total_uploaded_bytes": sum(point["uploaded_bytes"] for point in points),
        "total_error_bytes": sum(point["error_bytes"] for point in points),
        "total_active_bytes": sum(point["active_bytes"] for point in points),
        "points": points,
    }


def _timeline_rows_query(
    session: Session,
    hours: float = DEFAULT_TIMELINE_HOURS,
    destination_id: int | None = None,
) -> list[UploadedClip]:
    hours = max(0.25, min(float(hours or DEFAULT_TIMELINE_HOURS), 72.0))
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    since = now - dt.timedelta(hours=hours)
    query = session.query(UploadedClip).filter(
        or_(
            (UploadedClip.status == "done") & (UploadedClip.uploaded_at >= since),
            (UploadedClip.status != "done") & (UploadedClip.updated_at >= since),
        )
    )
    if destination_id is not None:
        query = query.filter(UploadedClip.destination_id == destination_id)
    return query.all()


def build_system_stats(
    upload_rows: list[UploadedClip] | None = None,
    timeline_hours: float = DEFAULT_TIMELINE_HOURS,
    session: Session | None = None,
) -> dict:
    cpu = _cpu_payload()
    network = _network_payload()
    # Server-stored history so the graph shows the whole window on page load
    # rather than the browser filling it in live. Empty when no DB session
    # (e.g. unit tests) or before the first samples are recorded.
    if session is not None:
        cpu_hist, rx_hist, tx_hist = _system_history(session, timeline_hours)
    else:
        cpu_hist, rx_hist, tx_hist = [], [], []
    cpu["history"] = cpu_hist
    network["rx_history"] = rx_hist
    network["tx_history"] = tx_hist
    return {
        "sampled_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cpu": cpu,
        "network": network,
        "upload_timeline": build_upload_timeline(upload_rows or [], timeline_hours),
        "filesystems": _filesystem_payload(),
    }


def build_upload_stats(session: Session, timeline_hours: float = DEFAULT_TIMELINE_HOURS) -> dict:
    destinations = session.query(Destination).order_by(Destination.name).all()
    overview = _build_upload_totals_query(session)
    system_rows = _timeline_rows_query(session, timeline_hours)

    return {
        "overview": overview,
        "system": build_system_stats(system_rows, timeline_hours, session=session),
        "destinations": [
            {
                **dest_dict(destination),
                **(destination_totals := _build_upload_totals_query(session, destination.id)),
                "storage": _destination_storage_payload(
                    destination,
                    destination_totals["uploaded_bytes"],
                ),
                "upload_timeline": build_upload_timeline(
                    _timeline_rows_query(session, timeline_hours, destination.id),
                    timeline_hours,
                ),
            }
            for destination in destinations
        ],
    }

def media_dict(m: MediaItem) -> dict:
    return {
        "id": m.id,
        "path": m.path,
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
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "tags": [t.name for t in m.tags],
        "uploads": [
            {
                "destination_id": u.destination_id,
                "status": u.status,
                "error": u.error,
                "bytes_uploaded": u.bytes_uploaded,
                "total_bytes": u.total_bytes,
                "progress": round((u.bytes_uploaded / u.total_bytes), 4) if u.total_bytes else 0,
                "remote_path": u.remote_path,
                "uploaded_at": u.uploaded_at.isoformat() if u.uploaded_at else None,
            }
            for u in m.upload_states
        ],
    }


def _is_attached_camera_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for device in get_device_monitor().get_devices():
        dcim = device.get("dcim_path")
        if not dcim:
            continue
        try:
            resolved.relative_to(Path(dcim).resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _safe_child_path(root: Path, path: str = "") -> Path:
    try:
        resolved_root = root.resolve()
        child = (resolved_root / path.strip("/")).resolve()
        child.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, "Path is outside the camera root") from exc
    return child


def _safe_device_media_path(path: str) -> Path:
    media_path = Path(path)
    try:
        resolved = media_path.resolve()
    except OSError as exc:
        raise HTTPException(400, "Camera file path is not accessible") from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(404, "Camera file not found")
    if classify(resolved) is None:
        raise HTTPException(400, "Path is not a supported media file")
    for device in get_device_monitor().get_devices():
        dcim = device.get("dcim_path")
        if not dcim:
            continue
        try:
            resolved.relative_to(Path(dcim).resolve())
            return resolved
        except (OSError, ValueError):
            continue
    raise HTTPException(400, "Path is outside a detected camera mount")


def _device_roots() -> list[Path]:
    roots = []
    for device in get_device_monitor().get_devices():
        root = device.get("path") or device.get("dcim_path")
        if not root:
            continue
        try:
            roots.append(Path(root).resolve())
        except OSError:
            continue
    return roots


def _safe_device_root(root_path: str) -> Path:
    try:
        requested = Path(root_path).resolve()
    except OSError as exc:
        raise HTTPException(400, "Camera root is not accessible") from exc
    for root in _device_roots():
        if requested == root:
            return root
    raise HTTPException(400, "Path is not a detected camera root")


def _safe_relative_path(root: Path, path: str = "", outside_message: str = "Path is outside root") -> Path:
    try:
        resolved_root = root.resolve()
        child = (resolved_root / path.strip("/")).resolve()
        child.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise HTTPException(400, outside_message) from exc
    return child


def _safe_filename(name: str) -> str:
    clean = name.strip()
    if not clean or clean in {".", ".."} or "/" in clean or "\\" in clean:
        raise HTTPException(400, "Invalid filename")
    return clean


def _entry_dict(entry: Path, root: Path) -> dict:
    stat = entry.stat()
    is_dir = entry.is_dir()
    rel = str(entry.relative_to(root))
    media_kind = None if is_dir else classify(entry)
    return {
        "name": entry.name,
        "path": "" if rel == "." else rel,
        "type": "directory" if is_dir else "file",
        "kind": media_kind,
        "playable": media_kind == "video",
        "size_bytes": None if is_dir else int(stat.st_size),
        "modified_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def _list_local_entries(root: Path, path: str = "", outside_message: str = "Path is outside root") -> dict:
    current = _safe_relative_path(root, path, outside_message)
    if not current.exists() or not current.is_dir():
        raise HTTPException(400, "Path is not a directory")
    entries = []
    for entry in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            entries.append(_entry_dict(entry, root.resolve()))
        except OSError:
            continue
    return {"root": str(root), "path": path.strip("/"), "entries": entries}


def _local_destination_root(destination: Destination) -> Path:
    if destination.type not in ("local", "nfs", "smb"):
        raise HTTPException(400, "This action is only available for mounted/local destinations")
    root = Path(destination.base_path or "/")
    if not root.exists() or not root.is_dir():
        raise HTTPException(400, f"Destination root is not mounted: {root}")
    return root


def _parse_file_datetime(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "Invalid datetime") from exc
    return parsed


class FileRenameReq(BaseModel):
    path: str
    filename: str


class FileTimestampReq(BaseModel):
    path: str
    modified_at: str


def _log_level(line: str) -> str | None:
    match = re.search(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", line)
    return match.group(1) if match else None


def _read_log_lines(limit: int = 500, min_level: str = "INFO") -> dict:
    settings = get_settings()
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    min_value = levels.get(min_level.upper(), 20)
    limit = max(1, min(limit, 2000))
    log_paths = [
        settings.log_dir / "drift.log.3",
        settings.log_dir / "drift.log.2",
        settings.log_dir / "drift.log.1",
        settings.log_dir / "drift.log",
    ]
    lines = []
    for path in log_paths:
        if not path.exists():
            continue
        try:
            for line in path.read_text(errors="replace").splitlines():
                level = _log_level(line)
                if level is None or levels.get(level, 0) >= min_value:
                    lines.append({"level": level or "INFO", "message": line})
        except OSError as exc:
            lines.append({"level": "ERROR", "message": f"Unable to read {path}: {exc}"})
    return {
        "log_file": str(settings.log_dir / "drift.log"),
        "min_level": min_level.upper(),
        "lines": lines[-limit:],
    }


# --- devices ----------------------------------------------------------------

@router.get("/devices")
def list_devices():
    # Served from the background DeviceMonitor's cache so the request never
    # blocks on a slow/remote mount (the scan excludes destinations + network
    # filesystems and runs on a timer).
    return get_device_monitor().get_devices()


class ImportDeviceReq(BaseModel):
    dcim_path: str
    auto_upload: Optional[bool] = None
    destination_ids: Optional[List[int]] = None
    paths: Optional[List[str]] = None
    group_uploads_by_month: bool = False


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


@router.get("/device-file-thumb")
def device_file_thumb(path: str):
    media_path = _safe_device_media_path(path)
    try:
        stat = media_path.stat()
    except OSError as exc:
        raise HTTPException(404, "Camera file not found") from exc
    key = hashlib.sha256(
        f"{media_path}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    ).hexdigest()
    out = get_settings().thumbnail_dir / "device" / f"{key}.jpg"
    if not out.exists():
        ok = make_thumbnail(media_path, classify(media_path) or "video", out)
        if not ok:
            raise HTTPException(404, "No thumbnail")
    return FileResponse(out, media_type="image/jpeg")


@router.get("/device-entries")
def list_device_entries(dcim_path: str, path: str = ""):
    root = Path(dcim_path)
    if not root.exists():
        raise HTTPException(400, "DCIM path does not exist")
    current = _safe_child_path(root, path)
    if not current.exists() or not current.is_dir():
        raise HTTPException(400, "Camera path is not a directory")
    entries = []
    for entry in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            stat = entry.stat()
            rel = str(entry.relative_to(root))
        except OSError:
            continue
        kind = "directory" if entry.is_dir() else "file"
        if kind == "file" and classify(entry) != "video":
            continue
        entries.append(
            {
                "name": entry.name,
                "path": rel,
                "full_path": str(entry) if kind == "file" else None,
                "type": kind,
                "size_bytes": None if kind == "directory" else stat.st_size,
                "modified_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return {"dcim_path": str(root), "path": path.strip("/"), "entries": entries}


# --- file explorer ----------------------------------------------------------

@router.get("/file-browser/camera/entries")
def browse_camera_entries(root_path: str, path: str = ""):
    root = _safe_device_root(root_path)
    return _list_local_entries(root, path, "Path is outside the camera root")


@router.get("/file-browser/camera/stream")
def stream_camera_file(root_path: str, path: str, request: Request):
    root = _safe_device_root(root_path)
    target = _safe_relative_path(root, path, "Path is outside the camera root")
    if classify(target) != "video":
        raise HTTPException(400, "Only video files can be played")
    return stream_file(request, target)


@router.post("/file-browser/camera/rename")
def rename_camera_file(req: FileRenameReq, root_path: str, session: Session = Depends(get_session)):
    root = _safe_device_root(root_path)
    old = _safe_relative_path(root, req.path, "Path is outside the camera root")
    if not old.exists() or not old.is_file():
        raise HTTPException(404, "Camera file not found")
    new = old.with_name(_safe_filename(req.filename))
    _safe_relative_path(root, str(new.relative_to(root)), "Path is outside the camera root")
    if new.exists():
        raise HTTPException(409, "Target filename already exists")
    old_str = str(old)
    old.rename(new)
    item = session.query(MediaItem).filter(MediaItem.path == old_str).first()
    if item:
        item.path = str(new)
        item.filename = new.name
        session.commit()
    return _entry_dict(new, root)


@router.post("/file-browser/camera/timestamp")
def timestamp_camera_file(req: FileTimestampReq, root_path: str, session: Session = Depends(get_session)):
    root = _safe_device_root(root_path)
    target = _safe_relative_path(root, req.path, "Path is outside the camera root")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Camera file not found")
    when = _parse_file_datetime(req.modified_at)
    os.utime(target, (when.timestamp(), when.timestamp()))
    item = session.query(MediaItem).filter(MediaItem.path == str(target)).first()
    if item:
        item.capture_time = when
        session.commit()
    return _entry_dict(target, root)


@router.get("/file-browser/destinations/{dest_id}/entries")
def browse_destination_file_entries(dest_id: int, path: str = "", session: Session = Depends(get_session)):
    destination = session.get(Destination, dest_id)
    if not destination:
        raise HTTPException(404, "Destination not found")
    if destination.type in ("local", "nfs", "smb"):
        root = _local_destination_root(destination)
        data = _list_local_entries(root, path, "Path is outside the destination root")
        data["destination_id"] = dest_id
        data["local_actions"] = True
        return data
    try:
        entries = get_backend(destination).list_entries(path)
        return {
            "destination_id": dest_id,
            "root": destination.base_path,
            "path": path.strip("/"),
            "local_actions": False,
            "entries": entries,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@router.get("/file-browser/destinations/{dest_id}/stream")
def stream_destination_file(dest_id: int, path: str, request: Request, session: Session = Depends(get_session)):
    destination = session.get(Destination, dest_id)
    if not destination:
        raise HTTPException(404, "Destination not found")
    root = _local_destination_root(destination)
    target = _safe_relative_path(root, path, "Path is outside the destination root")
    if classify(target) != "video":
        raise HTTPException(400, "Only video files can be played")
    return stream_file(request, target)


@router.post("/file-browser/destinations/{dest_id}/rename")
def rename_destination_file(dest_id: int, req: FileRenameReq, session: Session = Depends(get_session)):
    destination = session.get(Destination, dest_id)
    if not destination:
        raise HTTPException(404, "Destination not found")
    root = _local_destination_root(destination)
    old = _safe_relative_path(root, req.path, "Path is outside the destination root")
    if not old.exists() or not old.is_file():
        raise HTTPException(404, "Destination file not found")
    new = old.with_name(_safe_filename(req.filename))
    _safe_relative_path(root, str(new.relative_to(root)), "Path is outside the destination root")
    if new.exists():
        raise HTTPException(409, "Target filename already exists")
    old_str = str(old)
    old.rename(new)
    session.query(UploadedClip).filter(UploadedClip.remote_path == old_str).update(
        {UploadedClip.remote_path: str(new), UploadedClip.filename: new.name},
        synchronize_session=False,
    )
    session.query(UploadState).filter(UploadState.remote_path == old_str).update(
        {UploadState.remote_path: str(new)},
        synchronize_session=False,
    )
    session.commit()
    return _entry_dict(new, root)


@router.post("/file-browser/destinations/{dest_id}/timestamp")
def timestamp_destination_file(dest_id: int, req: FileTimestampReq, session: Session = Depends(get_session)):
    destination = session.get(Destination, dest_id)
    if not destination:
        raise HTTPException(404, "Destination not found")
    root = _local_destination_root(destination)
    target = _safe_relative_path(root, req.path, "Path is outside the destination root")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Destination file not found")
    when = _parse_file_datetime(req.modified_at)
    os.utime(target, (when.timestamp(), when.timestamp()))
    return _entry_dict(target, root)


@router.post("/import-device")
def import_device(req: ImportDeviceReq, session: Session = Depends(get_session)):
    """Import every clip from a connected DCIM device.

    With auto_upload this is the 'Upload Everything' flow.
    """
    prefs = get_app_settings(session)
    root = Path(req.dcim_path)
    if not root.exists():
        raise HTTPException(400, "DCIM path does not exist")
    auto_upload = prefs.auto_upload_on_import if req.auto_upload is None else req.auto_upload
    destination_ids = req.destination_ids
    if auto_upload and destination_ids is None:
        destination_ids = app_settings_dict(prefs)["default_destination_ids"]
    # A whole-device import de-dupes against any pending import so a reconnect (or
    # a double click) doesn't pile up redundant work. An explicit file selection
    # is always honoured.
    job_id, file_count = enqueue_device_import(
        root,
        paths=req.paths,
        auto_upload=auto_upload,
        destination_ids=destination_ids,
        group_uploads_by_month=req.group_uploads_by_month,
        dedup=not req.paths,
    )
    if file_count == 0:
        raise HTTPException(400, "No media files found on device")
    return {
        "job_id": job_id,
        "file_count": file_count,
        "already_queued": job_id is None,
        "auto_upload": auto_upload,
        "destination_ids": destination_ids,
        "group_uploads_by_month": req.group_uploads_by_month,
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
    path = Path(m.path)
    if delete_file and (m.source == "device" or _is_attached_camera_path(path)):
        raise HTTPException(400, "Refusing to delete files from an attached camera")
    if delete_file and path.exists():
        path.unlink()
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


# --- trips ------------------------------------------------------------------

class AlbumReq(BaseModel):
    name: str
    description: Optional[str] = None


@router.post("/trips")
@router.post("/albums")
def create_album(req: AlbumReq, session: Session = Depends(get_session)):
    if session.query(Album).filter(Album.name == req.name).first():
        raise HTTPException(409, "Trip exists")
    a = Album(name=req.name, description=req.description)
    session.add(a)
    session.commit()
    return {"id": a.id, "name": a.name}


@router.get("/trips")
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


@router.post("/trips/{album_id}/items")
@router.post("/albums/{album_id}/items")
def set_album_items(album_id: int, req: AlbumItemsReq, session: Session = Depends(get_session)):
    """Replace trip membership with the given ordered list (order = combine order)."""
    a = session.get(Album, album_id)
    if not a:
        raise HTTPException(404, "Trip not found")
    for it in list(a.items):
        session.delete(it)
    session.flush()
    for pos, mid in enumerate(req.media_ids):
        session.add(AlbumItem(album_id=album_id, media_id=mid, position=pos))
    session.commit()
    return {"album_id": album_id, "count": len(req.media_ids)}


@router.delete("/trips/{album_id}")
@router.delete("/albums/{album_id}")
def delete_album(album_id: int, session: Session = Depends(get_session)):
    a = session.get(Album, album_id)
    if not a:
        raise HTTPException(404, "Trip not found")
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
    base_path: str = "/mnt/NAS"
    path_template: str = "{year}/{month:02d}"
    is_default: bool = False
    enabled: bool = True
    rank: Optional[int] = None


def _validate_destination_type(dest_type: str) -> None:
    if dest_type not in ("nextcloud", "sftp", "local", "nfs", "smb", "rsync"):
        raise HTTPException(400, "Invalid destination type")


def ensure_default_nas_destination(session: Session) -> None:
    existing_default = (
        session.query(Destination)
        .filter(Destination.is_default == True, Destination.enabled == True)  # noqa: E712
        .first()
    )
    if existing_default:
        return
    existing_nas = (
        session.query(Destination)
        .filter(Destination.type == "local", Destination.base_path.in_(("/mnt/NAS", "/mnt/nas")))
        .first()
    )
    if existing_nas:
        existing_nas.is_default = True
        existing_nas.enabled = True
        session.commit()
        return
    session.add(
        Destination(
            name="Mounted NAS",
            type="local",
            base_path="/mnt/NAS",
            path_template="{year}/{month:02d}",
            is_default=True,
            enabled=True,
            rank=0,
        )
    )
    session.commit()


def _next_destination_rank(session: Session) -> int:
    highest = session.query(func.max(Destination.rank)).scalar()
    return (highest + 1) if highest is not None else 0


def _destination_from_req(req: DestinationReq) -> Destination:
    _validate_destination_type(req.type)
    return Destination(
        name=req.name or "Preview",
        type=req.type,
        host=req.host,
        port=req.port,
        username=req.username,
        secret_enc=encrypt(req.secret) if req.secret else None,
        base_path=req.base_path,
        path_template=req.path_template,
        is_default=req.is_default,
        enabled=req.enabled,
        rank=req.rank if req.rank is not None else 100,
    )


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
        "rank": d.rank,
        "has_secret": bool(d.secret_enc),
    }


def uploaded_clip_dict(r: UploadedClip) -> dict:
    return {
        "id": r.id,
        "destination_id": r.destination_id,
        "destination_name": r.destination.name if r.destination else None,
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


def recent_upload_dict(r: UploadedClip) -> dict:
    media = r.media
    return {
        **uploaded_clip_dict(r),
        "media": media_dict(media) if media else None,
    }


@router.get("/destinations")
def list_destinations(session: Session = Depends(get_session)):
    ensure_default_nas_destination(session)
    return [
        {
            **dest_dict(destination),
            "storage": _destination_storage_payload(
                destination,
                _build_upload_totals_query(session, destination.id)["uploaded_bytes"],
            ),
        }
        for destination in session.query(Destination)
        .order_by(Destination.rank, Destination.id)
        .all()
    ]


@router.post("/destinations")
def create_destination(req: DestinationReq, session: Session = Depends(get_session)):
    d = _destination_from_req(req)
    if req.rank is None:
        d.rank = _next_destination_rank(session)
    session.add(d)
    session.commit()
    return dest_dict(d)


@router.put("/destinations/{dest_id}")
def update_destination(dest_id: int, req: DestinationReq, session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    _validate_destination_type(req.type)
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
    if req.rank is not None:
        d.rank = req.rank
    session.commit()
    return dest_dict(d)


class ReorderReq(BaseModel):
    ordered_ids: List[int]


@router.post("/destinations/reorder")
def reorder_destinations(req: ReorderReq, session: Session = Depends(get_session)):
    """Set upload priority from an ordered list of destination ids (first = top)."""
    for position, dest_id in enumerate(req.ordered_ids):
        d = session.get(Destination, dest_id)
        if d:
            d.rank = position
    session.commit()
    return {"ok": True, "order": req.ordered_ids}


@router.delete("/destinations/{dest_id}")
def delete_destination(dest_id: int, session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    session.delete(d)
    session.commit()
    return {"deleted": dest_id}


def _run_destination_test(destination: Destination) -> dict:
    """Verify a destination end-to-end: connect, then upload+download a probe."""
    result = {"ok": False, "connection": False, "round_trip": False, "error": None}
    backend = get_backend(destination)
    try:
        backend.test_connection()
        result["connection"] = True
        backend.verify_round_trip()
        result["round_trip"] = True
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


# Declared before the "/{dest_id}/test" route so the static "preview" path is
# matched first (otherwise it parses as dest_id="preview" -> 422).
@router.post("/destinations/preview/test")
def test_destination_preview(req: DestinationReq):
    return _run_destination_test(_destination_from_req(req))


@router.post("/destinations/{dest_id}/test")
def test_destination(dest_id: int, session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    return _run_destination_test(d)


@router.post("/destinations/preview/folders")
def browse_destination_preview(req: DestinationReq, path: str = ""):
    d = _destination_from_req(req)
    try:
        folders = get_backend(d).list_directories(path)
        return {"path": path, "folders": folders}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


@router.post("/destinations/preview/entries")
def browse_destination_preview_entries(req: DestinationReq, path: str = ""):
    d = _destination_from_req(req)
    try:
        entries = get_backend(d).list_entries(path)
        return {"path": path, "entries": entries}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc


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


@router.get("/destinations/{dest_id}/entries")
def browse_destination_entries(dest_id: int, path: str = "", session: Session = Depends(get_session)):
    d = session.get(Destination, dest_id)
    if not d:
        raise HTTPException(404, "Not found")
    try:
        entries = get_backend(d).list_entries(path)
        return {"destination_id": dest_id, "path": path, "entries": entries}
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


@router.get("/logs")
def read_logs(limit: int = 500, min_level: str = "INFO"):
    return _read_log_lines(limit=limit, min_level=min_level)


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


@router.get("/recent-uploads")
def list_recent_uploads(
    limit: int = 24,
    days: int = 7,
    session: Session = Depends(get_session),
):
    since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=max(days, 1))
    rows = (
        session.query(UploadedClip)
        .filter(UploadedClip.status == "done", UploadedClip.uploaded_at.isnot(None))
        .filter(UploadedClip.uploaded_at >= since)
        .order_by(UploadedClip.uploaded_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    return [recent_upload_dict(r) for r in rows]


@router.get("/stats/uploads")
def upload_stats(
    timeline_hours: float = DEFAULT_TIMELINE_HOURS,
    session: Session = Depends(get_session),
):
    return build_upload_stats(session, timeline_hours=timeline_hours)


@router.get("/stats")
def stats(
    timeline_hours: float = DEFAULT_TIMELINE_HOURS,
    session: Session = Depends(get_session),
):
    return build_upload_stats(session, timeline_hours=timeline_hours)


# --- actions: upload / timestamp / merge ------------------------------------

class UploadReq(BaseModel):
    media_ids: List[int]
    destination_ids: Optional[List[int]] = None  # None -> defaults


@router.post("/upload")
def start_upload(req: UploadReq, session: Session = Depends(get_session)):
    if not req.media_ids:
        raise HTTPException(400, "No media selected")
    job_ids = enqueue_upload_jobs(req.media_ids, req.destination_ids)
    return {"job_ids": job_ids, "job_id": job_ids[0] if job_ids else None}


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
            raise HTTPException(404, "Trip not found")
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
        description=f"Combine {len(media_ids)} clips",
        payload={"media_ids": media_ids, "output_name": req.output_name, "album_id": req.album_id},
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
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "dismissed_at": j.dismissed_at.isoformat() if j.dismissed_at else None,
    }


def job_log_dict(row: JobLog) -> dict:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "level": row.level,
        "message": row.message,
        "progress": row.progress,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/jobs/overview")
def jobs_overview_endpoint(session: Session = Depends(get_session)):
    """Aggregate counts + overall progress across ALL jobs (not just a page)."""
    return jobs_overview(session)


@router.get("/jobs")
def list_jobs(
    limit: int = 100,
    include_dismissed: bool = False,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    def base():
        q = session.query(Job)
        return q if include_dismissed else q.filter(Job.dismissed_at.is_(None))

    # Running first, then paused, then queued, then everything else.
    status_rank = case(
        (Job.status == "running", 0), (Job.status == "paused", 1), (Job.status == "queued", 2), else_=3
    )

    if status:
        # Filtered view (clicking a summary box). Still lead with running so an
        # in-progress job is visible even inside a 'queued/running' filter.
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        rows = (
            base()
            .filter(Job.status.in_(wanted))
            .order_by(status_rank, Job.created_at.desc())
            .limit(limit)
            .all()
        )
        return [job_dict(j) for j in rows]

    # Default view: the worker runs the OLDEST queued job, so a plain
    # newest-first + limit hides the in-progress job behind a large backlog.
    # Surface active jobs first (in run order), then back-fill recent finished.
    terminal_reserve = 25
    active_limit = max(20, limit - terminal_reserve)
    active = (
        base()
        .filter(Job.status.in_(("running", "paused", "queued")))
        .order_by(status_rank, Job.created_at.asc())
        .limit(active_limit)
        .all()
    )
    terminal_room = max(0, limit - len(active))
    terminal = (
        base()
        .filter(Job.status.in_(("done", "error", "cancelled")))
        .order_by(Job.created_at.desc())
        .limit(terminal_room)
        .all()
        if terminal_room
        else []
    )
    return [job_dict(j) for j in active] + [job_dict(j) for j in terminal]


@router.get("/jobs/{job_id}/logs")
def list_job_logs(job_id: int, limit: int = 300, session: Session = Depends(get_session)):
    if not session.get(Job, job_id):
        raise HTTPException(404, "Job not found")
    rows = (
        session.query(JobLog)
        .filter(JobLog.job_id == job_id)
        .order_by(JobLog.created_at.desc(), JobLog.id.desc())
        .limit(max(1, min(limit, 1000)))
        .all()
    )
    return [job_log_dict(row) for row in rows]


@router.post("/jobs/pause_all")
def pause_all_jobs():
    return get_manager().pause_all()


@router.post("/jobs/resume_all")
def resume_all_jobs():
    return get_manager().resume_all()


@router.post("/jobs/stop_all")
def stop_all_jobs():
    return get_manager().stop_all()


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int):
    new_job_id = get_manager().retry(job_id)
    if new_job_id is None:
        raise HTTPException(404, "Job not found")
    return {"job_id": new_job_id}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    get_manager().request_cancel(job_id)
    return {"cancelled": job_id}


@router.post("/jobs/{job_id}/dismiss")
def dismiss_job(job_id: int):
    get_manager().dismiss(job_id)
    return {"dismissed": job_id}
