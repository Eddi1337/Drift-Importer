import copy
import datetime as dt
import json
from pathlib import Path
import struct

import pytest

from app import ride_render as queue
from app import ride_render_worker as worker


SIGNATURE = "ab" * 32


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "require_storage", lambda path: path.resolve())
    monkeypatch.setattr(worker, "require_storage", lambda path: path.resolve())
    path = tmp_path / "2026" / "10" / "DRIFT001.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"source original")
    return tmp_path, path


def clip(path="2026/10/DRIFT001.mp4", **extra):
    return {"id": "archive:1", "path": path, "start_time": "2026-10-18T12:00:00Z", "duration_s": 60} | extra


def gps():
    return [
        {"time": "2026-10-18T12:00:00Z", "latitude": 51.5, "longitude": -.1, "segment": 0},
        {"time": "2026-10-18T12:00:20Z", "latitude": 51.501, "longitude": -.099, "segment": 0},
        {"time": "2026-10-18T12:00:40Z", "latitude": 51.502, "longitude": -.098, "segment": 1},
        {"time": "2026-10-18T12:01:00Z", "latitude": 51.503, "longitude": -.097, "segment": 1},
    ]


def test_queue_preserves_original_and_does_not_copy_it(source):
    root, original = source
    before = original.stat()
    state = queue.queue_render(root, SIGNATURE, [clip()], gps(), title="Sunday ride")
    directory = root / ".drift" / "ride-renders" / SIGNATURE
    request = json.loads((directory / "request.json").read_text())
    assert state["status"] == "queued"
    assert request["clips"][0]["path"] == "2026/10/DRIFT001.mp4"
    assert request["clips"][0]["mtime_ns"] == before.st_mtime_ns
    assert {p.name for p in directory.iterdir()} == {"request.json", "state.json"}
    assert original.read_bytes() == b"source original"
    assert original.stat().st_mtime_ns == before.st_mtime_ns
    assert queue.queue_render(root, SIGNATURE, [clip()], gps(), title="Sunday ride") == state


def test_queue_rejects_changed_request_and_source(source):
    root, original = source
    queue.queue_render(root, SIGNATURE, [clip()], gps())
    with pytest.raises(ValueError, match="ride changed"):
        queue.queue_render(root, SIGNATURE, [clip(duration_s=50)], gps())
    directory = queue.job_directory(root, SIGNATURE)
    request = json.loads((directory / "request.json").read_text())
    original.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="source changed"):
        queue.validate_manifest(root, request)


def test_manifest_detects_tampering_and_timezone_missing(source):
    root, _ = source
    manifest = queue.build_manifest(root, SIGNATURE, [clip()], gps())
    manifest["clips"][0]["duration_s"] = 50
    with pytest.raises(ValueError, match="request changed"):
        queue.validate_manifest(root, manifest)
    with pytest.raises(ValueError, match="timezone"):
        queue.build_manifest(root, SIGNATURE, [clip(start_time="2026-10-18T12:00:00")], gps())


@pytest.mark.parametrize("value", ["../outside.mp4", "/etc/passwd", ".drift/film.mp4", "http://remote/film.mp4", "2026/../../outside.mp4", "2026\\escape.mp4"])
def test_source_paths_cannot_escape_archive(source, value):
    root, _ = source
    with pytest.raises(ValueError):
        queue.build_manifest(root, SIGNATURE, [clip(value)], gps())


def test_escaping_source_and_spool_symlinks_rejected(source, tmp_path):
    root, original = source
    (root / "2026" / "10" / "escape.mp4").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="outside"):
        queue.build_manifest(root, SIGNATURE, [clip("2026/10/escape.mp4")], gps())
    (root / ".drift").symlink_to(root / "2026")
    with pytest.raises(ValueError, match="symlink"):
        queue.queue_render(root, SIGNATURE, [clip()], gps())


def test_cancellation_never_touches_originals(source):
    root, original = source
    queue.queue_render(root, SIGNATURE, [clip()], gps())
    state = queue.cancel_render(root, SIGNATURE)
    assert state["status"] == "cancelled"
    assert original.read_bytes() == b"source original"
    assert queue.queue_render(root, SIGNATURE, [clip()], gps())["status"] == "queued"
    assert not (queue.job_directory(root, SIGNATURE) / "cancel").exists()


def test_route_and_marker_do_not_join_gps_gaps(source):
    root, _ = source
    manifest = queue.build_manifest(root, SIGNATURE, [clip()], gps())
    output = root / "route.png"
    project, index = worker.route_map(manifest, output)
    assert output.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack("!II", output.read_bytes()[16:24]) == (320, 212)
    ass = root / "overlay.ass"
    covered = worker.clip_ass(manifest, manifest["clips"][0], index, project, ass, 1)
    assert covered == [(0, 20), (40, 60)]
    lines = ass.read_text().splitlines()
    assert sum("\\move(" in line for line in lines) == 2
    assert any("0:00:20.00,0:00:40.00" in line and "GPS unavailable" in line for line in lines)
    # No movement event crosses the missing 20 seconds of actual GPS.
    assert not any("0:00:20.00,0:00:40.00" in line and "\\move(" in line for line in lines)


def test_unlocated_clip_shows_missing_gps(source):
    root, _ = source
    manifest = queue.build_manifest(root, SIGNATURE, [clip()], [])
    project, index = worker.route_map(manifest, root / "route.png")
    ass = root / "overlay.ass"
    assert worker.clip_ass(manifest, manifest["clips"][0], index, project, ass, 1) == []
    assert "GPS unavailable" in ass.read_text()
    assert "\\move(" not in ass.read_text()


def test_recording_gaps_do_not_enter_concatenated_duration(source):
    root, _ = source
    manifest = queue.build_manifest(root, SIGNATURE, [clip(), clip(id="archive:2", start_time="2026-10-18T12:30:00Z")], gps())
    assert manifest["duration_s"] == 120
    assert manifest["clips"][1]["start_time"] == "2026-10-18T12:30:00Z"


def test_worker_refuses_changed_source_before_starting_ffmpeg(source, monkeypatch):
    root, original = source
    queue.queue_render(root, SIGNATURE, [clip()], gps())
    original.write_bytes(b"changed")
    calls = []
    monkeypatch.setattr(worker, "_run", lambda *args, **kw: calls.append(args))
    assert worker.process_next(root)["status"] == "error"
    assert not calls
    assert "changed" in queue.get_render_state(root, SIGNATURE)["error"]
    assert not (queue.job_directory(root, SIGNATURE) / "ride.mp4").exists()


def test_worker_rejects_directory_name_injection(source):
    root, _ = source
    for signature in ("../outside", "", "ab;touch-file", "a" * 65):
        with pytest.raises(ValueError):
            queue.queue_render(root, signature, [clip()], gps())


def test_pi_can_queue_from_inventory_without_source_stats(source, monkeypatch):
    root, original = source
    info = original.stat()
    # Simulates slow/unavailable per-video stats on the wireless Pi. Only its
    # already-indexed fingerprint is queued; the wired worker verifies it live.
    monkeypatch.setattr(queue, "relative_source", lambda *args: pytest.fail("Pi must not stat every source"))
    state = queue.queue_render(root, SIGNATURE, [clip(size_bytes=info.st_size, mtime_ns=info.st_mtime_ns)], gps(), verify_sources=False)
    assert state["status"] == "queued"
    with pytest.raises(ValueError, match="fingerprint"):
        queue.build_manifest(root, "cd" * 32, [clip()], gps(), verify_sources=False)


def test_only_one_offloaded_worker_claims_a_job(source, monkeypatch):
    import fcntl
    root, _ = source
    queue.queue_render(root, SIGNATURE, [clip()], gps())
    lock = queue.job_directory(root, SIGNATURE).parent / ".worker.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(worker, "render_job", lambda *args, **kwargs: pytest.fail("Parallel worker claimed the ride"))
        assert worker.process_next(root) is None
    assert queue.get_render_state(root, SIGNATURE)["status"] == "queued"


def test_worker_restart_preserves_completed_parts_for_resume(source, monkeypatch):
    root, _ = source
    queue.queue_render(root, SIGNATURE, [clip()], gps())
    monkeypatch.setattr(worker, "_STOP", True)
    state = worker.render_job(root, SIGNATURE)
    assert state["status"] == "queued"
    assert "resume" in state["message"]
    assert (queue.job_directory(root, SIGNATURE) / ".work").is_dir()


def test_real_encoder_combines_mixed_audio_and_publishes_photos_first(source, monkeypatch):
    """Run when FFmpeg/ffprobe are installed; no network or real archive used."""
    import hashlib
    import os
    import shutil
    import subprocess
    import sys
    from xml.sax.saxutils import escape
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Real encoder integration requires FFmpeg and ffprobe")
    root, first = source
    # Standalone macOS FFmpeg builds can include Fontconfig without a default
    # configuration. Use public OS fonts and a disposable cache for this test;
    # the Linux worker image supplies its normal Fontconfig/DejaVu packages.
    if sys.platform == "darwin" and not os.environ.get("FONTCONFIG_FILE"):
        cache = root / "font-cache"
        cache.mkdir()
        config = root / "fonts.conf"
        config.write_text("<?xml version='1.0'?>\n<fontconfig>"
            "<dir>/System/Library/Fonts</dir><dir>/System/Library/Fonts/Supplemental</dir>"
            f"<cachedir>{escape(str(cache))}</cachedir>"
            "<alias><family>Sans</family><prefer><family>Arial</family></prefer></alias>"
            "<alias><family>DejaVu Sans</family><prefer><family>Arial</family></prefer></alias>"
            "</fontconfig>")
        monkeypatch.setenv("FONTCONFIG_FILE", str(config))
    second = first.with_name("DRIFT002.mp4")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=48000", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
        "-threads", "1", "-c:a", "aac", str(first)], check=True, timeout=30)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
        "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", str(second)], check=True, timeout=30)
    originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)}
    clips = [clip(duration_s=2), clip("2026/10/DRIFT002.mp4", id="archive:2", duration_s=2, start_time="2026-10-18T12:00:10Z")]
    points = [
        {"time": "2026-10-18T12:00:00Z", "latitude": 51.5, "longitude": -.1, "segment": 0},
        {"time": "2026-10-18T12:00:01Z", "latitude": 51.501, "longitude": -.099, "segment": 0},
        {"time": "2026-10-18T12:00:10Z", "latitude": 51.503, "longitude": -.098, "segment": 1},
        {"time": "2026-10-18T12:00:12Z", "latitude": 51.504, "longitude": -.097, "segment": 1},
    ]
    queue.queue_render(root, SIGNATURE, clips, points)
    recorded = []
    real_atomic = worker.atomic_json

    def recording_atomic(path, value):
        recorded.append(copy.deepcopy(value))
        real_atomic(path, value)
    monkeypatch.setattr(worker, "atomic_json", recording_atomic)
    state = worker.process_next(root)
    assert state["status"] == "done", state.get("error") or state
    assert abs(state["duration_s"] - 4) < .1
    assert len(state["snapshots"]) == 6
    assert any(s.get("status") == "rendering" and len(s.get("snapshots", [])) == 6 and s["progress"] == .03 for s in recorded)
    movie = queue.job_directory(root, SIGNATURE) / state["output"]
    _, streams = worker._probe(movie)
    assert any(s.get("width") == 1280 and s.get("height") == 720 for s in streams)
    assert any(s["codec_type"] == "audio" for s in streams)
    assert next(int(s["nb_frames"]) for s in streams if s["codec_type"] == "video") == 100
    assert queue.stamp(state["snapshots"][2]["time"]) < queue.stamp("2026-10-18T12:00:02Z")
    assert queue.stamp(state["snapshots"][3]["time"]) > queue.stamp("2026-10-18T12:00:10Z")
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())
    assert not (movie.parent / ".work").exists()
