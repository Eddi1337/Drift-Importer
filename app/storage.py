"""Fail closed on missing NAS mounts, including autofs placeholder directories."""
from __future__ import annotations

import re
from pathlib import Path

from .config import get_settings

NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "smbfs"}


def filesystem_type(path: Path) -> str | None:
    resolved = path.resolve()
    # Trigger systemd automount before inspecting the actual mounted filesystem.
    resolved.stat()
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return None
    found = (0, None)
    for line in mountinfo.read_text().splitlines():
        left, _, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or not tail:
            continue
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if resolved.is_relative_to(mount) and len(str(mount)) >= found[0]:
            found = (len(str(mount)), tail[0])
    return found[1]


def require_storage(root: Path) -> Path:
    root = root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"NAS directory {root} is unavailable")
    if get_settings().require_nas_mount and filesystem_type(root) not in NETWORK_FS:
        raise RuntimeError(f"{root} is not a mounted NAS. Refusing to write to the SD card.")
    return root


def require_working_storage(path: Path) -> None:
    settings = get_settings()
    if settings.require_nas_mount:
        root = require_storage(settings.nas_root)
        if not path.resolve().is_relative_to(root):
            raise RuntimeError(f"Working directory {path} must be on the NAS at {root}")
    path.mkdir(parents=True, exist_ok=True)


def archive_path(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(root.resolve()):
        return None
    return candidate
