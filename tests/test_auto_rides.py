import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import auto_rides as ar
from app import roadtrips as rt
from app.archive_models import ArchiveFile
from app.auto_ride_models import AutoRide, AutoRideClip, AutoRideDiscovery
from app.database import Base
from app.models import CorrectedArchive, Destination, MediaItem, RecordingDate, UploadedClip
from app.roadtrip_models import LocationDay, RoadtripClipMetadata, RoadtripOuting, RoadtripSettings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    nas = tmp_path / "NAS"
    nas.mkdir()
    monkeypatch.setattr(ar, "get_settings", lambda: SimpleNamespace(nas_root=nas))
    points = [{"t": f"2026-10-02T10:{m:02}:00Z", "lat": 51.5, "lon": -.1 - m / 10000, "accuracy": 10} for m in range(60)]
    calls = []
    def load(session, month, source, **kwargs):
        calls.append((month, source, kwargs))
        return points
    monkeypatch.setattr(rt, "load_month", load)
    with Session(engine) as session:
        session.add(Destination(id=1, name="NAS", type="local", base_path=str(nas)))
        session.add(RoadtripSettings(id=1, entity_id="device_tracker.phone", timezone="Europe/London",
            home_radius_m=150, max_gap_s=900, max_accuracy_m=100, max_speed_kmh=180))
        session.add(RoadtripOuting(id=1, signature="outing1", source="ha:device_tracker.phone", month="2026-10",
            start_time=rt.timestamp("2026-10-02T09:50:00Z"), end_time=rt.timestamp("2026-10-02T11:30:00Z"),
            duration_s=6000, distance_m=10000, point_count=60, gap_count=0, complete=True))
        session.add(LocationDay(source="ha:device_tracker.phone", day="2026-10-02", cache_path=str(nas / "private.json.gz"), point_count=60))
        session.flush()
        yield session, nas, points, calls


def footage(setup, number, start=None, *, status="confirmed", time_source="metadata", reasons=None,
        month="2026-10", duration=600, holding=False, correction=False):
    session, nas, _, _ = setup
    filename = f"DVR{number:05}.MP4"
    start = rt.timestamp(start or f"2026-10-02T10:{(number - 1) * 10:02}:00Z")
    path = f"/camera/DCIM/107MEDIA/{filename}"
    original = rt.timestamp("2020-01-01T00:00:00Z") if correction else start
    item = MediaItem(path=path, filename=filename, kind="video", size_bytes=100,
        capture_time=start, duration_s=duration, checksum=str(number))
    record = RecordingDate(path=path, camera_root="/camera/DCIM", relative_path="107MEDIA/" + filename,
        size_bytes=100, mtime_ns=number, checksum=str(number), original_time=original,
        corrected_time=start if correction else None, duration_s=duration,
        time_source=time_source, status=status, reasons=json.dumps(reasons or []))
    session.add_all([item, record])
    session.flush()
    relative = ("Holding/" if holding else "") + month.replace("-", "/") + "/" + filename
    ledger = UploadedClip(destination_id=1, source_media_id=item.id, filename=filename,
        size_bytes=100, checksum=str(number), status="done", remote_path=str(nas / relative))
    session.add(ledger)
    session.flush()
    archived = ArchiveFile(destination_id=1, root_path=str(nas), path=relative, kind="video",
        size_bytes=100, mtime_ns=number, ctime_ns=number, device="1", inode=str(number), ledger_id=ledger.id)
    session.add(archived)
    session.flush()
    return archived, item, record


def test_exact_timestamps_create_one_idempotent_automatic_ride(setup):
    session, _, _, calls = setup
    footage(setup, 1)
    footage(setup, 2)
    report = ar.discover(session)
    ride = session.query(AutoRide).one()
    assert ride.active and ride.confidence >= .9 and ride.clip_count == 2
    assert ride.start_time == rt.timestamp("2026-10-02T10:00:00Z").replace(tzinfo=None)
    assert session.query(AutoRideClip).count() == 2
    assert len(calls) == 1 and report["discovered_ride_count"] == 1
    payload = json.loads(ride.payload)
    assert payload["evidence"]["clock_shift_seconds"] == 0
    before = session.connection().exec_driver_sql("SELECT total_changes()").scalar()
    ar.discover(session)
    after = session.connection().exec_driver_sql("SELECT total_changes()").scalar()
    assert before == after and session.query(AutoRide).count() == 1


def test_mtime_bad_future_month_and_held_dates_are_excluded(setup):
    session, _, _, _ = setup
    footage(setup, 1, time_source="mtime", status="review", reasons=["Missing metadata"])
    footage(setup, 2, status="review", reasons=["A higher media-folder / DVR number has an earlier camera timestamp"])
    footage(setup, 3, start="1970-01-01T10:00:00Z", month="1970-01")
    footage(setup, 4, start="2027-10-01T10:00:00Z", month="2027-10")
    footage(setup, 5, month="2026-09")
    footage(setup, 6, status="review", reasons=list(ar.SOFT_REVIEW_REASONS), holding=True)
    clips, summary = ar.eligible_clips(session)
    assert clips == []
    assert summary["exclusions"] == {"mtime_only_date": 1, "reset_reused_or_uncertain_date": 1,
        "reset_or_future_date": 2, "archive_month_mismatch": 1, "unconfirmed_holding_date": 1}
    assert ar.discover(session)["discovered_ride_count"] == 0


def test_media_capture_time_without_provenance_never_becomes_a_ride(setup):
    session, _, _, _ = setup
    for n in (1, 2):
        _, _, record = footage(setup, n)
        session.delete(record)
    session.flush()
    result = ar.discover(session)
    assert result["exclusions"]["missing_metadata_provenance"] == 2
    assert ar.list_rides(session)["rides"] == []


def test_identity_checked_probe_can_supply_metadata_provenance(setup):
    session, _, _, _ = setup
    for n in (1, 2):
        archive, _, record = footage(setup, n)
        session.delete(record)
        session.add(RoadtripClipMetadata(archive_id=archive.id, identity=rt._metadata_identity(archive),
            duration_s=600, observed_time=rt.timestamp(f"2026-10-02T10:{(n-1)*10:02}:00Z")))
    session.flush()
    assert ar.discover(session)["discovered_ride_count"] == 1
    session.query(ArchiveFile).first().mtime_ns += 1
    session.flush()
    assert ar.eligible_clips(session)[1]["exclusions"]["missing_metadata_provenance"] == 1


def test_failed_probe_cannot_supply_automatic_ride_timestamps(setup):
    session, _, _, _ = setup
    archive, _, record = footage(setup, 1)
    session.delete(record)
    session.add(RoadtripClipMetadata(archive_id=archive.id, identity=rt._metadata_identity(archive),
        duration_s=600, observed_time=rt.timestamp("2026-10-02T10:00:00Z"), error="File changed during probe"))
    session.flush()
    clips, report = ar.eligible_clips(session)
    assert clips == [] and report["exclusions"] == {"missing_metadata_provenance": 1}


def test_corrected_copy_is_preferred_without_duplicate_recording(setup):
    session, nas, _, _ = setup
    original, _, record = footage(setup, 1, correction=True)
    corrected = ArchiveFile(destination_id=1, root_path=str(nas), path="Corrected/2026/10/fixed.mp4",
        kind="video", size_bytes=120, mtime_ns=99, ctime_ns=99, device="1", inode="99")
    session.add(corrected)
    session.flush()
    session.add(CorrectedArchive(recording_id=record.id, destination_id=1, corrected_time=record.corrected_time,
        original_path=str(nas / original.path), corrected_path=str(nas / corrected.path)))
    session.flush()
    clips, summary = ar.eligible_clips(session)
    assert len(clips) == 1 and clips[0]["archive_id"] == corrected.id
    assert clips[0]["start_time"] == "2026-10-02T10:00:00Z"
    assert clips[0]["timing_basis"] == "confirmed_date_correction"
    assert summary["exclusions"]["duplicate_recording_copy"] == 1


def test_late_unsupported_tail_does_not_discard_supported_ride(setup):
    session, _, _, _ = setup
    footage(setup, 1)
    footage(setup, 2)
    footage(setup, 3, start="2026-10-02T12:00:00Z")
    result = ar.discover(session)
    assert result["discovered_ride_count"] == 1
    assert result["exclusions"]["no_exact_ha_time_overlap"] == 1
    assert session.query(AutoRide).one().clip_count == 2


def test_distant_recording_clusters_split_even_with_one_multiday_ha_outing(setup):
    session, _, points, _ = setup
    session.get(RoadtripOuting, 1).end_time = rt.timestamp("2026-10-04T12:00:00Z")
    for n in (1, 2):
        footage(setup, n)
    footage(setup, 3, start="2026-10-04T10:00:00Z")
    footage(setup, 4, start="2026-10-04T10:10:00Z")
    points.extend([{**p, "t": p["t"].replace("10-02", "10-04")} for p in list(points)])
    result = ar.discover(session)
    assert result["discovered_ride_count"] == 2
    assert [r.clip_count for r in session.query(AutoRide)] == [2, 2]


def confirmed_return(setup):
    session, _, points, _ = setup
    prefs = rt.preferences(session)
    prefs.home_lat, prefs.home_lon = 51.5, -.1
    outing = session.get(RoadtripOuting, 1)
    outing.end_time = rt.timestamp('2026-10-02T14:50:00Z')
    departure = rt.timestamp('2026-10-02T12:00:00Z')
    points[:] = [{'t': '2026-10-02T10:00:00Z', 'lat': 52.45, 'lon': -1.7, 'accuracy': 10}]
    for minute in range(0, 171, 5):
        if 75 < minute < 100:
            continue  # Real GPS gap at a fuel stop; never interpolate across it.
        points.append({'t': rt.iso(departure + dt.timedelta(minutes=minute)),
            'lat': 52.45 - .95 * minute / 170, 'lon': -1.7 + 1.6 * minute / 170, 'accuracy': 10})
    for n in range(30):
        # The saved correction is 63 minutes late, with a 20-minute stop.
        start = departure + dt.timedelta(minutes=63 + n * 5 + (20 if n >= 15 else 0))
        footage(setup, n + 1, start=start, duration=300, correction=True)
    return ar.eligible_clips(session)[0], [outing], points, prefs


def test_full_confirmed_return_realigns_with_dwell_home_and_gps_evidence(setup):
    session, _, _, _ = setup
    confirmed_return(setup)
    report = ar.discover(session)
    ride = session.query(AutoRide).one()
    assert report['discovered_ride_count'] == 1 and ride.clip_count == 30
    assert rt.iso(ride.start_time) == '2026-10-02T12:00:00Z'
    assert rt.iso(ride.end_time) == '2026-10-02T14:50:00Z'
    assert ride.confidence_label == 'GPS-aligned estimate'
    assert json.loads(ride.payload)['evidence']['clock_shift_seconds'] == -63 * 60
    detail = ar.ride_detail(session, ride.id)
    assert len(detail['clips']) == 30
    assert {c['timing_basis'] for c in detail['clips']} == {'confirmed_return_duration'}
    assert session.query(RecordingDate).first().corrected_time == rt.timestamp('2026-10-02T13:03:00Z').replace(tzinfo=None)
    # No route line crosses the GPS hole, even though the videos form one trip.
    for feature in detail['geojson']['features']:
        assert len(feature['geometry']['coordinates']) < 20
    ar.discover(session)
    assert session.query(AutoRide).count() == 1


def test_return_can_be_evidenced_when_outbound_home_departure_was_missing(setup):
    from app.auto_ride_alignment import align_confirmed_returns
    clips, outings, points, prefs = confirmed_return(setup)
    outings[0].complete = False
    assert all(c.get('association_offset_s') == -63 * 60 for c in align_confirmed_returns(clips, outings, points, prefs))


@pytest.mark.parametrize('missing', ['dwell', 'home', 'arrival', 'approved', 'corrected', 'gps', 'unique'])
def test_return_alignment_refuses_weak_or_ambiguous_evidence(setup, missing):
    from app.auto_ride_alignment import align_confirmed_returns
    clips, outings, points, prefs = confirmed_return(setup)
    if missing == 'dwell':
        points[:] = points[1:]
    elif missing == 'home':
        prefs.home_lat = None
    elif missing == 'arrival':
        points[-1] = points[-1] | {'lat': 52.45, 'lon': -1.7}
    elif missing == 'approved':
        clips[0]['approved_date'] = False
    elif missing == 'corrected':
        clips[0]['timing_basis'] = 'camera_metadata'
    elif missing == 'gps':
        points[:] = [points[0], points[1], points[-1]]
    else:
        outings = outings + [SimpleNamespace(**{**outings[0].__dict__, 'id': 2})]
    result = align_confirmed_returns(clips, outings, points, prefs)
    assert not any(c.get('association_offset_s') for c in result)


def test_ambiguous_overlapping_outings_are_review_not_auto(setup):
    session, _, _, _ = setup
    footage(setup, 1)
    footage(setup, 2)
    session.add(RoadtripOuting(signature="tie", source="ha:device_tracker.phone", month="2026-10",
        start_time=rt.timestamp("2026-10-02T09:55:00Z"), end_time=rt.timestamp("2026-10-02T11:00:00Z"),
        duration_s=3900, distance_m=1000, point_count=30))
    session.flush()
    report = ar.discover(session)
    assert report["exclusions"]["ambiguous_ha_outing"] == 2
    assert ar.list_rides(session)["rides"] == []


def test_single_clip_stays_review(setup):
    session, _, _, _ = setup
    footage(setup, 1)
    report = ar.discover(session)
    assert report["review_count"] == 1 and report["discovered_ride_count"] == 0


def test_nearby_fixes_support_confirmed_dates_but_never_bridge_gps_gap(setup):
    session, _, points, _ = setup
    footage(setup, 1)
    footage(setup, 2)
    points[:] = [{"t": "2026-10-02T09:55:00Z", "lat": 51.5, "lon": -.1},
        {"t": "2026-10-02T10:25:00Z", "lat": 51.5, "lon": -.12}]
    assert ar.discover(session)["discovered_ride_count"] == 1
    row = session.query(AutoRide).one()
    assert row.confidence == .85 and row.distance_m is None
    detail = ar.ride_detail(session, row.id)
    assert detail["located_clip_count"] == 0 and not detail["geojson"]["features"]
    assert all(c["average_speed_kmh"] is None for c in detail["clips"])


def test_historical_only_review_needs_strong_continuous_gps(setup):
    session, _, points, _ = setup
    footage(setup, 1, status="review", reasons=list(ar.SOFT_REVIEW_REASONS))
    footage(setup, 2, status="review", reasons=list(ar.SOFT_REVIEW_REASONS))
    assert ar.discover(session)["discovered_ride_count"] == 1
    points[:] = [points[0], points[-1]]
    report = ar.discover(session)
    assert report["discovered_ride_count"] == 0
    assert session.query(AutoRide).one().status == "stale"


def test_list_is_metadata_only_and_no_raw_gps_is_persisted(setup, monkeypatch):
    session, _, _, _ = setup
    footage(setup, 1)
    footage(setup, 2)
    ar.discover(session)
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("List must not read the NAS"))
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: pytest.fail("List must not read GPS"))
    result = ar.list_rides(session)
    assert result["rides"][0]["association_kind"] == "automatic"
    text = " ".join(m.metadata_json for m in session.query(AutoRideClip)) + session.query(AutoRide).one().payload
    assert "latitude" not in text and "longitude" not in text and '"lat"' not in text


def test_changed_metadata_or_gps_invalidates_detail_until_rediscovered(setup):
    session, _, _, _ = setup
    archive, _, _ = footage(setup, 1)
    footage(setup, 2)
    ar.discover(session)
    row = session.query(AutoRide).one()
    archive.mtime_ns += 10
    session.flush()
    with pytest.raises(ValueError, match="footage"):
        ar.ride_detail(session, row.id)
    ar.discover(session)
    day = session.query(LocationDay).one()
    day.point_count += 1
    session.flush()
    with pytest.raises(ValueError, match="GPS"):
        ar.ride_detail(session, row.id)


def test_detail_uses_actual_video_bounds_not_multiday_ha_bounds(setup):
    session, _, _, calls = setup
    session.get(RoadtripOuting, 1).end_time = rt.timestamp("2026-10-04T12:00:00Z")
    footage(setup, 1)
    footage(setup, 2)
    ar.discover(session)
    row = session.query(AutoRide).one()
    result = ar.ride_detail(session, row.id)
    assert result["association_kind"] == "automatic" and result["outing"]["duration_s"] == 1200
    assert result["outing"]["start_time"] == "2026-10-02T10:00:00Z"
    assert result["outing"]["end_time"] == "2026-10-02T10:20:00Z"
    assert calls[-1][2]["start_time"] == rt.timestamp("2026-10-02T09:45:00Z")
    assert calls[-1][2]["end_time"] == rt.timestamp("2026-10-02T10:35:00Z")
    assert min(p["time"] for p in result["time_series"]) == result["outing"]["start_time"]
    assert max(p["time"] for p in result["time_series"]) == result["outing"]["end_time"]


def test_fingerprint_tracks_date_revision_and_source_configuration(setup):
    session, _, _, _ = setup
    _, _, record = footage(setup, 1)
    before = ar.input_fingerprint(session)
    record.revision += 1
    session.flush()
    assert ar.input_fingerprint(session) != before
    before = ar.input_fingerprint(session)
    session.get(RoadtripSettings, 1).entity_id = "person.other"
    session.flush()
    assert ar.input_fingerprint(session) != before


def test_unconfirmed_camera_counter_regression_is_excluded_even_with_soft_flags(setup):
    session, _, _, _ = setup
    footage(setup, 1, start="2026-10-02T10:10:00Z", status="review", reasons=list(ar.SOFT_REVIEW_REASONS))
    footage(setup, 2, start="2026-10-02T10:00:00Z", status="review", reasons=list(ar.SOFT_REVIEW_REASONS))
    assert ar.eligible_clips(session)[1]["exclusions"]["camera_sequence_regression"] == 2


def test_normalized_gallery_outline_preserves_gaps_and_never_stores_coordinates(setup):
    session, _, _, _ = setup
    footage(setup, 1)
    footage(setup, 2)
    ar.discover(session)
    preview = ar.list_rides(session)["rides"][0]["route_preview"]
    assert preview and sum(len(piece) for piece in preview) <= 120
    assert all(0 <= value <= 1 for piece in preview for point in piece for value in point)
    assert len({point[0] for piece in preview for point in piece}) > 1
    assert all(point[1] == .5 for piece in preview for point in piece)
    assert all(abs(point[0] - 51.5) > 1 for piece in preview for point in piece)


def test_gallery_outline_point_cap_never_bridges_continuous_segments():
    start = rt.timestamp("2026-10-02T10:00:00Z")
    tracks = [[{"t": rt.iso(start + dt.timedelta(minutes=base + i)), "lat": 51.5,
        "lon": -.1 - (base + i) / 10000} for i in range(20)] for base in (0, 40)]
    clip = {"start_time": rt.iso(start), "end_time": rt.iso(start + dt.timedelta(hours=1))}
    outline, truncated = ar._route_preview(tracks, [clip], max_points=6)
    assert truncated and len(outline) == 2
    assert sum(len(piece) for piece in outline) <= 6


def test_discovery_holds_no_sqlite_write_lock_during_second_month_nas_read(setup, tmp_path, monkeypatch):
    import sqlite3
    session, _, points, _ = setup
    footage(setup, 1)
    footage(setup, 2)
    footage(setup, 3, start="2026-11-02T10:00:00Z", month="2026-11")
    footage(setup, 4, start="2026-11-02T10:10:00Z", month="2026-11")
    session.add(RoadtripOuting(signature="outing2", source="ha:device_tracker.phone", month="2026-11",
        start_time=rt.timestamp("2026-11-02T09:50:00Z"), end_time=rt.timestamp("2026-11-02T11:30:00Z"),
        duration_s=6000, distance_m=10000, point_count=60, gap_count=0, complete=True))
    session.commit()
    path = tmp_path / "concurrent.db"
    with sqlite3.connect(path) as target:
        session.connection().connection.driver_connection.backup(target)
    file_engine = create_engine(f"sqlite:///{path}")
    observed = []
    def load(session, month, source, **kwargs):
        if month == "2026-11":
            # Models the progress/cancellation writer on another connection.
            with sqlite3.connect(path, timeout=.1) as concurrent:
                concurrent.execute("UPDATE roadtrip_settings SET max_gap_s=max_gap_s WHERE id=1")
                concurrent.commit()
            observed.append("writer committed")
        return [{**p, "t": p["t"].replace("2026-10", month)} for p in points]
    monkeypatch.setattr(rt, "load_month", load)
    with Session(file_engine) as current:
        report = ar.discover(current, now=rt.timestamp("2026-12-01T00:00:00Z"))
        assert report["discovered_ride_count"] == 2 and observed == ["writer committed"]
