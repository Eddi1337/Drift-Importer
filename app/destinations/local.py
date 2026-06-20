"""Local / mounted-NAS destination.

A NAS mounted on the Pi (via /etc/fstab) is the simplest, most reliable way to
push to local storage: we just stream-copy into a directory. base_path is the
mount point (e.g. /mnt/nas/camera).
"""
from __future__ import annotations

import os
import shutil
import datetime as dt
from pathlib import Path

from ..config import get_settings
from ..media import checksum as sampled_checksum
from .base import ProgressCb, RemoteEntry, UploadBackend, join_remote


class LocalBackend(UploadBackend):
    def _root(self) -> Path:
        return Path(self.destination.base_path or "/")

    def list_directories(self, path: str = "") -> list[str]:
        root = self._root() / path.strip("/")
        if not root.exists():
            raise FileNotFoundError(f"Path does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {root}")
        return sorted(
            entry.name for entry in root.iterdir() if entry.is_dir()
        )

    def list_entries(self, path: str = "") -> list[RemoteEntry]:
        root = self._root() / path.strip("/")
        if not root.exists():
            raise FileNotFoundError(f"Path does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {root}")
        rows: list[RemoteEntry] = []
        for entry in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            try:
                stat = entry.stat()
            except OSError:
                continue
            rows.append(
                {
                    "name": entry.name,
                    "path": join_remote("/", path, entry.name).strip("/"),
                    "type": "directory" if entry.is_dir() else "file",
                    "size_bytes": None if entry.is_dir() else int(stat.st_size),
                    "modified_at": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
        return rows

    def test_connection(self) -> None:
        root = self._root()
        if not root.exists():
            raise FileNotFoundError(f"Path does not exist: {root}")
        if not os.access(root, os.W_OK):
            raise PermissionError(f"Path is not writable: {root}")

    def storage_info(self) -> dict[str, int | None]:
        usage = shutil.disk_usage(self._root())
        return {
            "free_bytes": int(usage.free),
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
        }

    def get_resume_offset(self, remote_dir: str, filename: str, size_bytes: int) -> int:
        dest_dir = Path(join_remote(str(self._root()), remote_dir))
        target = dest_dir / filename
        tmp = target.with_suffix(target.suffix + ".part")
        if tmp.exists():
            return min(tmp.stat().st_size, size_bytes)
        return 0

    def remote_file_matches(
        self,
        remote_dir: str,
        filename: str,
        size_bytes: int,
        checksum: str,
    ) -> bool:
        target = Path(join_remote(str(self._root()), remote_dir)) / filename
        if not target.exists() or not target.is_file():
            return False
        if target.stat().st_size != size_bytes:
            return False
        return sampled_checksum(target) == checksum

    def upload(
        self,
        local_path,
        remote_dir,
        filename,
        progress: ProgressCb = None,
        start_offset: int = 0,
    ) -> str:
        settings = get_settings()
        local_path = Path(local_path)
        dest_dir = Path(join_remote(str(self._root()), remote_dir))
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / filename
        tmp = target.with_suffix(target.suffix + ".part")
        total = local_path.stat().st_size
        written = start_offset
        chunk = settings.upload_chunk_bytes
        if start_offset >= total and target.exists():
            if progress and total:
                progress(total, total)
            return str(target)
        mode = "ab" if start_offset else "wb"
        with local_path.open("rb") as src, tmp.open(mode) as dst:
            if start_offset:
                src.seek(start_offset)
            while True:
                buf = src.read(chunk)
                if not buf:
                    break
                dst.write(buf)
                written += len(buf)
                if progress and total:
                    progress(written, total)
        os.replace(tmp, target)
        return str(target)
