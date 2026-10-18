"""Bounded full-file verification and explicit, guarded NAS hardlinking.

Callers obtain ``root`` from a configured NAS destination, never a browser path.
They must hold the upload semaphore while linking so app uploads and recording
corrections cannot replace either path. Other NAS clients must leave files idle.
Sample hashes are candidates only: this module always reads both complete files
again immediately before a requested link operation.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .jobs import JobCancelled
from .storage import require_storage

BUFFER_BYTES = 1024 * 1024
READ_PAUSE_SECONDS = 0.002
_SERIAL = threading.RLock()
_IDENTITY_KEYS = ("device", "inode", "size_bytes", "mtime_ns", "ctime_ns")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ArchiveLinkError(RuntimeError):
    """The archive changed or is not safe to consolidate."""


def identity(info: os.stat_result) -> dict:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "nlink": info.st_nlink,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _same(actual: Mapping, expected: Mapping, keys=_IDENTITY_KEYS) -> bool:
    def equal(key):
        if key not in expected:
            return False
        if key in ("device", "inode"):
            return str(actual[key]) == str(expected[key])
        return actual[key] == expected[key]
    return all(equal(key) for key in keys)


def _expect(info: os.stat_result, expected: Mapping | None) -> dict:
    current = identity(info)
    if expected is not None and not _same(current, expected):
        raise ArchiveLinkError("File changed since the archive scan; scan it again")
    return current


def _parts(root: Path, path: Path | str) -> tuple[str, ...]:
    candidate = Path(path)
    try:
        relative = candidate.relative_to(root) if candidate.is_absolute() else candidate
    except ValueError as exc:
        raise ArchiveLinkError("Archive path escapes the configured NAS destination") from exc
    parts = relative.parts
    if not parts or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        raise ArchiveLinkError("Hidden, temporary or escaping archive paths are excluded")
    if any(part.lower().endswith(".part") for part in parts):
        raise ArchiveLinkError("Incomplete .part uploads are excluded")
    return parts


@contextmanager
def _open(root: Path, path: Path | str) -> Iterator[tuple[int, int, str]]:
    # Walk directory descriptors with O_NOFOLLOW instead of resolving away a
    # symlink. The descriptors pin parent directories during the operation.
    root = Path(root).absolute()
    if root.is_symlink():
        raise ArchiveLinkError("A NAS destination must not be a symlink")
    guarded = require_storage(root)
    if guarded != root:
        raise ArchiveLinkError("A NAS destination must not contain symlinks")
    parts = _parts(root, path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ArchiveLinkError("This system cannot safely open archive paths without symlinks")
    try:
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
            stack.callback(os.close, directory)
            for part in parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=directory)
                stack.callback(os.close, directory)
            descriptor = os.open(parts[-1], os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=directory)
            stack.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ArchiveLinkError("Only regular archive files can be verified or linked")
            yield descriptor, directory, parts[-1]
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ArchiveLinkError("Symlink archive paths are excluded") from exc
        if exc.errno == errno.ENOENT:
            raise ArchiveLinkError("Archive file is missing; scan the NAS again") from exc
        raise


class _Progress:
    def __init__(self, ctx=None, progress: Callable[[int, int], None] | None = None):
        self.ctx = ctx
        self.progress = progress
        self.checked_at = -float("inf")

    def update(self, amount: int, total: int, *, force: bool = False) -> None:
        # JobContext cancellation itself reads SQLite. Check once per second,
        # rather than on each MiB read across a large NAS archive.
        now = time.monotonic()
        if force or now - self.checked_at >= 1:
            self.checked_at = now
            if self.ctx is not None:
                if self.ctx.is_cancelled():
                    raise JobCancelled()
                self.ctx.set_progress(min(0.99, amount / max(total, 1)), "Verifying complete NAS files")
            if self.progress is not None:
                self.progress(amount, total)


def _hash(descriptor: int, expected: dict, report: _Progress, *, offset=0, total=None) -> str:
    total = expected["size_bytes"] if total is None else total
    _expect(os.fstat(descriptor), expected)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    read = 0
    report.update(offset, total, force=True)
    while True:
        block = os.read(descriptor, BUFFER_BYTES)
        if not block:
            break
        digest.update(block)
        read += len(block)
        report.update(offset + read, total)
        # Hash only one file at a time and yield CPU/I/O time to live backups.
        if READ_PAUSE_SECONDS:
            time.sleep(READ_PAUSE_SECONDS)
    _expect(os.fstat(descriptor), expected)
    if read != expected["size_bytes"]:
        raise ArchiveLinkError("File length changed during verification")
    report.update(offset + read, total, force=True)
    return digest.hexdigest()


def _path_matches(directory: int, name: str, expected: Mapping, *, content_only=False) -> None:
    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    keys = ("device", "inode", "size_bytes", "mtime_ns", "mode", "uid", "gid") if content_only else _IDENTITY_KEYS
    if not stat.S_ISREG(current.st_mode) or not _same(identity(current), expected, keys):
        raise ArchiveLinkError("Archive path changed during verification; scan it again")


def _extended_metadata(descriptor: int) -> dict:
    """Include ACL/security attributes; never guess when the NAS cannot read them."""
    try:
        names = os.listxattr(descriptor)
        return {name: os.getxattr(descriptor, name) for name in names}
    except (AttributeError, NotImplementedError, TypeError, OSError) as exc:
        raise ArchiveLinkError("Cannot verify extended attributes or ACLs on this filesystem; keep separate copies") from exc


def verify_file(root: Path, path: Path | str, *, expected: Mapping | None = None,
                cached: Mapping | None = None, ctx=None,
                progress: Callable[[int, int], None] | None = None) -> dict:
    """Return a full SHA-256 plus inode/stat identity without modifying the file.

    A trusted persisted full hash is reused only with the identical inode, size,
    nanosecond mtime AND ctime. A sample digest must never be passed as a full hash.
    Hardlink actions deliberately do not use this cache.
    """
    with _SERIAL, _open(root, path) as (descriptor, directory, name):
        current = _expect(os.fstat(descriptor), expected)
        digest = cached.get("full_sha256") if cached else None
        if not (isinstance(digest, str) and _SHA256.fullmatch(digest) and _same(current, cached)):
            digest = _hash(descriptor, current, _Progress(ctx, progress))
        _path_matches(directory, name, current)
        _expect(os.fstat(descriptor), current)
        return {**current, "full_sha256": digest}


def hardlink_files(root: Path, canonical: Path | str, duplicate: Path | str, *,
                   canonical_identity: Mapping, duplicate_identity: Mapping,
                   ctx=None, progress: Callable[[int, int], None] | None = None) -> dict:
    """Keep both names, atomically replace the duplicate with a verified hardlink.

    This must only run for an explicitly confirmed action, under the app upload
    semaphore. No source file is deleted. No cached/sample hash authorises a link.
    Metadata must already match because hardlinks subsequently share metadata.
    """
    with _SERIAL, _open(root, canonical) as (source_fd, source_dir, source_name), _open(root, duplicate) as (target_fd, target_dir, target_name):
        source = _expect(os.fstat(source_fd), canonical_identity)
        target = _expect(os.fstat(target_fd), duplicate_identity)
        if source["device"] != target["device"]:
            raise ArchiveLinkError("Hardlinks require both files to be on the same filesystem")
        _path_matches(source_dir, source_name, source)
        _path_matches(target_dir, target_name, target)
        if source["inode"] == target["inode"]:
            return {"status": "already_linked", "bytes_saved": 0, "canonical": source, "duplicate": target}
        if any(source[key] != target[key] for key in ("mode", "uid", "gid", "mtime_ns")):
            raise ArchiveLinkError("File dates or permissions differ. Hardlinks share metadata; keep separate copies")
        source_metadata = _extended_metadata(source_fd)
        target_metadata = _extended_metadata(target_fd)
        if source_metadata != target_metadata:
            raise ArchiveLinkError("Extended attributes or ACLs differ. Hardlinks share metadata; keep separate copies")
        if source["size_bytes"] != target["size_bytes"]:
            raise ArchiveLinkError("Files have different sizes and are not duplicates")
        report = _Progress(ctx, progress)
        total = source["size_bytes"] + target["size_bytes"]
        source_hash = _hash(source_fd, source, report, total=total)
        target_hash = _hash(target_fd, target, report, offset=source["size_bytes"], total=total)
        if source_hash != target_hash:
            raise ArchiveLinkError("Full SHA-256 differs; these files are not duplicates")
        _expect(os.fstat(source_fd), source)
        _expect(os.fstat(target_fd), target)
        _path_matches(source_dir, source_name, source)
        _path_matches(target_dir, target_name, target)
        require_storage(Path(root))
        temporary = f".drift-link-{uuid.uuid4().hex}.part"
        replaced = False
        created = False
        try:
            os.link(source_name, temporary, src_dir_fd=source_dir, dst_dir_fd=target_dir, follow_symlinks=False)
            created = True
            # Linking changes the source ctime/nlink itself; its content identity
            # must still match. The temporary entry must name that same inode.
            _path_matches(target_dir, temporary, source, content_only=True)
            _path_matches(source_dir, source_name, source, content_only=True)
            _expect(os.fstat(target_fd), target)
            _path_matches(target_dir, target_name, target)
            if _extended_metadata(source_fd) != source_metadata or _extended_metadata(target_fd) != target_metadata:
                raise ArchiveLinkError("Extended attributes or ACLs changed during verification; scan again")
            report.update(total, total, force=True)
            os.fsync(target_dir)
            os.replace(temporary, target_name, src_dir_fd=target_dir, dst_dir_fd=target_dir)
            replaced = True
            os.fsync(target_dir)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise ArchiveLinkError("Hardlinks require both files to be on the same filesystem") from exc
            if exc.errno in (errno.EOPNOTSUPP, errno.ENOSYS):
                raise ArchiveLinkError("This NAS filesystem does not support hardlinks") from exc
            if replaced:
                raise ArchiveLinkError("Files were linked, but directory durability could not be confirmed; rescan the archive") from exc
            raise
        finally:
            if created and not replaced:
                try:
                    os.unlink(temporary, dir_fd=target_dir)
                except FileNotFoundError:
                    pass
        return {"status": "linked", "bytes_saved": target["size_bytes"] if target["nlink"] == 1 else 0,
                "full_sha256": source_hash, "canonical": identity(os.fstat(source_fd)),
                "duplicate": identity(os.stat(target_name, dir_fd=target_dir, follow_symlinks=False))}
