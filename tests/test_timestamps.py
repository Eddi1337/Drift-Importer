import datetime as dt

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
