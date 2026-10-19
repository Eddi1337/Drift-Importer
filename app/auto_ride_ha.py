"""Bounded, relevant-day HA refreshes for newly archived camera footage."""
from __future__ import annotations

import datetime as dt
import json
import math
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import InvalidToken

from . import auto_rides as rides
from . import roadtrips as rt
from .auto_ride_models import AutoRideHARefresh
from .ha import _base_url, _headers
from .roadtrip_models import LocationDay
from .settings_store import get_app_settings

REFRESH_AFTER_S = 30 * 60
MAX_HOME_BYTES = 64 * 1024


def _plan(session, now):
    prefs = rt.preferences(session)
    source = "ha:" + prefs.entity_id if prefs.entity_id else ""
    if not source:
        return source, [], 0
    zone = ZoneInfo(prefs.timezone)
    today = now.astimezone(zone).date()
    relevant = {}
    for clip in rides.eligible_clips(session, now=now)[0]:
        end = rt.timestamp(clip["end_time"])
        day = rt.timestamp(clip["start_time"]).astimezone(zone).date()
        last = end.astimezone(zone).date()
        while day <= last:
            key = day.isoformat()
            relevant[key] = max(end, relevant.get(key, end))
            day += dt.timedelta(days=1)
    cached = {row.day: row for row in session.query(LocationDay).filter_by(source=source)}
    checks = {row.day: row for row in session.query(AutoRideHARefresh).filter_by(source=source)}
    wanted, skipped = {}, 0
    for key, end in relevant.items():
        row, check = cached.get(key), checks.get(key)
        date = dt.date.fromisoformat(key)
        if row and date < today - dt.timedelta(days=1):
            skipped += 1  # populated and known-empty historical days are stable
            continue
        checked = rt.timestamp(check.checked_at if check else row.imported_at) if check or row else None
        if row is None or checked is None or end > checked or (now - checked).total_seconds() >= REFRESH_AFTER_S:
            wanted[key] = {"day": key, "latest_clip_end": end, "primary": True}
        else:
            skipped += 1
    # A missing neighbouring local day may contain the departure/return. Never
    # refetch a known-empty historical boundary or request a future day.
    for key in relevant:
        day = dt.date.fromisoformat(key)
        for neighbour in (day - dt.timedelta(days=1), day + dt.timedelta(days=1)):
            value = neighbour.isoformat()
            if neighbour <= today and value not in cached and value not in wanted:
                wanted[value] = {"day": value, "latest_clip_end": None, "primary": value in relevant}
    return source, sorted(wanted.values(), key=lambda value: (not value["primary"], -dt.date.fromisoformat(value["day"]).toordinal())), skipped


def pending_refresh_days(session, *, now=None):
    """Metadata-only monitor hook, including the 30-minute recent-day timer."""
    settings = get_app_settings(session)
    if not settings.ha_base_url or not settings.ha_token:
        return []
    source, planned, _ = _plan(session, rt.timestamp(now or rt.utcnow()))
    rebuild = [row.day for row in session.query(AutoRideHARefresh).filter_by(source=source, result="rebuild_pending")]
    return sorted({value["day"] for value in planned} | set(rebuild))


def _fetch_day(client, settings, prefs, day, now, ctx):
    local = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), ZoneInfo(prefs.timezone))
    start, end = local.astimezone(rt.UTC), (local + dt.timedelta(days=1)).astimezone(rt.UTC)
    with client.stream("GET", _base_url(settings) + "/api/history/period/" + rt.iso(start), headers=_headers(settings),
            params={"filter_entity_id": prefs.entity_id, "end_time": rt.iso(min(end, now)), "significant_changes_only": "0"}) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes(65536):
            if ctx:
                rt._cancel(ctx)
            body.extend(chunk)
            if len(body) > rt.MAX_IMPORT_BYTES:
                raise ValueError("Location day exceeds the 16 MB processing limit")
    raw = json.loads(body)
    if not isinstance(raw, list) or any(not isinstance(group, list) for group in raw):
        raise ValueError("Unsupported history response")
    points, unusable = [], 0
    for group in raw:
        for value in group:
            point = rt.normalize_point(value, prefs.timezone) if isinstance(value, dict) else None
            if point is not None and start <= rt.timestamp(point["t"]) < end and rt.timestamp(point["t"]) <= now:
                points.append(point)
            else:
                unusable += 1
    clean, rejected = rt.clean_points(points, prefs)
    return clean, rejected + unusable


def _fetch_home(client, settings, ctx):
    """Read only HA's home entity, with a small independent response limit."""
    with client.stream("GET", _base_url(settings) + "/api/states/zone.home",
            headers=_headers(settings), timeout=20) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes(4096):
            if ctx:
                rt._cancel(ctx)
            body.extend(chunk)
            if len(body) > MAX_HOME_BYTES:
                raise ValueError("Home zone response exceeds the processing limit")
    value = json.loads(body)
    if not isinstance(value, dict) or value.get("entity_id") != "zone.home" or value.get("state") in ("unknown", "unavailable"):
        raise ValueError("Home zone is unavailable")
    attrs = value.get("attributes")
    if not isinstance(attrs, dict):
        raise ValueError("Home zone has no valid attributes")
    lat, lon, radius = attrs.get("latitude"), attrs.get("longitude"), attrs.get("radius", 150)
    if any(type(number) not in (int, float) or not math.isfinite(number) for number in (lat, lon, radius)):
        raise ValueError("Home zone has invalid coordinates or radius")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or not 20 <= radius <= 2000:
        raise ValueError("Home zone coordinates or radius are out of range")
    return lat, lon, radius


def refresh_ha_days(session, ctx=None, *, now=None, max_days=4):
    """Commit each checked day separately so network waits hold no write lock."""
    if not 0 <= max_days <= 7:
        raise ValueError("Refresh between zero and seven location days at a time")
    now = rt.timestamp(now or rt.utcnow())
    settings, prefs = get_app_settings(session), rt.preferences(session)
    report = {"imported_days": 0, "imported_day_keys": [], "pending_days": 0, "empty_days": 0,
        "preserved_days": 0, "unchanged_days": 0, "skipped_cached_days": 0, "checked_days": 0,
        "changed_months": [], "errors": [], "configured": bool(settings.ha_base_url and settings.ha_token and prefs.entity_id)}
    if not report["configured"]:
        return report
    source, wanted, skipped = _plan(session, now)
    changed = {row.day[:7] for row in session.query(AutoRideHARefresh).filter_by(source=source, result="rebuild_pending")}
    report["source"], report["skipped_cached_days"] = source, skipped
    report["pending_days"] = max(0, len(wanted) - max_days)
    if not wanted and not changed or not max_days:
        return report
    try:
        rt._cache_root()  # fail before HTTP or any fallback-directory write
    except (RuntimeError, OSError, ValueError):
        report["errors"].append({"message": "The mounted NAS is unavailable; automatic HA refresh is deferred."})
        report["pending_days"] = len(wanted)
        return report
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        if prefs.home_lat is None or prefs.home_lon is None:
            try:
                if ctx:
                    rt._cancel(ctx)
                home = _fetch_home(client, settings, ctx)
                prefs.home_lat, prefs.home_lon, prefs.home_radius_m = home
                session.commit()  # Persist home before history HTTP or rebuilding.
            except httpx.HTTPStatusError as exc:
                session.rollback()
                report["errors"].append({"message": f"Home Assistant home zone returned HTTP {exc.response.status_code}; automatic HA refresh is deferred."})
            except (httpx.HTTPError, InvalidToken, ValueError, RuntimeError, OSError, KeyError, TypeError):
                session.rollback()
                report["errors"].append({"message": "Home Assistant home zone could not be loaded; automatic HA refresh is deferred."})
            if report["errors"]:
                report["pending_days"] = len(wanted)
                report["pending_rebuild_months"] = sorted(changed)
                return report
        for value in wanted[:max_days]:
            if ctx:
                rt._cancel(ctx)
            day = value["day"]
            # Save/merge on this calling session, but release its write lock
            # before the next HTTP request. Root calls this before discovery.
            try:
                clean, rejected = _fetch_day(client, settings, prefs, day, now, ctx)
                existing = session.query(LocationDay).filter_by(source=source, day=day).one_or_none()
                if clean:
                    old = rt._read_day(existing, rt._cache_root()) if existing and existing.point_count else []
                    merged = rt.clean_points(list(({p["t"]: p for p in old} | {p["t"]: p for p in clean}).values()), prefs)[0]
                    if merged != old:
                        rt.save_day(session, source, day, merged, rejected)
                        changed.add(day[:7])
                        report["imported_days"] += 1
                        report["imported_day_keys"].append(day)
                        result = "rebuild_pending"
                    else:
                        report["unchanged_days"] += 1
                        result = "unchanged"
                elif existing and existing.point_count:
                    report["preserved_days"] += 1
                    result = "preserved"
                elif existing is None:
                    rt.save_day(session, source, day, [], rejected)
                    report["empty_days"] += 1
                    result = "empty"
                else:
                    report["empty_days"] += 1
                    result = "empty"
                check = session.get(AutoRideHARefresh, (source, day))
                if check is None:
                    check = AutoRideHARefresh(source=source, day=day)
                    session.add(check)
                check.checked_at, check.latest_clip_end, check.result = now, value["latest_clip_end"], result
                session.commit()
                report["checked_days"] += 1
            except httpx.HTTPStatusError as exc:
                session.rollback()
                report["errors"].append({"day": day, "message": f"Home Assistant history returned HTTP {exc.response.status_code}."})
            except (httpx.HTTPError, InvalidToken, ValueError, RuntimeError, OSError, KeyError, TypeError):
                session.rollback()
                report["errors"].append({"day": day, "message": "Location history could not be refreshed; existing GPS was preserved."})
    for month in sorted(changed):
        try:
            rt.rebuild_outings(session, month, source)
            for row in session.query(AutoRideHARefresh).filter(AutoRideHARefresh.source == source,
                    AutoRideHARefresh.day.like(month + "-%"), AutoRideHARefresh.result == "rebuild_pending"):
                row.result = "updated"
            session.commit()
        except (ValueError, RuntimeError, OSError):
            session.rollback()
            report["errors"].append({"month": month, "message": "GPS was saved, but journey rebuilding needs another refresh."})
    report["changed_months"] = sorted(changed)
    report["pending_rebuild_months"] = sorted({row.day[:7] for row in session.query(AutoRideHARefresh).filter_by(source=source, result="rebuild_pending")})
    return report
