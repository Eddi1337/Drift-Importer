from app.destinations.local import LocalBackend
from app.models import Destination


def test_local_backend_lists_child_directories(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "clip.mp4").write_text("x", encoding="utf-8")

    backend = LocalBackend(Destination(name="NAS", type="local", base_path=str(tmp_path)))

    assert backend.list_directories() == ["alpha", "beta"]
