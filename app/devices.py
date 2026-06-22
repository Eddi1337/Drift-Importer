"""Detection and scanning of attached DCIM camera devices.

The scan deliberately avoids network filesystems (NFS/CIFS) and configured
upload destinations: walking a busy NFS-mounted NAS can take many seconds (or
effectively hang), which used to block the device-detection request. Callers
pass the destinations to exclude; network mounts are detected from the mount
table. Every walk is also bounded by a deadline so a single scan can't run away.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from .config import get_settings
from .media import classify

# Filesystem types we never walk looking for a camera.
NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "ncpfs", "afs", "9p",
    "glusterfs", "ceph", "fuse.sshfs", "fuse.rclone", "fuse.glusterfs",
}

ExcludeFn = Callable[[Path], bool]


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


def _read_mount_table() -> list[tuple[str, str]]:
    """Return (mountpoint, fstype) pairs from the container and host tables."""
    rows: list[tuple[str, str]] = []
    for source in ("/proc/mounts", "/host/proc/1/mounts"):
        try:
            text = Path(source).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows.append((parts[1], parts[2]))
    return rows


def network_mountpoints() -> list[str]:
    """Mountpoints backed by a network filesystem (NFS/CIFS/…)."""
    return [mp for mp, fstype in _read_mount_table() if fstype in NETWORK_FS]


def build_excluder(exclude_paths: Iterable[str] = ()) -> ExcludeFn:
    """A fast, stat-free predicate: is this path a destination or network mount
    (or under one)? String-only so it never touches a slow/remote filesystem."""
    blocked = {str(Path(p)).rstrip("/") for p in exclude_paths if p}
    blocked |= {mp.rstrip("/") for mp in network_mountpoints()}
    blocked = {b for b in blocked if b and b != "/"}

    def is_excluded(path: Path) -> bool:
        p = str(path).rstrip("/")
        return any(p == b or p.startswith(b + "/") for b in blocked)

    return is_excluded


def _expired(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() > deadline


def find_dcim(
    root: Path,
    max_depth: int = 2,
    is_excluded: Optional[ExcludeFn] = None,
    deadline: Optional[float] = None,
) -> Optional[Path]:
    """Locate a DCIM directory under a mount root (case-insensitive)."""
    root = root.resolve()
    try:
        if not root.exists():
            return None
        queue: list[tuple[Path, int]] = [(root, 0)]
        while queue:
            if _expired(deadline):
                return None
            current, depth = queue.pop(0)
            for child in current.iterdir():
                if is_excluded and is_excluded(child):
                    continue
                try:
                    if not child.is_dir():
                        continue
                    if child.is_symlink() or child.name.startswith("."):
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


def find_media_root(
    root: Path,
    max_depth: int = 4,
    is_excluded: Optional[ExcludeFn] = None,
    deadline: Optional[float] = None,
) -> Optional[Path]:
    """Locate a mounted folder containing media when no DCIM folder exists."""
    root = root.resolve()
    try:
        if not root.exists() or root.is_symlink():
            return None
        queue: list[tuple[Path, int]] = [(root, 0)]
        while queue:
            if _expired(deadline):
                return None
            current, depth = queue.pop(0)
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            if any(child.is_file() and classify(child) == "video" for child in children):
                return current
            if depth >= max_depth:
                continue
            for child in children:
                if is_excluded and is_excluded(child):
                    continue
                try:
                    if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
                        queue.append((child, depth + 1))
                except OSError:
                    continue
    except OSError:
        return None
    return None


def _candidate_mounts(base: Path, is_excluded: Optional[ExcludeFn] = None) -> list[Path]:
    """Return likely mounted volumes below a configured mount base.

    Excluded paths (network mounts / destinations) are skipped *before* any
    stat or directory read, so a busy NAS is never touched here.
    """
    candidates = [base]

    def listdir(d: Path) -> list[Path]:
        try:
            return list(d.iterdir())
        except OSError:
            return []

    for child in listdir(base):
        if is_excluded and is_excluded(child):
            continue
        try:
            if not child.is_dir() or child.is_symlink() or child.name.startswith("."):
                continue
        except OSError:
            continue
        candidates.append(child)
        for grand in listdir(child):
            if is_excluded and is_excluded(grand):
                continue
            try:
                if grand.is_dir() and not grand.is_symlink() and not grand.name.startswith("."):
                    candidates.append(grand)
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


def detect_devices(
    exclude_paths: Iterable[str] = (),
    time_budget: Optional[float] = 8.0,
) -> List[DeviceCandidate]:
    """Scan configured mount paths for removable media with photos/videos.

    Network filesystems and ``exclude_paths`` (e.g. the upload destination) are
    skipped entirely, and the whole scan is bounded by ``time_budget`` seconds.
    """
    settings = get_settings()
    is_excluded = build_excluder(exclude_paths)
    deadline = time.monotonic() + time_budget if time_budget else None
    found: List[DeviceCandidate] = []
    seen_roots = set()
    for base in settings.mount_path_list:
        if _expired(deadline):
            break
        if is_excluded(base) or not base.exists():
            continue
        for mount in _candidate_mounts(base, is_excluded):
            if _expired(deadline):
                break
            if is_excluded(mount):
                continue
            dcim = find_dcim(mount, is_excluded=is_excluded, deadline=deadline)
            media_root = dcim or find_media_root(mount, is_excluded=is_excluded, deadline=deadline)
            if media_root is None:
                continue
            root_key = str(media_root.resolve())
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            root = _device_root(mount, media_root)
            free, total = _disk_usage(root)
            found.append(
                DeviceCandidate(
                    path=root,
                    label=_device_label(mount, media_root),
                    dcim_path=media_root,
                    free_bytes=free,
                    total_bytes=total,
                )
            )
    return found


def scan_media_files(
    root: Path,
    deadline: Optional[float] = None,
    is_excluded: Optional[ExcludeFn] = None,
) -> List[Path]:
    """Recursively list all video/image files under a directory (time-bounded)."""
    results: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if _expired(deadline):
            break
        if is_excluded and is_excluded(Path(dirpath)):
            dirnames[:] = []
            continue
        for name in filenames:
            p = Path(dirpath) / name
            if classify(p) is not None:
                results.append(p)
    return sorted(results)
