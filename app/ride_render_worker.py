"""Single offloaded encoder. Run on the server, never on the Raspberry Pi.

Only NAS requests are accepted; no HTTP server, credentials or shell commands.
Each clip is encoded sequentially with one CPU thread and atomically published.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import zlib

from .ride_render import (SIGNATURE, _read_json, atomic_json, get_render_state, iso,
    job_directory, relative_source, stamp, validate_manifest)
from .roadtrip_map import TrackIndex
from .storage import require_storage

WIDTH, HEIGHT = 1280, 720
MAP_W, MAP_H, MAP_X, MAP_Y = 320, 212, 940, 20
_STOP = False


class Cancelled(Exception):
    pass


class Interrupted(Exception):
    pass


def _chunk(kind, data):
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)


def _png(path, pixels, width, height):
    # Only a 200 KiB RGB buffer, without a new image dependency on ARM.
    scanlines = b"".join(b"\0" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(scanlines)) + _chunk(b"IEND", b""))


def route_map(manifest, output):
    """Offline route only; no external maps, geocoding or invented road names."""
    pixels = bytearray(bytes((13, 24, 36)) * MAP_W * MAP_H)
    points = manifest["time_series"]
    lat0 = sum(p["latitude"] for p in points) / max(1, len(points))
    coslat = max(.01, math.cos(math.radians(lat0)))
    xs, ys = [p["longitude"] * coslat for p in points], [p["latitude"] for p in points]
    west, east = (min(xs), max(xs)) if xs else (0, 1)
    south, north = (min(ys), max(ys)) if ys else (0, 1)
    scale = min((MAP_W - 40) / max(east - west, .0001), (MAP_H - 54) / max(north - south, .0001))
    cx, cy = (west + east) / 2, (south + north) / 2

    def project(point):
        return (round(MAP_W / 2 + (point["longitude"] * coslat - cx) * scale),
                round(MAP_H / 2 - (point["latitude"] - cy) * scale + 9))

    def dot(x, y, color, radius=1):
        for yy in range(y - radius, y + radius + 1):
            for xx in range(x - radius, x + radius + 1):
                if 0 <= xx < MAP_W and 0 <= yy < MAP_H:
                    offset = (yy * MAP_W + xx) * 3
                    pixels[offset:offset + 3] = bytes(color)

    def line(a, b, color, radius=1):
        dx, dy = b[0] - a[0], b[1] - a[1]
        steps = max(abs(dx), abs(dy), 1)
        for i in range(steps + 1):
            dot(round(a[0] + dx * i / steps), round(a[1] + dy * i / steps), color, radius)

    for x in range(0, MAP_W, 40):
        line((x, 0), (x, MAP_H - 1), (23, 40, 55), 0)
    for y in range(0, MAP_H, 40):
        line((0, y), (MAP_W - 1, y), (23, 40, 55), 0)
    index = TrackIndex(points, manifest["max_gap_s"])
    for segment, _ in index.segments:
        for a, b in zip(segment, segment[1:]):
            line(project(a), project(b), (64, 196, 223))
    _png(output, pixels, MAP_W, MAP_H)
    return project, index


def _ass_time(seconds):
    centiseconds = max(0, round(seconds * 100))
    return f"{centiseconds // 360000}:{centiseconds // 6000 % 60:02}:{centiseconds // 100 % 60:02}.{centiseconds % 100:02}"


def _ass_text(value):
    return str(value).replace("\\", " ").replace("{", "(").replace("}", ")").replace("\n", " ").replace("\r", " ")


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Label,DejaVu Sans,16,&H00FFFFFF,&H00FFFFFF,&H00101820,&H80101820,0,0,0,0,100,100,0,0,1,2,0,7,20,20,20,1
Style: Title,DejaVu Sans,23,&H00FFFFFF,&H00FFFFFF,&H00101820,&H80101820,-1,0,0,0,100,100,0,0,1,2,0,7,20,20,20,1
Style: Point,DejaVu Sans,25,&H0000DEFF,&H0000DEFF,&H00101820,&H80101820,0,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def clip_ass(manifest, clip, index, project, output, number):
    start, duration = stamp(clip["start_time"]), clip["duration_s"]
    pieces = index.interval(clip["start_time"], clip["end_time"])
    events = []

    def event(begin, end, style, text, layer=1):
        if end > begin:
            events.append(f"Dialogue: {layer},{_ass_time(begin)},{_ass_time(end)},{style},,0,0,0,,{text}\n")

    event(0, duration, "Title", "{\\pos(24,24)}" + _ass_text(manifest["title"]))
    event(0, duration, "Label", "{\\pos(24,60)}" + f"Clip {number}/{len(manifest['clips'])}  ·  " + _ass_text(Path(clip["path"]).name))
    event(0, duration, "Label", "{\\pos(954,28)}GPS route · estimate")
    event(0, duration, "Label", "{\\pos(24,654)}Recording time · " + _ass_text(manifest["timezone"]))
    covered = []
    for piece in pieces:
        for a, b in zip(piece, piece[1:]):
            begin, end = stamp(a["time"]) - start, stamp(b["time"]) - start
            x1, y1 = project(a)
            x2, y2 = project(b)
            event(begin, end, "Point", f"{{\\an5\\move({x1 + MAP_X},{y1 + MAP_Y},{x2 + MAP_X},{y2 + MAP_Y})}}●", 2)
            covered.append((begin, end))
    previous = 0.0
    for begin, end in sorted(covered):
        if begin > previous + .01:
            event(previous, begin, "Label", "{\\pos(954,207)}GPS unavailable")
        previous = max(previous, end)
    if duration > previous + .01:
        event(previous, duration, "Label", "{\\pos(954,207)}GPS unavailable")
    output.write_text(ASS_HEADER + "".join(events))
    return covered


def _probe(path, ffprobe="ffprobe"):
    result = subprocess.run([ffprobe, "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm,avi,mpegts", "-show_entries", "format=duration:stream=codec_type,width,height,nb_frames",
        "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=True)
    info = json.loads(result.stdout)
    return float(info["format"]["duration"]), info.get("streams", [])


def _run(command, directory, cancel, progress=None, duration=1, timezone="UTC", max_seconds=3600):
    """Consume FFmpeg progress while checking cancellation, without log growth."""
    if _STOP:
        raise Interrupted("Worker is restarting; the ride will resume")
    if cancel.exists():
        raise Cancelled("Cancelled")
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE, stderr=errors,
            text=True, env=os.environ | {"TZ": timezone})
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        began = time.monotonic()
        try:
            while proc.poll() is None:
                if _STOP:
                    raise Interrupted("Worker is restarting; the ride will resume")
                if cancel.exists():
                    raise Cancelled("Cancelled")
                if time.monotonic() - began > max_seconds:
                    raise RuntimeError("Video rendering exceeded its time limit")
                for key, _ in selector.select(.5):
                    line = key.fileobj.readline()
                    name, _, value = line.strip().partition("=")
                    if name == "out_time_us" and progress:
                        try:
                            progress(max(0, min(1, float(value) / (duration * 1_000_000))))
                        except ValueError:
                            pass
                if errors.tell() > 2 * 1024 * 1024:
                    raise RuntimeError("Video decoder reported too many errors")
            if proc.returncode:
                errors.seek(max(0, errors.tell() - 1600))
                raise RuntimeError("FFmpeg failed: " + errors.read().decode(errors="replace"))
        finally:
            selector.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()


def _fingerprint(root, clip):
    path = relative_source(root, clip["path"])
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (clip["size_bytes"], clip["mtime_ns"]):
        raise ValueError("A ride source changed while the film was being rendered")
    return path


def render_job(root, signature, *, ffmpeg="ffmpeg", ffprobe="ffprobe", update_interval=30):
    root = require_storage(Path(root))
    directory = job_directory(root, signature)
    manifest = validate_manifest(root, _read_json(directory / "request.json"))
    state = get_render_state(root, signature)
    if state["status"] not in {"queued", "rendering"}:
        return state
    work = directory / ".work"
    if work.is_symlink():
        raise ValueError("Ride workspace cannot be a symlink")
    work.mkdir(exist_ok=True)
    for name in ("route.png", "overlay.ass", "clock.txt", "concat.txt", "ride.mp4"):
        if (work / name).is_symlink():
            raise ValueError("Ride workspace files cannot be symlinks")
    # At most two derived-film copies; never copy the originals into scratch.
    estimate = manifest["duration_s"] * 750_000 + 64 * 1024 * 1024
    if shutil.disk_usage(work).free < estimate:
        raise RuntimeError("Not enough free NAS space for the ride film")
    last_write = 0.0
    state.pop("error", None)
    began = time.monotonic()

    def update(progress=0, message="Rendering ride film", force=False, **extra):
        nonlocal last_write
        now = time.monotonic()
        state.update(status="rendering", progress=round(min(.999, progress), 4), message=message,
            updated_at=iso(), elapsed_s=round(now - began), **extra)
        if force or now - last_write >= update_interval:
            atomic_json(directory / "state.json", state)
            last_write = now

    update(force=True, worker="offloaded", clip_count=len(manifest["clips"]))
    try:
        project, index = route_map(manifest, work / "route.png")
        # Publish actual source-frame photos before the long encode so the
        # gallery is useful immediately, even while an hours-long film renders.
        snapshots = []
        cancel = directory / "cancel"
        for number in range(1, 7):
            offset = manifest["duration_s"] * number / 7
            elapsed = 0.0
            selected = manifest["clips"][-1]
            for clip in manifest["clips"]:
                selected = clip
                if offset < elapsed + clip["duration_s"]:
                    break
                elapsed += clip["duration_s"]
            source_offset = max(0, min(selected["duration_s"] - .04, offset - elapsed))
            source = _fingerprint(root, selected)
            target = work / f"snapshot-{number}.jpg"
            if target.is_symlink():
                raise ValueError("Ride snapshot cannot be a symlink")
            _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", str(source_offset),
                "-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm,avi,mpegts", "-i", str(source),
                "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", "-threads", "1", str(target)],
                work, cancel, max_seconds=120)
            if not target.exists() or target.stat().st_size < 100:
                raise RuntimeError("A ride snapshot could not be created")
            _fingerprint(root, selected)
            os.replace(target, directory / target.name)
            snapshots.append({"filename": target.name, "video_time_s": round(offset, 3),
                "time": iso(stamp(selected["start_time"]) + source_offset)})
        update(.03, "Ride photos are ready; rendering the film", force=True, snapshots=snapshots)
        done_s, parts = 0.0, []
        for number, clip in enumerate(manifest["clips"], 1):
            if _STOP:
                raise Interrupted("Worker is restarting; the ride will resume")
            if cancel.exists():
                raise Cancelled("Cancelled")
            source = _fingerprint(root, clip)
            actual, streams = _probe(source, ffprobe)
            if not any(stream.get("codec_type") == "video" for stream in streams):
                raise ValueError("A ride source has no video stream")
            if abs(actual - clip["duration_s"]) > max(2, actual * .01):
                raise ValueError("A ride source duration differs from its matched timestamps")
            part = work / f"part-{number:04}.mp4"
            if part.is_symlink():
                raise ValueError("Ride workspace output cannot be a symlink")
            valid = False
            if part.exists():
                try:
                    length, _ = _probe(part, ffprobe)
                    valid = abs(length - clip["duration_s"]) <= max(.25, clip["duration_s"] * .001)
                except (ValueError, KeyError, subprocess.SubprocessError):
                    pass
            if not valid:
                clip_ass(manifest, clip, index, project, work / "overlay.ass", number)
                # read text from a file so no camera filename enters FFmpeg syntax.
                (work / "clock.txt").write_text("%{pts:localtime:" + str(stamp(clip["start_time"])) + ":%Y-%m-%d %H\\:%M\\:%S}")
                filters = ("[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,setpts=PTS-STARTPTS[base];"
                    "[base][1:v]overlay=940:20:eof_action=repeat,ass=overlay.ass,"
                    "drawtext=textfile=clock.txt:fontsize=23:fontcolor=white:borderw=2:bordercolor=black:x=24:y=680[v]")
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
                    "-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm,avi,mpegts",
                    "-i", str(source), "-loop", "1", "-i", "route.png"]
                has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
                if not has_audio:
                    command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                command += ["-filter_complex_threads", "1", "-filter_complex", filters, "-map", "[v]",
                    "-map", "0:a:0" if has_audio else "2:a:0", "-t", str(clip["duration_s"]),
                    "-c:v", "libx264", "-preset", "superfast", "-crf", "24", "-maxrate", "2500k", "-bufsize", "5000k",
                    "-threads", "1", "-pix_fmt", "yuv420p", "-af", "asetpts=PTS-STARTPTS,apad", "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
                    "-metadata", "creation_time=" + clip["start_time"], "-movflags", "+faststart", "-progress", "pipe:1", str(part)]
                _run(command, work, cancel, lambda value: update(.03 + .91 * (done_s + value * clip["duration_s"]) / manifest["duration_s"],
                    f"Rendering clip {number}/{len(manifest['clips'])}"), clip["duration_s"], manifest["timezone"],
                    max_seconds=max(1800, clip["duration_s"] * 30))
                length, _ = _probe(part, ffprobe)
                if abs(length - clip["duration_s"]) > max(.25, clip["duration_s"] * .001):
                    raise RuntimeError("Rendered clip duration could not be verified")
            _fingerprint(root, clip)
            parts.append(part)
            done_s += clip["duration_s"]
            update(.03 + .91 * done_s / manifest["duration_s"], f"Finished clip {number}/{len(manifest['clips'])}")
        # Encoded parts all have the same format; stream-copy final assembly.
        (work / "concat.txt").write_text("".join(f"file '{part.name}'\nduration {clip['duration_s']:.6f}\n"
            for part, clip in zip(parts, manifest["clips"])))
        movie = work / "ride.mp4"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1",
            "-i", "concat.txt", "-map", "0", "-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1", str(movie)]
        _run(command, work, cancel, lambda value: update(.94 + value * .03, "Assembling the ride film"),
            manifest["duration_s"], max_seconds=max(1800, manifest["duration_s"]))
        length, _ = _probe(movie, ffprobe)
        if abs(length - manifest["duration_s"]) > max(1, len(parts) * .08):
            raise RuntimeError("Combined ride film duration could not be verified")
        validate_manifest(root, manifest)
        if _STOP:
            raise Interrupted("Worker is restarting; the ride will resume")
        if cancel.exists():
            raise Cancelled("Cancelled")
        os.replace(movie, directory / "ride.mp4")
        state.update(status="done", progress=1, output="ride.mp4", snapshots=snapshots,
            size_bytes=(directory / "ride.mp4").stat().st_size, duration_s=length, updated_at=iso(),
            elapsed_s=round(time.monotonic() - began), message="Ride film and snapshots are ready")
        state.pop("error", None)
        state.pop("resume", None)
        atomic_json(directory / "state.json", state)
        shutil.rmtree(work)
    except Exception as error:
        status = "queued" if isinstance(error, Interrupted) else "cancelled" if isinstance(error, Cancelled) else "error"
        if isinstance(error, Interrupted):
            state["resume"] = True
        state.update(status=status, error=str(error)[:2000],
            message="Worker restarting; ride will resume" if isinstance(error, Interrupted) else "Cancelled" if isinstance(error, Cancelled) else "Ride rendering failed", updated_at=iso())
        atomic_json(directory / "state.json", state)
        if isinstance(error, Cancelled):
            shutil.rmtree(work)
    return state


def process_next(root, *, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    root = require_storage(Path(root))
    spool = root / ".drift" / "ride-renders"
    if not spool.exists():
        return None
    if spool.is_symlink():
        raise ValueError("Ride render spool cannot be a symlink")
    lock = spool / ".worker.lock"
    if lock.is_symlink():
        raise ValueError("Ride render lock cannot be a symlink")
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        candidates = []
        for directory in sorted(spool.iterdir(), key=lambda value: value.name):
            if not SIGNATURE.fullmatch(directory.name) or directory.is_symlink():
                continue
            try:
                state = get_render_state(root, directory.name)
                if state["status"] in {"queued", "rendering"}:
                    candidates.append((0 if state["status"] == "rendering" or state.get("resume") else 1,
                        state.get("queued_at", state.get("updated_at", "")), directory.name))
            except Exception as error:
                atomic_json(directory / "state.json", {"signature": directory.name, "status": "error",
                    "progress": 0, "error": str(error)[:2000], "message": "Ride rendering failed", "updated_at": iso()})
                return {"signature": directory.name, "status": "error"}
        if candidates:
            _, _, signature = min(candidates)
            try:
                return render_job(root, signature, ffmpeg=ffmpeg, ffprobe=ffprobe)
            except Exception as error:
                directory = job_directory(root, signature)
                state = get_render_state(root, signature)
                state.update(status="error", error=str(error)[:2000], message="Ride rendering failed", updated_at=iso())
                atomic_json(directory / "state.json", state)
                return state
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nas-root", type=Path, default=Path("/mnt/NAS"))
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    def stop(*_):
        global _STOP
        _STOP = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not _STOP:
        try:
            result = process_next(args.nas_root)
            if result:
                print(json.dumps({key: result.get(key) for key in ("signature", "status", "error")}), flush=True)
        except Exception as error:
            print(json.dumps({"status": "unavailable", "error": str(error)[:500]}), flush=True)
        if args.once:
            break
        for _ in range(max(1, round(max(5, min(args.poll_seconds, 300)) * 5))):
            if _STOP:
                break
            time.sleep(.2)


if __name__ == "__main__":
    main()
