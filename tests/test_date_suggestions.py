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
