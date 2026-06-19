"""Concrete background job handlers, registered with the JobManager."""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Iterable, List, NamedTuple

from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .database import session_scope
from .destinations import get_backend
from .destinations.base import join_remote, render_remote_dir
from .ha import publish_state
from .jobs import JobContext, handler
from .media import (
    capture_time_or_mtime,
    checksum,
    classify,
    make_thumbnail,
    probe,
)
from .merge import merge_clips
from .models import AppSettings, Destination, MediaItem, UploadedClip, UploadState, utcnow
from .settings_store import get_app_settings
from .timestamps import set_file_mtime, shift_datetime, write_metadata_creation_time

log = logging.getLogger("drift.tasks")


def _thumb_path_for(media_id: int) -> Path:
    settings = get_settings()
    return settings.thumbnail_dir / f"{media_id}.jpg"


def import_one(session, path: Path, source: str, derived: bool = False) -> MediaItem:
    """Insert or fetch a MediaItem for a path, populating metadata."""
    path_str = str(path)
    existing = session.query(MediaItem).filter(MediaItem.path == path_str).first()
    if existing:
        _refresh_metadata_from_file(existing, path)
        return existing
    kind = classify(path) or "video"
    info = probe(path)
    cs = checksum(path)
    existing = session.query(MediaItem).filter(MediaItem.checksum == cs).first()
    if existing:
        _refresh_metadata_from_file(existing, path)
        return existing
    item = MediaItem(
        path=path_str,
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
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = (
            session.query(MediaItem)
            .filter((MediaItem.path == path_str) | (MediaItem.checksum == cs))
            .first()
        )
        if existing:
            return existing
        raise
    return item


def _refresh_metadata_from_file(item: MediaItem, path: Path) -> None:
    """Refresh capture metadata from the file without replacing user-only fields."""
    if not path.exists():
        return
    info = probe(path)
    try:
        mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        mtime = None
    metadata_time = info.get("capture_time")
    is_mtime_fallback = (
        item.capture_time is not None
        and mtime is not None
        and abs((item.capture_time - mtime).total_seconds()) < 2
    )
    if metadata_time and (item.capture_time is None or is_mtime_fallback):
        item.capture_time = metadata_time
    item.duration_s = item.duration_s if item.duration_s is not None else info["duration_s"]
    item.codec = item.codec or info["codec"]
    item.width = item.width or info["width"]
    item.height = item.height or info["height"]
    if mtime is not None:
        try:
            item.size_bytes = path.stat().st_size
        except OSError:
            pass


def _mark_upload_progress(
    mid: int,
    did: int,
    clip_id: int,
    sent: int,
    total: int,
    status: str = "uploading",
    error: str | None = None,
) -> None:
    with session_scope() as s:
        state = (
            s.query(UploadState)
            .filter(UploadState.media_id == mid, UploadState.destination_id == did)
            .first()
        )
        if state:
            state.status = status
            state.error = error
            state.bytes_uploaded = sent
            state.total_bytes = total
            state.updated_at = utcnow()
            if status == "done":
                state.uploaded_at = utcnow()
        clip = s.get(UploadedClip, clip_id)
        if clip:
            clip.status = status
            clip.bytes_uploaded = sent
            clip.size_bytes = total
            clip.last_error = error
            clip.updated_at = utcnow()


def _publish_upload_status(
    prefs: AppSettings,
    job_id: int,
    status: str,
    attributes: dict,
) -> None:
    progress_pct = attributes.get("progress_pct")
    state_value = progress_pct if isinstance(progress_pct, (int, float)) else status
    payload = dict(attributes)
    payload["status"] = status
    publish_state(prefs, f"job_{job_id}", state_value, payload)
    publish_state(prefs, "uploads", state_value, payload)


# --- import -----------------------------------------------------------------

@handler("import")
def handle_import(job_id: int, payload: dict, ctx: JobContext) -> None:
    """Import a set of files (by path) into the library."""
    # De-duplicate while preserving order: the same clip can be listed twice if
    # a device exposes nested/duplicate DCIM mounts.
    paths: List[str] = list(dict.fromkeys(payload.get("paths", [])))
    total = len(paths) or 1
    new_ids: List[int] = []
    ctx.log(f"Preparing to import {len(paths)} media files")
    for i, p in enumerate(paths):
        path = Path(p)
        if not path.exists():
            ctx.set_progress((i + 1) / total, f"Skipped missing file {path.name}")
            continue
        # Each file imports in its own transaction. A single failure (e.g. a
        # concurrent import job inserting the same path first, which trips the
        # UNIQUE constraint on media_items.path) must not abort the whole job.
        try:
            with session_scope() as s:
                item = import_one(s, path, source=payload.get("source", "device"))
                new_ids.append(item.id)
            ctx.set_progress((i + 1) / total, f"Imported {path.name}")
        except IntegrityError:
            # Already imported by a concurrent job / earlier run — reuse it.
            with session_scope() as s:
                existing = (
                    s.query(MediaItem).filter(MediaItem.path == str(path)).first()
                )
                if existing:
                    new_ids.append(existing.id)
            log.info("Skipped already-imported file %s", path)
            ctx.set_progress((i + 1) / total, f"Already imported {path.name}")
        except Exception:  # noqa: BLE001
            log.exception("Failed to import %s", path)
            ctx.set_progress((i + 1) / total, f"Failed {path.name}")
    new_ids = list(dict.fromkeys(new_ids))
    # Thumbnails as a follow-up so import returns fast.
    get_manager_enqueue("thumbnail", {"media_ids": new_ids})
    ctx.log(f"Queued thumbnails for {len(new_ids)} imported files", progress=1.0)
    # Optionally queue uploads (the "Upload Everything" flow).
    if payload.get("auto_upload"):
        dest_ids = payload.get("destination_ids")
        if payload.get("group_uploads_by_month"):
            enqueue_upload_jobs_by_month(new_ids, dest_ids, description_prefix="Auto-upload")
        else:
            enqueue_upload_jobs(new_ids, dest_ids, description_prefix="Auto-upload")


# --- thumbnail --------------------------------------------------------------

@handler("thumbnail")
def handle_thumbnail(job_id: int, payload: dict, ctx: JobContext) -> None:
    media_ids: List[int] = payload.get("media_ids", [])
    total = len(media_ids) or 1
    ctx.log(f"Preparing thumbnails for {len(media_ids)} media items")
    for i, mid in enumerate(media_ids):
        with session_scope() as s:
            item = s.get(MediaItem, mid)
            if not item or not Path(item.path).exists():
                ctx.set_progress((i + 1) / total, f"Skipped thumbnail for missing media {mid}")
                continue
            path = Path(item.path)
            kind = item.kind
        out = _thumb_path_for(mid)
        ctx.set_progress(i / total, f"Generating thumbnail for {path.name}")
        with ctx.ffmpeg_semaphore:
            ok = make_thumbnail(path, kind, out)
        if ok:
            with session_scope() as s:
                item = s.get(MediaItem, mid)
                if item:
                    item.thumbnail = str(out)
            ctx.set_progress((i + 1) / total, f"Generated thumbnail for {path.name}")
        else:
            ctx.set_progress((i + 1) / total, f"Thumbnail failed for {path.name}")


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
    ctx.log(f"Preparing upload batch for {len(media_ids)} media items")

    with session_scope() as s:
        prefs = get_app_settings(s)
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
    _publish_upload_status(
        prefs,
        job_id,
        "running",
        {"job_id": job_id, "total_items": total, "completed_items": 0, "progress_pct": 0},
    )

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
                ctx.set_progress(done / total, f"Skipped missing media {mid} for destination {did}")
                continue
            local_path_obj = Path(item.path)
            _refresh_metadata_from_file(item, local_path_obj)
            local_size = item.size_bytes or local_path_obj.stat().st_size
            if not item.checksum:
                item.checksum = checksum(local_path_obj)
            state = (
                s.query(UploadState)
                .filter(UploadState.media_id == mid, UploadState.destination_id == did)
                .first()
            )
            if state is None:
                state = UploadState(media_id=mid, destination_id=did)
                s.add(state)
            clip = (
                s.query(UploadedClip)
                .filter(UploadedClip.destination_id == did, UploadedClip.checksum == item.checksum)
                .first()
            )
            if clip is None:
                clip = UploadedClip(
                    destination_id=did,
                    source_media_id=mid,
                    checksum=item.checksum,
                    filename=item.filename,
                    size_bytes=local_size,
                    status="pending",
                )
                s.add(clip)
                s.flush()
            else:
                clip.source_media_id = mid
                clip.filename = item.filename
                clip.size_bytes = local_size
                clip.updated_at = utcnow()
            ledger_done = clip.status == "done" and bool(clip.remote_path)
            state.status = "uploading"
            state.error = None
            state.total_bytes = local_size
            state.updated_at = utcnow()
            clip.status = "uploading"
            clip.last_error = None
            local_path = item.path
            filename = item.filename
            checksum_value = item.checksum
            remote_dir = render_remote_dir(dest.path_template, item.capture_time)
            dest_detached = dest  # used only for read-only backend config below
            backend = get_backend(dest_detached)
            dest_name = dest.name
            clip_id = clip.id
            remote_hint = join_remote(dest.base_path or "/", remote_dir, filename)
        ctx.log(f"Resolved {filename} to {remote_hint}", progress=done / total)

        # 2) Perform the upload with no DB transaction held open.
        if ledger_done:
            ctx.set_progress(done / total, f"Verifying {filename} on {dest_name}")
            try:
                if backend.remote_file_matches(remote_dir, filename, local_size, checksum_value):
                    log.info(
                        "Verified existing upload for media=%s destination=%s remote=%s",
                        mid,
                        did,
                        remote_hint,
                    )
                    with session_scope() as s:
                        state = (
                            s.query(UploadState)
                            .filter(
                                UploadState.media_id == mid,
                                UploadState.destination_id == did,
                            )
                            .first()
                        )
                        clip = s.get(UploadedClip, clip_id)
                        if state:
                            state.status = "done"
                            state.remote_path = clip.remote_path if clip else remote_hint
                            state.error = None
                            state.bytes_uploaded = local_size
                            state.total_bytes = local_size
                            state.uploaded_at = state.uploaded_at or utcnow()
                            state.updated_at = utcnow()
                        if clip:
                            clip.status = "done"
                            clip.bytes_uploaded = local_size
                            clip.size_bytes = local_size
                            clip.last_error = None
                            clip.uploaded_at = clip.uploaded_at or utcnow()
                            clip.updated_at = utcnow()
                    done += 1
                    ctx.set_progress(done / total, f"Verified existing {filename}")
                    _publish_upload_status(
                        prefs,
                        job_id,
                        "running",
                        {
                            "job_id": job_id,
                            "current_file": filename,
                            "current_destination": dest_name,
                            "completed_items": done,
                            "total_items": total,
                            "progress_pct": round((done / total) * 100, 1),
                            "mode": "verified-deduplicated",
                        },
                    )
                    continue
                log.warning(
                    "Ledger row for media=%s destination=%s did not verify; re-uploading",
                    mid,
                    did,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "Remote verification failed before upload for media=%s destination=%s",
                    mid,
                    did,
                )
                with session_scope() as s:
                    clip = s.get(UploadedClip, clip_id)
                    if clip:
                        clip.status = "pending"
                        clip.last_error = f"Pre-upload verification failed: {exc}"
                        clip.updated_at = utcnow()

        start_offset = backend.get_resume_offset(remote_dir, filename, local_size)
        if start_offset:
            ctx.log(f"Resuming {filename} at {start_offset} of {local_size} bytes")
        else:
            ctx.log(f"Starting {filename} upload at 0 of {local_size} bytes")
        _mark_upload_progress(mid, did, clip_id, start_offset, local_size)
        last_persist = 0.0
        upload_started_at = time.monotonic()

        def on_progress(sent: int, total_bytes: int, _done=done):
            nonlocal last_persist
            frac = (sent / total_bytes) if total_bytes else 0
            ctx.set_progress((_done + frac) / total, f"Uploading {filename} -> {dest_name}")
            now = time.monotonic()
            if now - last_persist >= 0.75 or sent >= total_bytes:
                _mark_upload_progress(mid, did, clip_id, sent, total_bytes)
                _publish_upload_status(
                    prefs,
                    job_id,
                    "running",
                    {
                        "job_id": job_id,
                        "current_file": filename,
                        "current_destination": dest_name,
                        "checksum": checksum_value,
                        "bytes_uploaded": sent,
                        "total_bytes": total_bytes,
                        "completed_items": done,
                        "total_items": total,
                        "progress_pct": round(((_done + frac) / total) * 100, 1),
                    },
                )
                last_persist = now

        result_status = "done"
        result_remote = None
        result_error = None
        duration_s = None
        throughput_bps = None
        with ctx.upload_semaphore:
            try:
                log.info(
                    "Starting upload media=%s destination=%s file=%s bytes=%s offset=%s",
                    mid,
                    did,
                    filename,
                    local_size,
                    start_offset,
                )
                result_remote = backend.upload(
                    local_path,
                    remote_dir,
                    filename,
                    on_progress,
                    start_offset=start_offset,
                )
            except Exception as exc:  # noqa: BLE001
                result_status = "error"
                result_error = str(exc)[:2000]
                ctx.log(f"Upload failed for {filename}: {result_error}", level="ERROR")
                log.exception("Upload failed for media %s -> dest %s", mid, did)
        if result_status == "done":
            duration_s = max(0.0, time.monotonic() - upload_started_at)
            transferred_bytes = max(0, local_size - start_offset)
            if transferred_bytes > 0 and duration_s > 0:
                throughput_bps = transferred_bytes / duration_s
            try:
                if not backend.remote_file_matches(remote_dir, filename, local_size, checksum_value):
                    result_status = "error"
                    result_error = "Remote verification failed after upload"
                    ctx.log(f"Verification failed for {filename}", level="ERROR")
                    log.error(
                        "Remote verification failed after upload media=%s destination=%s remote=%s",
                        mid,
                        did,
                        result_remote,
                    )
                else:
                    ctx.log(f"Verified {filename} at {result_remote}")
                    log.info(
                        "Verified uploaded file media=%s destination=%s remote=%s",
                        mid,
                        did,
                        result_remote,
                    )
            except Exception as exc:  # noqa: BLE001
                result_status = "error"
                result_error = f"Remote verification failed after upload: {exc}"[:2000]
                ctx.log(f"Verification errored for {filename}: {exc}", level="ERROR")
                log.exception(
                    "Remote verification errored after upload media=%s destination=%s",
                    mid,
                    did,
                )

        # 3) Record the outcome in another short transaction.
        with session_scope() as s:
            state = (
                s.query(UploadState)
                .filter(UploadState.media_id == mid, UploadState.destination_id == did)
                .first()
            )
            clip = s.get(UploadedClip, clip_id)
            if state:
                state.status = result_status
                state.remote_path = result_remote
                state.error = result_error
                state.bytes_uploaded = local_size if result_status == "done" else state.bytes_uploaded
                state.total_bytes = local_size
                state.updated_at = utcnow()
                if result_status == "done":
                    state.uploaded_at = utcnow()
            if clip:
                clip.remote_path = result_remote
                clip.temp_remote_path = f"{remote_hint}.part"
                clip.status = result_status
                clip.size_bytes = local_size
                clip.bytes_uploaded = local_size if result_status == "done" else clip.bytes_uploaded
                clip.upload_duration_s = duration_s if result_status == "done" else clip.upload_duration_s
                clip.upload_throughput_bps = (
                    throughput_bps if result_status == "done" else clip.upload_throughput_bps
                )
                clip.uploaded_at = utcnow() if result_status == "done" else clip.uploaded_at
                clip.last_error = result_error
                clip.updated_at = utcnow()
        done += 1
        ctx.set_progress(done / total)
        _publish_upload_status(
            prefs,
            job_id,
            "running" if done < total else result_status,
            {
                "job_id": job_id,
                "current_file": filename,
                "current_destination": dest_name,
                "completed_items": done,
                "total_items": total,
                "progress_pct": round((done / total) * 100, 1),
                "last_result": result_status,
                "last_error": result_error,
            },
        )

    _publish_upload_status(
        prefs,
        job_id,
        "done",
        {"job_id": job_id, "total_items": total, "completed_items": done, "progress_pct": 100},
    )


# --- helper to enqueue follow-up jobs without a circular import -------------

def get_manager_enqueue(kind: str, payload: dict, description: str = "") -> int:
    from .jobs import get_manager

    return get_manager().enqueue(kind, description=description or kind, payload=payload)


class UploadPlanRow(NamedTuple):
    media_id: int
    filename: str
    capture_time: dt.datetime | None


def _month_label(capture_time: dt.datetime | None) -> str:
    if capture_time is None:
        return "Undated"
    return capture_time.strftime("%B %Y")


def _month_sort_key(row: UploadPlanRow) -> tuple[int, int, dt.datetime, str]:
    if row.capture_time is None:
        return (9999, 13, dt.datetime.max, row.filename.lower())
    return (
        row.capture_time.year,
        row.capture_time.month,
        row.capture_time,
        row.filename.lower(),
    )


def _month_upload_plan(rows: Iterable[UploadPlanRow]) -> list[UploadPlanRow]:
    return sorted(rows, key=_month_sort_key)


def _month_upload_groups(rows: Iterable[UploadPlanRow]) -> list[tuple[str, list[UploadPlanRow]]]:
    groups: list[tuple[str, list[UploadPlanRow]]] = []
    for row in _month_upload_plan(rows):
        label = _month_label(row.capture_time)
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(row)
    return groups


def enqueue_upload_jobs(
    media_ids: list[int],
    destination_ids: list[int] | None = None,
    description_prefix: str = "Upload",
) -> list[int]:
    media_ids = list(dict.fromkeys(media_ids))
    if not media_ids:
        return []
    with session_scope() as s:
        rows = (
            s.query(MediaItem.id, MediaItem.filename)
            .filter(MediaItem.id.in_(media_ids))
            .all()
        )
        names = {mid: filename for mid, filename in rows}
    job_ids = []
    for mid in media_ids:
        filename = names.get(mid, f"item {mid}")
        job_ids.append(
            get_manager_enqueue(
                "upload",
                {"media_ids": [mid], "destination_ids": destination_ids},
                description=f"{description_prefix} {filename}",
            )
        )
    return job_ids


def enqueue_upload_jobs_by_month(
    media_ids: list[int],
    destination_ids: list[int] | None = None,
    description_prefix: str = "Upload",
) -> list[int]:
    media_ids = list(dict.fromkeys(media_ids))
    if not media_ids:
        return []
    with session_scope() as s:
        rows = [
            UploadPlanRow(mid, filename, capture_time)
            for mid, filename, capture_time in (
                s.query(MediaItem.id, MediaItem.filename, MediaItem.capture_time)
                .filter(MediaItem.id.in_(media_ids))
                .all()
            )
        ]
    job_ids = []
    for month, month_rows in _month_upload_groups(rows):
        media_ids_for_month = [row.media_id for row in month_rows]
        count = len(month_rows)
        noun = "video" if count == 1 else "videos"
        job_ids.append(
            get_manager_enqueue(
                "upload",
                {"media_ids": media_ids_for_month, "destination_ids": destination_ids},
                description=f"{description_prefix} {month} ({count} {noun})",
            )
        )
    return job_ids
