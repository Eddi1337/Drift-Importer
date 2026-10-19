"""Automatic ride scheduling and compact offloaded-render progress indexes.

Gallery reads never touch NFS. Discovery and request preparation are serialized
with camera/NAS maintenance; the encoder runs separately on the wired server.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import threading
import time

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .config import get_settings
from .database import Base, session_scope
from .jobs import ACTIVE_STATES, get_manager, handler
from .models import Job, utcnow

log = logging.getLogger(__name__)
_QUEUE_LOCK = threading.Lock()


class AutoRideRender(Base):
    __tablename__ = "auto_ride_renders"
    ride_id: Mapped[int] = mapped_column(ForeignKey("auto_rides.id", ondelete="CASCADE"), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64))
    input_signature: Mapped[str] = mapped_column(String(64))
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow)


def _json(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def render_summary(session, ride):
    row = session.get(AutoRideRender, ride.id)
    state = _json(row.state_json) if row and row.input_signature == ride.input_signature else {"status": "not_queued", "progress": 0}
    if state.get("status") == "not_queued":
        for job in session.query(Job).filter_by(kind="auto_ride_prepare").order_by(Job.id.desc()):
            if _json(job.payload).get("ride_id") == ride.id:
                if job.status in ACTIVE_STATES:
                    state = {"status": "queued", "progress": 0, "message": "Preparing the route and NAS film request"}
                elif job.status == "error":
                    state = {"status": "error", "progress": 0, "error": job.error}
                break
    base = f"/api/roadtrips/auto/{ride.id}"
    snapshots = []
    if state.get("status") in {"rendering", "done"}:
        for snap in state.get("snapshots", [])[:6]:
            name = snap.get("filename", "")
            if name in {f"snapshot-{i}.jpg" for i in range(1, 7)}:
                snapshots.append({"url": base + "/snapshot/" + name[9:-4], "time": snap.get("time")})
    return {key: state.get(key) for key in ("status", "progress", "message", "error", "updated_at")} | {
        "video_url": base + "/film" if state.get("status") == "done" else None,
        "snapshots": snapshots,
    }


def list_payload(session):
    from .auto_rides import list_rides
    from .auto_ride_models import AutoRide
    payload = list_rides(session)
    for summary in payload["rides"]:
        row = session.get(AutoRide, summary["id"])
        summary["render"] = render_summary(session, row)
        summary["snapshots"] = summary["render"]["snapshots"]
    job = session.query(Job).filter(Job.kind == "auto_rides_discover", Job.status.in_(ACTIVE_STATES)).first()
    payload["job"] = {"id": job.id, "status": job.status, "progress": job.progress} if job else None
    return payload


def enqueue_discovery():
    with _QUEUE_LOCK, session_scope() as session:
        job = session.query(Job).filter(Job.kind == "auto_rides_discover", Job.status.in_(ACTIVE_STATES)).first()
        if job:
            return {"job_id": job.id, "already_queued": True}
        return {"job_id": get_manager().enqueue("auto_rides_discover", description="Find rides from video times and Home Assistant journeys")}


def enqueue_render(ride_id, *, force=False):
    from .auto_ride_models import AutoRide
    with _QUEUE_LOCK, session_scope() as session:
        ride = session.get(AutoRide, ride_id)
        if not ride or not ride.active or ride.status != "ready":
            raise ValueError("This ride needs automatic discovery before rendering")
        index = session.get(AutoRideRender, ride_id)
        if index and index.input_signature == ride.input_signature and (not force or _json(index.state_json).get("status") in {"done", "queued", "rendering"}):
            return render_summary(session, ride)
        for job in session.query(Job).filter(Job.kind == "auto_ride_prepare", Job.status.in_(ACTIVE_STATES)):
            if _json(job.payload).get("ride_id") == ride_id:
                return {"job_id": job.id, "status": job.status}
        return {"job_id": get_manager().enqueue("auto_ride_prepare", description=f"Prepare ride film: {ride.name}", payload={"ride_id": ride_id}), "status": "queued"}


@handler("auto_rides_discover")
def handle_discovery(job_id, payload, ctx):
    from .auto_rides import discover
    from .auto_ride_models import AutoRide, AutoRideDiscovery
    from .auto_ride_ha import refresh_ha_days
    with ctx.upload_semaphore:
        with session_scope() as session:
            refresh = refresh_ha_days(session, ctx=ctx)
            report = discover(session, ctx=ctx)
            report["ha_refresh"] = refresh
            session.get(AutoRideDiscovery, 1).report = json.dumps(report)
            rides = [row.id for row in session.query(AutoRide).filter_by(active=True, status="ready")]
            stale_renders = []
            for index in session.query(AutoRideRender):
                ride = session.get(AutoRide, index.ride_id)
                if (not ride or not ride.active or ride.input_signature != index.input_signature) and _json(index.state_json).get("status") in {"queued", "rendering"}:
                    stale_renders.append((index.ride_id, index.signature))
            session.get(Job, job_id).result = json.dumps(report)
        from .ride_render import cancel_render
        for ride_id, signature in stale_renders:
            try:
                state = cancel_render(get_settings().nas_root, signature)
                with session_scope() as session:
                    index = session.get(AutoRideRender, ride_id)
                    if index and index.signature == signature:
                        index.state_json, index.updated_at = json.dumps(state), utcnow()
            except (ValueError, RuntimeError, OSError):
                log.warning("Outdated ride film cancellation deferred")
        if get_settings().auto_rides_render:
            for ride_id in rides:
                if ctx.is_cancelled():
                    from .jobs import JobCancelled
                    raise JobCancelled()
                enqueue_render(ride_id)
    ctx.set_progress(1, f"Found {len(rides)} automatic rides; uncertain footage remains excluded")


@handler("auto_ride_prepare")
def handle_prepare(job_id, payload, ctx):
    from .auto_rides import ride_detail, ride_clips
    from .auto_ride_models import AutoRide
    from .archive_models import ArchiveFile
    from .ride_render import queue_render, cancel_render
    from .jobs import JobCancelled
    def cancelled():
        if ctx.is_cancelled():
            raise JobCancelled()
    with ctx.upload_semaphore:
        cancelled()
        with session_scope() as session:
            ride = session.get(AutoRide, payload["ride_id"])
            if not ride or not ride.active:
                raise ValueError("The automatic ride is no longer current")
            detail = ride_detail(session, ride.id)
            root = get_settings().nas_root
            clips = []
            for clip in detail["clips"]:
                archived = session.get(ArchiveFile, clip["archive_id"])
                if not archived or not archived.present:
                    raise ValueError("A ride original is no longer indexed on the NAS")
                path = Path(clip["path"])
                relative = path.relative_to(root) if path.is_absolute() else path
                clips.append({"id": clip["id"], "path": str(relative), "duration_s": clip["duration_s"],
                    "start_time": clip["start_time"], "end_time": clip["end_time"],
                    "size_bytes": archived.size_bytes, "mtime_ns": archived.mtime_ns})
            from .roadtrips import preferences
            prefs = preferences(session)
            timezone, max_gap_s = prefs.timezone, prefs.max_gap_s
            ride_id, input_signature, title = ride.id, ride.input_signature, ride.name
        # Do not hold a SQLite transaction while waiting for NAS metadata writes.
        content = {"version": 1, "clips": clips, "time_series": detail["time_series"], "timezone": timezone, "max_gap_s": max_gap_s}
        signature = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        cancelled()
        state = queue_render(root, signature, clips, detail["time_series"], timezone=timezone,
            title=title, max_gap_s=max_gap_s, verify_sources=False)
        try:
            cancelled()
            with session_scope() as session:
                ride = session.get(AutoRide, ride_id)
                if not ride or not ride.active or ride.input_signature != input_signature:
                    raise ValueError("Ride timestamps changed while preparing the film; rediscover it")
                ride_clips(session, ride)
                index = session.get(AutoRideRender, ride.id)
                if index is None:
                    index = AutoRideRender(ride_id=ride.id, signature=signature, input_signature=ride.input_signature)
                    session.add(index)
                index.signature, index.input_signature = signature, ride.input_signature
                index.state_json, index.updated_at = json.dumps(state), utcnow()
                session.get(Job, job_id).result = json.dumps({"ride_id": ride.id, "status": state["status"]})
            ctx.set_progress(1, "Ride film queued on the wired server; no encoding runs on the Pi")
        except (JobCancelled, ValueError):
            state = cancel_render(root, signature)
            with session_scope() as session:
                index = session.get(AutoRideRender, ride_id)
                if index and index.signature == signature:
                    index.state_json, index.updated_at = json.dumps(state), utcnow()
            raise


class AutoRideMonitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="drift-auto-rides", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def tick(self, *, check_inputs=True):
        from .auto_ride_models import AutoRideDiscovery
        from .auto_rides import input_fingerprint
        if get_settings().processing_role == "processor":
            from .processing_sync import ProcessingSyncState
            with session_scope() as session:
                initial = session.get(ProcessingSyncState, 1)
                if not initial or not initial.seeded:
                    return  # Do not create empty preferences before Pi seeding.
        if check_inputs:
            from .auto_ride_ha import pending_refresh_days
            with session_scope() as session:
                previous = session.get(AutoRideDiscovery, 1)
                report = _json(previous.report) if previous else {}
                refresh = report.get("ha_refresh", {})
                changed = not previous or bool(report.get("deferred_months")) or bool(refresh.get("errors") or refresh.get("pending_days") or refresh.get("pending_rebuild_months")) or previous.input_signature != input_fingerprint(session) or bool(pending_refresh_days(session))
            if changed:
                enqueue_discovery()
        # Only active renders need NAS polling. Gallery/API requests use this
        # compact SQLite cache, independent of NAS latency.
        with session_scope() as session:
            from .auto_ride_models import AutoRide
            pending = []
            for row in session.query(AutoRideRender):
                if _json(row.state_json).get("status") in {"queued", "rendering"}:
                    ride = session.get(AutoRide, row.ride_id)
                    stale = not ride or not ride.active or ride.input_signature != row.input_signature
                    pending.append((row.ride_id, row.signature, stale))
        from .ride_render import get_render_state, cancel_render
        for ride_id, signature, stale in pending:
            if self.stop_event.is_set():
                return
            try:
                # Retry cancellations after a NAS outage even if inputs have
                # not changed since the discovery that first made it stale.
                state = (cancel_render if stale else get_render_state)(get_settings().nas_root, signature)
            except (ValueError, RuntimeError, OSError):
                continue  # A storage interruption must not overwrite known progress.
            with session_scope() as session:
                row = session.get(AutoRideRender, ride_id)
                encoded = json.dumps(state)
                if row and row.signature == signature and row.state_json != encoded:
                    row.state_json, row.updated_at = encoded, utcnow()

    def run(self):
        last_inputs = float("-inf")
        if self.stop_event.wait(20):
            return
        while not self.stop_event.is_set():
            now = time.monotonic()
            check = now - last_inputs >= get_settings().auto_rides_interval_s
            try:
                self.tick(check_inputs=check)
                if check:
                    last_inputs = now
            except Exception:
                log.exception("Automatic ride refresh deferred")
            self.stop_event.wait(60)


monitor = AutoRideMonitor()
