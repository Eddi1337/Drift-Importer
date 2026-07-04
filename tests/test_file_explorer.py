import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import MediaItem
from app.routers import api


class DummyMonitor:
    def __init__(self, root):
        self.root = root

    def get_devices(self):
        return [{"path": str(self.root), "dcim_path": str(self.root / "DCIM"), "label": "Drift Card"}]


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_camera_explorer_browses_whole_camera_root(tmp_path, monkeypatch):
    root = tmp_path / "Drift Card"
    dcim = root / "DCIM"
    event = root / "EVENT"
    dcim.mkdir(parents=True)
    event.mkdir()
    (event / "LOCK001.MP4").write_bytes(b"video")

    monkeypatch.setattr(api, "get_device_monitor", lambda: DummyMonitor(root))

    data = api.browse_camera_entries(str(root))

    names = {entry["name"] for entry in data["entries"]}
    assert {"DCIM", "EVENT"} <= names

    event_data = api.browse_camera_entries(str(root), "EVENT")
    assert event_data["entries"][0]["name"] == "LOCK001.MP4"
    assert event_data["entries"][0]["playable"] is True


def test_camera_explorer_rename_and_timestamp_update_media_row(tmp_path, monkeypatch):
    root = tmp_path / "Drift Card"
    dcim = root / "DCIM"
    dcim.mkdir(parents=True)
    clip = dcim / "CLIP001.MP4"
    clip.write_bytes(b"video")
    session = _session()
    session.add(
        MediaItem(
            path=str(clip),
            filename=clip.name,
            kind="video",
            size_bytes=clip.stat().st_size,
            checksum="abc",
            source="device",
        )
    )
    session.commit()
    monkeypatch.setattr(api, "get_device_monitor", lambda: DummyMonitor(root))

    renamed = api.rename_camera_file(
        api.FileRenameReq(path="DCIM/CLIP001.MP4", filename="CLIP002.MP4"),
        root_path=str(root),
        session=session,
    )
    assert renamed["name"] == "CLIP002.MP4"
    assert not clip.exists()
    assert (dcim / "CLIP002.MP4").exists()

    api.timestamp_camera_file(
        api.FileTimestampReq(path="DCIM/CLIP002.MP4", modified_at="2026-07-04T12:34"),
        root_path=str(root),
        session=session,
    )
    item = session.query(MediaItem).one()
    assert item.filename == "CLIP002.MP4"
    assert item.capture_time == dt.datetime(2026, 7, 4, 12, 34)
