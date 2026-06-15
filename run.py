"""Local/dev entrypoint: `python run.py`."""
from __future__ import annotations

import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    # Single worker: the background job threads live in this process and share
    # the SQLite database. Multiple workers would duplicate the worker pool.
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=1)
