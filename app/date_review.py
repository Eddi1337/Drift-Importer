"""Camera clock checks, explicit correction previews and recoverable NAS moves.

Camera originals are never edited. MP4 movie headers are read with small seeks;
no ffprobe process or video copy is needed for the normal date scan.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

from .database import session_scope
from .destinations.base import render_remote_dir
from .jobs import ACTIVE_STATES, JobCancelled, get_manager, handler
from .models import CameraDateCheck, CorrectedArchive, DateCorrection, Destination, Job, MediaItem, RecordingDate, UploadedClip, UploadState, utcnow
from .storage import archive_path, require_storage

UTC = dt.timezone.utc
LOCAL = ZoneInfo("Europe/London")
lock = threading.RLock()
APPROVED = {"trusted", "confirmed"}


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def local(value):
    return utc(value).astimezone(LOCAL)


def iso(value):
    return utc(value).isoformat() if value else None


def parse_local(value: str) -> dt.datetime:
    result = dt.datetime.fromisoformat(value)
    if result.tzinfo is None:
        first, second = result.replace(tzinfo=LOCAL, fold=0), result.replace(tzinfo=LOCAL, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("This time crosses a clock change. Supply an ISO time with its +00:00 or +01:00 offset.")
        result = first
        if result.astimezone(UTC).astimezone(LOCAL).replace(tzinfo=None) != first.replace(tzinfo=None):
            raise ValueError("That local time does not exist because of the clock change")
    return result.astimezone(UTC).replace(tzinfo=None)


def sequence_key(path: Path):
    """Folder rollover takes precedence over the DVR counter. EVENT is separate."""
    file = re.fullmatch(r"DVR(\d+)\.(?:MP4|MOV)", path.name, re.I)
    folder = re.fullmatch(r"(\d+)MEDIA", path.parent.name, re.I)
    if not file or not folder:
        return None
    return int(folder[1]), int(file[1])


def is_camera_video(path: Path):
    return bool(re.fullmatch(r"DVR\d+\.(?:MP4|MOV)", path.name, re.I)) and (
        sequence_key(path) is not None or any(p.name.upper() == "EVENT" for p in path.parents)
    )


def fingerprint(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def mp4_clock(path: Path):
    """Extract mvhd creation time and duration, skipping large mdat payloads."""
    size = path.stat().st_size
    with path.open("rb", buffering=0) as file:
        def atoms(start, end):
            position = start
            for _ in range(1000):
                if position + 8 > end:
                    return
                file.seek(position)
                header = file.read(8)
                if len(header) != 8:
                    return
                length, name = struct.unpack(">I4s", header)
                offset = 8
                if length == 1:
                    extended = file.read(8)
                    if len(extended) != 8:
                        return
                    length, offset = struct.unpack(">Q", extended)[0], 16
                if length == 0:
                    length = end - position
                if length < offset or position + length > end:
                    return
                yield name, position + offset, position + length
                position += length
        for name, start, end in atoms(0, size):
            if name != b"moov":
                continue
            for child, body, child_end in atoms(start, end):
                if child != b"mvhd":
                    continue
                file.seek(body)
                data = file.read(min(36, child_end - body))
                if len(data) < 20:
                    break
                if data[0] == 0:
                    created, _, scale, duration = struct.unpack(">IIII", data[4:20])
                elif data[0] == 1 and len(data) >= 32:
                    created, _, scale, duration = struct.unpack(">QQIQ", data[4:32])
                else:
                    break
                when = dt.datetime(1904, 1, 1) + dt.timedelta(seconds=created) if created else None
                return when, duration / scale if scale else None
    return None, None


def observation(session, path: Path):
    if not is_camera_video(path):
        return None
    size, mtime = fingerprint(path)
    return session.query(RecordingDate).filter_by(path=str(path), size_bytes=size, mtime_ns=mtime).first()


def approved_paths(session, paths):
    paths = list(paths)
    guarded = [p for p in paths if is_camera_video(p)]
    rows = session.query(RecordingDate).filter(RecordingDate.path.in_([str(p) for p in guarded])).all() if guarded else []
    known = {(r.path, r.size_bytes, r.mtime_ns): r for r in rows}
    result = []
    for path in paths:
        if not is_camera_video(path):
            result.append(path)
            continue
        try:
            row = known.get((str(path), *fingerprint(path)))
        except OSError:
            continue
        if row and row.status in APPROVED:
            result.append(path)
    return result


def require_approved(session, item):
    if item.derived or item.source != "device" or not is_camera_video(Path(item.path)):
        return None
    row = observation(session, Path(item.path))
    if row is None or row.status not in APPROVED:
        raise ValueError(f"Dates need checking for {item.filename}. Open Import → Recording dates before uploading.")
    item.capture_time = row.corrected_time or row.original_time
    return row


def queue_check(root: Path):
    from .workflow import camera_root
    root = camera_root(str(root))
    with lock, session_scope() as session:
        for job in session.query(Job).filter(Job.kind == "date_scan", Job.status.in_(ACTIVE_STATES)):
            if json.loads(job.payload or "{}").get("camera_root") == str(root):
                return job.id
        return get_manager().enqueue("date_scan", description="Check camera dates and recording order", payload={"camera_root": str(root), "connected_at": iso(utcnow())})


def analyse(rows, new_ids, connected_at, previous_rows=()):
    """Flag clock resets by sequence; only new footage gets the ride-end rule."""
    now = utc(connected_at)
    today = local(now).date()
    ordered = sorted([r for r in rows if sequence_key(Path(r.path))], key=lambda r: sequence_key(Path(r.path)))
    regressions = set()
    for before, after in zip(ordered, ordered[1:]):
        if before.original_time and after.original_time and utc(after.original_time) < utc(before.original_time) - dt.timedelta(seconds=2):
            regressions.update((before.id, after.id))
    new_ordered = [r for r in ordered if r.id in new_ids]
    prior_keys = [sequence_key(Path(r.path)) for r in previous_rows if sequence_key(Path(r.path))]
    reused = {r.id for r in new_ordered if prior_keys and sequence_key(Path(r.path)) <= max(prior_keys)}
    latest = ordered[-1] if ordered else None
    latest_end = (utc(latest.original_time) + dt.timedelta(seconds=latest.duration_s or 0)) if latest and latest.original_time else None
    recent = latest_end and local(latest_end).date() in (today, today - dt.timedelta(days=1)) and latest_end <= now + dt.timedelta(minutes=5)
    # A confirmed offset can carry forward, but only across increasing counters,
    # increasing camera times and a plausible latest ride-end day.
    calibrated = [r for r in ordered if r.status == "confirmed" and r.reuse_offset and r.original_time and r.corrected_time]
    anchor = calibrated[-1] if calibrated else None
    offset = (utc(anchor.corrected_time) - utc(anchor.original_time)) if anchor else None
    carry = bool(anchor and new_ordered and all(sequence_key(Path(r.path)) > sequence_key(Path(anchor.path)) and r.original_time and utc(r.original_time) >= utc(anchor.original_time) for r in new_ordered))
    if carry:
        end = latest_end + offset if latest_end else None
        carry = bool(end and local(end).date() in (today, today - dt.timedelta(days=1)) and end <= now + dt.timedelta(minutes=5) and not regressions.intersection(new_ids) and not reused)
    for row in rows:
        if row.status in ("confirmed", "applying"):
            continue
        reasons = []
        if not row.original_time or row.time_source != "metadata":
            reasons.append("Recording metadata is missing; file modification time is only a fallback")
        if row.original_time and utc(row.original_time) > now + dt.timedelta(minutes=5):
            reasons.append("Camera timestamp is in the future")
        if row.id in reused:
            reasons.append("New or replaced recording uses an earlier/reused folder and DVR counter")
        if row.id in regressions:
            reasons.append("A higher media-folder / DVR number has an earlier camera timestamp")
        if row.id in new_ids:
            if not recent:
                reasons.append("Newest ride does not end on the connection day or previous day")
            elif row.original_time and local(row.original_time).date() < today - dt.timedelta(days=1):
                reasons.append("Historical footage needs its date confirmed separately")
            if carry and row in new_ordered and row.time_source == "metadata":
                row.corrected_time = (utc(row.original_time) + offset).replace(tzinfo=None)
                reasons = []
                row.reuse_offset = True
                row.metadata_copy = anchor.metadata_copy
        # Never silently clear a prior warning just because the Pi date changed.
        if row.id not in new_ids and row.status == "review":
            reasons = json.loads(row.reasons or "[]") or ["Date needs confirmation"]
        row.status = "review" if reasons else "trusted"
        row.reasons = json.dumps(reasons)


@handler("date_scan")
def scan_job(job_id, payload, ctx):
    from .workflow import camera_root, scan_videos
    root = camera_root(payload["camera_root"])
    paths = [p for p in scan_videos(root) if is_camera_video(p)]
    before = {str(p): fingerprint(p) for p in paths}
    with session_scope() as session:
        old = {(r.path, r.size_bytes, r.mtime_ns): r for r in session.query(RecordingDate).filter_by(camera_root=str(root))}
    rows, new_rows = [], []
    for index, path in enumerate(paths):
        if ctx.is_cancelled():
            raise JobCancelled()
        key = (str(path), *before[str(path)])
        row = old.get(key)
        if row is None:
            when, duration = mp4_clock(path)
            source = "metadata"
            if when is None:
                when = dt.datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(tzinfo=None)
                source = "mtime"
            row = RecordingDate(camera_root=str(root), path=str(path), relative_path=str(path.relative_to(root)), size_bytes=key[1], mtime_ns=key[2], original_time=when, duration_s=duration, time_source=source, status="review", first_seen=dt.datetime.fromisoformat(payload["connected_at"]).replace(tzinfo=None))
            new_rows.append(row)
        rows.append(row)
        if index % 50 == 0:
            ctx.set_progress(index / max(1, len(paths)), f"Checking dates: {index + 1}/{len(paths)}")
    # A disconnect or partial/changing scan must not authorise uploads.
    after_paths = [p for p in scan_videos(camera_root(str(root))) if is_camera_video(p)]
    if {str(p): fingerprint(p) for p in after_paths} != before:
        raise RuntimeError("Camera files changed during the date check. Run Check dates again.")
    with session_scope() as session:
        # Re-read old observations: a confirmation may have committed while
        # headers were being read. Never overwrite its hold or corrected time.
        session.add_all(new_rows)
        session.flush()
        rows = [session.get(RecordingDate, r.id) for r in rows]
        analyse(rows, {r.id for r in new_rows}, dt.datetime.fromisoformat(payload["connected_at"]), old.values())
        check = session.get(CameraDateCheck, str(root))
        if check is None:
            check = CameraDateCheck(camera_root=str(root))
            session.add(check)
        check.observed_ids = json.dumps([r.id for r in rows])
        check.checked_at = utcnow()
        held = sum(r.status not in APPROVED for r in rows)
        session.get(Job, job_id).result = json.dumps({"total": len(rows), "held": held})
    ctx.set_progress(1, f"Dates checked: {len(rows) - held} ready, {held} need review")


def review_data(session, root: str):
    check = session.get(CameraDateCheck, root)
    ids = json.loads(check.observed_ids) if check else []
    rows = session.query(RecordingDate).filter(RecordingDate.id.in_(ids)).all() if ids else []
    rows.sort(key=lambda r: (sequence_key(Path(r.path)) or (100000, 0), r.relative_path))
    jobs = session.query(Job).filter(Job.kind.in_(("date_scan", "date_correction"))).order_by(Job.id.desc()).limit(20).all()
    relevant = [j for j in jobs if json.loads(j.payload or "{}").get("camera_root") == root]
    active = next((j for j in relevant if j.status in ACTIVE_STATES), None)
    failure = next((j for j in relevant if j.status in ("error", "cancelled")), None) if not active and any(r.status == "applying" for r in rows) else None
    # Flags can change at confirmation without reading thousands of files again.
    return {"failure": {"id": failure.id, "error": failure.error or failure.status} if failure else None, "timezone": "Europe/London", "checked_at": iso(check.checked_at) if check else None, "job": {"id": active.id, "status": active.status, "detail": active.detail, "progress": active.progress} if active else None, "total": len(rows), "held": sum(r.status not in APPROVED for r in rows), "files": [{"id": r.id, "path": r.relative_path, "folder": str(Path(r.relative_path).parent), "sequence": sequence_key(Path(r.path)), "recorded_time": iso(r.original_time), "effective_time": iso(r.corrected_time or r.original_time), "local_time": local(r.corrected_time or r.original_time).isoformat() if r.original_time else None, "duration_s": r.duration_s, "status": r.status, "reasons": json.loads(r.reasons), "revision": r.revision} for r in rows]}


def make_preview(session, root, ids, anchor_id, anchor_time, anchor_end, keep, destination_id, metadata_copy, reuse_offset):
    from .workflow import nas_destination
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Select a recording range")
    rows = session.query(RecordingDate).filter(RecordingDate.id.in_(ids), RecordingDate.camera_root == root).all()
    if len(rows) != len(ids):
        raise ValueError("Recording selection changed; check dates again")
    if any(r.status == "applying" for r in rows):
        raise ValueError("A correction is already running for this selection")
    ordered = sorted([r for r in rows if sequence_key(Path(r.path))], key=lambda r: sequence_key(Path(r.path)))
    if any(a.original_time and b.original_time and b.original_time < a.original_time - dt.timedelta(seconds=2) for a, b in zip(ordered, ordered[1:])):
        raise ValueError("This range crosses a camera clock reset. Correct separate ranges on each side of the reset.")
    rows.sort(key=lambda r: (sequence_key(Path(r.path)) or (100000, 0), r.relative_path))
    dest = nas_destination(session, destination_id)
    nas = require_storage(Path(dest.base_path))
    delta = dt.timedelta(0)
    if not keep:
        anchor = next((r for r in rows if r.id == anchor_id), None)
        if anchor is None or anchor.original_time is None or (anchor_end and anchor.duration_s is None):
            raise ValueError("Choose an anchor with a recording timestamp and duration")
        target = parse_local(anchor_time)
        basis = (anchor.corrected_time or anchor.original_time) + dt.timedelta(seconds=(anchor.duration_s or 0) if anchor_end else 0)
        delta = target - basis
    entries = []
    for row in rows:
        if row.original_time is None or fingerprint(Path(row.path)) != (row.size_bytes, row.mtime_ns):
            raise ValueError("Camera file changed or is unavailable; run Check dates again")
        when = (row.corrected_time or row.original_time) + delta
        if utc(when) > utcnow() + dt.timedelta(minutes=5) or when.year < 2000:
            raise ValueError("Corrected times must be plausible past recording times")
        item = session.query(MediaItem).filter_by(path=row.path).first()
        ledgers = session.query(UploadedClip).filter_by(destination_id=dest.id, checksum=item.checksum, status="done").all() if item else []
        moves = []
        for ledger in ledgers:
            source = archive_path(nas, ledger.remote_path)
            if source is None:
                raise ValueError("An archive path resolves outside the NAS")
            target = archive_path(nas, str(nas / render_remote_dir(dest.path_template, local(when)) / source.name))
            if target is None:
                raise ValueError("Destination folder template resolves outside the NAS")
            moves.append({"ledger_id": ledger.id, "old": str(source), "new": str(target)})
        # Existing originals not indexed by this app can still be recovered from
        # the original date folder. The job requires a full hash match first.
        old_folder = archive_path(nas, str(nas / render_remote_dir(dest.path_template, local(row.original_time))))
        candidates = []
        if old_folder and old_folder.is_dir():
            candidates = [str(p) for p in old_folder.glob(Path(row.path).stem + "*." + Path(row.path).suffix.lstrip(".")) if p.is_file() and (p.name == Path(row.path).name or re.fullmatch(re.escape(Path(row.path).stem) + r"_[0-9a-f]{16}\.MP4", p.name, re.I))]
        entries.append({"id": row.id, "revision": row.revision, "path": row.relative_path, "old_time": iso(row.corrected_time or row.original_time), "new_time": iso(when), "moves": moves, "candidates": candidates, "candidate_moves": [{"old": p, "new": str(nas / render_remote_dir(dest.path_template, local(when)) / Path(p).name)} for p in candidates]})
    plan = {"camera_root": root, "destination_id": dest.id, "base_path": str(nas), "path_template": dest.path_template, "metadata_copy": metadata_copy, "reuse_offset": reuse_offset, "entries": entries}
    correction = DateCorrection(plan=json.dumps(plan))
    session.add(correction)
    session.flush()
    return {"plan_id": correction.id, **plan}


def confirm_plan(session, plan_id):
    with lock:
        correction = session.get(DateCorrection, plan_id)
        if correction is None:
            raise ValueError("Correction preview was not found")
        if correction.job_id:
            return {"job_id": correction.job_id}
        if correction.status != "preview":
            raise ValueError("Create a fresh correction preview")
        plan = json.loads(correction.plan)
        selected_paths = {session.get(RecordingDate, e["id"]).path for e in plan["entries"]}
        selected_media = {m.id for m in session.query(MediaItem).filter(MediaItem.path.in_(selected_paths))}
        for job in session.query(Job).filter_by(kind="upload", status="running"):
            if selected_media.intersection(json.loads(job.payload or "{}").get("media_ids", [])):
                raise ValueError("Wait for the selected files to finish uploading, then confirm this preview again")
        for entry in plan["entries"]:
            row = session.get(RecordingDate, entry["id"])
            if row is None or row.revision != entry["revision"] or row.status == "applying" or fingerprint(Path(row.path)) != (row.size_bytes, row.mtime_ns):
                raise ValueError("The preview is stale; preview this correction again")
            row.status = "applying"
        correction.status = "confirmed"
        job = Job(kind="date_correction", description=f"Correct dates and NAS folders for {len(plan['entries'])} clips", payload=json.dumps({"plan_id": plan_id, "camera_root": plan["camera_root"]}))
        session.add(job)
        session.flush()
        correction.job_id = job.id
        # The held files and durable job publish in a single transaction.
        session.commit()
        return {"job_id": correction.job_id}


def full_hash(path, ctx):
    digest = hashlib.sha256()
    before = fingerprint(path)
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            if ctx.is_cancelled():
                raise JobCancelled()
            digest.update(block)
    if fingerprint(path) != before:
        raise RuntimeError(f"File changed while reading: {path.name}")
    return digest.hexdigest()


def safe_move(source: Path, target: Path, expected_hash: str, ctx):
    """Link without overwrite, then publish DB pointers before removing old name."""
    if source == target:
        if full_hash(source, ctx) != expected_hash:
            raise RuntimeError("Original NAS file does not match the camera")
        return
    if source.exists() and full_hash(source, ctx) != expected_hash:
        raise RuntimeError("Original NAS file does not match the camera")
    if target.exists():
        if full_hash(target, ctx) != expected_hash:
            raise RuntimeError(f"Destination collision: {target.name}; neither file was overwritten")
        return
    if not source.exists():
        raise RuntimeError("Neither old nor new NAS file exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, target)  # atomic EEXIST protection; all paths are on this NAS


def write_audit(root, original, row):
    value = {"camera_path": row.relative_path, "original_creation_time": iso(row.original_time), "corrected_creation_time": iso(row.corrected_time or row.original_time), "timezone": "Europe/London", "original_bytes_preserved": True, "correction_id": row.correction_id}
    path = original.with_name(original.name + ".dates.json")
    if not path.resolve().is_relative_to(root):
        raise RuntimeError("Audit path resolves outside the NAS")
    with tempfile.NamedTemporaryFile("w", dir=original.parent, suffix=".part", delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def publish_metadata_copy(row_id, destination_id, original, ctx):
    """Caller holds ffmpeg then upload slots; perform slow I/O outside SQLite."""
    from .media import probe
    from .timestamps import metadata_copy
    with session_scope() as session:
        row = session.get(RecordingDate, row_id)
        dest = session.get(Destination, destination_id)
        root = require_storage(Path(dest.base_path))
        when = row.corrected_time or row.original_time
        stored = session.query(CorrectedArchive).filter_by(recording_id=row_id, destination_id=destination_id).first()
    original = archive_path(root, str(original))
    if original is None or not original.is_file():
        raise RuntimeError("Original copy is unavailable on the NAS")
    write_audit(root, original, row)
    if not row.metadata_copy or row.corrected_time is None or row.corrected_time == row.original_time:
        return
    target = archive_path(root, str(root / "Corrected" / local(when).strftime("%Y/%m") / f"{original.stem}_date-{hashlib.sha256(iso(when).encode()).hexdigest()[:10]}{original.suffix}"))
    if target is None:
        raise RuntimeError("Corrected copy path resolves outside the NAS")
    if stored and stored.corrected_time == when and stored.corrected_path == str(target) and target.is_file():
        return
    expected = probe(original)
    scratch = root / ".drift" / "tmp"
    if not scratch.resolve().is_relative_to(root):
        raise RuntimeError("Temporary directory resolves outside the NAS")
    scratch.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    if shutil.disk_usage(root).free < original.stat().st_size + 64 * 1024 * 1024:
        raise RuntimeError("Not enough NAS space for the corrected metadata copy")
    with tempfile.TemporaryDirectory(prefix="dates-", dir=scratch) as tmp:
        output = Path(tmp) / "corrected.mp4"
        before = fingerprint(original)
        metadata_copy(original, output, when)
        info = probe(output)
        if fingerprint(original) != before or info.get("stream_signature") != expected.get("stream_signature") or not info.get("duration_s") or abs(info["duration_s"] - (expected.get("duration_s") or 0)) > 1 or info.get("capture_time") != when:
            raise RuntimeError("Corrected metadata copy failed validation")
        if ctx.is_cancelled():
            raise JobCancelled()
        require_storage(root)
        if target.exists():
            # Recover publication that completed just before a process crash.
            if full_hash(target, ctx) != full_hash(output, ctx):
                raise RuntimeError("Corrected output collision; no overwrite performed")
        else:
            with output.open("rb") as file:
                os.fsync(file.fileno())
            os.link(output, target)
    os.utime(target, (utc(when).timestamp(), utc(when).timestamp()))
    with session_scope() as session:
        stored = session.query(CorrectedArchive).filter_by(recording_id=row_id, destination_id=destination_id).first()
        if stored is None:
            stored = CorrectedArchive(recording_id=row_id, destination_id=destination_id, corrected_time=when, original_path=str(original))
            session.add(stored)
        stored.corrected_time, stored.original_path, stored.corrected_path = when, str(original), str(target)


def queue_metadata(row_id, destination_id, original):
    with lock, session_scope() as session:
        for job in session.query(Job).filter(Job.kind == "date_metadata", Job.status.in_(ACTIVE_STATES)):
            payload = json.loads(job.payload or "{}")
            if payload.get("row_id") == row_id and payload.get("destination_id") == destination_id:
                return job.id
        return get_manager().enqueue("date_metadata", description="Publish corrected recording metadata on NAS", payload={"row_id": row_id, "destination_id": destination_id, "original": original})


@handler("date_metadata")
def metadata_job(job_id, payload, ctx):
    with ctx.ffmpeg_semaphore, ctx.upload_semaphore:
        publish_metadata_copy(payload["row_id"], payload["destination_id"], Path(payload["original"]), ctx)
    ctx.set_progress(1, "Corrected metadata copy saved on NAS")


@handler("date_correction")
def correction_job(job_id, payload, ctx):
    from .tasks import import_one
    # Match trip lock order (ffmpeg then upload) to avoid opposite-order deadlocks.
    with ctx.ffmpeg_semaphore, ctx.upload_semaphore:
        with session_scope() as session:
            correction = session.get(DateCorrection, payload["plan_id"])
            plan = json.loads(correction.plan)
            if correction.status == "done":
                return
            dest = session.get(Destination, plan["destination_id"])
            root = require_storage(Path(dest.base_path))
            if str(root) != plan["base_path"] or dest.path_template != plan["path_template"]:
                raise RuntimeError("NAS destination changed since preview; create a new preview")
        for index, entry in enumerate(plan["entries"]):
            if ctx.is_cancelled():
                raise JobCancelled()
            ctx.set_progress(index / len(plan["entries"]), f"Correcting {entry['path']}")
            with session_scope() as session:
                row = session.get(RecordingDate, entry["id"])
                if row.revision != entry["revision"] and row.correction_id != correction.id:
                    raise RuntimeError("A recording was corrected after this preview")
                if fingerprint(Path(row.path)) != (row.size_bytes, row.mtime_ns):
                    raise RuntimeError("Camera file changed; correction stopped")
                item = import_one(session, Path(row.path), source="device", verify_checksum=True)
                item_id, cs = item.id, item.checksum
                row.corrected_time = dt.datetime.fromisoformat(entry["new_time"]).replace(tzinfo=None)
                row.correction_id = correction.id
                row.metadata_copy, row.reuse_offset = plan["metadata_copy"], plan["reuse_offset"]
                item.capture_time = row.corrected_time
                moves = list(entry["moves"])
                existing = session.query(UploadedClip).filter_by(destination_id=dest.id, checksum=cs, status="done").first()
                if existing and not moves:
                    old = archive_path(root, existing.remote_path)
                    if old is None:
                        raise RuntimeError("Recorded NAS archive resolves outside the destination")
                    new = archive_path(root, str(root / render_remote_dir(dest.path_template, local(row.corrected_time)) / old.name))
                    if new is None:
                        raise RuntimeError("Corrected NAS path escapes the destination")
                    moves.append({"ledger_id": existing.id, "old": str(old), "new": str(new)})
            digest = None
            for candidate in entry["candidates"] if not moves else []:
                path = archive_path(root, candidate)
                if not path or not path.is_file() or path.stat().st_size != row.size_bytes:
                    continue
                digest = digest or full_hash(Path(row.path), ctx)
                if full_hash(path, ctx) != digest:
                    continue
                with session_scope() as session:
                    ledger = UploadedClip(destination_id=dest.id, source_media_id=item_id, checksum=cs, filename=path.name, size_bytes=row.size_bytes, status="done", remote_path=str(path), bytes_uploaded=row.size_bytes)
                    session.add(ledger)
                    session.flush()
                    new = archive_path(root, str(root / render_remote_dir(dest.path_template, local(row.corrected_time)) / path.name))
                    if new is None:
                        raise RuntimeError("Corrected NAS path escapes the destination")
                    moves.append({"ledger_id": ledger.id, "old": str(path), "new": str(new)})
                break
            with session_scope() as session:
                # Persist the exact move journal before any filesystem mutation.
                entry["moves"] = moves
                session.get(DateCorrection, correction.id).plan = json.dumps(plan)
            for move in moves:
                require_storage(root)
                source, target = archive_path(root, move["old"]), archive_path(root, move["new"])
                if not source or not target:
                    raise RuntimeError("Archive path escaped the NAS")
                digest = digest or full_hash(Path(row.path), ctx)
                safe_move(source, target, digest, ctx)
                os.utime(target, (utc(row.corrected_time).timestamp(), utc(row.corrected_time).timestamp()))
                with session_scope() as session:
                    ledger = session.get(UploadedClip, move["ledger_id"])
                    ledger.remote_path = str(target)
                    for state in session.query(UploadState).filter_by(destination_id=dest.id, media_id=item_id):
                        state.remote_path = str(target)
                    # Do not delete the old name until its new ledger is committed.
                if source != target and source.exists():
                    source.unlink()
                publish_metadata_copy(row.id, dest.id, target, ctx)
            with session_scope() as session:
                row = session.get(RecordingDate, entry["id"])
                row.status, row.reasons = "confirmed", "[]"
                row.revision = entry["revision"] + 1
        with session_scope() as session:
            session.get(DateCorrection, correction.id).status = "done"
        ctx.set_progress(1, "Dates confirmed; existing NAS copies reorganised")
    # Respect the user's automation setting, only for these confirmed files.
    from .settings_store import get_app_settings
    from .tasks import enqueue_device_import
    with session_scope() as session:
        prefs = get_app_settings(session)
        if prefs.auto_upload_on_import:
            enqueue_device_import(Path(plan["camera_root"]), paths=[str(session.get(RecordingDate, e["id"]).path) for e in plan["entries"]], auto_upload=True, destination_ids=[plan["destination_id"]], fingerprint_on_import=True)
