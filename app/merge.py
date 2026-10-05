"""Merge ordered 5-minute clips into one longer clip via ffmpeg concat.

Uses the concat demuxer with stream copy (-c copy): no re-encoding, low CPU,
suitable for the Pi Zero 2 W. If the inputs differ in codec/resolution the
stream-copy concat will fail; we detect that and report it rather than silently
re-encoding (which would be very slow on this hardware).
"""
from __future__ import annotations

import datetime as dt
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from .config import get_settings
from .media import probe


class MergeError(Exception):
    pass


def check_compatible(paths: Sequence[Path]) -> None:
    """Raise MergeError if clips can't be stream-copy concatenated."""
    if len(paths) < 2:
        raise MergeError("Need at least two clips to merge.")
    signatures = []
    for p in paths:
        if not p.exists():
            raise MergeError(f"Missing file: {p}")
        info = probe(p)
        signatures.append((info["codec"], info["width"], info["height"], info.get("stream_signature")))
    first = signatures[0]
    for p, sig in zip(paths, signatures):
        if sig != first:
            raise MergeError(
                "Clips have mismatched codec/resolution and cannot be merged "
                f"without re-encoding: {p.name} is {sig}, expected {first}."
            )


def build_concat_command(
    paths: Sequence[Path], output: Path, list_file: Path, report_progress: bool = False, creation_time: Optional[dt.datetime] = None
) -> List[str]:
    settings = get_settings()
    command = [
        settings.ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-map", "0",
        "-c", "copy",
        str(output),
    ]
    if creation_time is not None:
        when = creation_time.replace(tzinfo=dt.timezone.utc) if creation_time.tzinfo is None else creation_time.astimezone(dt.timezone.utc)
        stamp = when.isoformat().replace("+00:00", "Z")
        command[-1:-1] = ["-metadata", f"creation_time={stamp}", "-metadata:s", f"creation_time={stamp}"]
    if report_progress:
        # ffmpeg's machine-readable progress is much more reliable than trying
        # to parse its human-oriented status line.
        command[2:2] = ["-v", "error", "-progress", "pipe:1", "-nostats"]
    return command


def write_concat_list(paths: Sequence[Path], list_file: Path) -> None:
    lines = []
    for p in paths:
        # ffmpeg concat list format: escape single quotes.
        safe = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_file.write_text("\n".join(lines) + "\n")


def merge_clips(
    paths: Sequence[Path], output: Path, progress: Optional[Callable[[float], None]] = None, creation_time: Optional[dt.datetime] = None
) -> Dict:
    """Merge clips in the given order. Returns ffmpeg result info."""
    check_compatible(paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep even the concat manifest beside the NAS output, never in /tmp on SD.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=output.parent) as tf:
        list_file = Path(tf.name)
    try:
        write_concat_list(paths, list_file)
        total_duration = sum(float(probe(path).get("duration_s") or 0) for path in paths)
        if progress and total_duration > 0:
            cmd = build_concat_command(paths, output, list_file, report_progress=True, creation_time=creation_time)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            last_progress = -1.0
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    key, separator, value = line.strip().partition("=")
                    if separator and key in ("out_time_us", "out_time_ms"):
                        try:
                            # Despite its older name, out_time_ms is emitted in
                            # microseconds by the ffmpeg versions used on Pi OS.
                            fraction = float(value) / (total_duration * 1_000_000)
                        except ValueError:
                            continue
                        fraction = max(0.0, min(1.0, fraction))
                        if fraction - last_progress >= 0.01:
                            progress(fraction)
                            last_progress = fraction
                _, stderr = proc.communicate(timeout=3600)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
                raise MergeError("ffmpeg concat timed out after one hour")
            except BaseException:
                proc.kill()
                proc.communicate()
                raise
            if proc.returncode != 0:
                raise MergeError(f"ffmpeg concat failed: {stderr[-2000:]}")
            progress(1.0)
        else:
            cmd = build_concat_command(paths, output, list_file, creation_time=creation_time)
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if res.returncode != 0:
                raise MergeError(f"ffmpeg concat failed: {res.stderr[-2000:]}")
        if not output.exists() or output.stat().st_size == 0:
            raise MergeError("Merge produced no output file.")
        return {"output": str(output), "size": output.stat().st_size}
    finally:
        list_file.unlink(missing_ok=True)
