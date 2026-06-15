import datetime as dt

from app.destinations.base import join_remote, render_remote_dir


def test_render_year_month():
    when = dt.datetime(2024, 3, 7, 9, 0, 0)
    assert render_remote_dir("{year}/{month:02d}", when) == "2024/03"


def test_render_with_day():
    when = dt.datetime(2024, 12, 1)
    assert render_remote_dir("{year}/{month:02d}/{day:02d}", when) == "2024/12/01"


def test_render_handles_no_time():
    # Falls back to "now"; just make sure it doesn't raise and returns a string.
    assert isinstance(render_remote_dir("{year}", None), str)


def test_render_unknown_token_is_safe():
    assert render_remote_dir("static/path", dt.datetime(2024, 1, 1)) == "static/path"


def test_join_remote():
    assert join_remote("/base", "2024/03", "clip.mp4") == "/base/2024/03/clip.mp4"
    assert join_remote("/", "a", "b") == "/a/b"
    assert join_remote("/base/", "/sub/", "f.mp4") == "/base/sub/f.mp4"
