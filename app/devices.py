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


def find_dcim(root: Path, max_depth: int = 2) -> Optional[Path]:
    """Locate a DCIM directory under a mount root (case-insensitive)."""
    root = root.resolve()
    try:
        if not root.exists():
            return None
        queue: list[tuple[Path, int]] = [(root, 0)]
        while queue:
            current, depth = queue.pop(0)
            for child in current.iterdir():
                try:
                    if not child.is_dir():
                        continue
                    if child.name.lower() == "dcim":
                        return child
                    if depth < max_depth:
                        queue.append((child, depth + 1))
                except OSError:
                    continue
    except OSError:
        return None
    return None


def _candidate_mounts(base: Path) -> list[Path]:
    """Return likely mounted volumes below a configured mount base."""
    candidates = [base]
    try:
        first_level = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return candidates
    candidates.extend(first_level)
    for parent in first_level:
        try:
            candidates.extend(p for p in parent.iterdir() if p.is_dir())
        except OSError:
            continue
    return candidates


def _device_label(mount: Path, dcim: Path) -> str:
    try:
        return dcim.parent.name or mount.name or str(mount)
    except IndexError:
        return mount.name or str(mount)


def _device_root(mount: Path, dcim: Path) -> Path:
    try:
        return dcim.parent
    except IndexError:
        return mount


def detect_devices() -> List[DeviceCandidate]:
    """Scan configured mount paths for removable media with photos/videos."""
    settings = get_settings()
    found: List[DeviceCandidate] = []
    seen_dcim = set()
    for base in settings.mount_path_list:
        if not base.exists():
            continue
        for mount in _candidate_mounts(base):
            dcim = find_dcim(mount)
            if dcim is None:
                continue
            dcim_key = str(dcim.resolve())
            if dcim_key in seen_dcim:
                continue
            seen_dcim.add(dcim_key)
            root = _device_root(mount, dcim)
            free, total = _disk_usage(root)
            found.append(
                DeviceCandidate(
                    path=root,
                    label=_device_label(mount, dcim),
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
