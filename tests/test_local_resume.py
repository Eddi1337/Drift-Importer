from pathlib import Path
import hashlib

import pytest

from app.destinations import local as local_mod
from app.destinations.local import LocalBackend
from app.media import checksum as sampled_checksum
from app.models import Destination


def test_local_backend_resumes_partial_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIFT_UPLOAD_CHUNK_BYTES", "4")

    from app import config

    config.get_settings.cache_clear()

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0123456789abcdef")

    root = tmp_path / "remote"
    root.mkdir()
    destination = Destination(name="NAS", type="local", base_path=str(root))
    backend = LocalBackend(destination)

    remote_dir = "2026/06"
    partial_dir = root / remote_dir
    partial_dir.mkdir(parents=True)
    partial = partial_dir / "clip.mp4.part"
    partial.write_bytes(b"01234567")

    progress = []
    offset = backend.get_resume_offset(remote_dir, "clip.mp4", source.stat().st_size)
    assert offset == 8

    remote_path = backend.upload(
        source,
        remote_dir,
        "clip.mp4",
        progress=lambda sent, total: progress.append((sent, total)),
        start_offset=offset,
    )

    assert Path(remote_path).read_bytes() == source.read_bytes()
    assert not partial.exists()
    assert progress[-1] == (source.stat().st_size, source.stat().st_size)


def test_local_backend_verifies_remote_hash(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"same-size-content")
    checksum = sampled_checksum(source)

    root = tmp_path / "remote"
    remote_dir = root / "2026" / "06"
    remote_dir.mkdir(parents=True)
    remote = remote_dir / "clip.mp4"
    remote.write_bytes(source.read_bytes())

    backend = LocalBackend(Destination(name="NAS", type="local", base_path=str(root)))

    assert backend.remote_file_matches("2026/06", "clip.mp4", source.stat().st_size, checksum)
    assert not backend.remote_file_matches(
        "2026/06",
        "clip.mp4",
        source.stat().st_size,
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )

    remote.write_bytes(b"different-content")

    assert not backend.remote_file_matches("2026/06", "clip.mp4", source.stat().st_size, checksum)


def test_local_backend_refuses_missing_root(tmp_path):
    """A missing root (e.g. NAS not mounted) must not be silently created."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0123456789abcdef")

    root = tmp_path / "mnt" / "nas"  # never created -> mount absent
    backend = LocalBackend(Destination(name="NAS", type="local", base_path=str(root)))

    with pytest.raises(FileNotFoundError):
        backend.test_connection()
    with pytest.raises(FileNotFoundError):
        backend.upload(source, "2026/06", "clip.mp4")

    # Crucially, it did NOT create the root on the underlying filesystem.
    assert not root.exists()


def test_local_backend_refuses_when_disk_full(tmp_path, monkeypatch):
    """Preflight space check fails fast instead of filling the disk."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"0123456789abcdef")

    root = tmp_path / "remote"
    root.mkdir()
    backend = LocalBackend(Destination(name="NAS", type="local", base_path=str(root)))

    class _Usage:
        free = 1  # effectively no space

    monkeypatch.setattr(local_mod.shutil, "disk_usage", lambda _p: _Usage())

    with pytest.raises(OSError):
        backend.upload(source, "2026/06", "clip.mp4")

    # Nothing partial should have been written.
    assert not (root / "2026" / "06" / "clip.mp4.part").exists()
