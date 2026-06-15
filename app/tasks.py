"""Concrete background job handlers, registered with the JobManager."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import List

from .config import get_settings
from .database import session_scope
from .destinations import get_backend
from .destinations.base import render_remote_dir
from .jobs import JobContext, handler
from .media import (
    capture_time_or_mtime,
    checksum,
    classify,
    make_thumbnail,
    probe,
)
from .merge import merge_clips
from .models import Destination, MediaItem, UploadState, utcnow
from .timestamps import set_file_mtime, shift_datetime, write_metadata_creation_time

log = logging.getLogger("drift.tasks")


def _thumb_path_for(media_id: int) -> Path:
    settings = get_settings()
    return settings.thumbnail_dir / f"{media_id}.jpg"


def import_one(session, path: Path, source: str, derived: bool = False) -> MediaItem:
    """Insert or fetch a MediaItem for a path, populating metadata."""
    existing = session.query(MediaItem).filter(MediaItem.path == str(path)).first()
    if existing:
        return existing
    kind = classify(path) or "video"
    info = probe(path)
    cs = checksum(path)
    item = MediaItem(
        path=str(path),
        filename=path.name,
        kind=kind,
        size_bytes=path.stat().st_size,
        duration_s=info["duration_s"],
        codec=info["codec"],
        width=info["width"],
        height=info["height"],
        capture_time=capture_time_or_mtime(path, info),
        checksum=cs,
        source=source,
        derived=derived,
    )
    session.add(item)
    session.flush()
    return item


# --- import -----------------------------------------------------------------

@handler("import")
def handle_import(job_id: int, payload: dict, ctx: JobContext) -> None:
    """Import a set of files (by path) into the library."""
    paths: List[str] = payload.get("paths", [])
    total = len(paths) or 1
    new_ids: List[int] = []
    for i, p in enumerate(paths):
        path = Path(p)
        if not path.exists():
            continue
        with session_scope() as s:
            item = import_one(s, path, source=payload.get("source", "device"))
            new_ids.append(item.id)
        ctx.set_progress((i + 1) / total, f"Imported {path.name}")
    # Thumbnails as a follow-up so import returns fast.
    get_manager_enqueue("thumbnail", {"media_ids": new_ids})
    # Optionally queue uploads (the "Upload Everything" flow).
    if payload.get("auto_upload"):
        dest_ids = payload.get("destination_ids")
        get_manager_enqueue(
            "upload",
            {"media_ids": new_ids, "destination_ids": dest_ids},
            description="Auto-upload imported clips",
        )


# --- thumbnail --------------------------------------------------------------

@handler("thumbnail")
def handle_thumbnail(job_id: int, payload: dict, ctx: JobContext) -> None:
    media_ids: List[int] = payload.get("media_ids", [])
    total = len(media_ids) or 1
    for i, mid in enumerate(media_ids):
        with session_scope() as s:
            item = s.get(MediaItem, mid)
            if not item or not Path(item.path).exists():
                continue
            path = Path(item.path)
            kind = item.kind
        out = _thumb_path_for(mid)
        with ctx.ffmpeg_semaphore:
            ok = make_thumbnail(path, kind, out)
        if ok:
            with session_scope() as s:
                item = s.get(MediaItem, mid)
                if item:
                    item.thumbnail = str(out)
        ctx.set_progress((i + 1) / total)


# --- timestamp --------------------------------------------------------------

@handler("timestamp")
def handle_timestamp(job_id: int, payload: dict, ctx: JobContext) -> None:
    """Set or shift capture timestamps for a batch of media items.

    payload: media_ids, mode ('set'|'shift'), and either absolute (iso string)
    or delta (days/hours/minutes/seconds). write_metadata defaults True.
    """
    media_ids: List[int] = payload.get("media_ids", [])
    mode = payload.get("mode", "shift")
    write_meta = payload.get("write_metadata", True)
    total = len(media_ids) or 1
    for i, mid in enumerate(media_ids):
        with session_scope() as s:
            item = s.get(MediaItem, mid)
            if not item:
                continue
            current = item.capture_time or utcnow().replace(tzinfo=None)
            if mode == "set":
                new_time = dt.datetime.fromisoformat(payload["absolute"])
            else:
                new_time = shift_datetime(
                    current,
                    days=payload.get("days", 0),
                    hours=payload.get("hours", 0),
                    minutes=payload.get("minutes", 0),
                    seconds=payload.get("seconds", 0),
                )
            item.capture_time = new_time
            path = Path(item.path)
            kind = item.kind
        if path.exists():
            set_file_mtime(path, new_time)
            if write_meta and kind == "video":
                with ctx.ffmpeg_semaphore:
                    write_metadata_creation_time(path, new_time)
        ctx.set_progress((i + 1) / total, f"Updated {path.name}")


# --- merge ------------------------------------------------------------------

@handler("merge")
def handle_merge(job_id: int, payload: dict, ctx: JobContext) -> None:
    """Merge ordered clips into one. payload: media_ids (ordered), output_name."""
    settings = get_settings()
    media_ids: List[int] = payload.get("media_ids", [])
    with session_scope() as s:
        items = [s.get(MediaItem, mid) for mid in media_ids]
        items = [it for it in items if it]
        paths = [Path(it.path) for it in items]
        first_capture = items[0].capture_time if items else None
    name = payload.get("output_name") or f"merged_{int(utcnow().timestamp())}.mp4"
    output = settings.working_dir / name
    ctx.set_progress(0.05, f"Merging {len(paths)} clips")
    with ctx.ffmpeg_semaphore:
        merge_clips(paths, output)
    ctx.set_progress(0.9, "Indexing merged clip")
    with session_scope() as s:
        item = import_one(s, output, source="library", derived=True)
        if item.capture_time is None and first_capture:
            item.capture_time = first_capture
        new_id = item.id
    get_manager_enqueue("thumbnail", {"media_ids": [new_id]})


# --- upload -----------------------------------------------------------------

@handler("upload")
def handle_upload(job_id: int, payload: dict, ctx: JobContext) -> None:
    """Upload media items to one or more destinations.

    payload: media_ids, destination_ids (None -> all default destinations).
    Per-(media, destination) status is tracked in UploadState so a clip can go
    to several targets and partial failures are recoverable.
    """
    media_ids: List[int] = payload.get("media_ids", [])
    dest_ids = payload.get("destination_ids")

    with session_scope() as s:
        if dest_ids:
            dests = s.query(Destination).filter(Destination.id.in_(dest_ids)).all()
        else:
            dests = (
                s.query(Destination)
                .filter(Destination.is_default == True, Destination.enabled == True)  # noqa: E712
                .all()
            )
        dest_meta = [(d.id, d.name) for d in dests]

    if not dest_meta:
        raise RuntimeError("No destinations selected (and no default configured).")

    units = [(mid, did) for mid in media_ids for did, _ in dest_meta]
    total = len(units) or 1
    done = 0

    for mid, did in units:
        # 1) Read what we need and mark "uploading" in a short transaction, then
        #    release it. The long network transfer must NOT hold a write lock,
        #    or the progress callbacks (which write Job rows) would deadlock the
        #    single-writer SQLite database.
        with session_scope() as s:
            item = s.get(MediaItem, mid)
            dest = s.get(Destination, did)
            if not item or not dest or not Path(item.path).exists():
                done += 1
                ctx.set_progress(done / total)
                continue
            state = (
                s.query(UploadState)
                .filter(UploadState.media_id == mid, UploadState.destination_id == did)
                .first()
            )
            if state is None:
                state = UploadState(media_id=mid, destination_id=did)
                s.add(state)
            if state.status == "done":
                done += 1
                ctx.set_progress(done / total)
                continue
            state.status = "uploading"
            state.error = None
            local_path = item.path
            filename = item.filename
            remote_dir = render_remote_dir(dest.path_template, item.capture_time)
            dest_detached = dest  # used only for read-only backend config below
            backend = get_backend(dest_detached)
            dest_name = dest.name

        # 2) Perform the upload with no DB transaction held open.
        def on_progress(frac: float, _done=done):
            ctx.set_progress((_done + frac) / total, f"Uploading {filename} -> {dest_name}")

        result_status = "done"
        result_remote = None
        result_error = None
        with ctx.upload_semaphore:
            try:
                result_remote = backend.upload(local_path, remote_dir, filename, on_progress)
            except Exception as exc:  # noqa: BLE001
                result_status = "error"
                result_error = str(exc)[:2000]
                log.exception("Upload failed for media %s -> dest %s", mid, did)

        # 3) Record the outcome in another short transaction.
        with session_scope() as s:
            state = (
                s.query(UploadState)
                .filter(UploadState.media_id == mid, UploadState.destination_id == did)
                .first()
            )
            if state:
                state.status = result_status
                state.remote_path = result_remote
                state.error = result_error
                if result_status == "done":
                    state.uploaded_at = utcnow()
        done += 1
        ctx.set_progress(done / total)


# --- helper to enqueue follow-up jobs without a circular import -------------

def get_manager_enqueue(kind: str, payload: dict, description: str = "") -> int:
    from .jobs import get_manager

    return get_manager().enqueue(kind, description=description or kind, payload=payload)
