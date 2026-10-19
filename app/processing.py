"""Separate the camera uploader from the wired ride-processing service."""
from __future__ import annotations

import logging
import re
import threading

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .config import get_settings
from .database import session_scope

log = logging.getLogger(__name__)
PREFIX = "/api/roadtrips"
MAX_BODY = 17 * 1024 * 1024  # 16 MB monthly location export plus multipart framing


async def route_request(request, authenticate, security):
    from .pi_gateway import route_request as pi_route
    response = await pi_route(request, authenticate, security)
    if response is not None:
        return response
    if request.scope.get("drift_local_jobs"):
        return None
    settings = get_settings()
    path = request.url.path
    if settings.processing_role == "processor" and path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"} and not path.startswith(PREFIX + "/"):
        return JSONResponse({"detail": "Camera and upload actions run on the Pi"}, status_code=409)
    if settings.processing_role != "uploader" or not path.startswith(PREFIX + "/") or path == PREFIX + "/processor-input":
        return None
    try:
        authenticate(await security(request))
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    from .processing_sync import _source_url
    try:
        # The environment fixes the destination. Neither paths nor query values
        # can turn this into an arbitrary outbound proxy.
        base = _source_url(settings.processing_backend_url).split(PREFIX)[0]
    except ValueError:
        return JSONResponse({"detail": "The ride processor is not configured"}, status_code=503)
    target = base + path
    if request.url.query:
        target += "?" + request.url.query
    if request.method in {"GET", "HEAD"} and re.fullmatch(PREFIX + r"/auto/\d+/(?:film|snapshot/[1-6])", path):
        # Browser Range requests and movie bytes go directly to the wired LXC.
        return RedirectResponse(target, status_code=307)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY:
            return JSONResponse({"detail": "Use a location export smaller than 16 MB"}, status_code=413)
    headers = {key: request.headers[key] for key in ("content-type", "authorization", "if-none-match", "x-drift-proxy-hop") if key in request.headers}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=5), follow_redirects=False, trust_env=False) as client:
            async with client.stream(request.method, target, content=bytes(body), headers=headers) as remote:
                content = bytearray()
                async for chunk in remote.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BODY:
                        raise ValueError("Processor response exceeds the metadata limit")
                returned = {key: remote.headers[key] for key in ("content-type", "etag", "cache-control", "www-authenticate") if key in remote.headers}
                return Response(bytes(content), status_code=remote.status_code, headers=returned)
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"detail": "The ride processor is unavailable; camera uploads continue on the Pi"}, status_code=503)


class ProcessingSyncMonitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="drift-processing-input", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def tick(self):
        from .processing_sync import sync_from_source
        with session_scope() as session:
            result = sync_from_source(session, get_settings().processing_source_url)
        if result.get("changed"):
            from .auto_ride_jobs import enqueue_discovery
            enqueue_discovery()
        return result

    def run(self):
        if self.stop_event.wait(5):
            return
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # Do not log response bodies, URLs or HA credentials.
                log.warning("Pi metadata sync deferred; existing rides remain available")
            self.stop_event.wait(max(60, get_settings().processing_sync_interval_s))


sync_monitor = ProcessingSyncMonitor()
