"""Consolidation needs complete equality and preserves both archive names."""
import errno
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import archive_links as links
from app.jobs import JobCancelled


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "NAS"
    root.mkdir()
    monkeypatch.setattr(links, "require_storage", lambda path: Path(path).resolve())
    monkeypatch.setattr(links, "READ_PAUSE_SECONDS", 0)
    # The deployed Linux Python exposes descriptor xattr APIs. macOS Python
    # may omit them entirely; model an attribute-free supported NAS for these
    # link-behaviour tests, with separate fail-closed coverage below.
    if not hasattr(links.os, "listxattr"):
        monkeypatch.setattr(links.os, "listxattr", lambda descriptor: [], raising=False)
        monkeypatch.setattr(links.os, "getxattr", lambda descriptor, name: b"", raising=False)
    return root


def pair(root, first=b"same video bytes", second=None):
    a, b = root / "2026" / "a.mp4", root / "Holding" / "b.mp4"
    a.parent.mkdir(exist_ok=True)
    b.parent.mkdir(exist_ok=True)
    a.write_bytes(first)
    b.write_bytes(first if second is None else second)
    stamp = 1_750_000_000_123_456_789
    os.utime(a, ns=(stamp, stamp))
    os.utime(b, ns=(stamp, stamp))
    return a, b


def linked(root, a, b, **kwargs):
    return links.hardlink_files(root, a, b,
                               canonical_identity=links.identity(a.stat()),
                               duplicate_identity=links.identity(b.stat()), **kwargs)


def test_full_verification_hash_and_trusted_cache_require_exact_identity(archive, monkeypatch):
    a, _ = pair(archive)
    result = links.verify_file(archive, a)
    assert result["full_sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()
    assert result["ctime_ns"] == a.stat().st_ctime_ns
    # A still-identical cached full hash avoids another content read.
    monkeypatch.setattr(links, "_hash", lambda *args, **kwargs: pytest.fail("cache should be reused"))
    assert links.verify_file(archive, a, cached=result) == result


def test_cached_full_hash_invalidated_when_same_size_and_mtime_content_changes(archive):
    a, _ = pair(archive, b"AAAA")
    old = links.verify_file(archive, a)
    a.write_bytes(b"BBBB")
    os.utime(a, ns=(old["mtime_ns"], old["mtime_ns"]))
    assert a.stat().st_ctime_ns != old["ctime_ns"]
    updated = links.verify_file(archive, a, cached=old)
    assert updated["full_sha256"] == hashlib.sha256(b"BBBB").hexdigest()
    assert updated["full_sha256"] != old["full_sha256"]


def test_cache_missing_ctime_or_invalid_digest_never_skips_full_read(archive):
    a, _ = pair(archive)
    cached = links.verify_file(archive, a)
    cached.pop("ctime_ns")
    cached["full_sha256"] = "0" * 64
    assert links.verify_file(archive, a, cached=cached)["full_sha256"] != "0" * 64
    cached = {**links.identity(a.stat()), "full_sha256": "not a full digest"}
    assert links.verify_file(archive, a, cached=cached)["full_sha256"] != "not a full digest"


def test_expected_string_inode_identity_from_nfs_database_is_accepted(archive):
    a, _ = pair(archive)
    expected = links.identity(a.stat())
    expected.update(device=str(expected["device"]), inode=str(expected["inode"]))
    assert links.verify_file(archive, a, expected=expected)["inode"] == a.stat().st_ino


def test_stale_scan_identity_is_rejected_without_changing_paths(archive):
    a, b = pair(archive)
    expected = links.identity(b.stat())
    b.write_bytes(b"replacement video")
    before = b.read_bytes()
    with pytest.raises(links.ArchiveLinkError, match="changed since"):
        links.hardlink_files(archive, a, b, canonical_identity=links.identity(a.stat()), duplicate_identity=expected)
    assert b.read_bytes() == before
    assert a.stat().st_ino != b.stat().st_ino


def test_sampled_hash_collision_cannot_authorise_hardlink(archive):
    # Existing upload sampling uses the first/last 4 MiB. These candidates have
    # identical sampled regions and size, but different bytes in the middle.
    edge = b"s" * (4 * 1024 * 1024)
    a, b = pair(archive, edge + b"AAAA" + edge, edge + b"BBBB" + edge)
    before = b.stat().st_ino
    with pytest.raises(links.ArchiveLinkError, match="Full SHA-256 differs"):
        linked(archive, a, b)
    assert b.stat().st_ino == before
    assert b.read_bytes()[len(edge):len(edge)+4] == b"BBBB"
    assert not list(b.parent.glob(".drift-link-*"))


def test_confirmed_link_replaces_atomically_and_preserves_both_names(archive, monkeypatch):
    a, b = pair(archive)
    original_replace = links.os.replace
    old_target_inode = b.stat().st_ino
    calls = []

    def replace(source, target, **kwargs):
        # Up to the atomic rename, the duplicate still names its original inode.
        assert a.is_file() and b.is_file()
        assert b.stat().st_ino == old_target_inode
        assert os.stat(source, dir_fd=kwargs["src_dir_fd"]).st_ino == a.stat().st_ino
        calls.append((source, target))
        return original_replace(source, target, **kwargs)

    monkeypatch.setattr(links.os, "replace", replace)
    result = linked(archive, a, b)
    assert result["status"] == "linked"
    assert result["bytes_saved"] == a.stat().st_size
    assert a.read_bytes() == b.read_bytes() == b"same video bytes"
    assert a.stat().st_ino == b.stat().st_ino
    assert a.stat().st_nlink == 2
    assert len(calls) == 1
    assert sorted(p.name for p in b.parent.iterdir()) == ["b.mp4"]
    # Repeated requests are harmless when based on a current inventory.
    assert linked(archive, a, b)["status"] == "already_linked"


@pytest.mark.parametrize("difference", ["mtime", "mode"])
def test_metadata_difference_is_not_silently_discarded(archive, difference):
    a, b = pair(archive)
    if difference == "mtime":
        os.utime(b, ns=(b.stat().st_mtime_ns + 1, b.stat().st_mtime_ns + 1))
    else:
        a.chmod(0o600)
        b.chmod(0o644)
    with pytest.raises(links.ArchiveLinkError, match="dates or permissions differ"):
        linked(archive, a, b)
    assert a.stat().st_ino != b.stat().st_ino


@pytest.mark.parametrize("relative", ["../outside.mp4", ".drift/private.mp4", "2026/.hidden.mp4", "2026/open.mp4.part"])
def test_hidden_incomplete_and_escaping_paths_are_rejected(archive, relative):
    with pytest.raises(links.ArchiveLinkError, match="excluded|escapes"):
        links.verify_file(archive, relative)


def test_file_symlink_and_directory_symlink_are_rejected_even_inside_nas(archive):
    a, _ = pair(archive)
    shortcut = archive / "shortcut.mp4"
    shortcut.symlink_to(a)
    folder = archive / "shortcut"
    folder.symlink_to(a.parent, target_is_directory=True)
    for candidate in (shortcut, folder / a.name):
        with pytest.raises(links.ArchiveLinkError, match="Symlink"):
            links.verify_file(archive, candidate)


def test_symlink_outside_nas_is_never_read(archive, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = elsewhere / "secret.mp4"
    secret.write_bytes(b"not archive")
    (archive / "escape").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(links.ArchiveLinkError, match="Symlink"):
        links.verify_file(archive, "escape/secret.mp4")
    with pytest.raises(links.ArchiveLinkError, match="escapes"):
        links.verify_file(archive, secret)


def test_file_content_mutation_during_hash_is_rejected(archive, monkeypatch):
    a, _ = pair(archive, b"original video")
    before = a.stat()
    original_read = links.os.read
    changed = False

    def read(descriptor, size):
        nonlocal changed
        value = original_read(descriptor, size)
        if not changed:
            changed = True
            a.write_bytes(b"modified video")
            os.utime(a, ns=(before.st_mtime_ns, before.st_mtime_ns))
        return value

    monkeypatch.setattr(links.os, "read", read)
    with pytest.raises(links.ArchiveLinkError, match="changed since"):
        links.verify_file(archive, a)


def test_path_replacement_during_hash_is_rejected(archive):
    a, _ = pair(archive)
    moved = False

    def progress(amount, total):
        nonlocal moved
        if amount == total and not moved:
            moved = True
            replacement = a.with_name("new.mp4")
            replacement.write_bytes(a.read_bytes())
            os.replace(replacement, a)

    with pytest.raises(links.ArchiveLinkError, match="changed during"):
        links.verify_file(archive, a, progress=progress)


@pytest.mark.parametrize("number, message", [(errno.EXDEV, "same filesystem"), (errno.EOPNOTSUPP, "does not support")])
def test_link_unsupported_or_crossdevice_preserves_target(archive, monkeypatch, number, message):
    a, b = pair(archive)
    inode = b.stat().st_ino

    def failed_link(*args, **kwargs):
        raise OSError(number, "simulated NAS failure")

    monkeypatch.setattr(links.os, "link", failed_link)
    with pytest.raises(links.ArchiveLinkError, match=message):
        linked(archive, a, b)
    assert b.stat().st_ino == inode
    assert b.read_bytes() == a.read_bytes()
    assert not list(b.parent.glob(".drift-link-*"))


def test_replace_failure_preserves_target_and_cleans_staging_link(archive, monkeypatch):
    a, b = pair(archive)
    inode = b.stat().st_ino

    def failed_replace(*args, **kwargs):
        raise OSError(errno.EIO, "NAS interrupted")

    monkeypatch.setattr(links.os, "replace", failed_replace)
    with pytest.raises(OSError, match="NAS interrupted"):
        linked(archive, a, b)
    assert b.stat().st_ino == inode
    assert b.read_bytes() == a.read_bytes()
    assert a.stat().st_nlink == 1
    assert not list(b.parent.glob(".drift-link-*"))


def test_target_changed_after_verification_is_not_overwritten(archive, monkeypatch):
    a, b = pair(archive)
    original_link = links.os.link

    def changing_link(*args, **kwargs):
        original_link(*args, **kwargs)
        replacement = b.with_name("new.mp4")
        replacement.write_bytes(b"new content arriving from another client")
        os.replace(replacement, b)

    monkeypatch.setattr(links.os, "link", changing_link)
    with pytest.raises(links.ArchiveLinkError, match="changed since|changed during"):
        linked(archive, a, b)
    assert b.read_bytes() == b"new content arriving from another client"
    assert a.stat().st_nlink == 1
    assert not list(b.parent.glob(".drift-link-*"))


def test_cancellation_removes_temporary_link_without_replacing_target(archive):
    a, b = pair(archive)
    inode = b.stat().st_ino
    ctx = SimpleNamespace(is_cancelled=lambda: bool(list(b.parent.glob(".drift-link-*"))), set_progress=lambda *args: None)
    with pytest.raises(JobCancelled):
        linked(archive, a, b, ctx=ctx)
    assert b.stat().st_ino == inode
    assert a.stat().st_nlink == 1
    assert not list(b.parent.glob(".drift-link-*"))


def test_mount_guard_refuses_before_hashing_or_writing(archive, monkeypatch):
    a, b = pair(archive)
    def offline(root):
        raise RuntimeError("NAS is not mounted")
    monkeypatch.setattr(links, "require_storage", offline)
    with pytest.raises(RuntimeError, match="not mounted"):
        linked(archive, a, b)
    assert a.stat().st_ino != b.stat().st_ino


def test_extended_attributes_or_acl_differences_preserve_target(archive, monkeypatch):
    a, b = pair(archive)
    monkeypatch.setattr(links.os, 'listxattr', lambda descriptor: ['user.camera-note'])
    monkeypatch.setattr(links.os, 'getxattr', lambda descriptor, name: b'kept' if os.fstat(descriptor).st_ino == a.stat().st_ino else b'unique target metadata')
    inode = b.stat().st_ino
    with pytest.raises(links.ArchiveLinkError, match='attributes or ACLs differ'):
        linked(archive, a, b)
    assert b.stat().st_ino == inode


def test_unreadable_extended_attributes_refuse_link_conservatively(archive, monkeypatch):
    a, b = pair(archive)
    def unsupported(descriptor):
        raise OSError(errno.EOPNOTSUPP, 'NAS does not expose attributes')
    monkeypatch.setattr(links.os, 'listxattr', unsupported)
    with pytest.raises(links.ArchiveLinkError, match='Cannot verify extended attributes'):
        linked(archive, a, b)
    assert a.stat().st_ino != b.stat().st_ino


def test_missing_attribute_apis_refuse_link_conservatively(archive, monkeypatch):
    a, b = pair(archive)
    monkeypatch.delattr(links.os, 'listxattr', raising=False)
    with pytest.raises(links.ArchiveLinkError, match='Cannot verify extended attributes'):
        linked(archive, a, b)
    assert a.stat().st_ino != b.stat().st_ino
