from pathlib import Path

from app.destinations.local import LocalBackend
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
