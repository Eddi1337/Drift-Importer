"""Common interface and helpers for upload destinations."""
from __future__ import annotations

import datetime as dt
from pathlib import PurePosixPath
from typing import Callable, Optional

from ..models import Destination

ProgressCb = Optional[Callable[[int, int], None]]


def render_remote_dir(template: str, when: Optional[dt.datetime]) -> str:
    """Render a path template like '{year}/{month:02d}' from a capture time."""
    when = when or dt.datetime.now()
    try:
        return template.format(
            year=when.year,
            month=when.month,
            day=when.day,
            hour=when.hour,
        )
    except (KeyError, ValueError):
        return template


def join_remote(base: str, *parts: str) -> str:
    p = PurePosixPath(base or "/")
    for part in parts:
        part = part.strip("/")
        if part:
            p = p / part
    return str(p)


class UploadBackend:
    """Subclasses stream a local file to a remote target, reporting progress."""

    def __init__(self, destination: Destination):
        self.destination = destination

    def test_connection(self) -> None:
        """Raise an exception if the destination is unreachable/misconfigured."""
        raise NotImplementedError

    def get_resume_offset(self, remote_dir: str, filename: str, size_bytes: int) -> int:
        """Return existing uploaded bytes for a temporary remote file."""
        return 0

    def upload(
        self,
        local_path,
        remote_dir: str,
        filename: str,
        progress: ProgressCb = None,
        start_offset: int = 0,
    ) -> str:
        """Upload local_path into remote_dir/filename. Return the remote path."""
        raise NotImplementedError
