"""Nextcloud destination via WebDAV.

Streams the file with an HTTP PUT (chunked from disk, never buffered whole in
RAM). Creates intermediate collections with MKCOL. base_path is the WebDAV
root for the user, e.g. https://cloud.example.com/remote.php/dav/files/USER
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import httpx

from ..config import get_settings
from ..crypto import decrypt
from .base import ProgressCb, UploadBackend, join_remote


class NextcloudBackend(UploadBackend):
    def _auth(self):
        return (self.destination.username or "", decrypt(self.destination.secret_enc or ""))

    def _base_url(self) -> str:
        return (self.destination.base_path or "").rstrip("/")

    def test_connection(self) -> None:
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            resp = client.request("PROPFIND", self._base_url(), headers={"Depth": "0"})
            if resp.status_code >= 400:
                raise RuntimeError(f"WebDAV PROPFIND failed: HTTP {resp.status_code}")

    def _ensure_collections(self, client: httpx.Client, remote_dir: str) -> None:
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        accum = self._base_url()
        for part in parts:
            accum = f"{accum}/{part}"
            resp = client.request("MKCOL", accum)
            # 201 created, 405 already exists -> both fine.
            if resp.status_code not in (201, 405, 301):
                raise RuntimeError(f"MKCOL {accum} failed: HTTP {resp.status_code}")

    def upload(self, local_path, remote_dir, filename, progress: ProgressCb = None) -> str:
        settings = get_settings()
        local_path = Path(local_path)
        total = local_path.stat().st_size
        url = join_remote(self._base_url(), remote_dir, filename)
        # join_remote collapses the scheme's // — rebuild from base + suffix.
        suffix = join_remote("/", remote_dir, filename)
        url = self._base_url() + suffix

        def stream() -> Iterator[bytes]:
            sent = 0
            with local_path.open("rb") as f:
                while True:
                    buf = f.read(settings.upload_chunk_bytes)
                    if not buf:
                        break
                    sent += len(buf)
                    if progress and total:
                        progress(sent / total)
                    yield buf

        with httpx.Client(timeout=None, auth=self._auth()) as client:
            self._ensure_collections(client, remote_dir)
            resp = client.put(
                url,
                content=stream(),
                headers={"Content-Length": str(total)},
            )
            if resp.status_code not in (200, 201, 204):
                raise RuntimeError(f"PUT failed: HTTP {resp.status_code} {resp.text[:300]}")
        return url
