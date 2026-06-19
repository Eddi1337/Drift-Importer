import datetime as dt

from app.media import _parse_time
from app.timestamps import shift_datetime


def test_shift_positive():
    base = dt.datetime(2023, 1, 1, 12, 0, 0)
    out = shift_datetime(base, hours=3, minutes=12)
    assert out == dt.datetime(2023, 1, 1, 15, 12, 0)


def test_shift_negative_across_day():
    base = dt.datetime(2023, 1, 1, 1, 0, 0)
    out = shift_datetime(base, hours=-3)
    assert out == dt.datetime(2022, 12, 31, 22, 0, 0)


def test_shift_days():
    base = dt.datetime(2024, 2, 28, 0, 0, 0)
    out = shift_datetime(base, days=1)
    assert out == dt.datetime(2024, 2, 29, 0, 0, 0)  # leap year


def test_parse_metadata_time_with_timezone_offset():
    out = _parse_time("2026-05-31T23:30:00+0100")

    assert out == dt.datetime(2026, 5, 31, 22, 30, 0)


def test_parse_exif_style_metadata_time():
    out = _parse_time("2026:06:01 09:15:00")

    assert out == dt.datetime(2026, 6, 1, 9, 15, 0)
