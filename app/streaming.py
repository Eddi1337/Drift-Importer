"""HTTP Range streaming so the browser can scrub video without the server
ever buffering the whole file in memory."""
from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Iterator

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1024 * 1024  # 1 MiB read window keeps RAM use tiny


def stream_file(request: Request, path: Path) -> Response:
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")
    file_size = path.stat().st_size
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    range_header = request.headers.get("range")

    if range_header is None:
        def full() -> Iterator[bytes]:
            with path.open("rb") as f:
                while True:
                    data = f.read(_CHUNK)
                    if not data:
                        break
                    yield data

        return StreamingResponse(
            full(),
            media_type=content_type,
            headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"},
        )

    m = _RANGE_RE.match(range_header)
    if not m:
        raise HTTPException(416, "Invalid range")
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end:
        raise HTTPException(416, "Invalid range")
    length = end - start + 1

    def ranged() -> Iterator[bytes]:
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                data = f.read(min(_CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(ranged(), status_code=206, media_type=content_type, headers=headers)
