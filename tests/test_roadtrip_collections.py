"""Trip grouping is metadata-only and never upgrades an unverified camera date."""
import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import roadtrip_collections as trips
from app.archive_models import ArchiveFile
from app.database import Base
from app.models import Destination, MediaItem, UploadedClip
from app.roadtrip_models import RoadtripCollection, RoadtripCollectionClip, RoadtripMatch, RoadtripOuting


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Destination(id=1, name="NAS", type="local", base_path="/mnt/NAS"))
        session.flush()
        yield session


def clip(session, month="2026-06", filename="DVR00001.MP4", *, known_media=True):
    item = None
    if known_media:
        item = MediaItem(path=f"/camera/{month}/{filename}", filename=filename,
            kind="video", capture_time=dt.datetime(2020, 1, 1, 10), duration_s=600,
            checksum=month + filename, size_bytes=123)
        session.add(item)
        session.flush()
    ledger = UploadedClip(destination_id=1, source_media_id=item.id if item else None,
        filename=filename, checksum=month + filename, status="done", size_bytes=123,
        remote_path=month.replace("-", "/") + "/" + filename)
    session.add(ledger)
    session.flush()
    archived = ArchiveFile(destination_id=1, root_path="/mnt/NAS", path=ledger.remote_path,
        kind="video", ledger_id=ledger.id, size_bytes=123)
    session.add(archived)
    session.flush()
    return f"media:{item.id}" if item else f"archive:{archived.id}", item, ledger, archived


def match(session, clip_id, item, archived, month="2026-06", *, status="confirmed"):
    outing = RoadtripOuting(signature=month + clip_id, source="ha:test", month=month,
        start_time=dt.datetime(2026, 6, 4, 10), end_time=dt.datetime(2026, 6, 4, 11),
        duration_s=3600, distance_m=12000, point_count=4, complete=True)
    session.add(outing)
    session.flush()
    row = RoadtripMatch(month=month, group_key=clip_id, outing_id=outing.id, name="Day ride", status=status,
        mapping=json.dumps({"start_time": f"{month}-04T10:00:00Z", "clip_offsets": {},
            "group": {"relative_timing_available": False, "clips": [
                {"id": clip_id, "archive_id": archived.id, "media_id": item.id if item else None,
                 "filename": archived.path, "duration_s": 600}]}}))
    session.add(row)
    session.flush()
    return row


def test_collection_spans_months_without_rewriting_files_or_dates(session, monkeypatch):
    a, item_a, _, _ = clip(session)
    b, item_b, _, _ = clip(session, "2026-07")
    original = [item_a.capture_time, item_b.capture_time]
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("Grouping must not read video/GPS files"))
    monkeypatch.setattr(Path, "stat", lambda *a, **k: pytest.fail("Grouping must not stat the NAS"))
    result = trips.save_collection(session, "Summer road trip", [a, b])
    assert result["clip_ids"] == [a, b]
    assert result["months"] == ["2026-06", "2026-07"]
    assert result["size_bytes"] == 246 and result["duration_s"] == 1200
    assert result["start_time"] is None and result["matched_clip_count"] == 0
    assert [item_a.capture_time, item_b.capture_time] == original
    assert all(c["date_verified"] is False and c["location_status"] == "unmatched" for c in result["clips"])


def test_library_paginates_filters_and_includes_unknown_month(session):
    clip(session)
    clip(session, "2026-07")
    session.add(ArchiveFile(destination_id=1, root_path="/mnt/NAS", path="Unsorted/old.mp4", kind="video"))
    session.flush()
    result = trips.library(session, limit=1)
    assert result["total"] == 3 and result["has_more"] and len(result["clips"]) == 1
    result = trips.library(session, "2026-06")
    assert result["total"] == 1
    unknown = next(c for c in trips.library(session)["clips"] if c["month"] is None)
    assert unknown["size_bytes"] is None and unknown["month_basis"] == "unknown"
    for args in [("2026-99", 0, 1), (None, -1, 1), (None, 0, 10001)]:
        with pytest.raises(ValueError):
            trips.library(session, *args)


def test_archive_and_ledger_without_media_are_not_counted_twice(session):
    identity, _, _, _ = clip(session, known_media=False)
    result = trips.library(session)
    assert result["total"] == 1 and result["clips"][0]["id"] == identity


def test_confirmed_match_supplies_dates_but_unconfirmed_does_not(session):
    a, item, _, archived = clip(session)
    saved = match(session, a, item, archived)
    result = trips.save_collection(session, "Verified association", [a])
    assert result["match_ids"] == [saved.id]
    assert result["start_time"] == "2026-06-04T10:00:00Z"
    assert result["end_time"] == "2026-06-04T10:10:00Z"
    assert result["clips"][0]["observed_time"].startswith("2020-")
    saved.status = "rejected"
    session.flush()
    detail = trips.collection_detail(session, session.get(RoadtripCollection, result["id"]))
    assert detail["match_ids"] == [] and detail["start_time"] is None


def test_collection_supports_several_confirmed_days(session):
    a, item_a, _, archive_a = clip(session)
    b, item_b, _, archive_b = clip(session, "2026-07")
    first = match(session, a, item_a, archive_a)
    second = match(session, b, item_b, archive_b, "2026-07")
    result = trips.save_collection(session, "Summer", [a, b], [first.id, second.id])
    assert result["match_ids"] == [first.id, second.id]
    assert result["start_time"] == "2026-06-04T10:00:00Z"
    assert result["end_time"] == "2026-07-04T10:10:00Z"


def test_edit_replaces_membership_without_duplicates(session):
    a, _, _, _ = clip(session)
    b, _, _, _ = clip(session, "2026-07")
    result = trips.save_collection(session, "Original", [a, a, b])
    assert result["clip_count"] == 2
    row = session.get(RoadtripCollection, result["id"])
    edited = trips.save_collection(session, "Renamed", [b], row=row)
    assert edited["name"] == "Renamed" and edited["clip_ids"] == [b]
    assert session.query(RoadtripCollectionClip).count() == 1
    assert session.query(ArchiveFile).count() == 2 and session.query(UploadedClip).count() == 2


def test_collection_retains_unavailable_references_and_can_be_renamed(session):
    a, _, ledger, archive = clip(session)
    result = trips.save_collection(session, "Ride", [a])
    archive.present = False
    ledger.status = "missing"
    session.flush()
    row = session.get(RoadtripCollection, result["id"])
    result = trips.save_collection(session, "Ride renamed", [a], row=row)
    assert result["clips"][0]["filename"] == "DVR00001.MP4"
    assert result["unavailable_clip_count"] == 1


def test_media_deletion_recovers_archive_reference_and_confirmed_match(session):
    a, item, ledger, archive = clip(session)
    saved = match(session, a, item, archive)
    result = trips.save_collection(session, "Ride", [a])
    # Simulate the media row being removed (production FK uses SET NULL).
    ledger.source_media_id = None
    session.delete(item)
    session.flush()
    row = session.get(RoadtripCollection, result["id"])
    result = trips.collection_detail(session, row)
    assert result["unavailable_clip_count"] == 0
    assert result["clips"][0]["current_id"] == f"archive:{archive.id}"
    assert result["clip_ids"] == [a] and result["match_ids"] == [saved.id]
    assert trips.library(session)["clips"][0]["alignment"]["match_id"] == saved.id


def test_collection_rejects_unknown_ids_and_unrelated_routes(session):
    a, _, _, _ = clip(session)
    b, item_b, _, archive_b = clip(session, "2026-07")
    other = match(session, b, item_b, archive_b, "2026-07")
    for name, ids, matches in [(" ", [a], None), ("x", [], None), ("x", ["archive:999"], None),
            ("x", ["../../private"], None), ("x", [a], [other.id])]:
        with pytest.raises(ValueError):
            trips.save_collection(session, name, ids, matches)
    assert session.query(RoadtripCollection).count() == 0


def test_unknown_size_is_not_claimed_as_zero(session):
    a, _, ledger, archived = clip(session)
    ledger.size_bytes = 0
    archived.size_bytes = 0
    session.flush()
    result = trips.save_collection(session, "Unknown size", [a])
    assert result["size_bytes"] is None and result["unknown_size_count"] == 1
    assert result["known_size_bytes"] == 0


def test_aliases_of_same_source_do_not_duplicate_a_trip(session):
    identity, _, ledger, archive = clip(session)
    result = trips.save_collection(session, "One recording", [identity, f"archive:{archive.id}", f"ledger:{ledger.id}"])
    assert result["clip_count"] == 1


def test_conflicting_confirmed_dates_require_review_instead_of_arbitrary_bounds(session):
    identity, item, _, archived = clip(session)
    first = match(session, identity, item, archived)
    second = RoadtripMatch(month=first.month, group_key="other-subset", outing_id=first.outing_id,
        name="Conflicting day", status="confirmed", mapping=first.mapping.replace("10:00:00Z", "12:00:00Z"))
    session.add(second)
    session.flush()
    result = trips.save_collection(session, "Needs review", [identity])
    assert result["match_ids"] == [first.id, second.id]
    assert result["start_time"] is None and result["matched_clip_count"] == 0
    assert result["ambiguous_clip_count"] == 1
    assert result["clips"][0]["alignment"] is None
    assert result["clips"][0]["location_status"] == "ambiguous"


def test_different_outings_at_identical_times_are_ambiguous_but_equivalent_matches_are_not(session):
    identity, item, _, archived = clip(session)
    first = match(session, identity, item, archived)
    equivalent = RoadtripMatch(month=first.month, group_key="equivalent-subset", outing_id=first.outing_id,
        name="Same route and time", status="confirmed", mapping=first.mapping)
    session.add(equivalent)
    session.flush()
    result = trips.save_collection(session, "Equivalent", [identity])
    assert result["ambiguous_clip_count"] == 0 and result["matched_clip_count"] == 1
    other_outing = RoadtripOuting(signature="other-phone-same-time", source="file:other-phone",
        month=first.month, start_time=dt.datetime(2026, 6, 4, 10), end_time=dt.datetime(2026, 6, 4, 11),
        duration_s=3600, distance_m=10000, point_count=4, complete=True)
    session.add(other_outing)
    session.flush()
    conflicting = RoadtripMatch(month=first.month, group_key="conflicting-source", outing_id=other_outing.id,
        name="Another route at same time", status="confirmed", mapping=first.mapping)
    session.add(conflicting)
    session.flush()
    result = trips.collection_detail(session, session.get(RoadtripCollection, result["id"]))
    assert result["ambiguous_clip_count"] == 1 and result["start_time"] is None
    assert {a["source"] for a in result["clips"][0]["alignments"]} == {"ha:test", "file:other-phone"}


def test_collection_api_round_trip_and_delete_are_metadata_only(session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import get_session
    from app.routers.roadtrips import router

    identity, _, _, _ = clip(session)
    app = FastAPI()
    app.include_router(router, prefix="/api/roadtrips")
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        assert client.get("/api/roadtrips/files").json()["total"] == 1
        assert client.get("/api/roadtrips/library?month=2026-06").json()["total"] == 1
        response = client.post("/api/roadtrips/collections", json={"name": "Summer", "clip_ids": [identity]})
        assert response.status_code == 200, response.text
        created = response.json()
        route = f"/api/roadtrips/collections/{created['id']}"
        assert client.get(route).json()["clip_ids"] == [identity]
        assert client.get("/api/roadtrips/collections").json()["collections"][0]["name"] == "Summer"
        response = client.patch(route, json={"name": "Renamed", "clip_ids": [identity]})
        assert response.status_code == 200 and response.json()["name"] == "Renamed"
        assert client.post("/api/roadtrips/collections", json={"name": "Bad", "clip_ids": []}).status_code == 422
        assert client.post("/api/roadtrips/collections", json={"name": "Bad", "clip_ids": ["media:999"]}).status_code == 400
        assert client.delete(route).json()["deleted"]
        assert client.get(route).status_code == 404
    assert session.query(ArchiveFile).count() == 1 and session.query(MediaItem).count() == 1
