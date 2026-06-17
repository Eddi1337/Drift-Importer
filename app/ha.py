"""Home Assistant progress publishing."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .models import AppSettings

log = logging.getLogger("drift.ha")


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")


def publish_state(
    settings: AppSettings,
    entity_suffix: str,
    state: str | int | float,
    attributes: Optional[dict] = None,
) -> None:
    if not settings.ha_base_url or not settings.ha_token:
        return

    entity_id = f"sensor.{_slug(settings.ha_entity_prefix or 'drift_import')}_{_slug(entity_suffix)}"
    base_url = settings.ha_base_url.strip()
    if "://" not in base_url:
        base_url = "http://" + base_url
    url = base_url.rstrip("/") + f"/api/states/{entity_id}"
    headers = {
        "Authorization": f"Bearer {settings.ha_token}",
        "Content-Type": "application/json",
    }
    payload = {"state": state, "attributes": attributes or {}}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
    except Exception:  # noqa: BLE001
        log.exception("Failed to publish HA state for %s", entity_id)
