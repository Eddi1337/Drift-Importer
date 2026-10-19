import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app import auto_ride_ha as refresh
from app import roadtrips as rt
from app.auto_ride_models import AutoRideHARefresh
from app.database import Base
from app.roadtrip_models import LocationDay, RoadtripSettings


SOURCE = "ha:device_tracker.phone"
NOW = rt.timestamp("2026-10-18T12:00:00Z")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    nas = tmp_path / "NAS"
    nas.mkdir()
    monkeypatch.setattr(rt, "get_settings", lambda: SimpleNamespace(nas_root=nas))
    monkeypatch.setattr(rt, "require_storage", lambda path: Path(path).resolve())
    monkeypatch.setattr(refresh, "get_app_settings", lambda session: SimpleNamespace(ha_base_url="http://ha.invalid", ha_token="configured"))
    monkeypatch.setattr(refresh, "_headers", lambda settings: {})
    clips, requests, rebuilt = [], [], []
    monkeypatch.setattr(refresh.rides, "eligible_clips", lambda session, now=None: (clips, {}))
    monkeypatch.setattr(rt, "rebuild_outings", lambda session, month, source: rebuilt.append((month, source)))
    responses = {}
    def handle(request):
        requests.append(request)
        if request.url.path == "/api/states/zone.home":
            value = responses.get("zone.home", {"entity_id": "zone.home", "state": "0",
                "attributes": {"latitude": 51.5, "longitude": -.1, "radius": 175}})
            return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)
        if responses.get("before_history"):
            responses["before_history"](request)
        return httpx.Response(200, json=responses.get(str(request.url).split("/period/")[-1].split("?")[0][:10], []))
    client = httpx.Client
    monkeypatch.setattr(refresh.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle)))
    with Session(engine, expire_on_commit=False) as session:
        session.add(RoadtripSettings(id=1, entity_id="device_tracker.phone", timezone="Europe/London",
            home_lat=51.5, home_lon=-.1, home_radius_m=150, max_gap_s=900, max_accuracy_m=100, max_speed_kmh=180))
        session.commit()
        yield session, clips, requests, responses, rebuilt


def clip(day="2026-10-18", start="10:00:00", end="10:10:00"):
    return {"start_time": f"{day}T{start}Z", "end_time": f"{day}T{end}Z"}


def cache(session, day, points=(), *, imported="2026-10-18T09:00:00Z"):
    rt.save_day(session, SOURCE, day, list(points))
    row = session.query(LocationDay).filter_by(source=SOURCE, day=day).one()
    row.imported_at = rt.timestamp(imported)
    session.commit()
    return row


def gps(time, lon=-.1):
    return {"t": time, "lat": 51.5, "lon": lon, "accuracy": 10}


def history(time, lon=-.1):
    return {"state": "not_home", "last_updated": time, "attributes": {"latitude": 51.5, "longitude": lon, "gps_accuracy": 10}}


def test_populated_and_known_empty_historical_days_are_not_refetched(setup, monkeypatch):
    session, clips, requests, _, _ = setup
    clips.append(clip("2026-10-02"))
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        cache(session, day, [gps(day + "T10:00:00Z")] if day.endswith("02") else [])
    monkeypatch.setattr(rt, "_cache_root", lambda: pytest.fail("A cached plan must not touch the NAS"))
    assert refresh.pending_refresh_days(session, now=NOW) == []
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["checked_days"] == 0 and report["pending_days"] == 0 and requests == []


def test_missing_days_are_batched_and_known_empty_days_are_not_retried(setup):
    session, clips, requests, _, _ = setup
    clips.append(clip("2026-10-02"))
    first = refresh.refresh_ha_days(session, now=NOW, max_days=1)
    assert first["checked_days"] == 1 and first["pending_days"] == 2 and first["empty_days"] == 1
    assert session.query(LocationDay).filter_by(day="2026-10-02").one().point_count == 0
    refresh.refresh_ha_days(session, now=NOW, max_days=1)
    refresh.refresh_ha_days(session, now=NOW, max_days=1)
    assert refresh.pending_refresh_days(session, now=NOW) == [] and len(requests) == 3


def test_empty_current_response_preserves_old_points_without_rewriting_gps(setup):
    session, clips, requests, _, _ = setup
    clips.append(clip())
    cache(session, "2026-10-17")
    row = cache(session, "2026-10-18", [gps("2026-10-18T10:00:00Z"), gps("2026-10-18T10:10:00Z", -.11)])
    before, imported = Path(row.cache_path).read_bytes(), row.imported_at
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["preserved_days"] == 1 and report["imported_days"] == 0
    assert Path(row.cache_path).read_bytes() == before and row.imported_at == imported
    assert session.get(AutoRideHARefresh, (SOURCE, "2026-10-18")).result == "preserved"
    assert refresh.pending_refresh_days(session, now=NOW + dt.timedelta(minutes=10)) == []
    assert refresh.pending_refresh_days(session, now=NOW + dt.timedelta(minutes=31)) == ["2026-10-18"]


def test_new_relevant_gps_is_merged_and_only_changed_month_rebuilt(setup):
    session, clips, requests, responses, rebuilt = setup
    clips.append(clip())
    cache(session, "2026-10-17")
    old = gps("2026-10-18T10:00:00Z")
    row = cache(session, "2026-10-18", [old])
    # Local October 18 begins at 23:00 UTC on the 17th.
    responses["2026-10-17"] = [[history("2026-10-18T10:10:00Z", -.11)]]
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["imported_day_keys"] == ["2026-10-18"]
    assert rebuilt == [("2026-10", SOURCE)]
    saved = json.loads(__import__("gzip").decompress(Path(row.cache_path).read_bytes()))
    assert len(saved) == 2 and saved[0]["t"] == old["t"]
    assert requests[0].url.params["significant_changes_only"] == "0"
    assert requests[0].url.params["filter_entity_id"] == "device_tracker.phone"


def test_newer_clip_end_forces_refresh_inside_cooldown(setup):
    session, clips, _, _, _ = setup
    clips.append(clip(start="11:45:00", end="11:55:00"))
    cache(session, "2026-10-17")
    cache(session, "2026-10-18")
    session.add(AutoRideHARefresh(source=SOURCE, day="2026-10-18", checked_at=rt.timestamp("2026-10-18T11:50:00Z"), result="empty"))
    session.commit()
    assert refresh.pending_refresh_days(session, now=NOW) == ["2026-10-18"]


def test_dst_day_request_uses_local_midnights_not_fixed_24_hours(setup):
    session, clips, requests, _, _ = setup
    clips.append(clip("2026-10-25"))
    cache(session, "2026-10-24")
    cache(session, "2026-10-26")
    refresh.refresh_ha_days(session, now=rt.timestamp("2026-10-27T12:00:00Z"))
    assert "/period/2026-10-24T23:00:00Z" in str(requests[0].url)
    assert requests[0].url.params["end_time"] == "2026-10-26T00:00:00Z"


def test_offline_nas_defers_before_http_or_cache_write(setup, monkeypatch):
    session, clips, requests, _, _ = setup
    clips.append(clip())
    monkeypatch.setattr(rt, "_cache_root", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["pending_days"] == 2 and report["errors"] and not requests
    assert session.query(LocationDay).count() == 0


def test_oversized_ha_response_keeps_existing_gps_and_redacts_error(setup, monkeypatch):
    session, clips, _, responses, _ = setup
    clips.append(clip())
    cache(session, "2026-10-17")
    row = cache(session, "2026-10-18", [gps("2026-10-18T10:00:00Z")])
    responses["2026-10-17"] = [[history("2026-10-18T10:10:00Z")]]
    monkeypatch.setattr(rt, "MAX_IMPORT_BYTES", 16)
    before = Path(row.cache_path).read_bytes()
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["errors"] and Path(row.cache_path).read_bytes() == before
    assert "Authorization" not in json.dumps(report) and "ha.invalid" not in json.dumps(report)


def test_failed_outing_rebuild_retries_without_refetching_historical_gps(setup, monkeypatch):
    session, clips, requests, responses, _ = setup
    clips.append(clip("2026-10-02"))
    cache(session, "2026-10-01")
    cache(session, "2026-10-03")
    responses["2026-10-01"] = [[history("2026-10-02T10:00:00Z"), history("2026-10-02T10:10:00Z", -.11)]]
    monkeypatch.setattr(rt, "rebuild_outings", lambda *a: (_ for _ in ()).throw(RuntimeError("temporarily offline")))
    first = refresh.refresh_ha_days(session, now=NOW)
    assert first["errors"] and first["pending_rebuild_months"] == ["2026-10"]
    assert refresh.pending_refresh_days(session, now=NOW) == ["2026-10-02"]
    count = len(requests)
    monkeypatch.setattr(rt, "rebuild_outings", lambda *a: None)
    second = refresh.refresh_ha_days(session, now=NOW)
    assert not second["errors"] and second["pending_rebuild_months"] == [] and len(requests) == count


def missing_home(session):
    prefs = session.get(RoadtripSettings, 1)
    prefs.home_lat, prefs.home_lon = None, None
    session.commit()
    return prefs


def test_first_refresh_fetches_only_home_entity_and_commits_before_history(setup, monkeypatch):
    session, clips, requests, responses, _ = setup
    prefs = missing_home(session)
    clips.append(clip())
    committed = []
    event.listen(session, "after_commit", lambda session: committed.append(True))
    monkeypatch.setattr(refresh, "_headers", lambda settings: {"Authorization": "Bearer private-test-token"})
    def before_history(request):
        assert committed and not session.dirty
        assert (prefs.home_lat, prefs.home_lon, prefs.home_radius_m) == (51.5, -.1, 175)
    responses["before_history"] = before_history
    report = refresh.refresh_ha_days(session, now=NOW, max_days=1)
    assert report["checked_days"] == 1 and not report["errors"]
    assert requests[0].url.path == "/api/states/zone.home"
    assert requests[0].headers["Authorization"] == "Bearer private-test-token"
    assert requests[0].extensions["timeout"]["read"] == 20
    assert all(request.url.path != "/api/states" for request in requests)
    public = json.dumps(report)
    assert "latitude" not in public and "51.5" not in public and "private-test-token" not in public
    refresh.refresh_ha_days(session, now=NOW, max_days=1)
    assert sum(request.url.path == "/api/states/zone.home" for request in requests) == 1


def test_missing_home_with_only_pending_rebuild_is_initialized_before_rebuild(setup, monkeypatch):
    session, clips, requests, _, rebuilt = setup
    clips.append(clip("2026-10-02"))
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        cache(session, day)
    session.add(AutoRideHARefresh(source=SOURCE, day="2026-10-02", checked_at=NOW, result="rebuild_pending"))
    session.commit()
    prefs = missing_home(session)
    def rebuild(session, month, source):
        assert prefs.home_lat == 51.5 and not session.dirty
        rebuilt.append((month, source))
    monkeypatch.setattr(rt, "rebuild_outings", rebuild)
    report = refresh.refresh_ha_days(session, now=NOW)
    assert not report["errors"] and rebuilt == [("2026-10", SOURCE)]
    assert [r.url.path for r in requests] == ["/api/states/zone.home"]


def test_missing_home_does_not_fetch_when_all_days_are_cached(setup, monkeypatch):
    session, clips, requests, _, _ = setup
    clips.append(clip("2026-10-02"))
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        cache(session, day)
    missing_home(session)
    monkeypatch.setattr(rt, "_cache_root", lambda: pytest.fail("Cached outings need no NAS refresh"))
    assert not refresh.refresh_ha_days(session, now=NOW)["errors"]
    assert requests == []


@pytest.mark.parametrize("attributes", [
    {}, {"latitude": None, "longitude": -.1}, {"latitude": 91, "longitude": -.1},
    {"latitude": 51.5, "longitude": -181}, {"latitude": True, "longitude": -.1},
    {"latitude": "51.5", "longitude": -.1}, {"latitude": 51.5, "longitude": -.1, "radius": 0},
    {"latitude": 51.5, "longitude": -.1, "radius": 2001},
])
def test_invalid_home_defers_without_touching_cached_history(setup, attributes):
    session, clips, requests, responses, rebuilt = setup
    clips.append(clip())
    row = cache(session, "2026-10-18", [gps("2026-10-18T10:00:00Z")])
    before = Path(row.cache_path).read_bytes()
    prefs = missing_home(session)
    responses["zone.home"] = {"entity_id": "zone.home", "state": "0", "attributes": attributes}
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["errors"] and report["pending_days"] == 2
    assert prefs.home_lat is None and prefs.home_lon is None and not rebuilt
    assert Path(row.cache_path).read_bytes() == before
    assert [r.url.path for r in requests] == ["/api/states/zone.home"]
    assert "attributes" not in json.dumps(report) and "51.5" not in json.dumps(report)


@pytest.mark.parametrize("status", [401, 404, 503])
def test_home_http_error_is_redacted_and_defers_history(setup, status):
    session, clips, requests, responses, _ = setup
    clips.append(clip())
    missing_home(session)
    responses["zone.home"] = httpx.Response(status, text="private upstream diagnostics")
    report = refresh.refresh_ha_days(session, now=NOW)
    assert str(status) in report["errors"][0]["message"] and report["checked_days"] == 0
    assert len(requests) == 1 and "private" not in json.dumps(report)


def test_oversized_home_response_is_bounded_without_content_length(setup, monkeypatch):
    session, clips, requests, responses, _ = setup
    clips.append(clip())
    missing_home(session)
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 40
            yield b"y" * 40
    responses["zone.home"] = httpx.Response(200, stream=Chunks())
    monkeypatch.setattr(refresh, "MAX_HOME_BYTES", 64)
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["errors"] and len(requests) == 1
    assert session.get(RoadtripSettings, 1).home_lat is None


@pytest.mark.parametrize("body", [
    b"not valid JSON", b"[]",
    b'{"entity_id":"zone.home","attributes":{"latitude":NaN,"longitude":0}}',
    b'{"entity_id":"zone.home","attributes":{"latitude":0,"longitude":0,"radius":Infinity}}',
    b'{"entity_id":"zone.work","attributes":{"latitude":0,"longitude":0}}',
    b'{"entity_id":"zone.home","state":"unavailable","attributes":{"latitude":0,"longitude":0}}',
])
def test_malformed_or_nonfinite_home_response_defers_safely(setup, body):
    session, clips, requests, responses, _ = setup
    clips.append(clip())
    missing_home(session)
    responses["zone.home"] = httpx.Response(200, content=body)
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["errors"] and report["checked_days"] == 0 and len(requests) == 1
    assert session.get(RoadtripSettings, 1).home_lat is None


@pytest.mark.parametrize("failure", [httpx.ConnectError, InvalidToken])
def test_home_network_or_token_failure_preserves_existing_partial_preferences(setup, monkeypatch, failure):
    session, clips, requests, _, rebuilt = setup
    clips.append(clip())
    prefs = session.get(RoadtripSettings, 1)
    prefs.home_lon = None
    session.commit()
    def failed(*args):
        raise failure("private upstream details")
    monkeypatch.setattr(refresh, "_fetch_home", failed)
    report = refresh.refresh_ha_days(session, now=NOW)
    assert report["errors"] and not rebuilt and requests == []
    assert prefs.home_lat == 51.5 and prefs.home_lon is None
    assert "private" not in json.dumps(report)
