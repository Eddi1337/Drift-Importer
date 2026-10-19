"""Fuzzy matching stays advisory and never fabricates GPS across missing data."""
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import roadtrips as rt
from app.archive_models import ArchiveFile
from app.database import Base
from app.models import Destination, MediaItem, UploadedClip
from app.roadtrip_models import LocationDay, RoadtripOuting, RoadtripSettings


@pytest.fixture
def prefs():
    return SimpleNamespace(timezone="Europe/London", home_lat=51.5, home_lon=-.1,
        home_radius_m=150, max_accuracy_m=100, max_gap_s=900, max_speed_kmh=180)


def point(minute, lon=-.1, lat=51.5, accuracy=10, date="2026-06-04"):
    return {"t": f"{date}T{10 + minute // 60:02d}:{minute % 60:02d}:00Z",
        "lat": lat, "lon": lon, "accuracy": accuracy}


@pytest.fixture
def session(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    root = tmp_path / "NAS"
    root.mkdir()
    monkeypatch.setattr(rt, "get_settings", lambda: SimpleNamespace(nas_root=root))
    monkeypatch.setattr(rt, "require_storage", lambda path: Path(path).resolve())
    with Session(engine) as session:
        session.add(RoadtripSettings(id=1, timezone="Europe/London", home_lat=51.5, home_lon=-.1,
            home_radius_m=150, max_accuracy_m=100, max_gap_s=900, max_speed_kmh=180))
        session.flush()
        yield session


def test_ha_uses_attribute_update_time_and_filters_bad_accuracy(prefs):
    raw = {"last_changed": "2026-06-04T00:00:00Z", "last_updated": "2026-06-04T10:00:00Z",
        "attributes": {"latitude": 51.5, "longitude": -.1, "gps_accuracy": 20}}
    assert rt.normalize_point(raw)["t"] == "2026-06-04T10:00:00Z"
    bad = point(1, -.2, accuracy=500)
    clean, rejected = rt.clean_points([rt.normalize_point(raw), bad], prefs)
    assert len(clean) == 1 and rejected == 1


def test_gps_jump_rejected_and_stationary_home_is_not_roadtrip(prefs):
    clean, rejected = rt.clean_points([point(0), point(1, lon=20), point(2), point(10)], prefs)
    assert rejected == 1
    assert rt.extract_outings(clean, prefs) == []


def test_away_outing_includes_departure_return_but_does_not_join_long_gaps(prefs):
    points = [point(0), point(5, -.11), point(10, -.12), point(45, -.13), point(50, -.11), point(55)]
    outing = rt.extract_outings(points, prefs)[0]
    assert outing["duration_s"] == 55 * 60
    assert outing["complete"] is True
    assert outing["gap_count"] == 1
    expected = sum(rt.distance(points[a], points[b]) for a, b in [(0, 1), (1, 2), (3, 4), (4, 5)])
    assert outing["distance_m"] == pytest.approx(expected, abs=.1)


def test_partial_outing_does_not_claim_departure_or_return(prefs):
    outing = rt.extract_outings([point(0, -.11), point(10, -.12)], prefs)[0]
    assert outing["complete"] is False


def test_timezone_month_boundary_and_google_timeline_formats(tmp_path, prefs):
    # 23:30 UTC on May 31 is June 1 locally (BST).
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps({"semanticSegments": [{"timelinePath": [
        {"point": "geo:51.5,-0.1", "time": "2026-05-31T23:30:00Z"},
        {"point": "geo:51.5,-0.11", "time": "2026-06-01T00:00:00Z"}]}]}))
    points, rejected = rt.parse_import(path, "2026-06", prefs)
    assert len(points) == 2 and rejected == 0
    legacy = rt.normalize_point({"latitudeE7": 515000000, "longitudeE7": -1000000, "timestampMs": "1780567200000"})
    assert legacy["lat"] == 51.5 and legacy["lon"] == -.1


def test_gpx_parse_month_is_bounded(tmp_path, prefs):
    path = tmp_path / "track.gpx"
    path.write_text('<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
        '<trkpt lat="51.5" lon="-0.1"><time>2026-06-04T10:00:00Z</time></trkpt>'
        '<trkpt lat="51.5" lon="-0.2"><time>2026-07-04T10:00:00Z</time></trkpt>'
        '</trkseg></trk></gpx>')
    points, _ = rt.parse_import(path, "2026-06", prefs)
    assert len(points) == 1


def group(month="2026-06", recorded=3600, span=None):
    return {"key": "camera1", "month": month, "recorded_duration_s": recorded,
        "observed_span_s": span, "clips": [{"duration_s": recorded}]}


def outing(id=1, month="2026-06", duration=3600):
    return {"id": id, "month": month, "duration_s": duration,
        "start_time": f"{month}-04T10:00:00Z", "complete": True, "gap_count": 0}


def test_ambiguous_same_duration_outings_are_all_low_confidence():
    result = rt.rank_candidates(group(), [outing(), outing(2)])
    assert len(result) == 2 and all(item["confidence"] == "low" for item in result)
    assert all(item["provisional"] for item in result)
    assert "Multiple outings" in result[0]["reasons"][-1]


def test_recording_sum_is_lower_bound_not_whole_trip_duration():
    result = rt.rank_candidates(group(recorded=900), [outing(duration=3 * 3600)])
    assert len(result) == 1
    assert "lower bound" in " ".join(result[0]["reasons"])
    assert result[0]["confidence"] == "low"
    assert rt.rank_candidates(group(recorded=5 * 3600), [outing(duration=1800)]) == []
    assert rt.rank_candidates(group(), [outing(month="2026-07")]) == []


def test_per_clip_speed_uses_only_overlapping_gps_and_rejects_gap(prefs):
    points = [point(0), point(10, -.11), point(40, -.12), point(50, -.13)]
    segments = rt._track(points, prefs)
    first = rt.clip_speed(segments, rt.timestamp(points[0]["t"]), 600)
    assert first["coverage"] == 1
    assert first["distance_m"] == pytest.approx(rt.distance(points[0], points[1]), abs=.1)
    assert first["average_speed_kmh"] == pytest.approx(first["distance_m"] / 600 * 3.6, abs=.1)
    gap = rt.clip_speed(segments, rt.timestamp(point(15)["t"]), 600)
    assert gap["coverage"] == 0 and gap["average_speed_kmh"] is None
    partial = rt.clip_speed(segments, rt.timestamp(point(5)["t"]), 600)
    assert partial["coverage"] == .5 and partial["average_speed_kmh"] is None


def test_clean_points_downsamples_to_minute_and_preserves_home_boundary(prefs):
    points = [{"t": f"2026-06-04T10:00:{second:02d}Z", "lat": 51.5, "lon": -.1, "accuracy": 10} for second in range(60)]
    clean, _ = rt.clean_points(points, prefs)
    assert len(clean) == 2


def test_cache_private_nas_only_and_consistent_day_upsert(session):
    points = [point(0), point(10, -.12), point(20)]
    rt.save_day(session, "ha:person.ed", "2026-06-04", points)
    row = session.query(LocationDay).one()
    assert Path(row.cache_path).stat().st_mode & 0o777 == 0o600
    assert (Path(row.cache_path).parents[2]).stat().st_mode & 0o777 == 0o700
    assert rt.load_month(session, "2026-06", "ha:person.ed") == points
    rt.save_day(session, "ha:person.ed", "2026-06-04", points[:2])
    assert session.query(LocationDay).count() == 1
    assert len(rt.load_month(session, "2026-06", "ha:person.ed")) == 2


def test_offline_nas_refuses_import_before_any_location_directory(session, monkeypatch):
    root = rt.get_settings().nas_root
    def offline(path):
        raise RuntimeError("not a mounted NAS")
    monkeypatch.setattr(rt, "require_storage", offline)
    with pytest.raises(RuntimeError, match="mounted NAS"):
        rt.save_day(session, "ha:test", "2026-06-04", [point(0)])
    assert not (root / ".drift").exists()


def seed_clip(session, filename, captured, path="2026/06", duration=300):
    dest = session.get(Destination, 1)
    if dest is None:
        session.add(Destination(id=1, name="NAS", type="local", base_path="/mnt/NAS"))
    item = MediaItem(path=f"/media/camera/DCIM/107MEDIA/{filename}", filename=filename,
        capture_time=captured, duration_s=duration, checksum=filename, kind="video")
    session.add(item)
    session.flush()
    ledger = UploadedClip(destination_id=1, source_media_id=item.id, checksum=filename,
        filename=filename, status="done", remote_path=f"{path}/{filename}")
    session.add(ledger)
    session.flush()
    session.add(ArchiveFile(destination_id=1, root_path="/mnt/NAS", path=ledger.remote_path,
        kind="video", present=True, ledger_id=ledger.id))
    session.flush()
    return item


def test_wrong_day_and_year_still_matches_reliable_archive_folder(session):
    seed_clip(session, "DVR00001.MP4", dt.datetime(2020, 1, 1, 10))
    seed_clip(session, "DVR00002.MP4", dt.datetime(2020, 1, 1, 10, 5))
    groups = rt.clip_groups(session, "2026-06")
    assert len(groups) == 1 and groups[0]["clip_count"] == 2
    assert groups[0]["month_basis"] == "archive_folder"
    assert groups[0]["observed_span_s"] == 600
    assert rt.rank_candidates(groups[0], [outing(duration=600)])
    assert rt.clip_groups(session, "2020-01") == []


def test_camera_counter_rollover_sorts_by_source_folder(session):
    a = seed_clip(session, "DVR00999.MP4", dt.datetime(2020, 1, 1, 10))
    b = seed_clip(session, "DVR00001.MP4", dt.datetime(2020, 1, 1, 10, 5))
    b.path = b.path.replace("107MEDIA", "108MEDIA")
    session.flush()
    clips = rt.clip_groups(session, "2026-06")[0]["clips"]
    assert [c["media_id"] for c in clips] == [a.id, b.id]


def test_month_range_limited_and_dst_day_length():
    assert rt.months_between("2025-07", "2026-06")[-1] == "2026-06"
    with pytest.raises(ValueError, match="12 months"):
        rt.months_between("2025-06", "2026-06")
    with pytest.raises(ValueError):
        rt.months_between("2026-06", "2025-06")
    start, end = rt.month_bounds("2026-03", "Europe/London")
    assert (end - start).total_seconds() == 31 * 86400 - 3600


def test_preview_manual_anchor_timezone_gaps_and_confirmation_do_not_edit_dates(session):
    item = seed_clip(session, "DVR00001.MP4", dt.datetime(2020, 1, 1, 10), duration=600)
    points = [point(0), point(10, -.11), point(40, -.12), point(50)]
    rt.save_day(session, "ha:person.ed", "2026-06-04", points)
    rt.rebuild_outings(session, "2026-06", "ha:person.ed")
    outing_row = session.query(RoadtripOuting).one()
    clip_group = rt.clip_groups(session, "2026-06")[0]
    # 11am BST is 10am UTC. The camera's year/day is immaterial to the anchor.
    detail = rt.preview(session, "2026-06", clip_group["key"], outing_row.id, "2026-06-04T11:00:00")
    assert detail["start_time"] == "2026-06-04T10:00:00Z"
    assert detail["provisional"] is True
    assert len(detail["geojson"]["features"]) == 2
    assert detail["clips"][0]["average_speed_kmh"] is not None
    assert detail["clips"][0]["distance_m"] < outing_row.distance_m
    request = SimpleNamespace(month="2026-06", group_key=clip_group["key"], outing_id=outing_row.id,
        start_time=detail["start_time"], clip_offsets={item and f"media:{item.id}": 0},
        status="confirmed", name="A reviewed ride")
    result = rt.save_match(session, request)
    assert result["status"] == "confirmed"
    assert result["start_time"] == detail["start_time"]
    assert item.capture_time == dt.datetime(2020, 1, 1, 10)
    assert rt.state(session)["matches"][0]["name"] == "A reviewed ride"
    with pytest.raises(ValueError, match="no longer belongs"):
        rt.preview(session, "2026-06", clip_group["key"], outing_row.id, clip_offsets={"wrong": 10})


def test_empty_explicit_album_selection_cannot_fall_back_to_all_files(session):
    seed_clip(session, "DVR00001.MP4", dt.datetime(2020, 1, 1, 10))
    assert rt.clip_groups(session, "2026-06", album_id=999) == []
    assert rt.clip_groups(session, "2026-06", media_ids=[]) == []


def test_partial_export_merges_existing_days_and_empty_import_preserves_cache(session):
    source = "file:timeline"
    rt.save_day(session, source, "2026-06-04", [point(0), point(10, -.12), point(20)])
    rt._import_points(session, source, "2026-06", [point(40, -.11), point(50)])
    assert len(rt.load_month(session, "2026-06", source)) == 5
    with pytest.raises(ValueError, match="preserved"):
        rt._import_points(session, source, "2026-06", [])
    assert len(rt.load_month(session, "2026-06", source)) == 5


def test_metadata_cache_applies_only_to_exact_archive_identity(session):
    from app.roadtrip_models import RoadtripClipMetadata
    session.add(Destination(id=1, name="NAS", type="local", base_path="/mnt/NAS"))
    row = ArchiveFile(destination_id=1, root_path="/mnt/NAS", path="2026/06/DVR00001.MP4",
        kind="video", present=True, size_bytes=100, mtime_ns=10, ctime_ns=10, device="7", inode="8")
    session.add(row)
    session.flush()
    cached = RoadtripClipMetadata(archive_id=row.id, identity=rt._metadata_identity(row), duration_s=301,
        observed_time=dt.datetime(2020, 1, 1, 12))
    session.add(cached)
    session.flush()
    clips = rt.clip_groups(session, "2026-06")[0]["clips"]
    assert clips[0]["duration_s"] == 301
    assert clips[0]["observed_time"].startswith("2020-01-01")
    row.ctime_ns += 1
    session.flush()
    assert rt.clip_groups(session, "2026-06")[0]["clips"][0]["duration_s"] is None


def test_ha_historic_home_marker_survives_moved_home(prefs):
    # Current home is far away, but HA knew the old home when this was recorded.
    raw = [dict(point(0, -.5), home=True), dict(point(5, -.52), home=False),
        dict(point(10, -.53), home=False), dict(point(15, -.5), home=True)]
    outing = rt.extract_outings(raw, prefs)[0]
    assert outing["complete"] is True and outing["duration_s"] == 900
    normalized = rt.normalize_point({"state": "home", "last_changed": "2026-06-04T10:00:00Z",
        "attributes": {"latitude": 51.5, "longitude": -.5}})
    assert normalized["home"] is True
    assert rt.normalize_point({"state": "unavailable", "last_changed": "2026-06-04T10:00:00Z",
        "attributes": {"latitude": 51.5, "longitude": -.5}}) is None


def test_rejecting_alternative_does_not_overwrite_confirmed_roadtrip(session):
    from app.roadtrip_models import RoadtripMatch, RoadtripRejection
    seed_clip(session, "DVR00001.MP4", dt.datetime(2020, 1, 1, 10), duration=600)
    points = [point(0), point(10, -.11), point(20), point(40), point(50, -.11), point(60)]
    rt.save_day(session, "ha:person.ed", "2026-06-04", points)
    rt.rebuild_outings(session, "2026-06", "ha:person.ed")
    first, second = session.query(RoadtripOuting).order_by(RoadtripOuting.id).all()
    clip_group = rt.clip_groups(session, "2026-06")[0]
    request = SimpleNamespace(month="2026-06", group_key=clip_group["key"], outing_id=first.id,
        start_time=None, clip_offsets={}, status="confirmed", name="Good match")
    saved = rt.save_match(session, request)
    request.outing_id, request.status = second.id, "rejected"
    rt.save_match(session, request)
    row = session.get(RoadtripMatch, saved["id"])
    assert row.status == "confirmed" and row.outing_id == first.id
    assert session.query(RoadtripRejection).one().outing_id == second.id
    suggested = rt.candidates(session, "2026-06")["suggestions"]
    assert all(item["outing_id"] != second.id for item in suggested)


@pytest.mark.parametrize("role", ["standalone", "processor"])
def test_month_metadata_probe_is_serial_cached_and_rejects_changed_file(session, monkeypatch, role):
    from contextlib import contextmanager
    from app import archive
    from app.models import Job
    from app.roadtrip_models import RoadtripClipMetadata
    root = rt.get_settings().nas_root
    monkeypatch.setattr(rt, "get_settings", lambda: SimpleNamespace(nas_root=root, processing_role=role))
    path = root / "2026/06/DVR00001.MP4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"video header")
    session.add(Destination(id=1, name="NAS", type="local", base_path=str(root)))
    info = archive.stat_identity(path.stat())
    if role == "processor":
        info["device"] = "foreign-uploader-mount"
    row = ArchiveFile(destination_id=1, root_path=str(root), path="2026/06/DVR00001.MP4",
        kind="video", present=True, **info)
    session.add(row)
    job = Job(kind="roadtrip_probe", status="running")
    session.add(job)
    session.flush()
    @contextmanager
    def scope():
        yield session
        session.flush()
    monkeypatch.setattr(rt, "session_scope", scope)
    monkeypatch.setattr(archive, "require_storage", lambda value: Path(value).resolve())
    calls, locks = [], []
    def probe(path, ctx):
        calls.append(path)
        return 301, dt.datetime(2020, 1, 1, 10)
    monkeypatch.setattr(rt, "_probe_file", probe)
    @contextmanager
    def lock(name):
        locks.append(name)
        yield
    class Context:
        def is_cancelled(self): return False
        def set_progress(self, *args): pass
        @property
        def ffmpeg_semaphore(self): return lock("ffmpeg")
        @property
        def upload_semaphore(self): return lock("upload")
    rt.handle_probe(job.id, {"month": "2026-06"}, Context())
    assert locks == ["ffmpeg", "upload"]
    assert len(calls) == 1
    assert session.get(RoadtripClipMetadata, row.id).duration_s == 301
    assert session.get(RoadtripClipMetadata, row.id).identity == rt._metadata_identity(row)
    rt.handle_probe(job.id, {"month": "2026-06"}, Context())
    assert len(calls) == 1
    assert json.loads(job.result)["cached"] == 1
    # Re-inventory invalidates cached metadata; a changing file is never probed
    # under a stale identity or allowed to supply a false clip duration.
    cached = session.get(RoadtripClipMetadata, row.id)
    cached.identity = "invalid"
    path.write_bytes(b"changed video header")
    rt.handle_probe(job.id, {"month": "2026-06"}, Context())
    assert len(calls) == 1
    assert session.get(RoadtripClipMetadata, row.id).duration_s is None
    assert json.loads(job.result)["errors"] == 1


@pytest.mark.parametrize("changed_field", ["device", "inode", "size_bytes", "mtime_ns", "ctime_ns"])
def test_processor_probe_still_rejects_any_local_identity_change_during_read(session, monkeypatch, changed_field):
    from contextlib import contextmanager
    from app import archive
    from app.models import Job
    from app.roadtrip_models import RoadtripClipMetadata
    root = rt.get_settings().nas_root
    monkeypatch.setattr(rt, "get_settings", lambda: SimpleNamespace(nas_root=root, processing_role="processor"))
    path = root / "2026/06/DVR00001.MP4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"video header")
    session.add(Destination(id=1, name="NAS", type="local", base_path=str(root)))
    original = archive.stat_identity(path.stat())
    imported = original | {"device": "foreign-uploader-mount"}
    row = ArchiveFile(destination_id=1, root_path=str(root), path="2026/06/DVR00001.MP4", **imported)
    job = Job(kind="roadtrip_probe", status="running")
    session.add_all([row, job])
    session.flush()
    @contextmanager
    def scope():
        yield session
        session.flush()
    monkeypatch.setattr(rt, "session_scope", scope)
    monkeypatch.setattr(archive, "require_storage", lambda value: Path(value).resolve())
    calls = []
    def identity(value):
        result = original.copy()
        if calls:
            result[changed_field] = str(int(result[changed_field]) + 1) if changed_field in {"device", "inode"} else result[changed_field] + 1
        calls.append(value)
        return result
    monkeypatch.setattr(archive, "stat_identity", identity)
    monkeypatch.setattr(rt, "_probe_file", lambda *_: (301, dt.datetime(2026, 6, 4, 10)))
    @contextmanager
    def lock():
        yield
    class Context:
        def is_cancelled(self): return False
        def set_progress(self, *args): pass
        @property
        def ffmpeg_semaphore(self): return lock()
        @property
        def upload_semaphore(self): return lock()
    rt.handle_probe(job.id, {"month": "2026-06"}, Context())
    cached = session.get(RoadtripClipMetadata, row.id)
    assert cached.duration_s is None
    assert cached.observed_time is None
    assert "File changed while probing" in cached.error
    assert json.loads(job.result)["errors"] == 1


def test_expired_ha_history_preserves_cached_day_and_existing_outing(session, monkeypatch):
    from contextlib import contextmanager
    source = "ha:device_tracker.test"
    rt.save_day(session, source, "2026-06-04", [point(0), point(10, -.12), point(20)])
    rt.rebuild_outings(session, "2026-06", source)
    old = session.query(LocationDay).one()
    old_bytes = Path(old.cache_path).read_bytes()
    old_signature = session.query(RoadtripOuting).one().signature
    @contextmanager
    def scope():
        yield session
        session.flush()
    monkeypatch.setattr(rt, "session_scope", scope)
    monkeypatch.setattr(rt, "discover_entities", lambda s: None)
    monkeypatch.setattr(rt, "get_app_settings", lambda s: SimpleNamespace(ha_base_url="http://ha.test", ha_token="unused"))
    monkeypatch.setattr(rt, "_base_url", lambda s: "http://ha.test")
    monkeypatch.setattr(rt, "_headers", lambda s: {})
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_bytes(self, size): yield b'[[]]'
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            calls.append(kwargs["params"])
            return Response()
    monkeypatch.setattr(rt.httpx, "Client", Client)
    class Context:
        def is_cancelled(self): return False
        def set_progress(self, *args): pass
    report = rt._handle_import_ha_month(1, {"month": "2026-06", "entity_id": "device_tracker.test"}, Context())
    assert report["preserved_days"] == 1 and report["empty_days"] == 29
    assert Path(old.cache_path).read_bytes() == old_bytes
    assert session.query(RoadtripOuting).filter_by(active=True).one().signature == old_signature
    assert len(calls) == 30
    assert all("no_attributes" not in p and "minimal_response" not in p and p.get("significant_changes_only") == "0" for p in calls)


def test_manual_subset_supports_archive_only_clips_without_media_record(session):
    session.add(Destination(id=1, name="NAS", type="local", base_path="/mnt/NAS"))
    for i in range(3):
        session.add(ArchiveFile(destination_id=1, root_path="/mnt/NAS", path=f"2026/06/DVR0000{i}.MP4", kind="video", present=True))
    session.flush()
    all_clips = rt.clip_groups(session, "2026-06")[0]["clips"]
    selected = [all_clips[1]["id"], all_clips[2]["id"]]
    result = rt.clip_groups(session, "2026-06", clip_ids=selected)
    assert len(result) == 1 and [c["id"] for c in result[0]["clips"]] == selected
    assert result[0]["key"] != rt.clip_groups(session, "2026-06")[0]["key"]


def test_sparse_away_fixes_can_suggest_outing_without_inventing_route_or_speed(prefs):
    points = [dict(point(0, -.12), home=False), dict(point(40, -.2), home=False), dict(point(80, -.3), home=False)]
    outing = rt.extract_outings(points, prefs)[0]
    assert outing["duration_s"] == 4800 and outing["gap_count"] == 2
    assert outing["distance_m"] == 0
    assert outing["complete"] is False
    assert rt._track(points, prefs) == []
    assert rt.clip_speed([], rt.timestamp(points[0]["t"]), 600)["average_speed_kmh"] is None


def test_preview_reads_only_nonempty_outing_days_across_local_midnight(session, monkeypatch):
    item = seed_clip(session, "DVR00001.MP4", dt.datetime(2020, 1, 1, 10), duration=600)
    # BST: 22:55 UTC is 23:55 local; 23:05 UTC is 00:05 next day.
    a = {"t": "2026-06-04T22:55:00Z", "lat": 51.5, "lon": -.1, "accuracy": 10}
    b = {"t": "2026-06-04T23:05:00Z", "lat": 51.5, "lon": -.11, "accuracy": 10}
    source = "ha:device_tracker.test"
    rt.save_day(session, source, "2026-06-04", [a])
    rt.save_day(session, source, "2026-06-05", [b])
    rt.save_day(session, source, "2026-06-10", [point(0), point(10)])
    # A missing empty cache file must never be resolved or opened.
    session.add(LocationDay(source=source, day="2026-06-06", cache_path="/missing/not-opened.json.gz", point_count=0))
    outing = RoadtripOuting(signature="midnight", source=source, month="2026-06",
        start_time=rt.timestamp(a["t"]).replace(tzinfo=None), end_time=rt.timestamp(b["t"]).replace(tzinfo=None),
        duration_s=600, distance_m=rt.distance(a, b), point_count=2, gap_count=0, complete=False)
    session.add(outing)
    session.flush()
    opened = []
    original_open = rt.gzip.open
    def recording_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(rt.gzip, "open", recording_open)
    detail = rt.preview(session, "2026-06", rt.clip_groups(session, "2026-06")[0]["key"], outing.id)
    assert opened == ["2026-06-04.json.gz", "2026-06-05.json.gz"]
    assert len(detail["time_series"]) == 2
    assert detail["clips"][0]["coverage"] == 1


def test_bounded_location_read_crosses_month_boundary_and_dst_fallback(session):
    source = "ha:device_tracker.test"
    # Calendar month rollover must not be clipped by an SQL month-prefix filter.
    a = {"t": "2026-05-31T22:55:00Z", "lat": 51.5, "lon": -.1, "accuracy": 10}
    b = {"t": "2026-05-31T23:05:00Z", "lat": 51.5, "lon": -.11, "accuracy": 10}
    rt.save_day(session, source, "2026-05-31", [a])
    rt.save_day(session, source, "2026-06-01", [b])
    assert rt.load_month(session, "2026-05", source, start_time=a["t"], end_time=b["t"]) == [a, b]
    # Repeated 01:xx local times on the autumn DST transition are distinct UTC
    # instants, both in the same local cache day and both retained.
    c = {"t": "2026-10-25T00:50:00Z", "lat": 51.5, "lon": -.1, "accuracy": 10}
    d = {"t": "2026-10-25T01:10:00Z", "lat": 51.5, "lon": -.11, "accuracy": 10}
    rt.save_day(session, source, "2026-10-25", [c, d])
    assert rt.load_month(session, "2026-10", source, start_time=c["t"], end_time=d["t"]) == [c, d]


def test_month_loader_skips_empty_cache_rows_without_io(session, monkeypatch):
    session.add(LocationDay(source="test", day="2026-06-01", cache_path="/must-not-open.json.gz", point_count=0))
    session.flush()
    def unexpected(*args, **kwargs):
        pytest.fail("Empty location days must not issue any cache file reads")
    monkeypatch.setattr(rt.gzip, "open", unexpected)
    assert rt.load_month(session, "2026-06", "test") == []


def test_repeated_previews_reuse_ram_points_and_reimport_invalidates_them(session, monkeypatch):
    source = "ha:device_tracker.test"
    points = [point(0), point(10)]
    rt.save_day(session, source, "2026-06-04", points)
    opened = []
    original = rt.gzip.open
    def record(path, *args, **kwargs):
        opened.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(rt.gzip, "open", record)
    first = rt.load_month(session, "2026-06", source)
    first[0]["lat"] = 0  # an edited preview cannot poison the next one
    assert rt.load_month(session, "2026-06", source) == points
    assert len(opened) == 1
    changed = [dict(p, lat=p["lat"] + .001) for p in points]
    rt.save_day(session, source, "2026-06-04", changed)
    assert rt.load_month(session, "2026-06", source) == changed
    assert len(opened) == 2


def test_ram_location_cache_still_requires_live_nas(session, monkeypatch):
    rt.save_day(session, "test", "2026-06-04", [point(0)])
    assert rt.load_month(session, "2026-06", "test")
    def unavailable(*args):
        raise RuntimeError("NAS unavailable")
    monkeypatch.setattr(rt, "require_storage", unavailable)
    with pytest.raises(RuntimeError, match="NAS unavailable"):
        rt.load_month(session, "2026-06", "test")


def test_ram_location_cache_evicts_old_days_within_its_budget(session, monkeypatch):
    from collections import OrderedDict
    monkeypatch.setattr(rt, "_DAY_CACHE", OrderedDict())
    monkeypatch.setattr(rt, "_DAY_CACHE_BYTES", 0)
    points = [point(0)]
    budget = len(json.dumps(points, separators=(",", ":")).encode()) + 1
    monkeypatch.setattr(rt, "_DAY_CACHE_BUDGET", budget)
    rt.save_day(session, "test", "2026-06-04", points)
    rt.save_day(session, "test", "2026-06-05", [point(0, date="2026-06-05")])
    opened = []
    original = rt.gzip.open
    def record(path, *args, **kwargs):
        opened.append(Path(path).name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(rt.gzip, "open", record)
    assert len(rt.load_month(session, "2026-06", "test")) == 2
    assert 0 < rt._DAY_CACHE_BYTES <= budget
    rt.load_month(session, "2026-06", "test", start_time=point(0)["t"], end_time=point(10)["t"])
    assert opened == ["2026-06-04.json.gz", "2026-06-05.json.gz", "2026-06-04.json.gz"]
