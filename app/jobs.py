"""In-process background job system backed by SQLite.

Deliberately lightweight (no Celery/Redis) to fit the Pi Zero 2 W. A small pool
of worker threads pulls queued jobs from the DB. Separate semaphores cap
concurrent uploads and concurrent ffmpeg processes so the Pi is never thrashed.
Job state and progress are persisted so the web UI can display them and so jobs
survive being listed across restarts.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

from .config import get_settings
from .database import session_scope
from .models import Job, JobLog, utcnow

log = logging.getLogger("drift.jobs")

# kind -> handler(job_id, payload, ctx)
_HANDLERS: Dict[str, Callable] = {}


def handler(kind: str):
    def deco(fn):
        _HANDLERS[kind] = fn
        return fn

    return deco


class JobCancelled(Exception):
    pass


class JobContext:
    """Passed to handlers; lets them report progress and honour cancellation."""

    def __init__(self, manager: "JobManager", job_id: int):
        self.manager = manager
        self.job_id = job_id
        self._last_detail: Optional[str] = None

    def set_progress(self, value: float, detail: Optional[str] = None) -> None:
        value = max(0.0, min(1.0, value))
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job:
                job.progress = value
                if detail is not None:
                    job.detail = detail
                    if detail != self._last_detail:
                        s.add(
                            JobLog(
                                job_id=self.job_id,
                                level="INFO",
                                message=detail,
                                progress=value,
                            )
                        )
                        self._last_detail = detail
        if self.is_cancelled():
            raise JobCancelled()

    def log(self, message: str, level: str = "INFO", progress: Optional[float] = None) -> None:
        with session_scope() as s:
            s.add(
                JobLog(
                    job_id=self.job_id,
                    level=level.upper(),
                    message=message,
                    progress=progress,
                )
            )

    def is_cancelled(self) -> bool:
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            return bool(job and job.cancel_requested)

    @property
    def upload_semaphore(self) -> threading.Semaphore:
        return self.manager.upload_sem

    @property
    def ffmpeg_semaphore(self) -> threading.Semaphore:
        return self.manager.ffmpeg_sem


class JobManager:
    def __init__(self, worker_count: int = 2):
        settings = get_settings()
        self.worker_count = worker_count
        self.upload_sem = threading.Semaphore(settings.max_concurrent_uploads)
        self.ffmpeg_sem = threading.Semaphore(settings.max_concurrent_ffmpeg)
        self._stop = threading.Event()
        self._threads = []
        self._claim_lock = threading.Lock()

    def start(self) -> None:
        # Requeue jobs left "running" by a previous crash/restart.
        with session_scope() as s:
            for job in s.query(Job).filter(Job.status == "running").all():
                job.status = "queued"
                job.progress = 0.0
        for i in range(self.worker_count):
            t = threading.Thread(target=self._worker, name=f"drift-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("Job manager started with %d workers", self.worker_count)

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, kind: str, description: str = "", payload: Optional[dict] = None) -> int:
        with session_scope() as s:
            job = Job(
                kind=kind,
                description=description,
                status="queued",
                payload=json.dumps(payload or {}),
            )
            s.add(job)
            s.flush()
            return job.id

    def request_cancel(self, job_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job and job.status in ("queued", "running"):
                job.cancel_requested = True
                if job.status == "queued":
                    job.status = "cancelled"
                    job.finished_at = utcnow()

    def dismiss(self, job_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job:
                job.dismissed_at = utcnow()

    def _claim_next(self) -> Optional[int]:
        with self._claim_lock:
            with session_scope() as s:
                job = (
                    s.query(Job)
                    .filter(Job.status == "queued")
                    .order_by(Job.created_at)
                    .first()
                )
                if not job:
                    return None
                if job.cancel_requested:
                    job.status = "cancelled"
                    job.finished_at = utcnow()
                    return None
                job.status = "running"
                job.started_at = utcnow()
                return job.id

    def _worker(self) -> None:
        while not self._stop.is_set():
            job_id = self._claim_next()
            if job_id is None:
                time.sleep(1.0)
                continue
            self._run_job(job_id)

    def _run_job(self, job_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            kind = job.kind
            payload = json.loads(job.payload or "{}")
        handler_fn = _HANDLERS.get(kind)
        ctx = JobContext(self, job_id)
        try:
            if handler_fn is None:
                raise RuntimeError(f"No handler for job kind '{kind}'")
            ctx.log(f"Started {kind} job")
            handler_fn(job_id, payload, ctx)
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job and job.status == "running":
                    job.status = "done"
                    job.progress = 1.0
                    job.finished_at = utcnow()
            ctx.log(f"Finished {kind} job", progress=1.0)
        except JobCancelled:
            ctx.log("Job cancelled", level="WARNING")
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job:
                    job.status = "cancelled"
                    job.finished_at = utcnow()
            log.info("Job %s cancelled", job_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Job %s failed", job_id)
            ctx.log(f"Job failed: {exc}", level="ERROR")
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job:
                    job.status = "error"
                    job.error = str(exc)[:4000]
                    job.finished_at = utcnow()


_manager: Optional[JobManager] = None


def get_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
