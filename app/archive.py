"""Incremental NAS media inventory and cheap duplicate candidate discovery.

Only same-size candidates are read, at three small locations. A sampled match
is never proof of equality: full verification and hardlinking are separate,
explicit operations. This cache is rebuildable and never edits upload history.
"""
from __future__ import annotations

import hashlib
import os
import stat
import time
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func

from .archive_models import ArchiveFile, ArchiveScan
from .database import session_scope
from .jobs import JobCancelled
from .media import IMAGE_EXTS, VIDEO_EXTS
from .models import Destination, Job, UploadedClip, UploadState, utcnow
from .storage import require_storage

MOUNTED_TYPES = ("local", "nfs", "smb")
SAMPLE_BYTES = 64 * 1024
BATCH_SIZE = 100
IDENTITY_FIELDS = ("size_bytes", "mtime_ns", "ctime_ns", "device", "inode")


class _Progress:
    def __init__(self, ctx=None):
        self.ctx, self.checked, self.emitted = ctx, 0.0, 0.0

    def check(self, force=False):
        now = time.monotonic()
        if self.ctx and (force or now - self.checked >= 0.5):
            self.checked = now
            if self.ctx.is_cancelled():
                raise JobCancelled()

    def update(self, value, detail, force=False):
        self.check(force)
        now = time.monotonic()
        if self.ctx and (force or now - self.emitted >= 15):
            self.emitted = now
            self.ctx.set_progress(value, detail)


def stat_identity(value) -> dict:
    return {
        "size_bytes": value.st_size, "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns, "device": str(value.st_dev),
        "inode": str(value.st_ino), "nlink": value.st_nlink,
        "mode": stat.S_IMODE(value.st_mode), "uid": value.st_uid, "gid": value.st_gid,
    }


def identity_dict(row: ArchiveFile) -> dict:
    return {name: getattr(row, name) for name in (*IDENTITY_FIELDS, "nlink", "mode", "uid", "gid")}


def invalidate_file(row: ArchiveFile) -> None:
    row.sample_sha256 = row.full_sha256 = row.verified_at = None


def _same_identity(left: dict, right: dict) -> bool:
    return all(str(left[k]) == str(right[k]) for k in IDENTITY_FIELDS)


def _path(root: Path, value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    # Do not resolve symlinks here; no inventory operation follows them.
    path = Path(os.path.abspath(path))
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return None


def active_archive_paths(session, destination_id: int, root: Path) -> set[str]:
    """Final and temporary names being changed by an unfinished upload."""
    paths = set()
    for row in session.query(UploadedClip.remote_path, UploadedClip.temp_remote_path).filter(
        UploadedClip.destination_id == destination_id, UploadedClip.status != "done"
    ):
        paths.update(p for value in row for p in [_path(root, value)] if p)
    for (value,) in session.query(UploadState.remote_path).filter(
        UploadState.destination_id == destination_id, UploadState.status != "done"
    ):
        path = _path(root, value)
        if path:
            paths.add(path)
    return paths


def _ledger(session, destination_id, root):
    hints = {}
    for row in session.query(UploadedClip.id, UploadedClip.remote_path, UploadedClip.status).filter(
        UploadedClip.destination_id == destination_id
    ).order_by(UploadedClip.updated_at):
        relative = _path(root, row.remote_path)
        if relative:
            hints[relative] = (row.id, row.status)
    return hints


def _seed_ledger(session, destination_id, root):
    """Represent historical successful uploads even before their first scan."""
    for clip in session.query(UploadedClip).filter_by(destination_id=destination_id, status="done"):
        relative = _path(root, clip.remote_path)
        if not relative or Path(relative).suffix.lower() not in VIDEO_EXTS | IMAGE_EXTS:
            continue
        if session.query(ArchiveFile.id).filter_by(destination_id=destination_id, path=relative).first():
            continue
        session.add(ArchiveFile(destination_id=destination_id, root_path=str(root), path=relative,
                                size_bytes=clip.size_bytes or 0, present=False, eligible=False,
                                kind="video" if Path(relative).suffix.lower() in VIDEO_EXTS else "image",
                                ledger_id=clip.id, ledger_status=clip.status))


def record_uploaded_file(session, destination, clip) -> None:
    """Cheap incremental inventory update: one stat, no directory or content read.

    Upload success must never depend on this rebuildable report cache.
    """
    if destination.type not in MOUNTED_TYPES or not clip.remote_path:
        return
    try:
        root = _safe_root(destination.base_path)
        relative = _path(root, clip.remote_path)
        if not relative or Path(relative).suffix.lower() not in VIDEO_EXTS | IMAGE_EXTS:
            return
        info = _safe_stat(root, relative)
    except (OSError, RuntimeError):
        return
    row = session.query(ArchiveFile).filter_by(destination_id=destination.id, path=relative).one_or_none()
    if row is None:
        row = ArchiveFile(destination_id=destination.id, root_path=str(root), path=relative,
                          present=False, size_bytes=0, mtime_ns=0, ctime_ns=0, device="0", inode="0",
                          nlink=1, mode=0, uid=0, gid=0)
        session.add(row)
    _update_row(row, root, relative, info, (clip.id, clip.status), False)


def _safe_root(value) -> Path:
    root = Path(os.path.abspath(value))
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise RuntimeError(f"Archive root has a symbolic link component: {root}")
    return require_storage(root)


def _safe_stat(root: Path, relative: str):
    path = root / relative
    if Path(relative).is_absolute() or not path.is_relative_to(root) or ".." in Path(relative).parts:
        raise RuntimeError("Archive path is outside its destination")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise RuntimeError("Symbolic links are excluded from archive operations")
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Archive entry is not a regular file")
    return info


def _walk(root: Path, progress: _Progress):
    """No followlinks, hidden cache directories, .part files, or silent errors."""
    pending = [root]
    while pending:
        directory = pending.pop()
        progress.check()
        with os.scandir(directory) as entries:
            for entry in entries:
                progress.check()
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif Path(entry.name).suffix.lower() in VIDEO_EXTS | IMAGE_EXTS:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode):
                        yield Path(entry.path), info


def sample_fingerprint(root: Path, relative: str, expected: dict, progress=None) -> str:
    """Read at most 192 KiB; reject replacement or modification during reading."""
    progress = progress or _Progress()
    before = _safe_stat(root, relative)
    if not _same_identity(stat_identity(before), expected):
        raise RuntimeError("File changed since inventory; refresh the archive")
    digest = hashlib.sha256()
    digest.update(f"drift-archive-sample-v1:{before.st_size}:".encode())
    offsets = sorted({0, max(0, before.st_size // 2 - SAMPLE_BYTES // 2), max(0, before.st_size - SAMPLE_BYTES)})
    # O_NOFOLLOW closes the final component symlink race on Linux.
    fd = os.open(root / relative, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        if not _same_identity(stat_identity(os.fstat(source.fileno())), expected):
            raise RuntimeError("File changed while opening")
        for offset in offsets:
            progress.check()
            source.seek(offset)
            digest.update(offset.to_bytes(8, "big"))
            digest.update(source.read(SAMPLE_BYTES))
        after = stat_identity(os.fstat(source.fileno()))
    if not _same_identity(after, expected) or not _same_identity(stat_identity(_safe_stat(root, relative)), expected):
        raise RuntimeError("File changed during sampling; refresh the archive")
    return digest.hexdigest()


def _update_row(row, root, relative, info, hint, active):
    identity = stat_identity(info)
    changed = not row.present or row.root_path != str(root) or not _same_identity(identity_dict(row), identity)
    if changed or active:
        invalidate_file(row)
    row.root_path, row.path, row.present = str(root), relative, True
    for key, value in identity.items():
        if getattr(row, key) != value:
            setattr(row, key, value)
    row.kind = "video" if Path(relative).suffix.lower() in VIDEO_EXTS else "image"
    row.ledger_id, row.ledger_status = hint or (None, None)
    row.eligible = not active
    # Unchanged rows stay untouched on disk; last successful scan proves freshness.
    if changed:
        row.last_seen_at = utcnow()
    if changed or row.checked_at is None:
        row.checked_at = utcnow()


def scan_archive(destination_ids=None, ctx=None) -> dict:
    """Refresh selected mounted destinations, then sample same-size candidates.

    Inventory metadata commits in batches. An interrupted/failed walk keeps all
    previously known files present, so an unplugged NAS cannot erase the cache.
    """
    progress = _Progress(ctx)
    with session_scope() as session:
        query = session.query(Destination).filter(Destination.enabled.is_(True))
        if destination_ids is not None:
            query = query.filter(Destination.id.in_(destination_ids))
        destinations = [(d.id, d.base_path, d.type) for d in query.order_by(Destination.rank, Destination.id)]
    failures = []
    completed = []
    for index, (destination_id, base_path, kind) in enumerate(destinations):
        progress.check(True)
        if kind not in MOUNTED_TYPES:
            failures.append({"destination_id": destination_id, "error": "Remote destination is not mounted; archive scanning supports mounted/local paths only"})
            continue
        with session_scope() as session:
            _seed_ledger(session, destination_id, Path(os.path.abspath(base_path)))
            scan = session.get(ArchiveScan, destination_id) or ArchiveScan(destination_id=destination_id, root_path=base_path)
            scan.root_path, scan.status, scan.error = base_path, "running", None
            scan.started_at, scan.finished_at, scan.files_seen = utcnow(), None, 0
            session.add(scan)
        seen = set()
        try:
            root = _safe_root(base_path)
            with session_scope() as session:
                active, hints = active_archive_paths(session, destination_id, root), _ledger(session, destination_id, root)
                for path, info in _walk(root, progress):
                    relative = path.relative_to(root).as_posix()
                    row = session.query(ArchiveFile).filter_by(destination_id=destination_id, path=relative).one_or_none()
                    if row is None:
                        row = ArchiveFile(destination_id=destination_id, root_path=str(root), path=relative, present=False,
                                          size_bytes=0, mtime_ns=0, ctime_ns=0, device="0", inode="0", nlink=1, mode=0, uid=0, gid=0)
                        session.add(row)
                    _update_row(row, root, relative, info, hints.get(relative), relative in active)
                    seen.add(relative)
                    if len(seen) % BATCH_SIZE == 0:
                        session.commit()
                        progress.update(0.6 * index / max(1, len(destinations)), f"Inventoried {len(seen)} archive files")
                # Only a fully completed traversal may mark old paths missing.
                progress.check(True)
                _safe_root(base_path)
                for row in session.query(ArchiveFile).filter_by(destination_id=destination_id):
                    if row.path not in seen or row.root_path != str(root):
                        row.present, row.eligible = False, False
                        if row.checked_at is None:
                            row.checked_at = utcnow()
                        invalidate_file(row)
                scan = session.get(ArchiveScan, destination_id)
                scan.status, scan.files_seen, scan.finished_at, scan.last_success_at = "done", len(seen), utcnow(), utcnow()
                session.commit()
            completed.append(destination_id)
        except (OSError, RuntimeError, JobCancelled) as exc:
            with session_scope() as session:
                scan = session.get(ArchiveScan, destination_id)
                scan.status = "cancelled" if isinstance(exc, JobCancelled) else "error"
                scan.error, scan.files_seen, scan.finished_at = str(exc) or "Cancelled", len(seen), utcnow()
            if isinstance(exc, JobCancelled):
                raise
            failures.append({"destination_id": destination_id, "error": str(exc)})
    _sample_candidates(progress, completed)
    progress.update(1.0, "Archive scan complete", True)
    with session_scope() as session:
        return {**archive_summary(session), "errors": failures}


def _sample_candidates(progress, completed):
    if not completed:
        return
    with session_scope() as session:
        sizes = [size for (size,) in session.query(ArchiveFile.size_bytes).filter_by(present=True, eligible=True)
                 .group_by(ArchiveFile.size_bytes).having(func.count(ArchiveFile.id) > 1)]
        rows = session.query(ArchiveFile).filter(
            ArchiveFile.present.is_(True), ArchiveFile.eligible.is_(True),
            ArchiveFile.destination_id.in_(completed), ArchiveFile.size_bytes.in_(sizes),
            ArchiveFile.sample_sha256.is_(None)).all() if sizes else []
        candidates = [(row.id, row.root_path, row.path, identity_dict(row)) for row in rows]
    updates = []

    def persist():
        # No write transaction is held open during a NAS content read, and at
        # most one cache transaction is committed per 100 sampled files.
        with session_scope() as session:
            for file_id, expected, digest in updates:
                row = session.get(ArchiveFile, file_id)
                if row and _same_identity(identity_dict(row), expected):
                    if digest:
                        row.sample_sha256 = digest
                    else:
                        row.eligible = False
                        invalidate_file(row)
        updates.clear()

    for count, (file_id, root_path, relative, expected) in enumerate(candidates, 1):
        progress.check()
        try:
            digest = sample_fingerprint(_safe_root(root_path), relative, expected, progress)
        except (OSError, RuntimeError):
            digest = None
        updates.append((file_id, expected, digest))
        if len(updates) >= BATCH_SIZE:
            persist()
        progress.update(0.8, f"Checked small samples from {count} same-size files")
    if updates:
        persist()


def _physical_key(row):
    # nlink==1 means equal inode values alone cannot imply a shared copy.
    if row.nlink > 1 and row.inode != "0":
        return (row.device, row.inode, row.size_bytes, row.mtime_ns, row.ctime_ns)
    return str(row.absolute_path)


def file_dict(row: ArchiveFile) -> dict:
    return {"id": row.id, "destination_id": row.destination_id, "path": row.path,
            "root_path": row.root_path, "absolute_path": str(row.absolute_path), "kind": row.kind,
            **identity_dict(row), "present": row.present, "eligible": row.eligible,
            "ledger_status": row.ledger_status, "status": "present" if row.present else "missing" if row.checked_at else "unchecked",
            "mtime": row.mtime_ns / 1_000_000_000, "sample_sha256": row.sample_sha256,
            "full_sha256": row.full_sha256, "verified_at": row.verified_at.isoformat() if row.verified_at else None}


def list_files(session, destination_id=None, offset=0, limit=100, status="all") -> dict:
    query = session.query(ArchiveFile)
    if status == "present":
        query = query.filter(ArchiveFile.present.is_(True))
    elif status == "missing":
        query = query.filter(ArchiveFile.present.is_(False), ArchiveFile.checked_at.isnot(None))
    elif status == "unchecked":
        query = query.filter(ArchiveFile.present.is_(False), ArchiveFile.checked_at.is_(None))
    if destination_id is not None:
        query = query.filter(ArchiveFile.destination_id == destination_id)
    count = query.count()
    rows = query.order_by(ArchiveFile.path, ArchiveFile.id).offset(max(0, offset)).limit(max(1, min(500, limit)))
    return {"files": [file_dict(row) for row in rows], "total": count}


def archive_summary(session) -> dict:
    # One dictionary entry per absolute path avoids overlapping destination roots.
    paths = {}
    for row in session.query(ArchiveFile).filter_by(present=True):
        key = str(row.absolute_path)
        if key not in paths or (not paths[key].eligible and row.eligible):
            paths[key] = row
    rows = list(paths.values())
    scans = list(session.query(ArchiveScan))
    last_scan = max((s.last_success_at for s in scans if s.last_success_at), default=None)
    physical = {_physical_key(row): row.size_bytes for row in rows}
    unsupported = [{"destination_id": d.id, "name": d.name, "type": d.type}
                   for d in session.query(Destination).filter(Destination.enabled.is_(True), Destination.type.notin_(MOUNTED_TYPES))]
    job = session.query(Job).filter(Job.kind.in_(("archive_scan", "archive_verify", "archive_hardlink")),
                                    Job.status.in_(("queued", "running", "paused"))).order_by(Job.id).first()
    active_job = {"id": job.id, "status": job.status, "description": job.description,
                  "progress": job.progress, "detail": job.detail} if job else None
    return {"total_files": len(rows) + session.query(ArchiveFile).filter_by(present=False).count(), "present_files": len(rows), "total_bytes": sum(r.size_bytes for r in rows),
            "physical_files": len(physical), "physical_bytes": sum(physical.values()),
            "missing_files": session.query(ArchiveFile).filter(ArchiveFile.present.is_(False), ArchiveFile.checked_at.isnot(None)).count(),
            "unchecked_files": session.query(ArchiveFile).filter(ArchiveFile.present.is_(False), ArchiveFile.checked_at.is_(None)).count(),
            "sample_unchecked_files": sum(not r.sample_sha256 and not r.full_sha256 for r in rows),
            "last_scan": last_scan.isoformat() if last_scan else None, "active_job": active_job,
            "destinations": [{"destination_id": s.destination_id, "root_path": s.root_path, "status": s.status,
                              "files_seen": s.files_seen, "error": s.error,
                              "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None} for s in scans],
            "unsupported_destinations": unsupported}


def duplicate_groups(session) -> list[dict]:
    samples = defaultdict(list)
    seen = set()
    for row in session.query(ArchiveFile).filter_by(present=True, eligible=True).filter(ArchiveFile.sample_sha256.isnot(None)).order_by(ArchiveFile.id):
        path = str(row.absolute_path)
        if path in seen:
            continue
        seen.add(path)
        samples[(row.size_bytes, row.sample_sha256)].append(row)
    groups = []
    for (size, sample), rows in samples.items():
        if len(rows) < 2:
            continue
        # Full hashes split sampled matches: differing verified files are excluded
        # unless an unverified file might still match either verified subgroup.
        verified = defaultdict(list)
        unknown = []
        for row in rows:
            (verified[row.full_sha256] if row.full_sha256 else unknown).append(row)
        subsets = [("verified", digest, members) for digest, members in verified.items() if len(members) > 1]
        if unknown:
            subsets.append(("candidate", sample, rows))
        for status, digest, members in subsets:
            physical = {_physical_key(row) for row in members}
            references = defaultdict(list)
            for row in members:
                references[_physical_key(row)].append(row)
            # An inode with additional, undiscovered links will remain allocated
            # even if every filename in this report is linked to another copy.
            # Only count inodes whose complete link set is represented here.
            removable = sum(max(row.nlink for row in copies) <= len(copies) for copies in references.values())
            savings = max(0, removable - (1 if removable == len(physical) else 0)) * size
            if len(physical) == 1:
                status = "already_linked"
            membership = [(r.id, identity_dict(r)) for r in members]
            key = hashlib.sha256(f"{size}:{status}:{digest}:{membership}".encode()).hexdigest()[:24]
            groups.append({"id": key, "status": status, "size_bytes": size,
                           "reclaimable_bytes": savings,
                           "files": [file_dict(row) for row in members]})
    return sorted(groups, key=lambda group: (-group["reclaimable_bytes"], group["id"]))


def duplicate_report(session, offset=0, limit=100) -> dict:
    groups = duplicate_groups(session)
    # Candidate savings are estimates, and never added to verified savings.
    summary = {"candidate_groups": sum(g["status"] == "candidate" for g in groups),
               "verified_groups": sum(g["status"] == "verified" for g in groups),
               "already_linked_groups": sum(g["status"] == "already_linked" for g in groups),
               "reclaimable_bytes": sum(g["reclaimable_bytes"] for g in groups if g["status"] == "verified"),
               "candidate_reclaimable_bytes": sum(g["reclaimable_bytes"] for g in groups if g["status"] == "candidate")}
    return {"groups": groups[max(0, offset):max(0, offset) + max(1, min(500, limit))], "total": len(groups), "summary": summary}


def get_duplicate_group(session, group_id: str) -> dict | None:
    return next((group for group in duplicate_groups(session) if group["id"] == group_id), None)
