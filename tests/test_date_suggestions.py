"""Suggested dates remain evidence-labelled proposals, never approvals."""
import datetime as dt

from app.date_review import date_suggestions
from app.models import RecordingDate


NOW = dt.datetime(2026, 10, 5, 10, tzinfo=dt.timezone.utc)


def clip(number, time, *, folder=107, status="review", corrected=None, event=False):
    path = f"/camera/EVENT/E_DVR{number:05}.MP4" if event else f"/camera/DCIM/{folder}MEDIA/DVR{number:05}.MP4"
    return RecordingDate(id=number + (10000 if event else 0), path=path, original_time=time,
                         corrected_time=corrected, duration_s=300, time_source="metadata",
                         status=status, reasons='["Clock needs review"]')


def test_approved_neighbours_offer_offset_without_confirming_recording():
    before = clip(1, dt.datetime(2026, 9, 20, 10), status="confirmed", corrected=dt.datetime(2026, 10, 4, 10))
    middle = clip(2, dt.datetime(2026, 9, 20, 10, 5))
    after = clip(3, dt.datetime(2026, 9, 20, 10, 10), status="confirmed", corrected=dt.datetime(2026, 10, 4, 10, 10))
    result = date_suggestions([after, middle, before], NOW)
    assert result[middle.id]["suggested_time"] == "2026-10-04T10:05:00+00:00"
    assert result[middle.id]["suggestion_confidence"] == "high"
    assert result[middle.id]["suggestion_anchor_ids"] == [before.id, after.id]
    assert result[middle.id]["suggested_local_time"].endswith("+01:00")
    assert middle.status == "review" and middle.corrected_time is None
    assert before.id not in result and after.id not in result


def test_bad_middle_timestamp_can_be_bounded_by_consecutive_filename_durations():
    before = clip(1, dt.datetime(2026, 10, 4, 10), status="trusted")
    middle = clip(2, dt.datetime(2020, 1, 1))
    after = clip(3, dt.datetime(2026, 10, 4, 10, 10), status="trusted")
    result = date_suggestions([before, middle, after], NOW)[middle.id]
    assert result["suggested_time"] == "2026-10-04T10:05:00+00:00"
    assert result["suggestion_confidence"] == "medium"
    assert "durations" in result["suggestion_reason"]


def test_counter_gap_does_not_invent_uninterrupted_recording():
    before = clip(1, dt.datetime(2026, 10, 4, 10), status="trusted")
    middle = clip(12, dt.datetime(2020, 1, 1))
    after = clip(20, dt.datetime(2026, 10, 4, 12), status="trusted")
    result = date_suggestions([before, middle, after], NOW)[middle.id]
    assert "Bounded" not in result["suggestion_reason"]


def test_clock_reset_does_not_inherit_previous_approved_offset():
    before = clip(1, dt.datetime(2026, 9, 20, 10), status="confirmed", corrected=dt.datetime(2026, 10, 4, 10))
    reset = clip(2, dt.datetime(2020, 1, 1))
    result = date_suggestions([before, reset], NOW)[reset.id]
    assert result["suggestion_confidence"] == "low"
    assert result["suggestion_anchor_ids"] == []
    assert "connected" in result["suggestion_reason"]


def test_disagreeing_approved_offsets_do_not_pick_an_anchor():
    before = clip(1, dt.datetime(2026, 9, 20, 10), status="confirmed", corrected=dt.datetime(2026, 10, 3, 10))
    middle = clip(2, dt.datetime(2026, 9, 20, 10, 5))
    after = clip(3, dt.datetime(2026, 9, 20, 10, 10), status="confirmed", corrected=dt.datetime(2026, 10, 4, 10, 10))
    result = date_suggestions([before, middle, after], NOW)[middle.id]
    assert result["suggestion_confidence"] == "low"
    assert result["suggestion_anchor_ids"] == []


def test_event_date_matches_unique_counter_with_header_evidence():
    original = dt.datetime(2026, 9, 20, 10)
    recording = clip(1, original, status="confirmed", corrected=dt.datetime(2026, 10, 4, 10))
    event = clip(1, original, event=True)
    result = date_suggestions([recording, event], NOW)[event.id]
    assert result["suggested_time"] == "2026-10-04T10:00:00+00:00"
    assert result["suggestion_confidence"] == "high"
    # The counter alone is ambiguous after folder rollover.
    other = clip(1, original, folder=108)
    other.id = 90
    result = date_suggestions([recording, other, event], NOW)[event.id]
    assert result["suggestion_confidence"] == "low"
    assert result["suggestion_anchor_ids"] == []


def test_approved_offset_disproved_by_connection_time_is_not_repeated():
    # Real case: DVR00935 was confirmed 63 minutes late, so reusing its offset
    # ended today's ride after the camera was plugged in.
    offset = dt.timedelta(days=14, hours=3, minutes=35)
    approved = clip(935, dt.datetime(2026, 9, 20, 14, 36), status="confirmed", corrected=dt.datetime(2026, 9, 20, 14, 36) + offset)
    ride = clip(990, dt.datetime(2026, 10, 22, 4, 22))
    newest = clip(29, dt.datetime(2026, 10, 22, 7, 39), folder=108)
    newest.first_seen = dt.datetime(2026, 11, 5, 10, 19)
    result = date_suggestions([approved, ride, newest], dt.datetime(2026, 11, 5, 10, 25, tzinfo=dt.timezone.utc))
    assert result[newest.id]["suggested_time"] == "2026-11-05T10:14:00+00:00"
    assert result[ride.id]["suggested_time"] == "2026-11-05T06:57:00+00:00"
    assert result[ride.id]["suggestion_confidence"] == "low"
    assert "too late" in result[ride.id]["suggestion_reason"]


def test_consistent_approved_offset_is_kept_when_camera_connected_later():
    offset = dt.timedelta(days=14)
    approved = clip(1, dt.datetime(2026, 9, 20, 10), status="confirmed", corrected=dt.datetime(2026, 9, 20, 10) + offset)
    newest = clip(2, dt.datetime(2026, 9, 20, 18))
    newest.first_seen = dt.datetime(2026, 10, 5, 9)
    result = date_suggestions([approved, newest], NOW)[newest.id]
    assert result["suggested_time"] == "2026-10-04T18:00:00+00:00"
    assert result["suggestion_confidence"] == "medium"


def test_later_reconnect_does_not_move_the_connection_estimate():
    newest = clip(1, dt.datetime(2020, 1, 1, 10))
    newest.first_seen = dt.datetime(2026, 10, 4, 12, 5)
    result = date_suggestions([newest], NOW)[newest.id]
    assert result["suggested_time"] == "2026-10-04T12:00:00+00:00"
