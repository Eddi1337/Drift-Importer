"""Read-only transfer accounting and a small RAM-only throughput window.

Job percentages mix indexing, movies and transfers. They cannot predict bytes
remaining. These estimates use the upload queue and committed upload ledger;
Home Assistant's recorder provides the progress history without extra SD writes.
"""
from __future__ import annotations

from collections import deque
import datetime as dt
import json
import threading
import time

from sqlalchemy import case, func

from .models import Destination, Job, MediaItem, UploadedClip, UploadState


def upload_queue_snapshot(session) -> dict:
    jobs = session.query(Job).filter(
        Job.dismissed_at.is_(None), Job.kind == "upload",
        Job.status.in_(("queued", "running", "paused")),
    ).all()
    defaults = [r[0] for r in session.query(Destination.id).filter(
        Destination.enabled.is_(True), Destination.is_default.is_(True),
    ).all()]
    units = set()
    malformed = False
    for job in jobs:
        try:
            payload = json.loads(job.payload or "{}")
            media_ids = payload.get("media_ids", [])
            dest_ids = payload.get("destination_ids") or defaults
            if not media_ids or not dest_ids:
                malformed = True
            units.update((int(mid), int(did)) for mid in media_ids for did in dest_ids)
        except (ValueError, TypeError, AttributeError):
            malformed = True
    media = {m.id: m for m in session.query(MediaItem).filter(
        MediaItem.id.in_({mid for mid, _ in units})
    ).all()} if units else {}
    states = {(r.media_id, r.destination_id): r for r in session.query(UploadState).filter(
        UploadState.media_id.in_(media),
    ).all()} if media else {}
    # Sum the durable byte checkpoints, including finished clips. It continues
    # increasing when one upload job finishes and the next starts.
    ledger = session.query(
        UploadedClip.destination_id, UploadedClip.checksum, UploadedClip.status,
        UploadedClip.bytes_uploaded, UploadedClip.size_bytes, UploadedClip.remote_path,
        UploadedClip.full_verification_failed,
    ).filter(
        UploadedClip.destination_id.in_({did for _, did in units}),
        UploadedClip.checksum.in_({m.checksum for m in media.values() if m.checksum}),
    ).all() if units else []
    by_checksum = {(r.destination_id, r.checksum): r for r in ledger}
    # Aggregate historical counters in SQLite, rather than loading an entire
    # year's ledger into Pi RAM on every publisher tick.
    persisted = func.coalesce(UploadedClip.bytes_uploaded, 0)
    size = func.coalesce(UploadedClip.size_bytes, 0)
    durable = int(session.query(func.coalesce(func.sum(case(
        (persisted < 0, 0), (persisted > size, size), else_=persisted,
    )), 0)).scalar()) if jobs else 0
    remaining, failed, seen = 0, 0, set()
    for mid, did in units:
        item = media.get(mid)
        if item is None or not item.size_bytes:
            malformed = True
            continue
        key = (did, item.checksum or f"media:{mid}")
        if key in seen:
            continue
        seen.add(key)
        record = by_checksum.get((did, item.checksum)) if item.checksum else None
        state = states.get((mid, did))
        invalid_copy = bool(record and record.full_verification_failed)
        if record and record.status == "done" and record.remote_path and not invalid_copy:
            continue
        if not record and state and state.status == "done" and state.remote_path:
            continue
        # The checksum ledger wins over an older per-media state (especially
        # when a corrupt destination is being uploaded again from byte zero).
        uploaded = (record.bytes_uploaded or 0) if record else (state.bytes_uploaded or 0) if state else 0
        if invalid_copy and record.status != "uploading":
            uploaded = 0
        remaining += max(0, item.size_bytes - uploaded)
        failed += int(bool(invalid_copy or (record and record.status == "error") or
                           (state and state.status == "error")))
    indexing = session.query(Job.id).filter(
        Job.dismissed_at.is_(None), Job.kind == "import",
        Job.status.in_(("queued", "running", "paused")),
    ).first() is not None
    return {
        "remaining_bytes": remaining,
        "durable_bytes": durable,
        "known_upload_queue": bool(jobs) and not malformed,
        "running_uploads": sum(j.status == "running" for j in jobs),
        "failed_transfers": failed,
        "estimated_scope": "Indexed upload queue; camera indexing can add more files" if indexing
                           else "Current upload queue only; excludes other tasks",
    }


class TransferEstimator:
    """Cache at most ten minutes of byte checkpoints; never persist a sample."""

    def __init__(self):
        self._samples = deque(maxlen=121)
        self._last_change = None
        self._was_running = False
        self._lock = threading.Lock()

    def observe(self, queue: dict, *, now: float | None = None,
                wall_time: dt.datetime | None = None) -> dict:
        now = time.monotonic() if now is None else now
        wall_time = wall_time or dt.datetime.now(dt.timezone.utc)
        durable = queue["durable_bytes"]
        with self._lock:
            if not queue["running_uploads"] or not self._was_running:
                self._samples.clear()
                self._last_change = None
            self._was_running = bool(queue["running_uploads"])
            if self._samples and durable < self._samples[-1][1]:
                self._samples.clear()  # reset/retry must not manufacture a rate
                self._last_change = None
            if not self._samples or now - self._samples[-1][0] >= 5:
                if self._samples and durable > self._samples[-1][1]:
                    self._last_change = now
                self._samples.append((now, durable))
            while len(self._samples) > 1 and now - self._samples[0][0] > 300:
                self._samples.popleft()
            first = self._samples[0]
            elapsed = now - first[0]
            rate = (durable - first[1]) / elapsed if elapsed >= 30 else 0
            stale = self._last_change is None or now - self._last_change > 120
            eta = None
            reason = "Waiting for recent upload throughput"
            if not queue["known_upload_queue"]:
                reason = "No complete upload queue to estimate"
            elif not queue["running_uploads"]:
                reason = "Uploads are not running"
            elif queue["failed_transfers"]:
                reason = "Failed transfers need review"
            elif stale or rate <= 0:
                reason = "Waiting for recent upload throughput"
            else:
                eta = int(queue["remaining_bytes"] / rate)
                reason = "Estimate from recent committed upload bytes"
        return {
            **queue, "throughput_bytes_per_s": round(rate) if not stale else None,
            "eta_seconds": eta,
            "eta_at": (wall_time + dt.timedelta(seconds=eta)).isoformat() if eta is not None else None,
            "eta_status": reason,
            "eta_summary": _duration(eta) if eta is not None else "Estimating…" if queue["running_uploads"] else "No upload ETA",
            "history_url": "/jobs",
        }


def _duration(seconds: int) -> str:
    if seconds < 60:
        return "Under a minute for queued uploads"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"About {minutes} min for queued uploads"
    hours, minutes = divmod(minutes, 60)
    return f"About {hours} h {minutes} min for queued uploads"


_estimator = TransferEstimator()


def transfer_history_report(session) -> dict:
    """Current ETA plus existing job events; GET requests create no DB writes."""
    from .models import JobLog
    report = _estimator.observe(upload_queue_snapshot(session))
    rows = session.query(JobLog).order_by(JobLog.created_at.desc(), JobLog.id.desc()).limit(200).all()
    report["events"] = [{"job_id": r.job_id, "time": r.created_at.isoformat() if r.created_at else None,
                         "progress": r.progress, "level": r.level, "message": r.message}
                        for r in reversed(rows)]
    return report


def transfer_estimate(session) -> dict:
    return _estimator.observe(upload_queue_snapshot(session))
