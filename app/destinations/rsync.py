"""Rsync-over-SSH destination.

This backend assumes key-based SSH auth is available to the running process.
It intentionally does not handle passwords; rsync over SSH should be deployed
with SSH keys for unattended background uploads.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from .base import ProgressCb, UploadBackend, join_remote


class RsyncBackend(UploadBackend):
    def _target(self) -> str:
        if not self.destination.host:
            raise RuntimeError("Rsync destination requires a host")
        user = f"{self.destination.username}@" if self.destination.username else ""
        return f"{user}{self.destination.host}"

    def _ssh_cmd(self) -> list[str]:
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if self.destination.port:
            cmd.extend(["-p", str(self.destination.port)])
        return cmd

    def _remote_base(self) -> str:
        return self.destination.base_path or "."

    def _remote_dir(self, remote_dir: str) -> str:
        return join_remote(self._remote_base(), remote_dir)

    def _run_ssh(self, remote_command: str) -> str:
        cmd = [*self._ssh_cmd(), self._target(), remote_command]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "SSH command failed").strip())
        return result.stdout

    def test_connection(self) -> None:
        self._run_ssh(f"test -d {shlex.quote(self._remote_base())}")

    def list_directories(self, path: str = "") -> list[str]:
        root = join_remote(self._remote_base(), path)
        output = self._run_ssh(
            "find "
            + shlex.quote(root)
            + " -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'"
        )
        return sorted(line for line in output.splitlines() if line)

    def storage_info(self) -> dict:
        output = self._run_ssh(f"df -P -B1 {shlex.quote(self._remote_base())} | tail -1")
        parts = output.split()
        if len(parts) < 5:
            return {"free_bytes": None, "total_bytes": None, "used_bytes": None}
        total = int(parts[1])
        used = int(parts[2])
        free = int(parts[3])
        return {"free_bytes": free, "total_bytes": total, "used_bytes": used}

    def get_resume_offset(self, remote_dir: str, filename: str, size_bytes: int) -> int:
        remote_path = join_remote(self._remote_dir(remote_dir), filename)
        output = self._run_ssh(
            "if [ -f "
            + shlex.quote(remote_path)
            + " ]; then stat -c %s "
            + shlex.quote(remote_path)
            + "; else echo 0; fi"
        )
        try:
            return min(int(output.strip() or "0"), size_bytes)
        except ValueError:
            return 0

    def upload(
        self,
        local_path,
        remote_dir,
        filename,
        progress: ProgressCb = None,
        start_offset: int = 0,
    ) -> str:
        local_path = Path(local_path)
        total = local_path.stat().st_size
        full_dir = self._remote_dir(remote_dir)
        remote_path = join_remote(full_dir, filename)
        self._run_ssh(f"mkdir -p {shlex.quote(full_dir)}")

        ssh_transport = " ".join(shlex.quote(part) for part in self._ssh_cmd())
        cmd = [
            "rsync",
            "-a",
            "--partial",
            "--append-verify",
            "--info=progress2",
            "-e",
            ssh_transport,
            str(local_path),
            f"{self._target()}:{remote_path}",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdout is not None
        progress_re = re.compile(r"\s*([\d,]+)\s+(\d+)%")
        for line in proc.stdout:
            match = progress_re.search(line)
            if match and progress and total:
                sent = int(match.group(1).replace(",", ""))
                progress(min(sent, total), total)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"rsync failed with exit code {code}")
        if progress and total:
            progress(total, total)
        return remote_path
