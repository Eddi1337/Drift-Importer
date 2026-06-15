"""Detection and scanning of attached DCIM camera devices."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import get_settings
from .media import classify


@dataclass
class DeviceCandidate:
    path: Path
    label: str
    dcim_path: Optional[Path]
    free_bytes: int
    total_bytes: int


def _disk_usage(path: Path):
    import shutil

    try:
        usage = shutil.disk_usage(str(path))
        return usage.free, usage.total
    except OSError:
        return 0, 0


def find_dcim(root: Path) -> Optional[Path]:
    """Locate a DCIM directory under a mount root (case-insensitive)."""
    try:
        if not root.exists():
            return None
        for child in root.iterdir():
            try:
                if child.is_dir() and child.name.lower() == "dcim":
                    return child
            except OSError:
                continue
    except OSError:
        return None
    return None


def detect_devices() -> List[DeviceCandidate]:
    """Scan configured mount paths for removable media with photos/videos."""
    settings = get_settings()
    found: List[DeviceCandidate] = []
    seen = set()
    for base in settings.mount_path_list:
        try:
            if not base.exists():
                continue
            # A mount base like /media/<user> contains one dir per mounted volume.
            sub = []
            for p in base.iterdir():
                try:
                    if p.is_dir():
                        sub.append(p)
                except OSError:
                    continue
            candidates = [base] + sub
        except OSError:
            continue
        for mount in candidates:
            if mount in seen:
                continue
            seen.add(mount)
            dcim = find_dcim(mount)
            if dcim is None and mount == base:
                continue
            free, total = _disk_usage(mount)
            found.append(
                DeviceCandidate(
                    path=mount,
                    label=mount.name or str(mount),
                    dcim_path=dcim,
                    free_bytes=free,
                    total_bytes=total,
                )
            )
    return found


def scan_media_files(root: Path) -> List[Path]:
    """Recursively list all video/image files under a directory."""
    results: List[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=lambda e: None):
        for name in filenames:
            p = Path(dirpath) / name
            if classify(p) is not None:
                results.append(p)
    return sorted(results)
