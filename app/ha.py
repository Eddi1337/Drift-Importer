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


def delete_entity(settings: AppSettings, full_entity_id: str) -> bool:
    """Delete a state from HA. Returns True only when one was actually removed
    (200); a 404 means it wasn't there, which is fine but not counted."""
    if not _configured(settings):
        return False
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.delete(
                f"{_base_url(settings)}/api/states/{full_entity_id}",
                headers=_headers(settings),
            )
            return resp.status_code == 200
    except Exception:  # noqa: BLE001
        log.warning("Failed to delete HA state %s", full_entity_id)
        return False


def prune_legacy_job_entities(settings: AppSettings, job_ids) -> int:
    """Remove the old per-upload-job (``..._job_<id>``) and ``..._uploads``
    entities that earlier versions published, leaving only the overall ones.

    Deletes specific entities by id (we know the job ids) rather than listing
    all of HA's states — some HA instances 500 on ``GET /api/states``.
    """
    if not _configured(settings):
        return 0
    prefix = _slug(settings.ha_entity_prefix or "drift_import")
    removed = 0
    if delete_entity(settings, f"sensor.{prefix}_uploads"):
        removed += 1
    for job_id in job_ids:
        if delete_entity(settings, f"sensor.{prefix}_job_{job_id}"):
            removed += 1
    return removed
