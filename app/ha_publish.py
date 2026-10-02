"""Background publisher of Drift's overall status to Home Assistant.

Exposes ONLY a small, overall picture: overall task progress (a single
percent across all sub-jobs), the overall status, and whether the camera is
connected. Per-job entities are not published, and any legacy per-job/uploads
entities from older versions are pruned on first run.

Runs as one daemon thread (like the rest of the app's background work). It
POSTs to HA when the published snapshot changes, and also refreshes unchanged
state periodically because REST-created HA states do not survive HA restarts.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import ha
from .database import session_scope
from .devicescan import get_device_monitor
from .jobs import jobs_overview
from .models import Job
from .settings_store import get_app_settings

log = logging.getLogger("drift.ha")

_FORCE_PUBLISH_INTERVAL_S = 60.0

# The overall progress/status published to HA is the same aggregate the jobs
# page uses (count-based across the current run).
compute_jobs_overview = jobs_overview


def camera_status() -> tuple[bool, Optional[str]]:
    """Whether a real DCIM camera is attached (a mounted NAS/media path is not)."""
    for device in get_device_monitor().get_devices():
        if device.get("dcim_path"):
            return True, device.get("label")
    return False, None


class HAPublisher:
    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pruned = False
        self._last_published: Optional[tuple] = None
        self._last_publish_time = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="drift-ha", daemon=True)
        self._thread.start()
        log.info("HA publisher started (interval=%ss)", self.interval)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("HA publish tick failed")

    def _prune_legacy(self, prefs) -> None:
        try:
            with session_scope() as s:
                job_ids = [row[0] for row in s.query(Job.id).filter(Job.kind == "upload").all()]
            removed = ha.prune_legacy_job_entities(prefs, job_ids)
            if removed:
                log.info("Pruned %d legacy HA job entities", removed)
        except Exception:  # noqa: BLE001
            log.exception("Legacy HA entity prune failed")

    def _tick(self) -> None:
        with session_scope() as s:
            prefs = get_app_settings(s)
            if not (prefs.ha_base_url and prefs.ha_token):
                return
            overview = compute_jobs_overview(s)
            running_jobs = s.query(Job).filter(
                Job.status == "running", Job.dismissed_at.is_(None)
            ).order_by(Job.started_at, Job.id).limit(4).all()
            kinds = tuple(sorted({job.kind for job in running_jobs}))
            labels = {
                "upload": "Uploading to NAS", "import": "Indexing camera",
                "verify": "Verifying camera backup", "trip": "Creating trip movie",
                "merge": "Joining videos", "thumbnail": "Creating previews",
                "timestamp": "Updating video timestamps",
            }
            task_summary = " · ".join(labels.get(kind, kind.title()) for kind in kinds)
            task_detail = running_jobs[0].detail or running_jobs[0].description if running_jobs else ""

        # Clear out the legacy per-job/uploads entities once, the first time we
        # find HA configured. Runs off the publish path because it can be many
        # deletes (one per historical upload job).
        if not self._pruned:
            self._pruned = True
            threading.Thread(
                target=self._prune_legacy, args=(prefs,), name="drift-ha-prune", daemon=True
            ).start()

        now = time.monotonic()
        camera_connected, camera_label = camera_status()

        snapshot = (
            prefs.ha_base_url, prefs.ha_entity_prefix, prefs.ha_token,
            overview["work_percent"],
            overview["status"],
            overview["active"],
            overview["running"],
            overview["queued"], overview["paused"],
            overview["work_completed_in_run"], overview["work_total_in_run"],
            task_summary, task_detail,
            camera_connected,
            camera_label,
        )
        if not self._should_publish(snapshot, now):
            return
        progress_ok = ha.publish_state(
            prefs,
            "progress",
            overview["work_percent"],
            {
                "status": overview["status"],
                "active_jobs": overview["active"],
                "running_jobs": overview["running"],
                "queued_jobs": overview["queued"],
                "paused_jobs": overview["paused"],
                "completed_tasks": overview["work_completed_in_run"],
                "total_tasks": overview["work_total_in_run"],
                "task_summary": task_summary or overview["status"].title(),
                "task_detail": (task_detail or "")[:256],
                "task_kinds": list(kinds),
                "camera_connected": camera_connected,
                "unit_of_measurement": "%",
                "friendly_name": "Drift task progress",
                "icon": "mdi:camera-gopro",
            },
        )
        active_ok = ha.publish_state(
            prefs, "active", "on" if overview["running"] else "off",
            {"friendly_name": "Drift tasks running", "device_class": "running"},
            domain="binary_sensor",
        )
        camera_ok = ha.publish_state(
            prefs,
            "camera",
            "connected" if camera_connected else "disconnected",
            {
                "device": camera_label,
                "friendly_name": "Drift camera",
                "icon": "mdi:camera" if camera_connected else "mdi:camera-off",
            },
        )
        # Failed requests retry on the next tick rather than waiting a minute.
        if progress_ok and active_ok and camera_ok:
            self._last_published = snapshot
            self._last_publish_time = now

    def _should_publish(self, snapshot: tuple, now: float) -> bool:
        if snapshot != self._last_published:
            return True
        return (now - self._last_publish_time) >= _FORCE_PUBLISH_INTERVAL_S


_publisher: Optional[HAPublisher] = None


def get_publisher() -> HAPublisher:
    global _publisher
    if _publisher is None:
        _publisher = HAPublisher()
    return _publisher
