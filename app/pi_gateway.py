"""Fast local UI with bounded status caches and explicit controls for the Pi.

All destinations come from configuration. Writes are sent once and never cached
or retried. Cached reads are labelled stale if the Pi goes away; static UI and
locally hosted trip maps remain available independently of camera transfers.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import time
import re
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import get_settings

MAX_BODY = 17 * 1024 * 1024
MAX_CACHE = 32 * 1024 * 1024
READ_TTL = 3.0
SAFE_HEADERS = ('content-type', 'content-range', 'accept-ranges', 'etag',
                'last-modified', 'content-disposition', 'www-authenticate', 'location')


def pi_origin(settings):
    value = settings.processing_source_url.rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Configure a fixed Pi HTTP origin without embedded credentials')
    return value


@dataclass
class CachedRead:
    body: bytes
    status: int
    headers: dict
    saved: float
    generation: int


class PiGateway:
    def __init__(self):
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.locks = {}
        self.lock_users = {}
        self.refreshing = {}
        self.generation = 0
        self.online = None
        self.last_seen = None
        self.task = None

    def invalidate(self):
        self.generation += 1
        self.cache.clear()
        self.cache_bytes = 0

    def remember(self, key, entry):
        if entry.generation != self.generation:
            return
        previous = self.cache.pop(key, None)
        if previous:
            self.cache_bytes -= len(previous.body)
        if len(entry.body) > MAX_BODY or entry.generation != self.generation:
            return
        self.cache[key] = entry
        self.cache_bytes += len(entry.body)
        while self.cache_bytes > MAX_CACHE or len(self.cache) > 64:
            _, removed = self.cache.popitem(last=False)
            self.cache_bytes -= len(removed.body)

    def cached_response(self, entry, *, stale=False):
        headers = entry.headers | {'Cache-Control': 'no-store', 'X-Drift-Source': 'pi',
            'X-Drift-Cache-Age': str(round(time.monotonic() - entry.saved, 1)),
            'X-Drift-Stale': 'true' if stale else 'false'}
        return Response(entry.body, status_code=entry.status, headers=headers)

    async def refresh(self, key, target, headers):
        generation = self.generation
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3), follow_redirects=False, trust_env=False) as client:
                async with client.stream('GET', target, headers=headers) as remote:
                    if remote.status_code != 200:
                        previous = self.cache.pop(key, None)
                        if previous:
                            self.cache_bytes -= len(previous.body)
                        return
                    data = bytearray()
                    async for part in remote.aiter_bytes():
                        data.extend(part)
                        if len(data) > MAX_BODY:
                            raise ValueError('Pi response exceeds metadata limit')
                    returned = {k: remote.headers[k] for k in SAFE_HEADERS if k in remote.headers}
                    self.remember(key, CachedRead(bytes(data), 200, returned, time.monotonic(), generation))
                    self.online = True
                    self.last_seen = time.time()
        except (httpx.HTTPError, ValueError):
            self.online = False
        finally:
            self.refreshing.pop(key, None)

    async def proxy(self, request, origin):
        method = request.method
        headers = {k: request.headers[k] for k in ('content-type', 'authorization', 'range',
                   'if-range', 'if-none-match', 'if-modified-since') if k in request.headers}
        headers.update({'Accept-Encoding': 'identity', 'X-Drift-Proxy-Hop': '1'})
        auth_key = hashlib.sha256(headers.get('authorization', '').encode()).hexdigest()
        target = origin + request.url.path
        if request.url.query:
            target += '?' + request.url.query
        key = (method, target, auth_key)
        now = time.monotonic()
        entry = self.cache.get(key) if method == 'GET' else None
        if entry:
            age = now - entry.saved
            if age < 24 * 3600:
                if age >= READ_TTL and key not in self.refreshing:
                    self.refreshing[key] = asyncio.create_task(self.refresh(key, target, headers))
                return self.cached_response(entry, stale=self.online is False or age > 15)
        lock = self.locks.setdefault(key, asyncio.Lock()) if method == 'GET' else asyncio.Lock()
        if method == 'GET':
            self.lock_users[key] = self.lock_users.get(key, 0) + 1
        # One upstream read serves concurrent tabs. Waiters keep this lock alive.
        try:
            async with lock:
                entry = self.cache.get(key) if method == 'GET' else None
                if entry and time.monotonic() - entry.saved < READ_TTL:
                    return self.cached_response(entry)
                body = bytearray()
                async for part in request.stream():
                    body.extend(part)
                    if len(body) > MAX_BODY:
                        return JSONResponse({'detail': 'Request exceeds the 16 MB import limit'}, status_code=413)
                generation = self.generation
                client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=3, pool=3),
                    follow_redirects=False, trust_env=False)
                remote = None
                streamed = False
                try:
                    remote = await client.send(client.build_request(method, target, headers=headers, content=bytes(body)), stream=True)
                    self.online = True
                    self.last_seen = time.time()
                    returned = {k: remote.headers[k] for k in SAFE_HEADERS if k in remote.headers}
                    kind = remote.headers.get('content-type', '').lower()
                    if kind.startswith(('video/', 'audio/')) or 'attachment' in returned.get('content-disposition', ''):
                        if 'content-length' in remote.headers:
                            returned['content-length'] = remote.headers['content-length']
                        streamed = True
                        async def chunks():
                            try:
                                async for part in remote.aiter_raw():
                                    yield part
                            finally:
                                await remote.aclose()
                                await client.aclose()
                        return StreamingResponse(chunks(), status_code=remote.status_code, headers=returned)
                    data = bytearray()
                    async for part in remote.aiter_bytes():
                        data.extend(part)
                        if len(data) > MAX_BODY:
                            raise ValueError('Pi response exceeds metadata limit')
                    if method != 'GET':
                        self.invalidate()
                    elif remote.status_code == 200:
                        entry = CachedRead(bytes(data), remote.status_code, returned, time.monotonic(), generation)
                        self.remember(key, entry)
                        return self.cached_response(entry)
                    return Response(bytes(data), status_code=remote.status_code,
                                    headers=returned | {'Cache-Control': 'no-store', 'X-Drift-Source': 'pi'})
                except (httpx.HTTPError, ValueError):
                    self.online = False
                    if method == 'GET' and entry and time.monotonic() - entry.saved < 24 * 3600:
                        return self.cached_response(entry, stale=True)
                    message = ('The Pi is unavailable; cached trips and the UI remain available' if method == 'GET'
                               else 'The Pi did not confirm this command. Check its queue before retrying.')
                    return JSONResponse({'detail': message}, status_code=503)
                finally:
                    if not streamed:
                        if remote is not None:
                            await remote.aclose()
                        await client.aclose()
        finally:
            if method == 'GET':
                self.lock_users[key] -= 1
                if self.lock_users[key] == 0:
                    self.lock_users.pop(key, None)
                    self.locks.pop(key, None)

    async def pulse(self):
        while True:
            try:
                settings = get_settings()
                origin = pi_origin(settings)
                auth = (settings.auth_username, settings.auth_password) if settings.auth_enabled else None
                async with httpx.AsyncClient(timeout=httpx.Timeout(3, connect=2), trust_env=False,
                                             follow_redirects=False, auth=auth) as client:
                    response = await client.get(origin + '/healthz')
                    response.raise_for_status()
                    self.online = response.json().get('status') == 'ok'
                    if self.online:
                        self.last_seen = time.time()
            except (httpx.HTTPError, ValueError, KeyError):
                self.online = False
            await asyncio.sleep(6)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.pulse(), name='drift-pi-connection')

    async def stop(self):
        tasks = list(self.refreshing.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.refreshing.clear()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None


gateway = PiGateway()


async def route_request(request, authenticate, security):
    settings = get_settings()
    if settings.processing_role not in ('processor', 'frontend') or not getattr(settings, 'processing_source_url', ''):
        return None
    path = request.url.path
    if not path.startswith('/api/'):
        return None
    # Trip rendering/matching can retain its existing dedicated LXC role.
    if settings.processing_role == 'processor' and path.startswith('/api/roadtrips/'):
        return None
    try:
        authenticate(await security(request))
    except HTTPException as exc:
        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=exc.headers)
    if settings.processing_role == 'processor' and re.fullmatch(r'/api/processor/jobs(?:/(?:overview|pause_all|resume_all|stop_all|[0-9]+(?:/(?:logs|cancel|retry|dismiss))?))?',path):
        request.scope['path'] = path.replace('/api/processor/', '/api/', 1)
        request.scope['raw_path'] = request.scope['path'].encode()
        request.scope['drift_local_jobs'] = True
        return None
    if path == '/api/connection':
        return JSONResponse({'mode': settings.processing_role, 'pi_online': gateway.online,
            'last_seen': gateway.last_seen, 'trip_processing': 'lxc' if settings.processing_role == 'processor' else 'pi'})
    if path == '/api/agents':
        return JSONResponse({'agents': []})
    if request.headers.get('x-drift-proxy-hop'):
        return JSONResponse({'detail': 'Invalid Pi/UI routing loop'}, status_code=508)
    try:
        origin = pi_origin(settings)
    except ValueError:
        return JSONResponse({'detail': 'The Pi connection is not configured'}, status_code=503)
    return await gateway.proxy(request, origin)
