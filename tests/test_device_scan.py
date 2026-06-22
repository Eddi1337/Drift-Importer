from pathlib import Path

from app import devices
from app.devices import build_excluder, detect_devices


class _Settings:
    def __init__(self, paths):
        self._paths = [Path(p) for p in paths]

    @property
    def mount_path_list(self):
        return self._paths


def test_excluder_is_string_based_and_recursive():
    is_excluded = build_excluder(["/mnt/NAS"])
    assert is_excluded(Path("/mnt/NAS"))
    assert is_excluded(Path("/mnt/NAS/2025/11"))
    assert not is_excluded(Path("/mnt/nas"))          # case-sensitive
    assert not is_excluded(Path("/mnt"))              # parent isn't excluded
    assert not is_excluded(Path("/media/ed/Drift Card"))


def test_detect_devices_skips_excluded_destination(tmp_path, monkeypatch):
    base = tmp_path / "mnt"
    # A NAS-like destination that contains videos (would be picked up as a
    # media root) and a real camera with a DCIM folder.
    nas = base / "NAS" / "2025" / "11"
    nas.mkdir(parents=True)
    (nas / "DVR0001.MP4").write_bytes(b"x")
    dcim = base / "Drift Card" / "DCIM" / "100MEDIA"
    dcim.mkdir(parents=True)
    (dcim / "DVR0002.MP4").write_bytes(b"x")

    monkeypatch.setattr(devices, "get_settings", lambda: _Settings([str(base)]))

    found = detect_devices(exclude_paths=[str(base / "NAS")])
    dcims = [str(d.dcim_path) for d in found]

    assert any(p.lower().endswith("dcim") for p in dcims)        # camera found
    assert not any(str(base / "NAS") in p for p in dcims)        # NAS excluded
