"""Nextcloud destination via WebDAV.

Streams the file with an HTTP PUT (chunked from disk, never buffered whole in
RAM). Creates intermediate collections with MKCOL. base_path is the WebDAV
root for the user, e.g. https://cloud.example.com/remote.php/dav/files/USER
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import httpx

from ..config import get_settings
from ..crypto import decrypt
from .base import ProgressCb, RemoteEntry, UploadBackend, join_remote, make_probe


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

    def verify_round_trip(self) -> None:
        name, payload = make_probe()
        url = self._base_url() + "/" + name
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            put = client.put(url, content=payload)
            if put.status_code not in (200, 201, 204):
                raise RuntimeError(f"WebDAV PUT failed: HTTP {put.status_code}")
            try:
                got = client.get(url)
                if got.status_code >= 400:
                    raise RuntimeError(f"WebDAV GET failed: HTTP {got.status_code}")
                if got.content != payload:
                    raise RuntimeError("Read-back mismatch on Nextcloud destination")
            finally:
                client.request("DELETE", url)

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

    def list_entries(self, path: str = "") -> list[RemoteEntry]:
        target = self._base_url() + join_remote("/", path)
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            resp = client.request("PROPFIND", target, headers={"Depth": "1"})
            if resp.status_code >= 400:
                raise RuntimeError(f"WebDAV PROPFIND failed: HTTP {resp.status_code}")
        ns = {"d": "DAV:"}
        root = ET.fromstring(resp.text)
        rows: list[RemoteEntry] = []
        first = True
        for response in root.findall("d:response", ns):
            if first:
                first = False
                continue
            href = response.findtext("d:href", default="", namespaces=ns)
            name = unquote(href.rstrip("/").split("/")[-1])
            if not name:
                continue
            is_dir = response.find("d:propstat/d:prop/d:resourcetype/d:collection", ns) is not None
            size_text = response.findtext("d:propstat/d:prop/d:getcontentlength", namespaces=ns)
            modified_text = response.findtext("d:propstat/d:prop/d:getlastmodified", namespaces=ns)
            modified_at = None
            if modified_text:
                try:
                    modified_at = dt.datetime.strptime(
                        modified_text,
                        "%a, %d %b %Y %H:%M:%S %Z",
                    ).isoformat()
                except ValueError:
                    modified_at = modified_text
            rows.append(
                {
                    "name": name,
                    "path": join_remote("/", path, name).strip("/"),
                    "type": "directory" if is_dir else "file",
                    "size_bytes": None if is_dir else int(size_text or 0),
                    "modified_at": modified_at,
                }
            )
        return sorted(rows, key=lambda row: (row["type"] != "directory", row["name"].lower()))

    def storage_info(self) -> dict:
        body = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:quota-available-bytes/>
    <d:quota-used-bytes/>
  </d:prop>
</d:propfind>"""
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            resp = client.request(
                "PROPFIND",
                self._base_url(),
                headers={"Depth": "0", "Content-Type": "application/xml"},
                content=body,
            )
            if resp.status_code >= 400:
                return {"free_bytes": None, "total_bytes": None, "used_bytes": None}
        ns = {"d": "DAV:"}
        root = ET.fromstring(resp.text)
        prop = root.find("d:response/d:propstat/d:prop", ns)
        if prop is None:
            return {"free_bytes": None, "total_bytes": None, "used_bytes": None}
        free = prop.findtext("d:quota-available-bytes", namespaces=ns)
        used = prop.findtext("d:quota-used-bytes", namespaces=ns)
        try:
            free_bytes = int(free) if free not in (None, "-3") else None
            used_bytes = int(used) if used is not None else None
        except ValueError:
            return {"free_bytes": None, "total_bytes": None, "used_bytes": None}
        total_bytes = (
            free_bytes + used_bytes
            if free_bytes is not None and used_bytes is not None
            else None
        )
        return {
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
        }

    def get_storage_info(self) -> dict[str, int | None]:
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            resp = client.request("PROPFIND", self._base_url(), headers={"Depth": "0"})
            if resp.status_code >= 400:
                raise RuntimeError(f"WebDAV PROPFIND failed: HTTP {resp.status_code}")
        ns = {"d": "DAV:"}
        root = ET.fromstring(resp.text)
        response = root.find("d:response", ns)
        if response is None:
            return {"free_bytes": None, "total_bytes": None}
        free_text = response.findtext(
            "d:propstat/d:prop/d:quota-available-bytes",
            default="",
            namespaces=ns,
        )
        used_text = response.findtext(
            "d:propstat/d:prop/d:quota-used-bytes",
            default="",
            namespaces=ns,
        )
        try:
            free_bytes = int(free_text)
        except (TypeError, ValueError):
            free_bytes = None
        try:
            used_bytes = int(used_text)
        except (TypeError, ValueError):
            used_bytes = None
        total_bytes = None
        if free_bytes is not None and used_bytes is not None:
            total_bytes = free_bytes + used_bytes
        return {"free_bytes": free_bytes, "total_bytes": total_bytes}

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
        with httpx.Client(timeout=30, auth=self._auth()) as client:
            resp = client.head(tmp_url)
            if resp.status_code >= 400:
                return 0
            return min(int(resp.headers.get("Content-Length", "0") or 0), size_bytes)

    def remote_file_matches(
        self,
        remote_dir: str,
        filename: str,
        size_bytes: int,
        checksum: str,
    ) -> bool:
        import hashlib

        final_url = self._build_url(remote_dir, filename, part=False)
        with httpx.Client(timeout=None, auth=self._auth()) as client:
            head = client.head(final_url)
            if head.status_code >= 400:
                return False
            remote_size = int(head.headers.get("Content-Length", "0") or 0)
            if remote_size != size_bytes:
                return False
            h = hashlib.sha256()
            with client.stream("GET", final_url) as resp:
                if resp.status_code >= 400:
                    return False
                for chunk in resp.iter_bytes():
                    h.update(chunk)
            return h.hexdigest() == checksum

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
