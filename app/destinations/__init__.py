"""Upload destination backends."""
from __future__ import annotations

from typing import Callable, Optional

from ..models import Destination
from .base import UploadBackend
from .local import LocalBackend
from .nextcloud import NextcloudBackend
from .rsync import RsyncBackend
from .sftp import SFTPBackend

ProgressCb = Optional[Callable[[float], None]]


def get_backend(destination: Destination) -> UploadBackend:
    if destination.type == "nextcloud":
        return NextcloudBackend(destination)
    if destination.type == "rsync":
        return RsyncBackend(destination)
    if destination.type == "sftp":
        return SFTPBackend(destination)
    if destination.type in ("local", "nfs", "smb"):
        return LocalBackend(destination)
    raise ValueError(f"Unknown destination type: {destination.type}")
