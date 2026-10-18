import datetime as dt

import pytest

from app.roadtrip_map import TrackIndex, annotate_detail


def point(minute, longitude=-.1, segment=0):
    time = dt.datetime(2026, 6, 4, 22, 50, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minute)
    return {"time": time.isoformat(), "latitude": 51.5, "longitude": longitude,
            "segment": segment, "speed_kmh": 20}


def test_clip_between_fixes_gets_interpolated_path_and_numbered_location():
    detail = {"provisional": True, "time_series": [point(0), point(10, -.12)],
              "clips": [{"id": "media:1", "filename": "ride.mp4",
                         "start_time": point(2)["time"], "end_time": point(4)["time"]}]}
    result = annotate_detail(detail)
    clip = result["clips"][0]
    assert clip["number"] == 1 and clip["location_status"] == "proposed"
    assert clip["location"]["longitude"] == pytest.approx(-.104)
    assert clip["location_date"] == "2026-06-04"
    line = result["clip_geojson"]["features"][0]["geometry"]["coordinates"]
    assert line[0][0] == pytest.approx(-.104)
    assert line[-1][0] == pytest.approx(-.108)


def test_no_markers_or_paths_are_invented_inside_a_gps_gap():
    detail = {"time_series": [point(0), point(5, -.11), point(40, -.2), point(45, -.21)],
              "clips": [{"id": "archive:1", "start_time": point(15)["time"], "end_time": point(20)["time"]}]}
    result = annotate_detail(detail)
    assert result["clips"][0]["location"] is None
    assert result["unlocated_clip_count"] == 1
    assert not result["clip_geojson"]["features"]


def test_tracks_do_not_join_segments_and_show_local_dates_across_midnight():
    detail = {"provisional": False,
              "time_series": [point(0), point(5, -.11), point(10, -.12, 1), point(15, -.13, 1)],
              "clips": [{"id": "media:1", "start_time": point(0)["time"], "end_time": point(15)["time"]},
                        {"id": "media:2", "start_time": point(10)["time"], "end_time": point(15)["time"]}]}
    result = annotate_detail(detail, "Europe/London")
    assert len(result["clip_geojson"]["features"]) == 3
    assert [row["date"] for row in result["location_dates"]] == ["2026-06-04", "2026-06-05"]
    assert all(clip["location_status"] == "confirmed" for clip in result["clips"])


def test_missing_dates_and_zero_duration_clips_stay_unlocated():
    result = annotate_detail({"time_series": [point(0), point(10)], "clips": [
        {"id": "archive:1", "start_time": None, "end_time": None},
        {"id": "archive:2", "start_time": point(0)["time"], "end_time": point(0)["time"]}]})
    assert result["located_clip_count"] == 0
    assert not result["clip_geojson"]["features"]


def test_interval_search_excludes_unselected_times_at_both_edges():
    index = TrackIndex([point(i, -.1 - i / 1000) for i in range(100)])
    pieces = index.interval(point(80)["time"], point(82)["time"])
    assert len(pieces) == 1 and len(pieces[0]) == 3
    assert pieces[0][0]["time"].startswith("2026-06-05T00:10:")
