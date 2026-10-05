"""Camera offload verification and recording-day trips without local video copies."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

from .database import session_scope
from .devicescan import get_device_monitor
from .jobs import ACTIVE_STATES, JobCancelled, get_manager, handler
from .media import checksum, classify
from .merge import merge_clips
from .models import Album, AlbumItem, Destination, Job, MediaItem, TripMovie, UploadedClip, UploadState, utcnow
from .storage import archive_path, require_storage

enqueue_lock = threading.Lock()


def camera_roots() -> list[Path]:
    roots = sorted({Path(d["path"]).resolve() for d in get_device_monitor().get_devices()}, key=lambda p: len(p.parts))
    result = []
    for root in roots:
        if not any(root.is_relative_to(parent) for parent in result):
            result.append(root)
    return result


def camera_root(value: str) -> Path:
    requested = Path(value).resolve()
    if requested not in camera_roots() or not requested.is_dir():
        raise ValueError("Choose a currently connected camera")
    return requested


def scan_videos(root: Path) -> list[Path]:
    if not root.is_dir():
        raise RuntimeError("Camera is no longer connected")
    def fail(error):
        raise error
    result = []
    for folder, dirs, names in os.walk(root, onerror=fail, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(folder) / name).is_symlink()]
        for name in names:
            path = Path(folder) / name
            if classify(path) == "video":
                if path.is_symlink():
                    raise RuntimeError(f"Cannot verify camera symlink: {path}")
                result.append(path)
    return sorted(result)


def nas_destination(session, destination_id: int) -> Destination:
    dest = session.get(Destination, destination_id)
    if not dest or not dest.enabled or dest.type not in ("local", "nfs", "smb"):
        raise ValueError("Choose an enabled destination on the mounted NAS")
    return dest


def archived_copy(session, item: MediaItem, destination_id: int) -> Path | None:
    return archived_copies(session, [item], destination_id).get(item.id)


def archived_copies(session, items, destination_id: int) -> dict[int, Path]:
    """Read the ledger once, without loading ORM relationship trees per clip."""
    dest = nas_destination(session, destination_id)
    copies = dict(session.query(UploadedClip.checksum, UploadedClip.remote_path).filter(
        UploadedClip.destination_id == destination_id, UploadedClip.status == "done",
        UploadedClip.full_verification_failed.is_(False),
    ).all())
    available = {}
    for item in items:
        path = archive_path(Path(dest.base_path), copies.get(item.checksum))
        try:
            if path and path.is_file() and path.stat().st_size == item.size_bytes:
                available[item.id] = path
        except OSError:
            continue
    return available


def recording_days(session) -> dict[str, list[MediaItem]]:
    from .date_review import local, approved_paths
    days = {}
    items = session.query(MediaItem).filter(
        MediaItem.kind == "video", MediaItem.derived.is_(False), MediaItem.capture_time.is_not(None)
    ).order_by(MediaItem.capture_time, MediaItem.filename, MediaItem.id).all()
    ready = {str(p) for p in approved_paths(session, [Path(m.path) for m in items if m.source == "device"])}
    for item in items:
        if item.source == "device" and item.path not in ready:
            continue
        days.setdefault(local(item.capture_time).date().isoformat(), []).append(item)
    return days


def manifest(items, destination_id: int) -> str:
    data = [(m.id, m.checksum, m.size_bytes, m.capture_time.isoformat()) for m in items]
    return hashlib.sha256(json.dumps([destination_id, data]).encode()).hexdigest()


def trip_suggestions(session, destination_id: int) -> list[dict]:
    dest = nas_destination(session, destination_id)
    storage_error = None
    try:
        require_storage(Path(dest.base_path))
    except (OSError, RuntimeError) as exc:
        storage_error = str(exc)
    movies = {m.signature: m for m in session.query(TripMovie).filter_by(destination_id=destination_id)}
    jobs = {}
    for job in session.query(Job).filter(Job.kind == "trip").order_by(Job.id):
        jobs[json.loads(job.payload or "{}").get("signature")] = job
    result = []
    days = recording_days(session)
    available_ids = {} if storage_error else archived_copies(session, [m for items in days.values() for m in items], destination_id)
    for day, items in reversed(list(days.items())):
        signature = manifest(items, destination_id)
        movie = movies.get(signature)
        output = session.get(MediaItem, movie.media_id) if movie else None
        available = sum(item.id in available_ids for item in items)
        completed = bool(output and not storage_error and Path(output.path).is_file())
        job = jobs.get(signature)
        result.append({
            "day": day, "clip_count": len(items), "duration_s": sum(m.duration_s or 0 for m in items),
            "size_bytes": sum(m.size_bytes for m in items), "archived_count": available,
            "ready": not storage_error and available == len(items),
            "status": "complete" if completed else (job.status if job and job.status in ACTIVE_STATES else "suggested"),
            "job_id": job.id if job else None, "error": storage_error or (job.error if job and job.status == "error" else None),
            "movie_id": output.id if completed else None,
        })
    return result


def queue_trip(session, days: list[str], destination_id: int, name: str = "") -> dict:
    dest = nas_destination(session, destination_id)
    root = require_storage(Path(dest.base_path))
    all_days = recording_days(session)
    days = sorted(set(days))
    if not days or any(day not in all_days for day in days):
        raise ValueError("Choose recording days from the suggestions")
    items = [item for day in days for item in all_days[day]]
    if len(items) < 2:
        raise ValueError("A trip needs at least two clips")
    available = archived_copies(session, items, destination_id)
    missing = [m.filename for m in items if m.id not in available]
    if missing:
        raise ValueError(f"Import to the NAS first: {len(missing)} original clips are unavailable")
    signature = manifest(items, destination_id)
    with enqueue_lock:
        for job in session.query(Job).filter(Job.kind == "trip", Job.status.in_(ACTIVE_STATES)):
            if json.loads(job.payload or "{}").get("signature") == signature:
                return {"job_id": job.id, "already_queued": True}
        title = name.strip()[:200] or (f"Day {days[0]}" if len(days) == 1 else f"Trip {days[0]} to {days[-1]}")
        album = session.query(Album).filter_by(name=title).first()
        if album is None:
            album = Album(name=title, description="Recording days: " + ", ".join(days))
            session.add(album)
            session.flush()
        for entry in list(album.items):
            session.delete(entry)
        session.flush()
        for position, item in enumerate(items):
            session.add(AlbumItem(album_id=album.id, media_id=item.id, position=position))
        session.commit()
        job_id = get_manager().enqueue("trip", description=f"Create {title} · {len(items)} clips", payload={
            "media_ids": [m.id for m in items], "destination_id": destination_id,
            "signature": signature, "days": days, "album_id": album.id, "root": str(root),
        })
        return {"job_id": job_id, "album_id": album.id}


def _fingerprint(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _full_hash(path: Path, ctx) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            if ctx.is_cancelled():
                raise JobCancelled()
            digest.update(block)
    return digest.hexdigest()


@handler("verify")
def handle_verify(job_id: int, payload: dict, ctx) -> None:
    with ctx.upload_semaphore:
        root = camera_root(payload["camera_root"])
        with session_scope() as session:
            dest = nas_destination(session, payload["destination_id"])
            nas = require_storage(Path(dest.base_path))
        paths = scan_videos(root)
        if not paths:
            raise RuntimeError("No camera video files found; nothing has been verified")
        before = {str(p): _fingerprint(p) for p in paths}
        rows = []
        for index, path in enumerate(paths):
            ctx.set_progress(index / len(paths), f"Verifying {index + 1}/{len(paths)}: {path.name}")
            row = {"filename": str(path.relative_to(root)), "status": "missing", "error": None}
            try:
                require_storage(nas)
                sampled = checksum(path)
                with session_scope() as session:
                    ledger = session.query(UploadedClip).filter_by(
                        destination_id=dest.id, checksum=sampled, status="done"
                    ).first()
                    archived = archive_path(nas, ledger.remote_path if ledger else None)
                if archived and archived.is_file():
                    archived_before = _fingerprint(archived)
                    if archived_before[0] != before[str(path)][0]:
                        row["status"] = "mismatch"
                    elif _full_hash(path, ctx) != _full_hash(archived, ctx):
                        row["status"] = "mismatch"
                    else:
                        row["status"] = "verified"
                    if _fingerprint(path) != before[str(path)] or _fingerprint(archived) != archived_before:
                        row.update(status="error", error="File changed during verification; run Verify again")
                else:
                    row["error"] = "No completed NAS copy found. Import this video first."
                if row["status"] in ("missing", "mismatch"):
                    # Make Import repair the failed copy rather than trusting a
                    # historical done ledger or the fast sampled hash again.
                    with session_scope() as session:
                        failed = session.query(UploadedClip).filter_by(destination_id=dest.id, checksum=sampled).first()
                        if failed:
                            failed.status = "error"
                            failed.full_verification_failed = True
                            failed.last_error = "Full verification failed: " + row["status"]
                            for state in session.query(UploadState).join(MediaItem).filter(
                                UploadState.destination_id == dest.id, MediaItem.checksum == sampled
                            ):
                                state.status = "error"
                                state.error = failed.last_error
            except (OSError, RuntimeError) as exc:
                row.update(status="error", error=str(exc))
            rows.append(row)
        # A partial scan or a card disconnect must never produce an all-clear.
        after = {str(p): _fingerprint(p) for p in scan_videos(camera_root(str(root)))}
        if before != after:
            raise RuntimeError("Camera contents changed during verification; run Verify again")
        require_storage(nas)
        verified = sum(row["status"] == "verified" for row in rows)
        report = {"ok": verified == len(rows), "total": len(rows), "verified": verified,
                  "missing": sum(r["status"] == "missing" for r in rows),
                  "mismatch": sum(r["status"] == "mismatch" for r in rows),
                  "errors": sum(r["status"] == "error" for r in rows), "files": rows,
                  "camera_root": str(root), "destination_id": dest.id,
                  "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "method": "full SHA-256"}
        with session_scope() as session:
            session.get(Job, job_id).result = json.dumps(report)
        ctx.set_progress(1, f"{verified}/{len(rows)} videos verified" + (" — all NAS copies match" if report["ok"] else " — action needed"))


@handler("trip")
def handle_trip(job_id: int, payload: dict, ctx) -> None:
    # One ffmpeg process and one sequential NAS reader/writer on this small Pi.
    with ctx.ffmpeg_semaphore, ctx.upload_semaphore:
        with session_scope() as session:
            dest = nas_destination(session, payload["destination_id"])
            root = require_storage(Path(dest.base_path))
            items = [session.get(MediaItem, mid) for mid in payload["media_ids"]]
            if any(item is None for item in items) or manifest(items, dest.id) != payload["signature"]:
                raise RuntimeError("Trip inputs changed; refresh recording days and create again")
            available = archived_copies(session, items, dest.id)
            paths = [available.get(item.id) for item in items]
            if any(path is None for path in paths):
                raise RuntimeError("An original NAS clip is missing or incomplete. Import again first.")
            expected = [(m.size_bytes, m.checksum) for m in items]
            expected_duration = sum(m.duration_s or 0 for m in items)
            first_capture = items[0].capture_time
            required = sum(m.size_bytes for m in items) + 64 * 1024 * 1024
        if shutil.disk_usage(root).free < required:
            raise RuntimeError("The NAS does not have enough free space for this trip")
        tmp_root = root / ".drift" / "tmp"
        if not tmp_root.resolve().is_relative_to(root):
            raise RuntimeError("NAS temporary directory resolves outside the destination")
        tmp_root.mkdir(parents=True, exist_ok=True)
        output_dir = root / "Trips" / payload["days"][0][:4]
        if not output_dir.resolve().is_relative_to(root):
            raise RuntimeError("Trips directory resolves outside the destination")
        output_dir.mkdir(parents=True, exist_ok=True)
        # Unique immutable output names preserve previous movies if more clips
        # arrive later. Only a complete, probed movie is atomically published.
        label = "_to_".join([payload["days"][0], payload["days"][-1]]) if len(payload["days"]) > 1 else payload["days"][0]
        output = output_dir / f"trip_{label}_{payload['signature'][:12]}.mp4"
        before = [_fingerprint(path) for path in paths]
        for path, (size, digest) in zip(paths, expected):
            if path.stat().st_size != size or checksum(path) != digest:
                raise RuntimeError(f"Archived clip does not match the import: {path.name}")
        with tempfile.TemporaryDirectory(prefix="trip-", dir=tmp_root) as scratch:
            partial = Path(scratch) / "movie.mp4"
            merge_clips(paths, partial, creation_time=first_capture, progress=lambda p: ctx.set_progress(p * .9, f"Combining trip: {round(p * 100)}%"))
            ctx.set_progress(.92, "Checking and publishing trip to NAS")
            require_storage(root)
            if [_fingerprint(path) for path in paths] != before:
                raise RuntimeError("A source clip changed during the merge; movie was not published")
            from .media import probe
            info = probe(partial)
            if not info.get("codec") or not info.get("duration_s"):
                raise RuntimeError("Merged movie could not be validated")
            if expected_duration and abs(info["duration_s"] - expected_duration) > max(2, len(paths) * .5):
                raise RuntimeError("Merged movie duration does not match its input clips")
            with partial.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(partial, output)
        from .tasks import import_one, get_manager_enqueue
        with session_scope() as session:
            movie = import_one(session, output, source="library", derived=True)
            movie.capture_time = first_capture
            movie_id = movie.id
            ledger = session.query(UploadedClip).filter_by(destination_id=dest.id, checksum=movie.checksum).first()
            if ledger is None:
                ledger = UploadedClip(destination_id=dest.id, checksum=movie.checksum, filename=movie.filename)
                session.add(ledger)
            ledger.source_media_id = movie_id
            ledger.remote_path = str(output)
            ledger.size_bytes = ledger.bytes_uploaded = movie.size_bytes
            ledger.status = "done"
            ledger.last_error = None
            ledger.full_verification_failed = False
            ledger.uploaded_at = utcnow()
            state = session.query(UploadState).filter_by(media_id=movie_id, destination_id=dest.id).first()
            if state is None:
                state = UploadState(media_id=movie_id, destination_id=dest.id)
                session.add(state)
            state.status, state.remote_path = "done", str(output)
            state.bytes_uploaded = state.total_bytes = movie.size_bytes
            state.uploaded_at = utcnow()
            entry = session.query(TripMovie).filter_by(signature=payload["signature"]).first()
            if entry is None:
                session.add(TripMovie(signature=payload["signature"], album_id=payload["album_id"],
                                      media_id=movie_id, destination_id=dest.id, days=json.dumps(payload["days"])))
            else:
                entry.media_id = movie_id
            album = session.get(Album, payload["album_id"])
            if album and all(i.media_id != movie_id for i in album.items):
                session.add(AlbumItem(album_id=album.id, media_id=movie_id, position=len(album.items)))
            session.get(Job, job_id).result = json.dumps({"media_id": movie_id, "path": str(output), "days": payload["days"]})
        get_manager_enqueue("thumbnail", {"media_ids": [movie_id]})
        ctx.set_progress(1, "Trip saved to " + str(output))
