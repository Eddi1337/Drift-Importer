"""Local / mounted-NAS destination.

A NAS mounted on the Pi (via /etc/fstab) is the simplest, most reliable way to
push to local storage: we just stream-copy into a directory. base_path is the
mount point (e.g. /mnt/nas/camera).
"""
from __future__ import annotations

import os
from pathlib import Path

from ..config import get_settings
from .base import ProgressCb, UploadBackend, join_remote


class LocalBackend(UploadBackend):
    def _root(self) -> Path:
        return Path(self.destination.base_path or "/")

    def test_connection(self) -> None:
        root = self._root()
        if not root.exists():
            raise FileNotFoundError(f"Path does not exist: {root}")
        if not os.access(root, os.W_OK):
            raise PermissionError(f"Path is not writable: {root}")

    def upload(self, local_path, remote_dir, filename, progress: ProgressCb = None) -> str:
        settings = get_settings()
        local_path = Path(local_path)
        dest_dir = Path(join_remote(str(self._root()), remote_dir))
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / filename
        tmp = target.with_suffix(target.suffix + ".part")
        total = local_path.stat().st_size
        written = 0
        chunk = settings.upload_chunk_bytes
        with local_path.open("rb") as src, tmp.open("wb") as dst:
            while True:
                buf = src.read(chunk)
                if not buf:
                    break
                dst.write(buf)
                written += len(buf)
                if progress and total:
                    progress(written / total)
        os.replace(tmp, target)
        return str(target)
