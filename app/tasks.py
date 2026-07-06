"""Concrete background job handlers, registered with the JobManager."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Iterable, List, NamedTuple

from sqlalchemy.exc import IntegrityError

from .config import get_settings
from .database import session_scope
from .destinations import get_backend
from .destinations.base import join_remote, render_remote_dir
from .devices import scan_media_files
from .jobs import JobContext, handler
from .media import (
    capture_time_or_mtime,
    checksum,
    classify,
    make_thumbnail,
    probe,
)
from .merge import merge_clips
from .models import Album, AlbumItem, Destination, Job, MediaItem, UploadedClip, UploadState, utcnow
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
        if _needs_metadata_refresh(existing, path):
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


def _needs_metadata_refresh(item: MediaItem, path: Path) -> bool:
    """Whether a known file actually needs a (slow) ffprobe re-inspection.

    Camera files are immutable: if the size on disk still matches the indexed
    size and the probe-derived fields were captured, re-probing can't learn
    anything new. Skipping it keeps a card re-import (every camera reconnect)
    down to one stat per clip instead of one ffprobe subprocess per clip.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return True
    if item.size_bytes != size:
        return True
    if item.kind == "video":
        return item.duration_s is None or item.codec is None
    return item.width is None


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


# --- import -----------------------------------------------------------------

@handler("import")
def handle_import(job_id: int, payload: dict, ctx: JobContext) -> None:
    """Index a set of files (by path) into the library."""
    # De-duplicate while preserving order: the same clip can be listed twice if
    # a device exposes nested/duplicate DCIM mounts.
    paths: List[str] = list(dict.fromkeys(payload.get("paths", [])))
    total = len(paths) or 1
    auto_upload = bool(payload.get("auto_upload"))
    group_by_month = bool(payload.get("group_uploads_by_month"))
    dest_ids = payload.get("destination_ids")
    # New files are tracked separately from already-known ones: a fresh clip's
    # upload is enqueued the moment it's indexed (so transfers start while the
    # rest of the card is still being scanned), and re-verification uploads of
    # old clips queue strictly after all the new footage.
    new_ids: List[int] = []
    known_ids: List[int] = []
    seen_ids: set[int] = set()
    ctx.log(f"Preparing to index and fingerprint {len(paths)} media files")
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
                was_known = (
                    s.query(MediaItem.id).filter(MediaItem.path == str(path)).first()
                    is not None
                )
                item = import_one(s, path, source=payload.get("source", "device"))
                item_id = item.id
            if item_id in seen_ids:
                ctx.set_progress((i + 1) / total, f"Already indexed {path.name}")
                continue
            seen_ids.add(item_id)
            (known_ids if was_known else new_ids).append(item_id)
            if auto_upload and not group_by_month and not was_known:
                enqueue_upload_jobs([item_id], dest_ids, description_prefix="Auto-upload")
            ctx.set_progress((i + 1) / total, f"Indexed {path.name}")
        except IntegrityError:
            # Already imported by a concurrent job / earlier run — reuse it.
            with session_scope() as s:
                existing = (
                    s.query(MediaItem).filter(MediaItem.path == str(path)).first()
                )
                if existing and existing.id not in seen_ids:
                    seen_ids.add(existing.id)
                    known_ids.append(existing.id)
            log.info("Skipped already-indexed file %s", path)
            ctx.set_progress((i + 1) / total, f"Already indexed {path.name}")
        except Exception:  # noqa: BLE001
            log.exception("Failed to index %s", path)
            ctx.set_progress((i + 1) / total, f"Failed {path.name}")
    all_ids = new_ids + known_ids
    # Thumbnails as a follow-up so import returns fast. Only queue the items that
    # don't already have a thumbnail on disk: a device re-import (e.g. the camera
    # was reconnected) would otherwise regenerate every thumbnail, burning ffmpeg
    # time and starving uploads.
    thumb_ids = _media_needing_thumbnails(all_ids)
    if thumb_ids:
        get_manager_enqueue("thumbnail", {"media_ids": thumb_ids})
    ctx.log(
        f"Queued thumbnails for {len(thumb_ids)} of {len(all_ids)} indexed files",
        progress=1.0,
    )
    # Optionally queue uploads (the "Upload Everything" flow). New clips were
    # already enqueued inline above; only the month-grouped flow batches here.
    if auto_upload:
        if group_by_month:
            enqueue_upload_jobs_by_month(all_ids, dest_ids, description_prefix="Auto-upload")
        else:
            enqueue_upload_jobs(known_ids, dest_ids, description_prefix="Auto-upload")


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
            has_thumb = bool(item.thumbnail)
        out = _thumb_path_for(mid)
        if out.exists():
            # Already generated (e.g. by an earlier import of the same device).
            # Don't re-run ffmpeg; just make sure the row points at it.
            if not has_thumb:
                with session_scope() as s:
                    item = s.get(MediaItem, mid)
                    if item:
                        item.thumbnail = str(out)
            ctx.set_progress((i + 1) / total, f"Thumbnail already exists for {path.name}")
            continue
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
    album_id = payload.get("album_id")
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
        if album_id:
            album = s.get(Album, album_id)
            if album:
                next_pos = max((it.position for it in album.items), default=-1) + 1
                if all(it.media_id != new_id for it in album.items):
                    s.add(AlbumItem(album_id=album_id, media_id=new_id, position=next_pos))
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
        if dest_ids:
            dests = (
                s.query(Destination)
                .filter(Destination.id.in_(dest_ids))
                .order_by(Destination.rank, Destination.id)
                .all()
            )
        else:
            dests = (
                s.query(Destination)
                .filter(Destination.is_default == True, Destination.enabled == True)  # noqa: E712
                .order_by(Destination.rank, Destination.id)
                .all()
            )
        # Highest-priority (lowest rank) destination first for each clip.
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
                ctx.set_progress(done / total, f"Skipped missing media {mid} for destination {did}")
                continue
            local_path_obj = Path(item.path)
            if _needs_metadata_refresh(item, local_path_obj):
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
            if ledger_done:
                state.status = "done"
                state.remote_path = clip.remote_path
                state.error = None
                state.bytes_uploaded = local_size
                state.total_bytes = local_size
                state.uploaded_at = state.uploaded_at or utcnow()
                state.updated_at = utcnow()
                clip.bytes_uploaded = local_size
                clip.last_error = None
                local_filename = item.filename
                dest_name = dest.name
                done += 1
                ctx.set_progress(done / total, f"Already uploaded {local_filename} on {dest_name}")
                continue
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
            # Stamp the remote file with the capture time so its date matches the
            # {year}/{month} folder it lands in, not the moment it was uploaded.
            capture_mtime = item.capture_time.timestamp() if item.capture_time else None
            dest_detached = dest  # used only for read-only backend config below
            backend = get_backend(dest_detached)
            dest_name = dest.name
            clip_id = clip.id
            remote_hint = join_remote(dest.base_path or "/", remote_dir, filename)
        ctx.log(f"Resolved {filename} to {remote_hint}", progress=done / total)

        # 2) Perform the upload with no DB transaction held open.
        if not ledger_done:
            ctx.set_progress(done / total, f"Checking existing {filename} on {dest_name}")
            try:
                if backend.remote_file_matches(remote_dir, filename, local_size, checksum_value):
                    log.info(
                        "Found existing verified upload for media=%s destination=%s remote=%s",
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
                        now = utcnow()
                        if state:
                            state.status = "done"
                            state.remote_path = remote_hint
                            state.error = None
                            state.bytes_uploaded = local_size
                            state.total_bytes = local_size
                            state.uploaded_at = state.uploaded_at or now
                            state.updated_at = now
                        if clip:
                            clip.remote_path = remote_hint
                            clip.temp_remote_path = f"{remote_hint}.part"
                            clip.status = "done"
                            clip.size_bytes = local_size
                            clip.bytes_uploaded = local_size
                            clip.upload_duration_s = clip.upload_duration_s or 0.0
                            clip.upload_throughput_bps = clip.upload_throughput_bps or 0.0
                            clip.uploaded_at = clip.uploaded_at or now
                            clip.last_error = None
                            clip.updated_at = now
                    done += 1
                    ctx.set_progress(done / total, f"Verified existing {filename}")
                    continue
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"Existing-file check failed for {filename}: {exc}", level="WARNING")
                log.exception(
                    "Existing remote verification errored before upload media=%s destination=%s",
                    mid,
                    did,
                )

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
                    mtime=capture_mtime,
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

        if result_status == "done" and result_remote is None:
            result_remote = remote_hint

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


# --- helper to enqueue follow-up jobs without a circular import -------------

def get_manager_enqueue(kind: str, payload: dict, description: str = "") -> int:
    from .jobs import get_manager

    return get_manager().enqueue(kind, description=description or kind, payload=payload)


def _media_needing_thumbnails(media_ids: list[int]) -> list[int]:
    """Filter to media that have no thumbnail on disk yet (order preserved)."""
    if not media_ids:
        return []
    with session_scope() as s:
        thumbs = dict(
            s.query(MediaItem.id, MediaItem.thumbnail)
            .filter(MediaItem.id.in_(media_ids))
            .all()
        )
    need: list[int] = []
    for mid in media_ids:
        if mid not in thumbs:
            continue
        existing = thumbs[mid]
        if existing and Path(existing).exists():
            continue
        if _thumb_path_for(mid).exists():
            continue
        need.append(mid)
    return need


def _import_already_queued(dcim_root: str) -> bool:
    """True if a queued/running import for the same device is already pending.

    A camera reconnect (or a stray client poll) used to enqueue a fresh import
    every time it was detected, piling up redundant work. We de-dupe against the
    ``dcim_root`` stored in each import job's payload.
    """
    with session_scope() as s:
        rows = (
            s.query(Job.payload)
            .filter(Job.kind == "import", Job.status.in_(("queued", "running")))
            .all()
        )
    for (payload_json,) in rows:
        if not payload_json:
            continue
        try:
            data = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        if data.get("dcim_root") == dcim_root:
            return True
    return False


def enqueue_device_import(
    dcim_root: Path,
    *,
    paths: list[str] | None = None,
    auto_upload: bool = False,
    destination_ids: list[int] | None = None,
    group_uploads_by_month: bool = False,
    dedup: bool = True,
) -> tuple[int | None, int]:
    """Enqueue indexing of a connected device, de-duplicating reconnects.

    Returns ``(job_id, file_count)``. ``job_id`` is ``None`` when there are no
    files to index, or when ``dedup`` is set and an import for the same device
    is already queued/running.
    """
    root_str = str(dcim_root)
    found = [str(p) for p in scan_media_files(dcim_root)]
    if paths:
        requested = {str(Path(p)) for p in paths}
        found = [p for p in found if p in requested]
    if not found:
        return None, 0
    if dedup and _import_already_queued(root_str):
        log.info("Skipping duplicate device import for %s (already queued)", root_str)
        return None, len(found)
    job_id = get_manager_enqueue(
        "import",
        {
            "paths": found,
            "source": "device",
            "dcim_root": root_str,
            "auto_upload": auto_upload,
            "destination_ids": destination_ids,
            "group_uploads_by_month": group_uploads_by_month,
        },
        description=f"Index and fingerprint {len(found)} files from {dcim_root.name}",
    )
    return job_id, len(found)


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


def _destination_ids_for_upload(session, destination_ids: list[int] | None) -> list[int]:
    if destination_ids:
        rows = (
            session.query(Destination.id)
            .filter(Destination.id.in_(destination_ids))
            .order_by(Destination.rank, Destination.id)
            .all()
        )
    else:
        rows = (
            session.query(Destination.id)
            .filter(Destination.is_default == True, Destination.enabled == True)  # noqa: E712
            .order_by(Destination.rank, Destination.id)
            .all()
        )
    return [row[0] for row in rows]


def _media_ids_needing_upload(
    media_ids: list[int],
    destination_ids: list[int] | None = None,
) -> list[int]:
    """Return media ids that are not already done for every target destination."""
    if not media_ids:
        return []
    with session_scope() as s:
        dest_ids = _destination_ids_for_upload(s, destination_ids)
        if not dest_ids:
            return media_ids
        rows = (
            s.query(MediaItem.id, MediaItem.checksum)
            .filter(MediaItem.id.in_(media_ids))
            .all()
        )
        checksum_by_id = {mid: cs for mid, cs in rows}
        checksums = [cs for cs in checksum_by_id.values() if cs]
        done_pairs = set()
        if checksums:
            done_pairs = set(
                s.query(UploadedClip.destination_id, UploadedClip.checksum)
                .filter(
                    UploadedClip.destination_id.in_(dest_ids),
                    UploadedClip.checksum.in_(checksums),
                    UploadedClip.status == "done",
                    UploadedClip.remote_path.isnot(None),
                    UploadedClip.remote_path != "",
                )
                .all()
            )
    needed = []
    for mid in media_ids:
        cs = checksum_by_id.get(mid)
        if not cs or any((did, cs) not in done_pairs for did in dest_ids):
            needed.append(mid)
    return needed


def enqueue_upload_jobs(
    media_ids: list[int],
    destination_ids: list[int] | None = None,
    description_prefix: str = "Upload",
) -> list[int]:
    media_ids = list(dict.fromkeys(media_ids))
    media_ids = _media_ids_needing_upload(media_ids, destination_ids)
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
    media_ids = _media_ids_needing_upload(media_ids, destination_ids)
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
