"""Timestamp correction for clips whose camera clock is wrong.

Supports an absolute set and a relative shift. The corrected time is applied to
the DB record, the file mtime, and (best-effort) embedded container metadata.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import get_settings


def metadata_copy(source: Path, output: Path, when: dt.datetime) -> None:
    """Write a separate MP4 with UTC dates on every track, without encoding."""
    stamp = (when.replace(tzinfo=dt.timezone.utc) if when.tzinfo is None else when.astimezone(dt.timezone.utc)).isoformat()
    cmd = [get_settings().ffmpeg, "-v", "error", "-n", "-i", str(source),
           "-map", "0", "-c", "copy", "-map_metadata", "0",
           "-metadata", f"creation_time={stamp}", "-metadata:s", f"creation_time={stamp}", str(output)]
    result = subprocess.run(cmd, capture_output=True, timeout=1800)
    if result.returncode or not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("Could not create metadata-corrected video: " + result.stderr.decode(errors="replace")[-500:])


def shift_datetime(base: dt.datetime, *, days=0, hours=0, minutes=0, seconds=0) -> dt.datetime:
    return base + dt.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def set_file_mtime(path: Path, when: dt.datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def write_metadata_creation_time(path: Path, when: dt.datetime) -> bool:
    """Rewrite the container creation_time without re-encoding (stream copy).

    ffmpeg cannot edit metadata in place, so we write to a temp file and swap.
    Returns True if metadata was updated. Only attempted for video files.
    """
    settings = get_settings()
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = Path(tempfile.mkstemp(suffix=path.suffix, dir=str(path.parent))[1])
    cmd = [
        settings.ffmpeg, "-y",
        "-i", str(path),
        "-map", "0",
        "-c", "copy",
        "-metadata", f"creation_time={stamp}",
        str(tmp),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=600)
    if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        shutil.move(str(tmp), str(path))
        set_file_mtime(path, when)
        return True
    if tmp.exists():
        tmp.unlink()
    return False
