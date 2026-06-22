"""Background camera-detection so the API never blocks on a slow mount.

A daemon thread periodically scans for an attached camera (excluding the upload
destinations and any network filesystems) and caches the result. ``/api/devices``
just returns the cache, so the request returns instantly even while a NAS upload
is saturating the network.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .database import session_scope
from .devices import detect_devices, scan_media_files
from .models import Destination

log = logging.getLogger("drift.devices")

SCAN_INTERVAL_S = 8.0
SCAN_BUDGET_S = 8.0


def _device_dict(device, deadline: float) -> dict:
    file_count = len(scan_media_files(device.dcim_path, deadline=deadline)) if device.dcim_path else 0
    return {
        "path": str(device.path),
        "label": device.label,
        "dcim_path": str(device.dcim_path) if device.dcim_path else None,
        "free_bytes": device.free_bytes,
        "total_bytes": device.total_bytes,
        "file_count": file_count,
    }


class DeviceMonitor:
    def __init__(self, interval: float = SCAN_INTERVAL_S):
        self.interval = interval
        self._lock = threading.Lock()
        self._cache: list[dict] = []
        self._scanned = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="drift-devices", daemon=True)
        self._thread.start()
        log.info("Device monitor started (interval=%ss)", self.interval)

    def stop(self) -> None:
        self._stop.set()

    def get_devices(self) -> list[dict]:
        with self._lock:
            return list(self._cache)

    def has_scanned(self) -> bool:
        with self._lock:
            return self._scanned

    def _run(self) -> None:
        self._scan()  # populate immediately, then on the interval
        while not self._stop.wait(self.interval):
            self._scan()

    def _scan(self) -> None:
        try:
            with session_scope() as s:
                exclude = [d.base_path for d in s.query(Destination).all() if d.base_path]
            devices = detect_devices(exclude_paths=exclude, time_budget=SCAN_BUDGET_S)
            deadline = time.monotonic() + SCAN_BUDGET_S
            cache = [_device_dict(d, deadline) for d in devices]
            with self._lock:
                self._cache = cache
                self._scanned = True
        except Exception:  # noqa: BLE001
            log.exception("Device scan failed")


_monitor: Optional[DeviceMonitor] = None


def get_device_monitor() -> DeviceMonitor:
    global _monitor
    if _monitor is None:
        _monitor = DeviceMonitor()
    return _monitor
