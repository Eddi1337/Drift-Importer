from pathlib import Path


def test_detect_devices_finds_user_media_card_with_nested_dcim(tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    dcim = media_root / "ed" / "Drift Card" / "DCIM"
    nested = dcim / "100MEDIA"
    nested.mkdir(parents=True)
    (nested / "clip001.MP4").write_bytes(b"video")

    monkeypatch.setenv("DRIFT_MOUNT_PATHS", str(media_root))

    from app import config
    from app.devices import detect_devices, scan_media_files

    config.get_settings.cache_clear()

    devices = detect_devices()

    assert len(devices) == 1
    assert devices[0].label == "Drift Card"
    assert devices[0].dcim_path == dcim.resolve()
    assert scan_media_files(devices[0].dcim_path) == [nested / "clip001.MP4"]


def test_detect_devices_falls_back_to_mounted_video_folder(tmp_path, monkeypatch):
    media_root = tmp_path / "Volumes"
    clips = media_root / "Drift Card" / "VIDEO"
    clips.mkdir(parents=True)
    (clips / "clip002.MP4").write_bytes(b"video")

    monkeypatch.setenv("DRIFT_MOUNT_PATHS", str(media_root))

    from app import config
    from app.devices import detect_devices

    config.get_settings.cache_clear()

    devices = detect_devices()

    assert len(devices) == 1
    assert devices[0].label == "Drift Card"
    assert devices[0].dcim_path == clips.resolve()
