"""Merge ordered 5-minute clips into one longer clip via ffmpeg concat.

Uses the concat demuxer with stream copy (-c copy): no re-encoding, low CPU,
suitable for the Pi Zero 2 W. If the inputs differ in codec/resolution the
stream-copy concat will fail; we detect that and report it rather than silently
re-encoding (which would be very slow on this hardware).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Sequence

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
        signatures.append((info["codec"], info["width"], info["height"]))
    first = signatures[0]
    for p, sig in zip(paths, signatures):
        if sig != first:
            raise MergeError(
                "Clips have mismatched codec/resolution and cannot be merged "
                f"without re-encoding: {p.name} is {sig}, expected {first}."
            )


def build_concat_command(paths: Sequence[Path], output: Path, list_file: Path) -> List[str]:
    settings = get_settings()
    return [
        settings.ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(output),
    ]


def write_concat_list(paths: Sequence[Path], list_file: Path) -> None:
    lines = []
    for p in paths:
        # ffmpeg concat list format: escape single quotes.
        safe = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_file.write_text("\n".join(lines) + "\n")


def merge_clips(paths: Sequence[Path], output: Path) -> Dict:
    """Merge clips in the given order. Returns ffmpeg result info."""
    check_compatible(paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        list_file = Path(tf.name)
    try:
        write_concat_list(paths, list_file)
        cmd = build_concat_command(paths, output, list_file)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if res.returncode != 0:
            raise MergeError(f"ffmpeg concat failed: {res.stderr[-2000:]}")
        if not output.exists() or output.stat().st_size == 0:
            raise MergeError("Merge produced no output file.")
        return {"output": str(output), "size": output.stat().st_size}
    finally:
        list_file.unlink(missing_ok=True)
