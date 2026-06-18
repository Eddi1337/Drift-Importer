"""Drift-Import FastAPI application entrypoint."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import get_settings
from .database import init_db, session_scope
from .jobs import get_manager
from . import tasks  # noqa: F401  (registers job handlers)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _setup_logging() -> None:
    settings = get_settings()
    handler = RotatingFileHandler(
        settings.log_dir / "drift.log", maxBytes=2_000_000, backupCount=3
    )
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    init_db()
    from .routers.api import ensure_default_nas_destination

    with session_scope() as session:
        ensure_default_nas_destination(session)
    get_manager().start()
    logging.getLogger("drift").info("Drift-Import %s started", __version__)
    yield
    get_manager().stop()


app = FastAPI(title="Drift-Import", version=__version__, lifespan=lifespan)

_security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    """Optional single-user HTTP Basic auth (enabled when a password is set)."""
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, settings.auth_username)
    ok_pass = secrets.compare_digest(credentials.password, settings.auth_password)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Routers are imported here so they can share the templates object.
from .routers import pages, api  # noqa: E402

app.include_router(pages.router, dependencies=[Depends(require_auth)])
app.include_router(api.router, prefix="/api", dependencies=[Depends(require_auth)])


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}
