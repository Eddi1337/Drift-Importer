"""Helpers for loading and updating singleton app settings."""
from __future__ import annotations

from typing import Iterable

from .database import session_scope
from .models import AppSettings, utcnow


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


def get_app_settings(session=None) -> AppSettings:
    if session is not None:
        settings = session.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(id=1)
            session.add(settings)
            session.flush()
        return settings

    with session_scope() as s:
        settings = s.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(id=1)
            s.add(settings)
            s.flush()
        return settings


def app_settings_dict(settings: AppSettings) -> dict:
    return {
        "auto_import_on_connect": settings.auto_import_on_connect,
        "auto_upload_on_import": settings.auto_upload_on_import,
        "default_destination_ids": _parse_ids(settings.default_destination_ids),
        "ha_base_url": settings.ha_base_url or "",
        "ha_token": settings.ha_token or "",
        "ha_token_configured": bool(settings.ha_token),
        "ha_entity_prefix": settings.ha_entity_prefix or "drift_import",
    }


def encode_destination_ids(ids: Iterable[int]) -> str:
    clean = []
    for value in ids:
        try:
            clean.append(str(int(value)))
        except (TypeError, ValueError):
            continue
    return ",".join(clean)


def touch_settings(settings: AppSettings) -> None:
    settings.updated_at = utcnow()
