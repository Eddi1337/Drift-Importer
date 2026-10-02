import datetime as dt
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.ha_publish import HAPublisher, compute_jobs_overview  # alias of jobs.jobs_overview
from app.models import Job


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def test_overview_progress_is_count_based_over_the_current_run():
    session = _session()
    base = dt.datetime.utcnow().replace(microsecond=0)
    session.add_all([
        # Historical completed job from an earlier run — must NOT count.
        Job(kind="upload", status="done", progress=1.0, created_at=base - dt.timedelta(hours=2)),
        # Current run: created from the oldest active job onwards.
        Job(kind="upload", status="running", progress=0.5, created_at=base),
        Job(kind="upload", status="running", progress=0.1, created_at=base + dt.timedelta(seconds=1)),
        Job(kind="upload", status="queued", progress=0.0, created_at=base + dt.timedelta(seconds=2)),
        Job(kind="upload", status="done", progress=1.0, created_at=base + dt.timedelta(seconds=3)),
        Job(kind="upload", status="error", progress=0.3, created_at=base + dt.timedelta(seconds=4)),
    ])
    session.commit()

    o = compute_jobs_overview(session)

    assert o["active"] == 3 and o["running"] == 2 and o["queued"] == 1
    assert o["done"] == 2 and o["error"] == 1
    assert o["status"] == "running"
    assert o["completed_in_run"] == 1  # only the in-run done job, not the historical one
    assert o["total_in_run"] == 4      # 3 active + 1 completed-in-run
    # (1 done + (0.5 + 0.1) running progress) / 4 = 0.4
    assert o["percent"] == 40


def test_overview_progress_counts_only_upload_jobs():
    session = _session()
    base = dt.datetime.utcnow().replace(microsecond=0)
    session.add_all([
        Job(kind="import", status="running", progress=0.8, created_at=base),
        Job(kind="thumbnail", status="queued", progress=0.0, created_at=base + dt.timedelta(seconds=1)),
        Job(kind="upload", status="running", progress=0.25, created_at=base + dt.timedelta(seconds=2)),
        Job(kind="upload", status="queued", progress=0.0, created_at=base + dt.timedelta(seconds=3)),
    ])
    session.commit()

    o = compute_jobs_overview(session)

    assert o["active"] == 4
    assert o["completed_in_run"] == 0
    assert o["total_in_run"] == 2
    assert o["percent"] == 12
    # The all-work bar includes imports and thumbnails as well as uploads.
    assert o["work_percent"] == 26


def test_overview_progress_is_zero_when_active_work_has_no_uploads():
    session = _session()
    base = dt.datetime.utcnow().replace(microsecond=0)
    session.add_all([
        Job(kind="import", status="running", progress=0.8, created_at=base),
        Job(kind="thumbnail", status="queued", progress=0.0, created_at=base + dt.timedelta(seconds=1)),
    ])
    session.commit()

    o = compute_jobs_overview(session)

    assert o["active"] == 2
    assert o["total_in_run"] == 0
    assert o["percent"] == 0
    assert o["work_percent"] == 40


def test_lingering_paused_job_does_not_pin_progress_near_100():
    """Regression: a paused job left over from an earlier flood used to anchor the
    'current run' back over thousands of finished jobs, so a freshly connected
    batch read ~99% instead of ~0%."""
    session = _session()
    flood = dt.datetime.utcnow().replace(microsecond=0) - dt.timedelta(hours=3)
    # An earlier batch: one job the user paused, plus 200 jobs that completed.
    session.add(Job(kind="upload", status="paused", progress=0.0, created_at=flood))
    for i in range(200):
        t = flood + dt.timedelta(seconds=i + 1)
        session.add(Job(kind="thumbnail", status="done", progress=1.0,
                        created_at=t, finished_at=t + dt.timedelta(seconds=1)))
    # Hours later the queue has drained; a new card is connected -> fresh jobs.
    fresh = flood + dt.timedelta(hours=3)
    for i in range(5):
        session.add(Job(kind="upload", status="queued", progress=0.0,
                        created_at=fresh + dt.timedelta(seconds=i)))
    session.commit()

    o = compute_jobs_overview(session)

    # The new batch hasn't uploaded anything yet.
    assert o["percent"] == 0
    assert o["completed_in_run"] == 0
    assert o["total_in_run"] == 5


def test_overview_idle_when_nothing_active():
    session = _session()
    session.add(Job(kind="upload", status="done", progress=1.0))
    session.commit()

    o = compute_jobs_overview(session)

    assert o["active"] == 0
    assert o["status"] == "idle"
    assert o["percent"] == 100


def test_ha_publisher_refreshes_unchanged_state_periodically():
    publisher = HAPublisher()
    snapshot = (100, "idle", 0, 0, False, None)

    assert publisher._should_publish(snapshot, 100.0)
    publisher._last_published = snapshot
    publisher._last_publish_time = 100.0

    assert not publisher._should_publish(snapshot, 120.0)
    assert publisher._should_publish(snapshot, 160.0)


def test_ha_publish_logs_actionable_message_for_rejected_token(monkeypatch, caplog):
    from app import ha
    from app.models import AppSettings

    settings = AppSettings(ha_base_url="http://ha.local:8123", ha_token="invalid")
    request = httpx.Request("POST", "http://ha.local:8123/api/states/sensor.drift_import_progress")
    response = httpx.Response(401, request=request)

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(ha.httpx, "Client", lambda **kwargs: Client())

    ha.publish_state(settings, "progress", 0)

    assert "create a new long-lived access token" in caplog.text


@pytest.mark.parametrize("kind", ["upload", "import", "verify", "trip", "thumbnail"])
def test_publisher_counts_all_task_types_and_exposes_running_gate(monkeypatch, kind):
    from app import ha_publish
    from app.models import AppSettings

    session = _session()
    base = dt.datetime.utcnow()
    session.add_all([
        Job(kind=kind, status="running", progress=0.5, created_at=base, description="Current task"),
        Job(kind="upload", status="queued", created_at=base + dt.timedelta(seconds=1)),
    ])
    session.commit()

    @contextmanager
    def scope():
        yield session

    monkeypatch.setattr(ha_publish, "session_scope", scope)
    monkeypatch.setattr(ha_publish, "get_app_settings", lambda s: AppSettings(
        ha_base_url="http://ha.local:8123", ha_token="test", ha_entity_prefix="drift_import"))
    monkeypatch.setattr(ha_publish, "camera_status", lambda: (True, "Camera"))
    calls = {}

    def publish(prefs, suffix, state, attributes, **kwargs):
        calls[suffix] = (state, attributes, kwargs)
        return True

    monkeypatch.setattr(ha_publish.ha, "publish_state", publish)
    publisher = HAPublisher()
    publisher._pruned = True
    publisher._tick()
    assert calls["progress"][0] == 25
    assert calls["progress"][1]["task_kinds"] == [kind]
    assert calls["progress"][1]["task_detail"] == "Current task"
    assert calls["active"][0] == "on"
    assert calls["active"][2]["domain"] == "binary_sensor"

    # A queue alone, a paused task, or completed tasks must hide the card.
    for status in ["queued", "paused", "done"]:
        for job in session.query(Job).all():
            job.status = status
        session.commit()
        publisher._tick()
        assert calls["active"][0] == "off"

    # Network failure must leave the snapshot dirty so the next tick retries.
    publisher._last_published = None
    monkeypatch.setattr(ha_publish.ha, "publish_state", lambda *a, **k: False)
    publisher._tick()
    assert publisher._last_published is None


def test_ha_token_is_encrypted_masked_and_preserved_when_settings_saved(tmp_path, monkeypatch):
    from app import config, crypto, ha
    from app.models import AppSettings
    from app.routers.api import AppSettingsReq, update_settings
    from app.settings_store import app_settings_dict, get_app_settings, get_ha_token

    monkeypatch.setenv("DRIFT_DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    crypto._fernet.cache_clear()
    try:
        session = _session()
        session.add(AppSettings(id=1, ha_token="legacy-test-token"))
        session.commit()
        prefs = get_app_settings(session)
        session.commit()
        assert prefs.ha_token.startswith("fernet:")
        assert "legacy-test-token" not in prefs.ha_token
        assert get_ha_token(prefs) == "legacy-test-token"
        assert app_settings_dict(prefs)["ha_token"] == ""
        assert app_settings_dict(prefs)["ha_token_configured"] is True
        assert ha._headers(prefs)["Authorization"] == "Bearer legacy-test-token"
        encrypted = prefs.ha_token
        update_settings(AppSettingsReq(ha_base_url="http://ha.local:8123"), session)
        assert prefs.ha_token == encrypted
        result = update_settings(AppSettingsReq(ha_token="replacement-test-token"), session)
        assert result["ha_token"] == ""
        assert get_ha_token(prefs) == "replacement-test-token"
        update_settings(AppSettingsReq(clear_ha_token=True), session)
        assert prefs.ha_token is None
    finally:
        config.get_settings.cache_clear()
        crypto._fernet.cache_clear()
