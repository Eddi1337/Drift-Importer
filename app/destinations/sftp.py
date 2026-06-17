"""SFTP destination via paramiko. Streams from disk in chunks."""
from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath

import paramiko

from ..config import get_settings
from ..crypto import decrypt
from .base import ProgressCb, UploadBackend, join_remote


class SFTPBackend(UploadBackend):
    def _connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.destination.host,
            port=self.destination.port or 22,
            username=self.destination.username,
            password=decrypt(self.destination.secret_enc or "") or None,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        return client

    def test_connection(self) -> None:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            sftp.listdir(self.destination.base_path or ".")
            sftp.close()
        finally:
            client.close()

    def list_directories(self, path: str = "") -> list[str]:
        client = self._connect()
        try:
            sftp = client.open_sftp()
            root = join_remote(self.destination.base_path or "/", path)
            names = []
            for entry in sftp.listdir_attr(root):
                if stat.S_ISDIR(entry.st_mode):
                    names.append(entry.filename)
            sftp.close()
            return sorted(names)
        finally:
            client.close()

    @staticmethod
    def _mkdirs(sftp, remote_dir: str) -> None:
        parts = PurePosixPath(remote_dir).parts
        accum = ""
        for part in parts:
            accum = accum + "/" + part if accum or part == "/" else part
            if part == "/":
                accum = "/"
                continue
            try:
                sftp.stat(accum)
            except IOError:
                sftp.mkdir(accum)

    def get_resume_offset(self, remote_dir: str, filename: str, size_bytes: int) -> int:
        full_dir = join_remote(self.destination.base_path or "/", remote_dir)
        remote_path = join_remote(full_dir, filename)
        tmp_path = remote_path + ".part"
        client = self._connect()
        try:
            sftp = client.open_sftp()
            try:
                stat = sftp.stat(remote_path)
                if stat.st_size == size_bytes:
                    return size_bytes
            except IOError:
                pass
            try:
                stat = sftp.stat(tmp_path)
                return min(stat.st_size, size_bytes)
            except IOError:
                return 0
            finally:
                sftp.close()
        finally:
            client.close()

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
        full_dir = join_remote(self.destination.base_path or "/", remote_dir)
        remote_path = join_remote(full_dir, filename)
        client = self._connect()
        try:
            sftp = client.open_sftp()
            self._mkdirs(sftp, full_dir)
            sent = start_offset
            chunk = settings.upload_chunk_bytes
            tmp_path = remote_path + ".part"
            if start_offset >= total:
                if progress and total:
                    progress(total, total)
                sftp.close()
                return remote_path
            with local_path.open("rb") as src, sftp.open(tmp_path, "ab" if start_offset else "wb") as dst:
                dst.set_pipelined(True)
                if start_offset:
                    src.seek(start_offset)
                while True:
                    buf = src.read(chunk)
                    if not buf:
                        break
                    dst.write(buf)
                    sent += len(buf)
                    if progress and total:
                        progress(sent, total)
            try:
                sftp.remove(remote_path)
            except IOError:
                pass
            sftp.rename(tmp_path, remote_path)
            sftp.close()
        finally:
            client.close()
        return remote_path
