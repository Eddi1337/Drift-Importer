"""NAS-only queue for offloaded ride films; the Pi never runs the encoder.

Requests contain relative archive paths and private GPS data. The worker only
reads originals and publishes derivatives under its own signature directory.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from zoneinfo import ZoneInfo

from .storage import require_storage

VERSION = 1
MAX_CLIPS = 1500
MAX_POINTS = 30000
MAX_DURATION_S = 72 * 3600
SIGNATURE = re.compile(r"[a-f0-9]{16,64}\Z")
TERMINAL = {"done", "error", "cancelled"}


def stamp(value):
    value = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Ride timestamps must include their timezone")
    return value.timestamp()


def iso(value=None):
    value = dt.datetime.now(dt.timezone.utc) if value is None else dt.datetime.fromtimestamp(value, dt.timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _relative_name(value: str) -> PurePosixPath:
    relative = PurePosixPath(str(value))
    if relative.is_absolute() or not relative.parts or any(p in {".", "..", ""} for p in relative.parts):
        raise ValueError("Ride sources must be relative NAS archive paths")
    if ":" in relative.parts[0] or relative.parts[0].startswith(".") or "\\" in str(value):
        raise ValueError("Ride source is outside the video archive")
    if relative.suffix.lower() not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts"}:
        raise ValueError("Ride source must be a video file")
    return relative


def relative_source(root: Path, value: str) -> Path:
    """Reject traversal, URLs, private generated content and escaping symlinks."""
    relative = _relative_name(value)
    resolved = (root / str(relative)).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("Ride source is missing or outside the NAS")
    if resolved.suffix.lower() not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts"}:
        raise ValueError("Ride source must be a video file")
    return resolved


def job_directory(root: Path, signature: str, *, create=False) -> Path:
    if not isinstance(signature, str) or not SIGNATURE.fullmatch(signature):
        raise ValueError("Invalid ride render signature")
    root = require_storage(Path(root))
    directory = root / ".drift" / "ride-renders" / signature
    # Existing symlinks in any private component must not redirect writes.
    for path in (root / ".drift", root / ".drift" / "ride-renders", directory):
        if path.is_symlink():
            raise ValueError("Ride render directory cannot be a symlink")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    return directory


def atomic_json(path: Path, value):
    if path.is_symlink():
        raise ValueError("Ride state cannot be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, separators=(",", ":"), allow_nan=False)
            stream.flush()
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path, limit=16 * 1024 * 1024):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("Invalid ride render document")
    with path.open() as stream:
        return json.load(stream)


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_manifest(root: Path, signature: str, clips, time_series, *, timezone="Europe/London", title="Ride", max_gap_s=900, verify_sources=True):
    """Create immutable, bounded input metadata without copying any videos."""
    root = require_storage(Path(root))
    job_directory(root, signature)
    ZoneInfo(timezone)
    if not 0 < len(clips) <= MAX_CLIPS or len(time_series) > MAX_POINTS:
        raise ValueError("Ride render exceeds its clip or GPS point limit")
    result, total = [], 0.0
    previous = None
    for number, clip in enumerate(clips, 1):
        path = relative_source(root, clip["path"]) if verify_sources else root / str(_relative_name(clip["path"]))
        duration = float(clip.get("duration_s") or 0)
        start = stamp(clip["start_time"])
        if not math.isfinite(duration) or not 0 < duration <= 12 * 3600:
            raise ValueError("Ride clips need a known, positive duration")
        if previous is not None and start < previous:
            raise ValueError("Ride clips must be in recording order")
        previous = start
        total += duration
        if verify_sources:
            stat = path.stat()
            size_bytes, mtime_ns = stat.st_size, stat.st_mtime_ns
            if clip.get("size_bytes") is not None and int(clip["size_bytes"]) != size_bytes:
                raise ValueError("A ride source changed; refresh the ride first")
        else:
            # Archive inventory supplies these fingerprints without expensive
            # per-source NFS stats on the Pi. Worker always verifies them live.
            size_bytes, mtime_ns = int(clip.get("size_bytes") or 0), int(clip.get("mtime_ns") or 0)
            if size_bytes <= 0 or mtime_ns <= 0:
                raise ValueError("Indexed ride sources need their size and modification fingerprint")
        result.append({"id": str(clip.get("id", number)), "path": str(path.relative_to(root)),
            "duration_s": duration, "start_time": iso(start), "end_time": iso(start + duration),
            "size_bytes": size_bytes, "mtime_ns": mtime_ns})
    if total > MAX_DURATION_S:
        raise ValueError("One ride film cannot exceed 72 hours of recorded footage")
    points = []
    for point in time_series:
        latitude, longitude = float(point["latitude"]), float(point["longitude"])
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Invalid ride GPS position")
        points.append({"time": iso(stamp(point["time"])), "latitude": latitude, "longitude": longitude,
            "segment": str(point.get("segment", 0))[:128]})
    points.sort(key=lambda point: stamp(point["time"]))
    manifest = {"version": VERSION, "signature": signature, "title": str(title)[:256], "timezone": timezone,
        "clips": result, "time_series": points, "duration_s": total, "max_gap_s": min(900, max(1, float(max_gap_s)))}
    manifest["request_hash"] = _digest(manifest)
    return manifest


def validate_manifest(root: Path, manifest):
    if manifest.get("version") != VERSION:
        raise ValueError("Unsupported ride render version")
    expected = manifest.get("request_hash")
    body = {key: value for key, value in manifest.items() if key != "request_hash"}
    if expected != _digest(body):
        raise ValueError("Ride render request changed")
    rebuilt = build_manifest(root, manifest["signature"], manifest["clips"], manifest["time_series"],
        timezone=manifest["timezone"], title=manifest["title"], max_gap_s=manifest["max_gap_s"])
    if rebuilt["request_hash"] != expected:
        raise ValueError("A ride source changed since this render was queued")
    return manifest


def get_render_state(root: Path, signature: str):
    directory = job_directory(root, signature)
    path = directory / "state.json"
    if not path.exists():
        return {"status": "not_queued", "signature": signature, "progress": 0}
    state = _read_json(path, 128 * 1024)
    if state.get("signature") != signature:
        raise ValueError("Ride render state does not match this ride")
    return state


def queue_render(root: Path, signature: str, clips, time_series, *, timezone="Europe/London", title="Ride", max_gap_s=900, verify_sources=True):
    manifest = build_manifest(root, signature, clips, time_series, timezone=timezone, title=title, max_gap_s=max_gap_s, verify_sources=verify_sources)
    directory = job_directory(root, signature, create=True)
    existing = directory / "request.json"
    if existing.exists():
        if _read_json(existing).get("request_hash") != manifest["request_hash"]:
            raise ValueError("This ride changed; refresh it before requesting a new film")
        state = get_render_state(root, signature)
        if state["status"] not in {"error", "cancelled", "not_queued"}:
            return state
    (directory / "cancel").unlink(missing_ok=True)
    state = {"signature": signature, "status": "queued", "progress": 0, "clip_count": len(clips),
        "duration_s": manifest["duration_s"], "queued_at": iso(), "updated_at": iso(), "message": "Waiting for the offloaded video worker"}
    atomic_json(directory / "state.json", state)
    # Request is published last: worker never observes an incomplete queue item.
    atomic_json(existing, manifest)
    return state


def cancel_render(root: Path, signature: str):
    directory = job_directory(root, signature)
    state = get_render_state(root, signature)
    if state["status"] in {"queued", "rendering"}:
        atomic_json(directory / "cancel", {"at": iso()})
        if state["status"] == "queued":
            state.update(status="cancelled", message="Cancelled", updated_at=iso())
            atomic_json(directory / "state.json", state)
    return state
