"""Nextcloud destination via WebDAV.

Streams the file with an HTTP PUT (chunked from disk, never buffered whole in
RAM). Creates intermediate collections with MKCOL. base_path is the WebDAV
root for the user, e.g. https://cloud.example.com/remote.php/dav/files/USER
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import httpx

from ..config import get_settings
from ..crypto import decrypt
from .base import ProgressCb, UploadBackend, join_remote


class NextcloudBackend(UploadBackend):
    def _auth(self):
        return (self.destination.username or "", decrypt(self.destination.secret_enc or ""))

    def _base_url(self) -> str:
        return (self.destination.base_path or "").rstrip("/")

    def _build_url(self, remote_dir: str, filename: str, part: bool = False) -> str:
        suffix = join_remote("/", remote_dir, filename + (".part" if part else ""))
        return self._base_url() + suffix

    def test_connection(self) -> None:
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            resp = client.request("PROPFIND", self._base_url(), headers={"Depth": "0"})
            if resp.status_code >= 400:
                raise RuntimeError(f"WebDAV PROPFIND failed: HTTP {resp.status_code}")

    def list_directories(self, path: str = "") -> list[str]:
        target = self._base_url() + join_remote("/", path)
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            resp = client.request("PROPFIND", target, headers={"Depth": "1"})
            if resp.status_code >= 400:
                raise RuntimeError(f"WebDAV PROPFIND failed: HTTP {resp.status_code}")
        ns = {
            "d": "DAV:",
        }
        root = ET.fromstring(resp.text)
        names = []
        base_href = None
        for response in root.findall("d:response", ns):
            href = response.findtext("d:href", default="", namespaces=ns)
            if base_href is None:
                base_href = href.rstrip("/")
                continue
            kind = response.find("d:propstat/d:prop/d:resourcetype/d:collection", ns)
            if kind is None:
                continue
            child = unquote(href.rstrip("/").split("/")[-1])
            if child:
                names.append(child)
        return sorted(set(names))

    def _ensure_collections(self, client: httpx.Client, remote_dir: str) -> None:
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        accum = self._base_url()
        for part in parts:
            accum = f"{accum}/{part}"
            resp = client.request("MKCOL", accum)
            # 201 created, 405 already exists -> both fine.
            if resp.status_code not in (201, 405, 301):
                raise RuntimeError(f"MKCOL {accum} failed: HTTP {resp.status_code}")

    def get_resume_offset(self, remote_dir: str, filename: str, size_bytes: int) -> int:
        tmp_url = self._build_url(remote_dir, filename, part=True)
        final_url = self._build_url(remote_dir, filename, part=False)
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            final_resp = client.head(final_url)
            final_size = int(final_resp.headers.get("Content-Length", "0") or 0)
            if final_resp.status_code < 400 and final_size == size_bytes:
                return size_bytes
            resp = client.head(tmp_url)
            if resp.status_code >= 400:
                return 0
            return min(int(resp.headers.get("Content-Length", "0") or 0), size_bytes)

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
        total = local_path.stat().st_size
        tmp_url = self._build_url(remote_dir, filename, part=True)
        final_url = self._build_url(remote_dir, filename, part=False)

        def stream() -> Iterator[bytes]:
            sent = start_offset
            with local_path.open("rb") as f:
                if start_offset:
                    f.seek(start_offset)
                while True:
                    buf = f.read(settings.upload_chunk_bytes)
                    if not buf:
                        break
                    sent += len(buf)
                    if progress and total:
                        progress(sent, total)
                    yield buf

        with httpx.Client(timeout=None, auth=self._auth()) as client:
            self._ensure_collections(client, remote_dir)
            headers = {"Content-Length": str(max(0, total - start_offset))}
            if start_offset:
                headers["Content-Range"] = f"bytes {start_offset}-{total - 1}/{total}"
            resp = client.put(
                tmp_url,
                content=stream(),
                headers=headers,
            )
            if resp.status_code not in (200, 201, 204):
                if start_offset:
                    # Fallback for servers that reject ranged PUTs: restart cleanly.
                    return self.upload(local_path, remote_dir, filename, progress, start_offset=0)
                raise RuntimeError(f"PUT failed: HTTP {resp.status_code} {resp.text[:300]}")
            move = client.request(
                "MOVE",
                tmp_url,
                headers={"Destination": final_url, "Overwrite": "T"},
            )
            if move.status_code not in (201, 204):
                raise RuntimeError(f"MOVE failed: HTTP {move.status_code} {move.text[:300]}")
        return final_url
