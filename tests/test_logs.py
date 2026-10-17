from app.routers import api
import logging


def test_memory_log_view_is_bounded_and_does_not_read_old_disk_logs(monkeypatch):
    from app.recent_logs import recent_logs
    monkeypatch.setattr(api, "get_settings", lambda: type("Settings", (), {"log_to_file": False})())
    recent_logs.lines.clear()
    for i in range(1100):
        recent_logs.handle(logging.LogRecord("drift", logging.WARNING, "", 1, f"warning {i}", (), None))
    assert len(recent_logs.lines) == 1000
    payload = api._read_log_lines(limit=2, min_level="WARNING")
    assert payload["log_file"].startswith("Memory")
    assert [row["message"] for row in payload["lines"]] == ["warning 1098", "warning 1099"]
    assert api._read_log_lines(limit=2, min_level="ERROR")["lines"] == []
    recent_logs.lines.clear()


def test_read_log_lines_filters_info_and_above(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "drift.log").write_text(
        "\n".join(
            [
                "2026-06-19 10:00:00 DEBUG drift: hidden",
                "2026-06-19 10:01:00 INFO drift: imported",
                "2026-06-19 10:02:00 WARNING drift: slow",
                "2026-06-19 10:03:00 ERROR drift: failed",
            ]
        )
    )

    settings = type("Settings", (), {"log_dir": log_dir})()

    monkeypatch.setattr(api, "get_settings", lambda: settings)

    payload = api._read_log_lines(limit=10, min_level="INFO")

    messages = [row["message"] for row in payload["lines"]]
    assert len(messages) == 3
    assert all("DEBUG" not in message for message in messages)
    assert any("INFO" in message for message in messages)
    assert any("WARNING" in message for message in messages)
    assert any("ERROR" in message for message in messages)


def test_read_log_lines_respects_limit(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "drift.log").write_text("\n".join(f"INFO line {i}" for i in range(5)))

    settings = type("Settings", (), {"log_dir": log_dir})()

    monkeypatch.setattr(api, "get_settings", lambda: settings)

    payload = api._read_log_lines(limit=2, min_level="INFO")

    assert [row["message"] for row in payload["lines"]] == ["INFO line 3", "INFO line 4"]
