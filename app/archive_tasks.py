"""Explicit, single-file-at-a-time archive jobs sharing the NAS upload slot."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .archive import (MOUNTED_TYPES, _safe_root, _safe_stat, _same_identity, _update_row, active_archive_paths,
                      file_dict, get_duplicate_group, identity_dict, scan_archive)
from .archive_links import hardlink_files, verify_file
from .archive_models import ArchiveFile
from .database import session_scope
from .jobs import ARCHIVE_KINDS, JobCancelled, handler
from .models import Destination, Job, utcnow

QUEUE_LOCK = threading.Lock()


def active_archive_job(session):
    return session.query(Job).filter(Job.kind.in_(ARCHIVE_KINDS), Job.status.in_(("queued", "running", "paused"))).order_by(Job.id).first()


def job_summary(job):
    return {"id": job.id, "kind": job.kind, "status": job.status, "description": job.description,
            "progress": job.progress, "detail": job.detail, "error": job.error} if job else None


def _group(session, payload, required=None):
    group = get_duplicate_group(session, payload.get("group_id", ""))
    if not group or (required and group["status"] != required):
        raise RuntimeError("This duplicate report changed; refresh it before continuing")
    if set(payload.get("file_ids", [])) - {f["id"] for f in group["files"]}:
        raise RuntimeError("Selected files are not members of this duplicate group")
    for file in group["files"]:
        destination = session.get(Destination, file["destination_id"])
        if not destination or not destination.enabled or destination.type not in MOUNTED_TYPES or _safe_root(destination.base_path) != Path(file["root_path"]):
            raise RuntimeError("Archive destination changed; scan its current configured path again")
        root = _safe_root(file["root_path"])
        if file["path"] in active_archive_paths(session, file["destination_id"], root):
            raise RuntimeError("An upload is changing this file; scan again when it finishes")
        from .archive import stat_identity
        if not _same_identity(stat_identity(_safe_stat(root, file["path"])), file):
            raise RuntimeError("File changed since the report; scan the archive again")
    return group


def _result(job_id, result):
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job:
            job.result = json.dumps(result)


@handler("archive_scan")
def run_scan(job_id, payload, ctx):
    # Do not compete with a live camera transfer or a metadata correction for
    # bandwidth. An audit is explicit, bounded to one job, and cancellable.
    with ctx.upload_semaphore:
        if ctx.is_cancelled():
            raise JobCancelled()
        result = scan_archive(payload.get("destination_ids"), ctx)
    _result(job_id, result)
    if result["errors"]:
        raise RuntimeError("Archive scan incomplete: " + "; ".join(error["error"] for error in result["errors"]))


@handler("archive_verify")
def run_verify(job_id, payload, ctx):
    with ctx.upload_semaphore:
        with session_scope() as session:
            group = _group(session, payload)
        results = []
        for index, file in enumerate(group["files"]):
            if ctx.is_cancelled():
                raise JobCancelled()
            result = verify_file(Path(file["root_path"]), file["path"], expected=file, cached=file, ctx=ctx)
            results.append((file, result))
            ctx.set_progress((index + 1) / len(group["files"]), f"Fully verified {index + 1} of {len(group['files'])} files")
        with session_scope() as session:
            for file, result in results:
                row = session.get(ArchiveFile, file["id"])
                if not row or not _same_identity(identity_dict(row), file):
                    raise RuntimeError("Inventory changed during verification; scan again")
                row.full_sha256, row.verified_at = result["full_sha256"], utcnow()
    _result(job_id, {"files_verified": len(results), "distinct_hashes": len({r["full_sha256"] for _, r in results})})


@handler("archive_hardlink")
def run_hardlink(job_id, payload, ctx):
    if payload.get("confirm") is not True:
        raise RuntimeError("Hardlinking requires explicit confirmation")
    with ctx.upload_semaphore:
        with session_scope() as session:
            group = _group(session, payload, "verified")
        members = {f["id"]: f for f in group["files"]}
        keeper = members.get(payload.get("keeper_id"))
        target_ids = list(dict.fromkeys(payload.get("file_ids", [])))
        if not keeper or not target_ids or keeper["id"] in target_ids:
            raise RuntimeError("Select a kept file and one or more other copies")
        targets = [members[i] for i in target_ids]
        if any(f["destination_id"] != keeper["destination_id"] or f["root_path"] != keeper["root_path"] for f in targets):
            raise RuntimeError("Choose copies within the same configured NAS destination")
        results = []
        for target in targets:
            previous_inode, previous_device = str(target["inode"]), str(target["device"])
            result = hardlink_files(Path(keeper["root_path"]), keeper["path"], target["path"],
                                    canonical_identity=keeper, duplicate_identity=target, ctx=ctx)
            results.append({"file_id": target["id"], "status": result["status"], "bytes_saved": result["bytes_saved"]})
            # A successful hardlink changes ctime/link count on all names of
            # the inode. Refresh that metadata without losing its full proof.
            with session_scope() as session:
                inode = str(result["canonical"]["inode"])
                device = str(result["canonical"]["device"])
                rows = session.query(ArchiveFile).filter(
                    ((ArchiveFile.inode == inode) & (ArchiveFile.device == device)) |
                    ((ArchiveFile.inode == previous_inode) & (ArchiveFile.device == previous_device)) |
                    (ArchiveFile.id == target["id"])).all()
                for row in rows:
                    sample = row.sample_sha256
                    old_identity = identity_dict(row)
                    info = _safe_stat(Path(row.root_path), row.path)
                    _update_row(row, Path(row.root_path), row.path, info,
                                (row.ledger_id, row.ledger_status), False)
                    # Unlinking a selected alias also changes the old inode's
                    # ctime/nlink. Preserve proof for its unchanged other names,
                    # but never assign it to an unrelated replacement file.
                    is_canonical = str(info.st_ino) == inode and str(info.st_dev) == device
                    old_unchanged = (str(info.st_ino) == str(old_identity["inode"]) and
                                     str(info.st_dev) == str(old_identity["device"]) and
                                     info.st_size == old_identity["size_bytes"] and info.st_mtime_ns == old_identity["mtime_ns"])
                    if is_canonical or old_unchanged:
                        row.sample_sha256 = sample
                        row.full_sha256 = result.get("full_sha256") or keeper["full_sha256"]
                        row.verified_at = utcnow()
                keeper = file_dict(session.get(ArchiveFile, keeper["id"]))
                # Refresh other aliases affected by either link-count change.
                for remaining in targets:
                    refreshed = session.get(ArchiveFile, remaining["id"])
                    if (str(refreshed.inode), str(refreshed.device)) in ((inode, device), (previous_inode, previous_device)):
                        remaining.update(file_dict(refreshed))
            _result(job_id, {"files": results, "bytes_saved": sum(r["bytes_saved"] for r in results)})
        ctx.set_progress(1.0, f"Processed {len(results)} confirmed duplicate copies")
