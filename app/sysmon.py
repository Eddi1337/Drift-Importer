"""Background system monitor.

Samples host CPU% and network throughput on a fixed interval and persists each
tick to the ``system_samples`` table, pruning old rows. Storing the history
server-side lets the stats page render the last N minutes the instant it loads,
instead of the browser having to accumulate samples live after every page load.

Deliberately tiny (one daemon thread, like the rest of the app) to fit the Pi.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .database import session_scope
from .models import SystemSample, utcnow

log = logging.getLogger("drift.sysmon")

# 10s ticks keep the history light (last 6h ~= 2160 rows) while still being
# responsive enough for a live graph. Prune anything older than the retention.
SAMPLE_INTERVAL_S = 10.0
RETENTION = dt.timedelta(hours=6)
_PRUNE_EVERY_S = 300.0


def read_cpu_totals() -> Optional[tuple[int, int]]:
    """Return (total_jiffies, idle_jiffies) from /proc/stat, or None."""
    try:
        first = Path("/proc/stat").read_text().splitlines()[0].split()
    except (OSError, IndexError):
        return None
    if not first or first[0] != "cpu":
        return None
    values = [int(v) for v in first[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def read_network_totals() -> Optional[tuple[int, int]]:
    """Return (rx_bytes, tx_bytes) summed over non-loopback interfaces, or None."""
    try:
        lines = Path("/proc/net/dev").read_text().splitlines()[2:]
    except OSError:
        return None
    rx = tx = 0
    for line in lines:
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        if iface.strip() == "lo":
            continue
        parts = data.split()
        if len(parts) < 16:
            continue
        rx += int(parts[0])
        tx += int(parts[8])
    return rx, tx


class SystemMonitor:
    def __init__(self, interval: float = SAMPLE_INTERVAL_S, retention: dt.timedelta = RETENTION):
        self.interval = interval
        self.retention = retention
        self._lock = threading.Lock()
        self._last_cpu: Optional[tuple[int, int]] = None
        self._last_net: Optional[tuple[float, int, int]] = None
        self._latest = {
            "cpu_percent": None,
            "rx_bytes_per_s": 0,
            "tx_bytes_per_s": 0,
            "rx_bytes_total": None,
            "tx_bytes_total": None,
        }
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_prune = 0.0

    def start(self) -> None:
        # Prime the deltas (without storing) so the first persisted sample
        # reflects a real interval rather than counters-since-boot.
        try:
            self._tick(store=False)
        except Exception:  # noqa: BLE001
            log.exception("Initial system sample failed")
        self._thread = threading.Thread(target=self._run, name="drift-sysmon", daemon=True)
        self._thread.start()
        log.info("System monitor started (interval=%ss)", self.interval)

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        """Latest instantaneous values (for the gauge and current readouts)."""
        with self._lock:
            return dict(self._latest)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._tick(store=True)
            except Exception:  # noqa: BLE001
                log.exception("System monitor tick failed")

    def _tick(self, store: bool) -> None:
        now_mono = time.monotonic()

        cpu_percent: Optional[float] = None
        cpu = read_cpu_totals()
        if cpu is not None:
            if self._last_cpu is not None:
                total_delta = cpu[0] - self._last_cpu[0]
                idle_delta = cpu[1] - self._last_cpu[1]
                if total_delta > 0:
                    cpu_percent = round(
                        max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 1
                    )
            self._last_cpu = cpu

        rx_bps = tx_bps = 0
        rx_total = tx_total = None
        net = read_network_totals()
        if net is not None:
            rx_total, tx_total = net
            if self._last_net is not None:
                last_mono, last_rx, last_tx = self._last_net
                elapsed = max(0.001, now_mono - last_mono)
                rx_bps = max(0, int((net[0] - last_rx) / elapsed))
                tx_bps = max(0, int((net[1] - last_tx) / elapsed))
            self._last_net = (now_mono, net[0], net[1])

        load_1m: Optional[float] = None
        if hasattr(os, "getloadavg"):
            try:
                load_1m = round(os.getloadavg()[0], 2)
            except OSError:
                load_1m = None

        with self._lock:
            self._latest = {
                "cpu_percent": cpu_percent,
                "rx_bytes_per_s": rx_bps,
                "tx_bytes_per_s": tx_bps,
                "rx_bytes_total": rx_total,
                "tx_bytes_total": tx_total,
            }

        if not store:
            return
        with session_scope() as s:
            s.add(
                SystemSample(
                    cpu_percent=cpu_percent,
                    rx_bytes_per_s=rx_bps,
                    tx_bytes_per_s=tx_bps,
                    load_1m=load_1m,
                )
            )
        self._maybe_prune(now_mono)

    def _maybe_prune(self, now_mono: float) -> None:
        if now_mono < self._next_prune:
            return
        self._next_prune = now_mono + _PRUNE_EVERY_S
        cutoff = utcnow().replace(tzinfo=None) - self.retention
        try:
            with session_scope() as s:
                s.query(SystemSample).filter(SystemSample.created_at < cutoff).delete(
                    synchronize_session=False
                )
        except Exception:  # noqa: BLE001
            log.exception("Pruning old system samples failed")


_monitor: Optional[SystemMonitor] = None


def get_monitor() -> SystemMonitor:
    global _monitor
    if _monitor is None:
        _monitor = SystemMonitor()
    return _monitor
