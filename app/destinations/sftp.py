"""SFTP destination via paramiko. Streams from disk in chunks."""
from __future__ import annotations

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

    def upload(self, local_path, remote_dir, filename, progress: ProgressCb = None) -> str:
        settings = get_settings()
        local_path = Path(local_path)
        total = local_path.stat().st_size
        full_dir = join_remote(self.destination.base_path or "/", remote_dir)
        remote_path = join_remote(full_dir, filename)
        client = self._connect()
        try:
            sftp = client.open_sftp()
            self._mkdirs(sftp, full_dir)
            sent = 0
            chunk = settings.upload_chunk_bytes
            tmp_path = remote_path + ".part"
            with local_path.open("rb") as src, sftp.open(tmp_path, "wb") as dst:
                dst.set_pipelined(True)
                while True:
                    buf = src.read(chunk)
                    if not buf:
                        break
                    dst.write(buf)
                    sent += len(buf)
                    if progress and total:
                        progress(sent / total)
            try:
                sftp.remove(remote_path)
            except IOError:
                pass
            sftp.rename(tmp_path, remote_path)
            sftp.close()
        finally:
            client.close()
        return remote_path
