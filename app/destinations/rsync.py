"""Rsync-over-SSH destination.

This backend assumes key-based SSH auth is available to the running process.
It intentionally does not handle passwords; rsync over SSH should be deployed
with SSH keys for unattended background uploads.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
import datetime as dt
from pathlib import Path

from ..config import get_settings
from .base import ProgressCb, RemoteEntry, UploadBackend, join_remote, make_probe


def _ensure_known_hosts_dir() -> None:
    """StrictHostKeyChecking=accept-new needs a writable ~/.ssh to record hosts.

    In the container the process often runs as root with no ~/.ssh yet, which
    makes the first connection fail with an opaque error.
    """
    home = Path(os.path.expanduser("~"))
    try:
        (home / ".ssh").mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        pass


class RsyncBackend(UploadBackend):
    def _target(self) -> str:
        if not self.destination.host:
            raise RuntimeError("Rsync destination requires a host")
        user = f"{self.destination.username}@" if self.destination.username else ""
        return f"{user}{self.destination.host}"

    def _ssh_cmd(self) -> list[str]:
        _ensure_known_hosts_dir()
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        key_path = get_settings().ssh_key_path
        if key_path:
            cmd.extend(["-i", key_path, "-o", "IdentitiesOnly=yes"])
        if self.destination.port:
            cmd.extend(["-p", str(self.destination.port)])
        return cmd

    def _remote_base(self) -> str:
        return self.destination.base_path or "."

    def _remote_dir(self, remote_dir: str) -> str:
        return join_remote(self._remote_base(), remote_dir)

    def _run_ssh(self, remote_command: str) -> str:
        cmd = [*self._ssh_cmd(), self._target(), remote_command]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:  # ssh not installed
            raise RuntimeError("ssh client not found on the server") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"SSH connection to {self._target()} timed out") from exc
        if result.returncode != 0:
            raise RuntimeError(self._friendly_error(result.stderr or result.stdout))
        return result.stdout

    def _friendly_error(self, stderr: str) -> str:
        text = (stderr or "").strip()
        low = text.lower()
        if "permission denied" in low or "batch mode" in low or "publickey" in low:
            return (
                "SSH authentication failed — rsync over SSH needs key-based auth. "
                "Add the server's key to the running user's ~/.ssh (or set "
                "DRIFT_SSH_KEY_PATH) and authorise it on the remote host. "
                f"(ssh said: {text})"
            )
        if "host key verification" in low:
            return f"SSH host key verification failed: {text}"
        if "could not resolve" in low or "name or service not known" in low:
            return f"Could not resolve host {self.destination.host}: {text}"
        if "connection refused" in low or "connection timed out" in low:
            return f"Cannot reach {self._target()} on port {self.destination.port or 22}: {text}"
        return text or "SSH command failed"

    def test_connection(self) -> None:
        self._run_ssh(f"test -d {shlex.quote(self._remote_base())}")

    def verify_round_trip(self) -> None:
        name, payload = make_probe()
        remote_path = join_remote(self._remote_base(), name)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(payload)
            local_tmp = tmp.name
        try:
            ssh_transport = " ".join(shlex.quote(part) for part in self._ssh_cmd())
            cmd = [
                "rsync",
                "-a",
                "-e",
                ssh_transport,
                local_tmp,
                f"{self._target()}:{remote_path}",
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except FileNotFoundError as exc:
                raise RuntimeError("rsync is not installed on the server") from exc
            if result.returncode != 0:
                raise RuntimeError(self._friendly_error(result.stderr or result.stdout))
            got = self._run_ssh(f"cat {shlex.quote(remote_path)}")
            if got != payload.decode("utf-8") and got.rstrip("\n") != payload.decode("utf-8"):
                raise RuntimeError("Read-back mismatch on rsync destination")
        finally:
            try:
                os.remove(local_tmp)
            except OSError:
                pass
            try:
                self._run_ssh(f"rm -f {shlex.quote(remote_path)}")
            except Exception:  # noqa: BLE001
                pass

    def list_directories(self, path: str = "") -> list[str]:
        root = join_remote(self._remote_base(), path)
        output = self._run_ssh(
            "find "
            + shlex.quote(root)
            + " -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'"
        )
        return sorted(line for line in output.splitlines() if line)

    def list_entries(self, path: str = "") -> list[RemoteEntry]:
        root = join_remote(self._remote_base(), path)
        output = self._run_ssh(
            "find "
            + shlex.quote(root)
            + " -mindepth 1 -maxdepth 1 -printf '%f\\t%y\\t%s\\t%T@\\n'"
        )
        rows: list[RemoteEntry] = []
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            name, kind, size, mtime = parts
            if kind not in ("d", "f"):
                continue
            try:
                modified_at = dt.datetime.fromtimestamp(float(mtime)).isoformat()
            except ValueError:
                modified_at = None
            rows.append(
                {
                    "name": name,
                    "path": join_remote("/", path, name).strip("/"),
                    "type": "directory" if kind == "d" else "file",
                    "size_bytes": None if kind == "d" else int(size or 0),
                    "modified_at": modified_at,
                }
            )
        return sorted(rows, key=lambda row: (row["type"] != "directory", row["name"].lower()))

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
        return 0

    def remote_file_matches(
        self,
        remote_dir: str,
        filename: str,
        size_bytes: int,
        checksum: str,
    ) -> bool:
        remote_path = join_remote(self._remote_dir(remote_dir), filename)
        output = self._run_ssh(
            "if [ -f "
            + shlex.quote(remote_path)
            + " ]; then stat -c %s "
            + shlex.quote(remote_path)
            + " && sha256sum "
            + shlex.quote(remote_path)
            + " | awk '{print $1}'; else echo missing; fi"
        ).splitlines()
        if len(output) < 2:
            return False
        try:
            if int(output[0].strip()) != size_bytes:
                return False
        except ValueError:
            return False
        return output[1].strip().lower() == checksum.lower()

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
            "--checksum",
            "--partial",
            "--info=progress2",
            "-e",
            ssh_transport,
            str(local_path),
            f"{self._target()}:{remote_path}",
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("rsync is not installed on the server") from exc
        assert proc.stdout is not None
        progress_re = re.compile(r"\s*([\d,]+)\s+(\d+)%")
        recent: list[str] = []
        for line in proc.stdout:
            recent.append(line.rstrip())
            del recent[:-12]  # keep only the tail for error reporting
            match = progress_re.search(line)
            if match and progress and total:
                sent = int(match.group(1).replace(",", ""))
                progress(min(sent, total), total)
        code = proc.wait()
        if code != 0:
            tail = "\n".join(line for line in recent if line.strip())
            raise RuntimeError(
                self._friendly_error(tail) + f" (rsync exit code {code})"
            )
        if progress and total:
            progress(total, total)
        return remote_path
