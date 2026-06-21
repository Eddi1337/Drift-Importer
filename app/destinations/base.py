"""Common interface and helpers for upload destinations."""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import PurePosixPath
from typing import Callable, Optional, TypedDict

from ..models import Destination

ProgressCb = Optional[Callable[[int, int], None]]


def make_probe() -> tuple[str, bytes]:
    """A tiny, uniquely-named test file used to verify read+write end-to-end."""
    token = uuid.uuid4().hex
    return f".drift-selftest-{token}.tmp", f"drift-selftest:{token}".encode("utf-8")


class RemoteEntry(TypedDict):
    name: str
    path: str
    type: str
    size_bytes: int | None
    modified_at: str | None


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

    def verify_round_trip(self) -> None:
        """Write a small probe file, read it back, verify it, and delete it.

        Proves the destination is genuinely writable *and* readable
        end-to-end, not merely reachable. Raises on any mismatch or failure.
        """
        raise NotImplementedError

    def list_directories(self, path: str = "") -> list[str]:
        """Return child directories for the destination path or a subpath."""
        raise NotImplementedError

    def list_entries(self, path: str = "") -> list[RemoteEntry]:
        """Return child directories and files for a destination path."""
        return [
            {
                "name": name,
                "path": join_remote(path, name).strip("/"),
                "type": "directory",
                "size_bytes": None,
                "modified_at": None,
            }
            for name in self.list_directories(path)
        ]

    def storage_info(self) -> dict:
        """Return best-effort storage totals for the destination root."""
        return {"free_bytes": None, "total_bytes": None, "used_bytes": None}

    def get_storage_info(self) -> dict[str, int | None]:
        """Return storage totals when the backend can determine them."""
        return self.storage_info()

    def get_resume_offset(self, remote_dir: str, filename: str, size_bytes: int) -> int:
        """Return existing uploaded bytes for a temporary remote file."""
        return 0

    def remote_file_matches(
        self,
        remote_dir: str,
        filename: str,
        size_bytes: int,
        checksum: str,
    ) -> bool:
        """Return True only when the final remote file matches size and sha256."""
        return False

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
