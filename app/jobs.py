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

from sqlalchemy import case, func

from .config import get_settings
from .database import session_scope
from .models import Job, JobLog, utcnow

log = logging.getLogger("drift.jobs")

ACTIVE_STATES = ("queued", "running", "paused")

# Only look this far back when reconstructing the current run; a single run
# realistically never spans longer, and it bounds the scan as history grows.
RUN_LOOKBACK = dt.timedelta(days=30)

# kind -> handler(job_id, payload, ctx)
_HANDLERS: Dict[str, Callable] = {}


def _naive(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is not None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def current_run_start(session, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """``created_at`` where the current contiguous run of work began.

    A "run" is a stretch of jobs whose activity overlaps with no idle gap: while
    at least one job is queued or running the run continues; the moment the queue
    fully drains, the next job to arrive starts a fresh run. Detecting the gap
    (rather than anchoring on the oldest still-active job) keeps a lingering job
    from an old batch — most importantly a *paused* one, which a user can leave
    held indefinitely — from dragging the window back over thousands of long-since
    finished jobs and pinning the progress bar near 100%.

    Paused jobs are treated as transparent (they neither hold the queue busy nor
    anchor a run), since they're explicitly set aside by the user.
    """
    now = _naive(now) or dt.datetime.utcnow()
    cutoff = now - RUN_LOOKBACK
    rows = (
        session.query(Job.created_at, Job.finished_at, Job.status)
        .filter(Job.dismissed_at.is_(None), Job.created_at >= cutoff)
        .order_by(Job.created_at)
        .all()
    )
    run_start: Optional[dt.datetime] = None
    busy_until: Optional[dt.datetime] = None
    for created, finished, status in rows:
        created = _naive(created)
        if created is None:
            continue
        if status in ("running", "queued"):
            busy_end = dt.datetime.max          # active: queue never drains here
        elif status == "paused":
            busy_end = created                  # transparent / user-held
        else:                                   # done | error | cancelled
            busy_end = _naive(finished) or created
        if run_start is None or created > busy_until:
            # First job, or the queue had drained before this one was created.
            run_start = created
            busy_until = busy_end
        elif busy_end > busy_until:
            busy_until = busy_end
    return run_start


def jobs_overview(session) -> dict:
    """Aggregate job state across the whole table (not just a page of rows).

    The status counts are global (they drive the jobs-page summary boxes), but
    the progress bar is scoped to the *current run* (see ``current_run_start``)
    and is count-based — completed / total — so a fresh batch reads ~0% even when
    the table is full of finished jobs from earlier runs.
    """
    counts = dict(
        session.query(Job.status, func.count())
        .filter(Job.dismissed_at.is_(None))
        .group_by(Job.status)
        .all()
    )
    running = counts.get("running", 0)
    queued = counts.get("queued", 0)
    paused = counts.get("paused", 0)
    active = running + queued + paused

    run_start = current_run_start(session)
    if run_start is not None:
        run_running, run_queued, run_paused, run_done = (
            session.query(
                func.coalesce(func.sum(case((Job.status == "running", 1), else_=0)), 0),
                func.coalesce(func.sum(case((Job.status == "queued", 1), else_=0)), 0),
                func.coalesce(func.sum(case((Job.status == "paused", 1), else_=0)), 0),
                func.coalesce(func.sum(case((Job.status == "done", 1), else_=0)), 0),
            )
            .filter(Job.dismissed_at.is_(None), Job.created_at >= run_start)
            .one()
        )
        running_progress = (
            session.query(func.coalesce(func.sum(Job.progress), 0.0))
            .filter(
                Job.status == "running",
                Job.dismissed_at.is_(None),
                Job.created_at >= run_start,
            )
            .scalar()
        ) or 0.0
        done_in_run = int(run_done)
        total_run = int(run_running) + int(run_queued) + int(run_paused) + done_in_run
        progress = (done_in_run + float(running_progress)) / total_run if total_run else 1.0
    else:
        done_in_run = 0
        total_run = 0
        progress = 1.0

    if running:
        status = "running"
    elif queued:
        status = "queued"
    elif paused:
        status = "paused"
    else:
        status = "idle"

    return {
        "active": active,
        "running": running,
        "queued": queued,
        "paused": paused,
        "done": counts.get("done", 0),
        "error": counts.get("error", 0),
        "cancelled": counts.get("cancelled", 0),
        "completed_in_run": done_in_run,
        "total_in_run": total_run,
        "progress": progress,
        "percent": round(progress * 100),
        "status": status,
    }


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
        self._paused = threading.Event()
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

    def retry(self, job_id: int) -> Optional[int]:
        with session_scope() as s:
            original = s.get(Job, job_id)
            if not original:
                return None
            job = Job(
                kind=original.kind,
                description=f"Retry {original.description or original.kind}",
                status="queued",
                payload=original.payload,
            )
            s.add(job)
            s.flush()
            return job.id

    def request_cancel(self, job_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job and job.status in ("queued", "running", "paused"):
                job.cancel_requested = True
                if job.status in ("queued", "paused"):
                    job.status = "cancelled"
                    job.finished_at = utcnow()

    def pause_all(self) -> dict:
        self._paused.set()
        with session_scope() as s:
            queued = s.query(Job).filter(Job.status == "queued").all()
            for job in queued:
                job.status = "paused"
            running_count = s.query(Job).filter(Job.status == "running").count()
            return {"paused": len(queued), "running": running_count}

    def resume_all(self) -> dict:
        self._paused.clear()
        with session_scope() as s:
            paused = s.query(Job).filter(Job.status == "paused").all()
            for job in paused:
                job.status = "queued"
            return {"resumed": len(paused)}

    def stop_all(self) -> dict:
        self._paused.clear()
        with session_scope() as s:
            jobs = (
                s.query(Job)
                .filter(Job.status.in_(("queued", "running", "paused")))
                .all()
            )
            cancelled_now = 0
            running = 0
            for job in jobs:
                job.cancel_requested = True
                if job.status == "running":
                    running += 1
                else:
                    job.status = "cancelled"
                    job.finished_at = utcnow()
                    cancelled_now += 1
            return {"cancel_requested": len(jobs), "cancelled": cancelled_now, "running": running}

    def dismiss(self, job_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job:
                job.dismissed_at = utcnow()

    def _claim_next(self) -> Optional[int]:
        if self._paused.is_set():
            return None
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
