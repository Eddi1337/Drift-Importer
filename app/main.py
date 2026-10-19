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
from . import workflow  # noqa: F401
from . import date_review  # noqa: F401
from . import archive_tasks  # noqa: F401
from . import roadtrips  # noqa: F401
from . import auto_ride_jobs  # noqa: F401
from .camera_agents import AgentUnavailable

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _setup_logging() -> None:
    settings = get_settings()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    from .recent_logs import recent_logs
    recent_logs.setFormatter(fmt)
    if recent_logs not in root.handlers:
        root.addHandler(recent_logs)
    if settings.log_to_file:
        handler = RotatingFileHandler(
            settings.log_dir / "drift.log", maxBytes=2_000_000, backupCount=3
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    init_db()
    from .routers.api import ensure_default_nas_destination

    settings = get_settings()
    if settings.processing_role != "processor":
        with session_scope() as session:
            ensure_default_nas_destination(session)
    # Install even offline camera namespaces before workers inspect old paths.
    # A startup race must never consume remote work as locally missing files.
    if settings.processing_role == "server":
        from . import agent_bridge
        from .camera_agents import monitor as agents_monitor
        agent_bridge.start()
        agents_monitor.start()
    get_manager().start()
    from .sysmon import get_monitor
    from .ha_publish import get_publisher
    from .devicescan import get_device_monitor

    get_monitor().start()
    if settings.processing_role != "processor":
        get_publisher().start()
        get_device_monitor().start()
    from .processing import sync_monitor
    if settings.processing_role == "processor":
        sync_monitor.start()
    if settings.processing_role != "uploader" and settings.auto_rides_interval_s > 0:
        auto_ride_jobs.monitor.start()
    if get_settings().state_backup_interval_s > 0:
        from .crypto import _fernet
        from .state_backup import get_state_backup
        _fernet()  # Persist the encryption key before the first recovery snapshot.
        get_state_backup().start()
    logging.getLogger("drift").info("Drift-Import %s started", __version__)
    yield
    get_manager().stop()
    from .sysmon import get_monitor as _get_monitor
    from .ha_publish import get_publisher as _get_publisher
    from .devicescan import get_device_monitor as _get_device_monitor

    _get_monitor().stop()
    _get_publisher().stop()
    _get_device_monitor().stop()
    auto_ride_jobs.monitor.stop()
    if settings.processing_role == "server":
        from . import agent_bridge
        from .camera_agents import monitor as agents_monitor
        agents_monitor.stop()
        agent_bridge.stop()
    sync_monitor.stop()
    if get_settings().state_backup_interval_s > 0:
        from .state_backup import get_state_backup
        get_state_backup().stop()


app = FastAPI(title="Drift-Import", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def processing_routes(request: Request, call_next):
    from .processing import route_request
    response = await route_request(request, require_auth, _security)
    return response if response is not None else await call_next(request)

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
from .routers import archive as archive_api  # noqa: E402
from .routers import roadtrips as roadtrip_api  # noqa: E402
from .routers import agents as agents_api  # noqa: E402

app.include_router(pages.router, dependencies=[Depends(require_auth)])
app.include_router(api.router, prefix="/api", dependencies=[Depends(require_auth)])
app.include_router(archive_api.router, prefix="/api/archive", dependencies=[Depends(require_auth)])
app.include_router(roadtrip_api.router, prefix="/api/roadtrips", dependencies=[Depends(require_auth)])
app.include_router(agents_api.router, prefix="/api/agents", dependencies=[Depends(require_auth)])


@app.exception_handler(AgentUnavailable)
async def disconnected_agent(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail":"Camera agent is offline; reconnect it to resume"},status_code=503)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}
