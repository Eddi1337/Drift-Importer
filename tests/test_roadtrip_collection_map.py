"""A collection maps filmed, confirmed intervals without fabricating GPS."""
import datetime as dt
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import roadtrip_collections as collections
from app import roadtrip_collection_map as maps
from app import roadtrips as rt
from app.archive_models import ArchiveFile
from app.database import Base
from app.models import Destination, MediaItem, UploadedClip
from app.roadtrip_models import RoadtripCollection, RoadtripMatch, RoadtripOuting, RoadtripSettings

UTC = dt.timezone.utc


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Destination(id=1, name="NAS", type="local", base_path="/mnt/NAS"))
        session.add(RoadtripSettings(id=1, timezone="Europe/London", max_gap_s=900,
            max_accuracy_m=100, max_speed_kmh=180, home_radius_m=150))
        session.flush()
        yield session


def point(time, longitude=-.1):
    return {"t": rt.iso(time), "lat": 51.5, "lon": longitude, "accuracy": 10}


def footage(session, name, month="2026-06", duration=600):
    item = MediaItem(filename=name, path=f"/camera/{month}/{name}", kind="video", duration_s=duration,
        capture_time=dt.datetime(2020, 1, 1), checksum=month + name)
    session.add(item)
    session.flush()
    ledger = UploadedClip(destination_id=1, source_media_id=item.id, filename=name,
        remote_path=month.replace("-", "/") + "/" + name, checksum=item.checksum, status="done")
    session.add(ledger)
    session.flush()
    archive = ArchiveFile(destination_id=1, root_path="/mnt/NAS", path=ledger.remote_path,
        kind="video", size_bytes=100, ledger_id=ledger.id)
    session.add(archive)
    session.flush()
    return {"id": f"media:{item.id}", "media_id": item.id, "archive_id": archive.id,
        "filename": name, "duration_s": duration, "observed_time": "2020-01-01T00:00:00Z"}


def saved_match(session, clips, start="2026-06-04T10:00:00Z", *, month="2026-06", source="ha:phone", end=None, offsets=None):
    start_dt = rt.timestamp(start)
    end_dt = rt.timestamp(end) if end else start_dt + dt.timedelta(hours=1)
    outing = RoadtripOuting(signature=start + str(clips[0]["id"]), source=source, month=month,
        start_time=start_dt, end_time=end_dt, duration_s=(end_dt - start_dt).total_seconds(),
        distance_m=1000, point_count=10, complete=True)
    session.add(outing)
    session.flush()
    row = RoadtripMatch(month=month, group_key="g" + str(outing.id), outing_id=outing.id,
        name="Ride", status="confirmed", mapping=json.dumps({"start_time": start,
            "clip_offsets": offsets or {}, "group": {"clips": clips, "relative_timing_available": False,
                "month": month, "label": "Camera group", "clip_count": len(clips)}}))
    session.add(row)
    session.flush()
    return row


def collection(session, clip_ids):
    value = collections.save_collection(session, "Summer journey", clip_ids)
    return session.get(RoadtripCollection, value["id"])


def test_only_selected_video_intervals_appear_on_map(session, monkeypatch):
    first, second = footage(session, "first.mp4"), footage(session, "second.mp4")
    saved_match(session, [first, second])
    points = [point(f"2026-06-04T10:{minute:02}:00Z", -.1 - minute / 1000) for minute in range(0, 61, 5) if minute < 60]
    calls = []
    def load(*args, **kwargs):
        calls.append((args[1:3], kwargs))
        return points
    monkeypatch.setattr(rt, "load_month", load)
    result = maps.collection_map(session, collection(session, [second["id"]]))
    assert len(calls) == 1 and len(result["clips"]) == 1
    clip = result["clips"][0]
    assert clip["start_time"] == "2026-06-04T10:10:00Z" and clip["number"] == 1
    assert clip["location"]["longitude"] == pytest.approx(-.11)
    assert result["located_clip_count"] == 1
    assert all(f["properties"]["clip_id"] == second["id"] for f in result["geojson"]["features"])
    assert min(p["time"] for p in result["time_series"]) == "2026-06-04T10:10:00Z"
    assert max(p["time"] for p in result["time_series"]) == "2026-06-04T10:20:00Z"
    assert result["outing"]["distance_m"] == pytest.approx(rt.distance(point("2026-06-04T10:10:00Z", -.11), point("2026-06-04T10:20:00Z", -.12)), abs=.2)


def test_source_month_loaded_once_for_multiple_matches(session, monkeypatch):
    first, second = footage(session, "first.mp4"), footage(session, "second.mp4")
    saved_match(session, [first])
    saved_match(session, [second], "2026-06-05T10:00:00Z")
    calls = []
    def load(session, month, source, **kwargs):
        calls.append((month, source, kwargs))
        return [point(f"2026-06-{day:02}T10:{minute:02}:00Z", -.1 - minute / 1000)
            for day in (4, 5) for minute in (0, 5, 10)]
    monkeypatch.setattr(rt, "load_month", load)
    result = maps.collection_map(session, collection(session, [first["id"], second["id"]]))
    assert len(calls) == 1 and calls[0][0:2] == ("2026-06", "ha:phone")
    assert calls[0][2]["start_time"] == rt.timestamp("2026-06-04T10:00:00Z")
    assert calls[0][2]["end_time"] == rt.timestamp("2026-06-05T11:00:00Z")
    assert result["located_clip_count"] == 2
    assert len({p["segment"] for p in result["time_series"]}) == 2


def test_unmatched_and_ambiguous_files_remain_visible_without_gps_reads(session, monkeypatch):
    first, second = footage(session, "first.mp4"), footage(session, "second.mp4")
    saved_match(session, [first])
    saved_match(session, [first], "2026-06-05T10:00:00Z")
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: pytest.fail("No unambiguous locations to load"))
    result = maps.collection_map(session, collection(session, [first["id"], second["id"]]))
    assert len(result["clips"]) == 2 and result["unlocated_clip_count"] == 2
    assert all(c["start_time"] is None and c["location"] is None for c in result["clips"])
    assert result["clips"][0]["location_status"] == "ambiguous"
    assert result["geojson"]["features"] == []


def test_offline_month_keeps_confirmed_dates_and_other_month_still_maps(session, monkeypatch):
    first = footage(session, "june.mp4")
    second = footage(session, "july.mp4", "2026-07")
    saved_match(session, [first])
    saved_match(session, [second], "2026-07-05T10:00:00Z", month="2026-07")
    def load(session, month, source, **kwargs):
        if month == "2026-06":
            raise OSError("NAS unavailable; private path should not leak")
        return [point(f"2026-07-05T10:{m:02}:00Z", -.1 - m / 1000) for m in (0, 5, 10)]
    monkeypatch.setattr(rt, "load_month", load)
    result = maps.collection_map(session, collection(session, [first["id"], second["id"]]))
    assert len(result["clips"]) == 2 and result["located_clip_count"] == 1
    assert result["clips"][0]["start_time"] == "2026-06-04T10:00:00Z"
    assert result["clips"][0]["location"] is None
    assert result["map_errors"][0]["month"] == "2026-06"
    assert "private path" not in json.dumps(result)


def test_gps_gap_never_creates_video_pin_or_route(session, monkeypatch):
    first = footage(session, "gap.mp4")
    saved_match(session, [first], offsets={first["id"]: 1200})
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: [point("2026-06-04T10:00:00Z"),
        point("2026-06-04T10:05:00Z", -.11), point("2026-06-04T10:45:00Z", -.12), point("2026-06-04T10:50:00Z", -.13)])
    result = maps.collection_map(session, collection(session, [first["id"]]))
    assert result["clips"][0]["start_time"] == "2026-06-04T10:20:00Z"
    assert result["clips"][0]["location"] is None
    assert not result["geojson"]["features"] and not result["time_series"]


def test_route_limit_keeps_all_video_locations_and_warns(session, monkeypatch):
    first, second = footage(session, "first.mp4"), footage(session, "second.mp4")
    saved_match(session, [first, second])
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: [
        point(f"2026-06-04T10:{m:02}:00Z", -.1 - m / 10000) for m in range(30)])
    result = maps.collection_map(session, collection(session, [first["id"], second["id"]]), max_points=5)
    assert result["route_truncated"] and "limited to 5 points" in result["warning"]
    assert result["located_clip_count"] == 2 and all(c["location"] for c in result["clips"])
    assert sum(len(f["geometry"]["coordinates"]) for f in result["geojson"]["features"]) <= 5
    assert len(result["time_series"]) <= 5


def test_overlapping_cameras_do_not_double_distance_or_timeline(session, monkeypatch):
    first, second = footage(session, "front.mp4"), footage(session, "back.mp4")
    saved_match(session, [first, second], offsets={first["id"]: 0, second["id"]: 0})
    points = [point(f"2026-06-04T10:{m:02}:00Z", -.1 - m / 1000) for m in (0, 5, 10)]
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: points)
    result = maps.collection_map(session, collection(session, [first["id"], second["id"]]))
    assert result["outing"]["distance_m"] == pytest.approx(rt.distance(points[0], points[-1]), abs=.2)
    assert len(result["time_series"]) == 3


def test_timeline_distance_stays_chronological_across_source_sort_order(session, monkeypatch):
    morning, afternoon = footage(session, "morning.mp4"), footage(session, "afternoon.mp4")
    saved_match(session, [morning], "2026-06-04T10:00:00Z", source="ha:z-phone")
    saved_match(session, [afternoon], "2026-06-04T15:00:00Z", source="file:a-gpx")
    def load(session, month, source, **kwargs):
        hour = "10" if source == "ha:z-phone" else "15"
        return [point(f"2026-06-04T{hour}:{m:02}:00Z", -.1 - m / 1000) for m in (0, 5, 10)]
    monkeypatch.setattr(rt, "load_month", load)
    result = maps.collection_map(session, collection(session, [morning["id"], afternoon["id"]]))
    distances = [p["distance_m"] for p in result["time_series"]]
    assert distances == sorted(distances) and distances[0] == 0
    assert distances[2] == distances[3]  # no fabricated travel during the five-hour GPS gap
    assert distances[-1] == result["outing"]["distance_m"]


def test_different_sources_at_same_clip_time_do_not_choose_arbitrary_route(session, monkeypatch):
    first = footage(session, "ambiguous.mp4")
    initial = saved_match(session, [first], source="ha:phone")
    session.get(RoadtripOuting, initial.outing_id).signature += "first"
    session.flush()
    saved_match(session, [first], source="file:other-phone")
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: pytest.fail("Conflicting sources require review first"))
    result = maps.collection_map(session, collection(session, [first["id"]]))
    assert result["clips"][0]["alignment_conflict"]
    assert result["clips"][0]["start_time"] is None and result["clips"][0]["location"] is None
    assert not result["geojson"]["features"]


def test_overlapping_cameras_with_distinct_gps_sources_do_not_sum_distances(session, monkeypatch):
    first, second = footage(session, "front.mp4"), footage(session, "rear.mp4")
    saved_match(session, [first], source="ha:phone")
    saved_match(session, [second], source="file:gpx")
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: [point("2026-06-04T10:00:00Z"),
        point("2026-06-04T10:05:00Z", -.11), point("2026-06-04T10:10:00Z", -.12)])
    result = maps.collection_map(session, collection(session, [first["id"], second["id"]]))
    assert result["located_clip_count"] == 2
    assert all(c["average_speed_kmh"] is not None for c in result["clips"])
    assert result["distance_conflict"] and result["outing"]["distance_m"] is None
    assert result["outing"]["distance_basis"] == "ambiguous_sources"
    assert all(p["distance_m"] is None for p in result["time_series"])
    assert "total distance is unavailable" in result["warning"]


def test_cross_month_and_dst_utc_bounds_reach_loader_unchanged(session, monkeypatch):
    first = footage(session, "midnight.mp4", "2026-10", duration=1200)
    start, end = "2026-10-31T23:50:00Z", "2026-11-01T00:20:00Z"
    saved_match(session, [first], start, month="2026-10", end=end)
    calls = []
    def load(session, month, source, **kwargs):
        calls.append(kwargs)
        return [point(start), point("2026-11-01T00:00:00Z", -.11), point("2026-11-01T00:10:00Z", -.12)]
    monkeypatch.setattr(rt, "load_month", load)
    result = maps.collection_map(session, collection(session, [first["id"]]))
    assert calls[0]["start_time"] == rt.timestamp(start) and calls[0]["end_time"] == rt.timestamp(end)
    assert result["located_clip_count"] == 1
    assert result["clips"][0]["end_time"] == "2026-11-01T00:10:00Z"


def test_deleted_camera_media_row_still_maps_saved_archive_alias(session, monkeypatch):
    first = footage(session, "front.mp4")
    saved_match(session, [first])
    trip = collection(session, [first["id"]])
    ledger = session.query(UploadedClip).one()
    ledger.source_media_id = None
    session.delete(session.get(MediaItem, first["media_id"]))
    session.flush()
    before = session.execute(RoadtripCollection.__table__.select()).all()
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: [point("2026-06-04T10:00:00Z"),
        point("2026-06-04T10:05:00Z", -.11), point("2026-06-04T10:10:00Z", -.12)])
    result = maps.collection_map(session, trip)
    assert result["clips"][0]["id"] == first["id"]
    assert result["clips"][0]["current_id"] == f"archive:{first['archive_id']}"
    assert result["located_clip_count"] == 1
    assert session.execute(RoadtripCollection.__table__.select()).all() == before
    assert not session.dirty


@pytest.mark.parametrize("points", [[], [{"t": "bad time", "lat": 51.5, "lon": -.1}]])
def test_empty_or_unusable_gps_is_reported_without_hiding_files(session, monkeypatch, points):
    first = footage(session, "front.mp4")
    saved_match(session, [first])
    monkeypatch.setattr(rt, "load_month", lambda *a, **k: points)
    result = maps.collection_map(session, collection(session, [first["id"]]))
    assert len(result["clips"]) == 1 and result["clips"][0]["location"] is None
    assert result["clips"][0]["start_time"] == "2026-06-04T10:00:00Z"
    assert result["map_errors"] and "could not be loaded" in result["warning"]


def test_map_route_returns_all_unmatched_files_and_not_found(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import get_session
    from app.routers.roadtrips import router
    first = footage(session, "unmatched.mp4")
    trip = collection(session, [first["id"]])
    app = FastAPI()
    app.include_router(router, prefix="/api/roadtrips")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        response = client.get(f"/api/roadtrips/collections/{trip.id}/map")
        assert response.status_code == 200, response.text
        assert response.json()["collection"]["name"] == "Summer journey"
        assert response.json()["unlocated_clip_count"] == 1
        assert client.get("/api/roadtrips/collections/999/map").status_code == 404
