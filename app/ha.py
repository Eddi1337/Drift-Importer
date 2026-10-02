"""Home Assistant state publishing.

Drift exposes only a small, overall picture to HA — overall task progress,
status, and whether the camera is connected — not one entity per job. The
helpers here also let us clean up the legacy per-job/uploads entities that
older versions used to publish.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .models import AppSettings
from .settings_store import get_ha_token

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
        "Authorization": f"Bearer {get_ha_token(settings)}",
        "Content-Type": "application/json",
    }


def entity_id(settings: AppSettings, entity_suffix: str, domain: str = "sensor") -> str:
    prefix = _slug(settings.ha_entity_prefix or "drift_import")
    return f"{domain}.{prefix}_{_slug(entity_suffix)}"


def publish_state(
    settings: AppSettings,
    entity_suffix: str,
    state: str | int | float,
    attributes: Optional[dict] = None,
    *,
    domain: str = "sensor",
) -> bool:
    if not _configured(settings):
        return False
    full_entity_id = entity_id(settings, entity_suffix, domain)
    url = f"{_base_url(settings)}/api/states/{full_entity_id}"
    payload = {"state": state, "attributes": attributes or {}}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, headers=_headers(settings), json=payload)
            resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            log.error(
                "Home Assistant rejected the configured API token while publishing %s; "
                "create a new long-lived access token and update Drift settings",
                full_entity_id,
            )
        else:
            log.exception("Failed to publish HA state for %s", full_entity_id)
    except Exception:  # noqa: BLE001
        log.exception("Failed to publish HA state for %s", full_entity_id)
    return False


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
