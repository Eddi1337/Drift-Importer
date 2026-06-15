"""Media inspection: ffprobe metadata, thumbnails, checksums, streaming.

All operations stream from disk; whole media files are never read into memory.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .config import get_settings

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".ts", ".mts", ".m2ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".bmp", ".tiff"}


def classify(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def probe(path: Path) -> Dict:
    """Return normalized metadata for a media file using ffprobe."""
    settings = get_settings()
    cmd = [
        settings.ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    info: Dict = {
        "duration_s": None,
        "codec": None,
        "width": None,
        "height": None,
        "capture_time": None,
    }
    if out.returncode != 0 or not out.stdout:
        return info
    data = json.loads(out.stdout)
    fmt = data.get("format", {})
    if fmt.get("duration"):
        try:
            info["duration_s"] = float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    # creation_time from container tags, if the camera wrote one.
    tags = fmt.get("tags", {}) or {}
    ct = tags.get("creation_time")
    if ct:
        info["capture_time"] = _parse_time(ct)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            info["codec"] = stream.get("codec_name")
            info["width"] = stream.get("width")
            info["height"] = stream.get("height")
            stags = stream.get("tags", {}) or {}
            if info["capture_time"] is None and stags.get("creation_time"):
                info["capture_time"] = _parse_time(stags["creation_time"])
            break
    return info


def _parse_time(value: str) -> Optional[dt.datetime]:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def capture_time_or_mtime(path: Path, probed: Dict) -> dt.datetime:
    """Best-effort capture time: container metadata, else file mtime."""
    if probed.get("capture_time"):
        return probed["capture_time"]
    return dt.datetime.fromtimestamp(path.stat().st_mtime)


def checksum(path: Path, sample_bytes: int = 4 * 1024 * 1024) -> str:
    """Cheap content fingerprint for dedup: size + hash of head+tail samples.

    Hashing whole multi-GB clips on a Pi is too slow, so we sample the first
    and last few MiB plus the file size. Good enough to detect re-imports.
    """
    h = hashlib.sha256()
    size = path.stat().st_size
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(sample_bytes))
        if size > sample_bytes * 2:
            f.seek(-sample_bytes, 2)
            h.update(f.read(sample_bytes))
    return h.hexdigest()


def make_thumbnail(path: Path, kind: str, out_path: Path) -> bool:
    """Generate a small thumbnail. Returns True on success."""
    settings = get_settings()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "video":
        cmd = [
            settings.ffmpeg, "-y",
            "-ss", "1",
            "-i", str(path),
            "-frames:v", "1",
            "-vf", "scale=320:-1",
            str(out_path),
        ]
    else:
        cmd = [
            settings.ffmpeg, "-y",
            "-i", str(path),
            "-vf", "scale=320:-1",
            str(out_path),
        ]
    res = subprocess.run(cmd, capture_output=True, timeout=120)
    return res.returncode == 0 and out_path.exists()
