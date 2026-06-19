import datetime as dt

from app.tasks import UploadPlanRow, _month_upload_groups, _month_label, _month_upload_plan


def test_month_upload_plan_orders_by_capture_month_then_time():
    rows = [
        UploadPlanRow(4, "undated.mp4", None),
        UploadPlanRow(2, "feb-late.mp4", dt.datetime(2026, 2, 20, 9, 0, 0)),
        UploadPlanRow(3, "jan.mp4", dt.datetime(2026, 1, 31, 18, 0, 0)),
        UploadPlanRow(1, "feb-early.mp4", dt.datetime(2026, 2, 1, 8, 0, 0)),
    ]

    planned = _month_upload_plan(rows)

    assert [row.media_id for row in planned] == [3, 1, 2, 4]
    assert [_month_label(row.capture_time) for row in planned] == [
        "January 2026",
        "February 2026",
        "February 2026",
        "Undated",
    ]


def test_month_upload_groups_batch_media_by_month():
    rows = [
        UploadPlanRow(1, "feb-early.mp4", dt.datetime(2026, 2, 1, 8, 0, 0)),
        UploadPlanRow(2, "jan.mp4", dt.datetime(2026, 1, 31, 18, 0, 0)),
        UploadPlanRow(3, "feb-late.mp4", dt.datetime(2026, 2, 20, 9, 0, 0)),
    ]

    groups = _month_upload_groups(rows)

    assert [(label, [row.media_id for row in group]) for label, group in groups] == [
        ("January 2026", [2]),
        ("February 2026", [1, 3]),
    ]
