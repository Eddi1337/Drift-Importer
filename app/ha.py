"""Home Assistant state publishing.

Drift exposes only a small, overall picture to HA — overall upload progress,
status, and whether the camera is connected — not one entity per job. The
helpers here also let us clean up the legacy per-job/uploads entities that
older versions used to publish.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .models import AppSettings

log = logging.getLogger("drift.ha")


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def _configured(settings: AppSettings) -> bool:
    return bool(settings.ha_base_url and settings.ha_token)


def _base_url(settings: AppSettings) -> str:
    base_url = (settings.ha_base_url or "").strip()
    if "://" not in base_url:
        base_url = "http://" + base_url
    return base_url.rstrip("/")


def _headers(settings: AppSettings) -> dict:
    return {
        "Authorization": f"Bearer {settings.ha_token}",
        "Content-Type": "application/json",
    }


def entity_id(settings: AppSettings, entity_suffix: str) -> str:
    prefix = _slug(settings.ha_entity_prefix or "drift_import")
    return f"sensor.{prefix}_{_slug(entity_suffix)}"


def publish_state(
    settings: AppSettings,
    entity_suffix: str,
    state: str | int | float,
    attributes: Optional[dict] = None,
) -> None:
    if not _configured(settings):
        return
    url = f"{_base_url(settings)}/api/states/{entity_id(settings, entity_suffix)}"
    payload = {"state": state, "attributes": attributes or {}}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, headers=_headers(settings), json=payload)
            resp.raise_for_status()
    except Exception:  # noqa: BLE001
        log.exception("Failed to publish HA state for %s", entity_id(settings, entity_suffix))


def list_entity_ids(settings: AppSettings) -> list[str]:
    """Return all entity_ids currently in HA's state machine (best effort)."""
    if not _configured(settings):
        return []
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{_base_url(settings)}/api/states", headers=_headers(settings))
            resp.raise_for_status()
            return [str(row.get("entity_id", "")) for row in resp.json()]
    except Exception:  # noqa: BLE001
        log.exception("Failed to list HA states")
        return []


def delete_entity(settings: AppSettings, full_entity_id: str) -> bool:
    if not _configured(settings):
        return False
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.delete(
                f"{_base_url(settings)}/api/states/{full_entity_id}",
                headers=_headers(settings),
            )
            # 200 = removed, 404 = already gone; both are fine.
            return resp.status_code in (200, 404)
    except Exception:  # noqa: BLE001
        log.exception("Failed to delete HA state %s", full_entity_id)
        return False


def prune_legacy_job_entities(settings: AppSettings) -> int:
    """Remove the old per-upload-job (``..._job_<id>``) and ``..._uploads``
    entities that earlier versions published, leaving only the overall ones."""
    if not _configured(settings):
        return 0
    prefix = _slug(settings.ha_entity_prefix or "drift_import")
    job_prefix = f"sensor.{prefix}_job_"
    uploads_id = f"sensor.{prefix}_uploads"
    removed = 0
    for eid in list_entity_ids(settings):
        if eid.startswith(job_prefix) or eid == uploads_id:
            if delete_entity(settings, eid):
                removed += 1
    return removed
