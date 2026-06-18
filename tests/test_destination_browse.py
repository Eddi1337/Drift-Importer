from app.destinations.local import LocalBackend
from app.models import Destination


def test_local_backend_lists_child_directories(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "clip.mp4").write_text("x", encoding="utf-8")

    backend = LocalBackend(Destination(name="NAS", type="local", base_path=str(tmp_path)))

    assert backend.list_directories() == ["alpha", "beta"]


def test_local_backend_lists_files_and_directories(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "clip.mp4").write_text("x", encoding="utf-8")

    backend = LocalBackend(Destination(name="NAS", type="local", base_path=str(tmp_path)))

    entries = backend.list_entries()

    assert entries[0]["name"] == "alpha"
    assert entries[0]["type"] == "directory"
    assert entries[1]["name"] == "clip.mp4"
    assert entries[1]["type"] == "file"
    assert entries[1]["size_bytes"] == 1
