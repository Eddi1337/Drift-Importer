"""Background publisher of Drift's overall status to Home Assistant.

Exposes ONLY a small, overall picture: overall upload progress (a single
percent across all sub-jobs), the overall status, and whether the camera is
connected. Per-job entities are not published, and any legacy per-job/uploads
entities from older versions are pruned on first run.

Runs as one daemon thread (like the rest of the app's background work) and only
POSTs to HA when the published snapshot actually changes.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import ha
from .database import session_scope
from .devices import detect_devices
from .jobs import jobs_overview
from .models import Job
from .settings_store import get_app_settings

log = logging.getLogger("drift.ha")

# Camera detection walks mount paths (incl. an NFS NAS), so do it less often
# than the cheap DB-only progress refresh.
_DEVICE_SCAN_INTERVAL_S = 15.0

# The overall progress/status published to HA is the same aggregate the jobs
# page uses (count-based across the current run).
compute_jobs_overview = jobs_overview


def camera_status() -> tuple[bool, Optional[str]]:
    """Whether a real DCIM camera is attached (a mounted NAS/media path is not)."""
    for device in detect_devices():
        if device.dcim_path is not None and device.dcim_path.name.lower() == "dcim":
            return True, device.label
    return False, None


class HAPublisher:
    def __init__(self, interval: float = 5.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pruned = False
        self._last_published: Optional[tuple] = None
        self._camera: tuple[bool, Optional[str]] = (False, None)
        self._next_device_scan = 0.0

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

        # Clear out the legacy per-job/uploads entities once, the first time we
        # find HA configured. Runs off the publish path because it can be many
        # deletes (one per historical upload job).
        if not self._pruned:
            self._pruned = True
            threading.Thread(
                target=self._prune_legacy, args=(prefs,), name="drift-ha-prune", daemon=True
            ).start()

        now = time.monotonic()
        if now >= self._next_device_scan:
            self._camera = camera_status()
            self._next_device_scan = now + _DEVICE_SCAN_INTERVAL_S
        camera_connected, camera_label = self._camera

        snapshot = (
            overview["percent"],
            overview["status"],
            overview["active"],
            overview["running"],
            camera_connected,
            camera_label,
        )
        if snapshot == self._last_published:
            return
        self._last_published = snapshot

        ha.publish_state(
            prefs,
            "progress",
            overview["percent"],
            {
                "status": overview["status"],
                "active_jobs": overview["active"],
                "running_jobs": overview["running"],
                "queued_jobs": overview["queued"],
                "camera_connected": camera_connected,
                "unit_of_measurement": "%",
                "friendly_name": "Drift upload progress",
                "icon": "mdi:cloud-upload",
            },
        )
        ha.publish_state(
            prefs,
            "camera",
            "connected" if camera_connected else "disconnected",
            {
                "device": camera_label,
                "friendly_name": "Drift camera",
                "icon": "mdi:camera" if camera_connected else "mdi:camera-off",
            },
        )


_publisher: Optional[HAPublisher] = None


def get_publisher() -> HAPublisher:
    global _publisher
    if _publisher is None:
        _publisher = HAPublisher()
    return _publisher
