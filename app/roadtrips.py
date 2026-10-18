"""Bounded, advisory camera/GPS matching without rewriting camera dates.

Location records are sensitive: raw points are private NAS files, never logs or
SQLite rows. Only one location job runs at a time and HA is fetched one day at a
time. Distances never bridge missing GPS; speed is based on covered clip time.
"""
from __future__ import annotations

import calendar
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
import threading
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func

from .config import get_settings
from .database import session_scope
from .ha import _base_url, _headers
from .jobs import JobCancelled, handler
from .models import AlbumItem, Job, MediaItem, UploadedClip, utcnow
from .roadtrip_models import LocationDay, RoadtripClipMetadata, RoadtripMatch, RoadtripOuting, RoadtripRejection, RoadtripSettings
from .settings_store import get_app_settings
from .storage import require_storage

UTC = dt.timezone.utc
MAX_DAY_POINTS = 20000
MAX_MONTH_POINTS = 50000
MAX_IMPORT_BYTES = 16 * 1024 * 1024
_LOCATION_LOCK = threading.Lock()
_DAY_CACHE_LOCK = threading.Lock()
_DAY_CACHE = OrderedDict()
_DAY_CACHE_BYTES = 0
_DAY_CACHE_BUDGET = 2 * 1024 * 1024


def _cancel(ctx):
    if ctx.is_cancelled():
        raise JobCancelled()


def timestamp(value, timezone="UTC") -> dt.datetime:
    if isinstance(value, dt.datetime):
        result = value
    elif isinstance(value, (int, float)) or isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value):
        number = float(value)
        result = dt.datetime.fromtimestamp(number / 1000 if number > 1e11 else number, UTC)
    else:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo(timezone))
    return result.astimezone(UTC)


def iso(value) -> str:
    return timestamp(value).isoformat().replace("+00:00", "Z")


def month_bounds(month: str, timezone="UTC"):
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", month):
        raise ValueError("Choose a month in YYYY-MM format")
    year, number = map(int, month.split("-"))
    start = dt.datetime(year, number, 1, tzinfo=ZoneInfo(timezone))
    end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def preferences(session) -> RoadtripSettings:
    result = session.get(RoadtripSettings, 1)
    if result is None:
        result = RoadtripSettings(id=1)
        session.add(result)
        session.flush()
    return result


def preferences_dict(value):
    return {key: getattr(value, key) for key in (
        "entity_id", "timezone", "home_radius_m", "max_accuracy_m", "max_gap_s", "max_speed_kmh"
    )} | {"home_available": value.home_lat is not None and value.home_lon is not None}


def distance(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2 - lat1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1, math.sqrt(h)))


def normalize_point(raw: dict, timezone="UTC") -> dict | None:
    try:
        if raw.get("state") in ("unknown", "unavailable"):
            return None
        attrs = raw.get("attributes", raw)
        lat = attrs.get("latitude", attrs.get("lat"))
        lon = attrs.get("longitude", attrs.get("lon", attrs.get("lng")))
        if lat is None and "latitudeE7" in raw:
            lat, lon = float(raw["latitudeE7"]) / 1e7, float(raw["longitudeE7"]) / 1e7
        location = raw.get("point") or raw.get("latLng")
        if location and lat is None:
            if isinstance(location, dict):
                lat, lon = location.get("latitude"), location.get("longitude")
            else:
                lat, lon = str(location).removeprefix("geo:").replace("°", "").split(",", 1)
        lat, lon = float(lat), float(lon)
        accuracy = float(attrs.get("gps_accuracy", attrs.get("accuracy", attrs.get("accuracyMeters", 0))) or 0)
        time = raw.get("last_updated") or raw.get("timestamp") or raw.get("timestampMs") or raw.get("time") or raw.get("startTime") or raw.get("last_changed")
        when = timestamp(time, timezone)
        if not all(math.isfinite(x) for x in (lat, lon, accuracy)) or not (-90 <= lat <= 90 and -180 <= lon <= 180) or accuracy < 0:
            return None
        result = {"t": iso(when), "lat": lat, "lon": lon, "accuracy": accuracy}
        if raw.get("state") in ("home", "not_home"):
            result["home"] = raw["state"] == "home"
        return result
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def at_home(point, prefs):
    # HA's historical zone classification survives a moved house/boat. The
    # current home coordinates are only a fallback for GPX/Timeline fixes.
    if isinstance(point.get("home"), bool):
        return point["home"]
    return distance({"lat": prefs.home_lat, "lon": prefs.home_lon}, point) <= prefs.home_radius_m


def clean_points(points, prefs) -> tuple[list[dict], int]:
    """Drop poor fixes and isolated impossible jumps; split gaps, never fill them."""
    normalized = []
    rejected = 0
    for raw in points:
        point = raw if all(k in raw for k in ("t", "lat", "lon")) else normalize_point(raw, prefs.timezone)
        if point is None or point.get("accuracy", 0) > prefs.max_accuracy_m:
            rejected += 1
        else:
            normalized.append(point)
    normalized.sort(key=lambda p: p["t"])
    result = []
    for point in normalized:
        if result:
            elapsed = (timestamp(point["t"]) - timestamp(result[-1]["t"])).total_seconds()
            if elapsed <= 0:
                rejected += 1
                continue
            if elapsed <= prefs.max_gap_s and distance(result[-1], point) / elapsed * 3.6 > prefs.max_speed_kmh:
                rejected += 1
                continue
        result.append(point)
    # Cache at roughly one-minute resolution, retaining home/away transitions
    # and endpoints. This bounds a month's RAM footprint on a Pi Zero.
    sampled = []
    home = {"lat": prefs.home_lat, "lon": prefs.home_lon} if prefs.home_lat is not None and prefs.home_lon is not None else None
    for index, point in enumerate(result):
        previous = sampled[-1] if sampled else None
        transition = bool(home and previous and at_home(point, prefs) != at_home(previous, prefs))
        if previous is None or index == len(result) - 1 or transition or (timestamp(point["t"]) - timestamp(previous["t"])).total_seconds() >= 60:
            sampled.append(point)
    return sampled, rejected


def _cache_root() -> Path:
    root = require_storage(get_settings().nas_root)
    target = root / ".drift" / "locations"
    if not target.resolve().is_relative_to(root) or target.is_symlink():
        raise RuntimeError("Location cache must remain within the mounted NAS")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.stat().st_mode & 0o777 != 0o700:
        os.chmod(target, 0o700)
    return target


def _cache_path(source: str, day: str):
    folder = _cache_root() / hashlib.sha256(source.encode()).hexdigest()[:16] / day[:7]
    if not folder.resolve().is_relative_to(_cache_root()):
        raise RuntimeError("Unsafe location cache path")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder / (day + ".json.gz")


def save_day(session, source: str, day: str, points: list[dict], rejected=0):
    if len(points) > MAX_DAY_POINTS:
        raise ValueError("Too many GPS fixes in one day; export at a lower sampling rate")
    path = _cache_path(source, day)
    descriptor, temp = tempfile.mkstemp(prefix=".location-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as output:
                output.write(json.dumps(points, separators=(",", ":")).encode())
            raw.flush()
            os.fsync(raw.fileno())
        require_storage(get_settings().nas_root)
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        Path(temp).unlink(missing_ok=True)
    row = session.query(LocationDay).filter_by(source=source, day=day).one_or_none()
    if row is None:
        row = LocationDay(source=source, day=day, cache_path=str(path))
        session.add(row)
    row.cache_path, row.point_count, row.rejected_count = str(path), len(points), rejected
    row.imported_at = utcnow()
    session.flush()
    # Imported coordinates replace any previous RAM copy, even when the new
    # day has the same number of points or the clock timestamp is unchanged.
    global _DAY_CACHE_BYTES
    with _DAY_CACHE_LOCK:
        for key in list(_DAY_CACHE):
            if key[0] == str(path):
                _DAY_CACHE_BYTES -= _DAY_CACHE.pop(key)[1]


def _read_day(row, root):
    """Keep at most 2 MiB of private GPS text in RAM, never on the SD card."""
    global _DAY_CACHE_BYTES
    path = Path(row.cache_path)
    if not Path(os.path.abspath(path)).is_relative_to(root):
        raise RuntimeError("Unsafe saved location file")
    key = (str(path), row.imported_at, row.point_count)
    with _DAY_CACHE_LOCK:
        cached = _DAY_CACHE.get(key)
        if cached is not None:
            _DAY_CACHE.move_to_end(key)
            data = cached[0]
        else:
            data = None
    if data is None:
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise RuntimeError("Unsafe saved location file")
        with gzip.open(path, "rt") as handle:
            data = handle.read(MAX_IMPORT_BYTES + 1)
        if len(data) > MAX_IMPORT_BYTES:
            raise ValueError("Saved location day exceeds the processing limit")
        points = json.loads(data)
        if not isinstance(points, list) or len(points) > MAX_DAY_POINTS:
            raise ValueError("Saved location day exceeds the processing limit")
        size = len(data.encode("utf-8"))
        if size <= _DAY_CACHE_BUDGET:
            with _DAY_CACHE_LOCK:
                if key not in _DAY_CACHE:
                    while _DAY_CACHE and _DAY_CACHE_BYTES + size > _DAY_CACHE_BUDGET:
                        _DAY_CACHE_BYTES -= _DAY_CACHE.popitem(last=False)[1][1]
                    _DAY_CACHE[key] = (data, size)
                    _DAY_CACHE_BYTES += size
        return points
    # Return fresh objects: callers cannot alter another preview's GPS points.
    return json.loads(data)


def merge_day(session, source, day, points, prefs):
    existing = session.query(LocationDay).filter_by(source=source, day=day).one_or_none()
    if existing and existing.point_count:
        path = Path(existing.cache_path)
        if not path.resolve().is_relative_to(_cache_root()) or path.is_symlink():
            raise RuntimeError("Unsafe saved location file")
        with gzip.open(path, "rt") as handle:
            text = handle.read(MAX_IMPORT_BYTES + 1)
        if len(text) > MAX_IMPORT_BYTES:
            raise ValueError("Saved location day exceeds the processing limit")
        old = json.loads(text)
        # New observations replace the same instant; partial retention windows
        # or partial GPX exports never discard the rest of an archived day.
        points = list(({p["t"]: p for p in old} | {p["t"]: p for p in points}).values())
    return clean_points(points, prefs)[0]


def load_month(session, month: str, source: str | None = None, *, start_time=None, end_time=None) -> list[dict]:
    root = _cache_root()
    rows = session.query(LocationDay).filter(LocationDay.point_count > 0)
    if start_time is not None and end_time is not None:
        # Outing times are UTC; cache days follow the configured local zone.
        # Query days directly so midnight, DST and month boundaries stay intact
        # without opening an entire month of tiny files across a slow NAS.
        zone = ZoneInfo(preferences(session).timezone)
        first_day = timestamp(start_time).astimezone(zone).date().isoformat()
        last_day = timestamp(end_time).astimezone(zone).date().isoformat()
        if first_day > last_day:
            raise ValueError("Outing end must follow its start")
        rows = rows.filter(LocationDay.day >= first_day, LocationDay.day <= last_day)
    else:
        rows = rows.filter(LocationDay.day.like(month + "-%"))
    if source:
        rows = rows.filter_by(source=source)
    result = []
    for row in rows.order_by(LocationDay.day):
        result.extend(_read_day(row, root))
        if len(result) > MAX_MONTH_POINTS:
            raise ValueError("Too many locations for one month; use a less frequent GPS export")
    return sorted(result, key=lambda p: p["t"])


def extract_outings(points: list[dict], prefs) -> list[dict]:
    if prefs.home_lat is None or prefs.home_lon is None:
        raise ValueError("Home location is needed: connect Home Assistant zone.home or set home coordinates")
    home = {"lat": prefs.home_lat, "lon": prefs.home_lon}
    points, _ = clean_points(points, prefs)
    outings, current, previous = [], [], None
    left_home = False

    def finish(returned_home):
        nonlocal current, left_home
        if len(current) >= 2:
            duration = (timestamp(current[-1]["t"]) - timestamp(current[0]["t"])).total_seconds()
            length, gaps = 0.0, 0
            for a, b in zip(current, current[1:]):
                gap = (timestamp(b["t"]) - timestamp(a["t"])).total_seconds()
                if gap <= prefs.max_gap_s:
                    length += distance(a, b)
                else:
                    gaps += 1
            # Sparse but clearly separated away fixes still establish a
            # possible outing. Do not invent the missing route/distance: those
            # remain gaps, and per-clip speed remains unavailable.
            spread = max((distance(current[0], p) for p in current), default=0)
            if duration >= 300 and (length >= 200 or spread >= 200):
                outings.append({"start_time": current[0]["t"], "end_time": current[-1]["t"],
                    "duration_s": duration, "distance_m": round(length, 1), "point_count": len(current),
                    "gap_count": gaps, "complete": left_home and returned_home})
        current, left_home = [], False

    for point in points:
        home_now = at_home(point, prefs)
        if current and (timestamp(point["t"]) - timestamp(current[-1]["t"])).total_seconds() > 24 * 3600:
            finish(False)
        if home_now:
            if current:
                gap = (timestamp(point["t"]) - timestamp(current[-1]["t"])).total_seconds()
                if gap <= prefs.max_gap_s:
                    current.append(point)
                finish(gap <= prefs.max_gap_s)
        else:
            if not current and previous and at_home(previous, prefs):
                gap = (timestamp(point["t"]) - timestamp(previous["t"])).total_seconds()
                if gap <= prefs.max_gap_s:
                    current.append(previous)
                    left_home = True
            current.append(point)
        previous = point
    finish(False)
    return outings


def rebuild_outings(session, month, source):
    prefs = preferences(session)
    points = load_month(session, month, source)
    for row in session.query(RoadtripOuting).filter_by(month=month, source=source):
        row.active = False
    for outing in extract_outings(points, prefs):
        signature = hashlib.sha256((source + outing["start_time"] + outing["end_time"]).encode()).hexdigest()
        row = session.query(RoadtripOuting).filter_by(signature=signature).one_or_none()
        if row is None:
            row = RoadtripOuting(signature=signature, source=source, month=month)
            session.add(row)
        for key, value in outing.items():
            setattr(row, key, timestamp(value).replace(tzinfo=None) if key.endswith("_time") else value)
        row.active = True
    session.flush()


def outing_dict(row):
    return {"id": row.id, "source": row.source, "month": row.month, "start_time": iso(row.start_time),
        "end_time": iso(row.end_time), "duration_s": row.duration_s, "distance_m": row.distance_m,
        "point_count": row.point_count, "gap_count": row.gap_count, "complete": row.complete}


def discover_entities(session):
    settings = get_app_settings(session)
    if not settings.ha_base_url or not settings.ha_token:
        return {"entities": [], "home_available": preferences_dict(preferences(session))["home_available"]}
    with httpx.Client(timeout=20) as client:
        response = client.get(_base_url(settings) + "/api/states", headers=_headers(settings))
        response.raise_for_status()
        states = response.json()
    entities = []
    prefs = preferences(session)
    for state in states:
        entity = state.get("entity_id", "")
        attrs = state.get("attributes", {})
        if entity == "zone.home" and attrs.get("latitude") is not None and attrs.get("longitude") is not None:
            prefs.home_lat, prefs.home_lon = float(attrs["latitude"]), float(attrs["longitude"])
            prefs.home_radius_m = max(50, float(attrs.get("radius", 150)))
        if entity.startswith(("person.", "device_tracker.")) and attrs.get("latitude") is not None:
            entities.append({"entity_id": entity, "name": attrs.get("friendly_name", entity)})
    return {"entities": entities, "home_available": prefs.home_lat is not None and prefs.home_lon is not None}


def _iter_json_points(value):
    if isinstance(value, list):
        for child in value:
            yield from _iter_json_points(child)
    elif isinstance(value, dict):
        if any(k in value for k in ("latitude", "latitudeE7", "lat", "point", "latLng", "attributes")):
            yield value
        # On-device Timeline exports use semanticSegments / timelinePath. Visits
        # alone are not continuous tracks and are intentionally not inferred.
        for key in ("locations", "points", "semanticSegments", "timelinePath"):
            if key in value:
                yield from _iter_json_points(value[key])


def parse_import(path: Path, month: str, prefs) -> tuple[list[dict], int]:
    start, end = month_bounds(month, prefs.timezone)
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("Location export is too large; choose a monthly export under 16 MB")
    points, rejected = [], 0
    if path.suffix.lower() == ".gpx":
        # iterparse keeps a large GPX from creating an in-memory XML tree.
        def records():
            for _, element in ET.iterparse(path, events=("end",)):
                if element.tag.rsplit("}", 1)[-1] in ("trkpt", "wpt", "rtept"):
                    time = next((x.text for x in element if x.tag.rsplit("}", 1)[-1] == "time"), None)
                    yield {"latitude": element.get("lat"), "longitude": element.get("lon"), "timestamp": time}
                    element.clear()
        raw_points = records()
    else:
        with path.open() as handle:
            raw_points = _iter_json_points(json.load(handle))
    for raw in raw_points:
        point = normalize_point(raw, prefs.timezone)
        if point is None:
            rejected += 1
        elif start <= timestamp(point["t"]) < end:
            points.append(point)
        if len(points) > MAX_MONTH_POINTS:
            raise ValueError("Too many locations in one month; export a lower sampling frequency")
    clean, dropped = clean_points(points, prefs)
    return clean, rejected + dropped


def _import_points(session, source, month, points, rejected=0):
    prefs = preferences(session)
    days = defaultdict(list)
    for point in points:
        day = timestamp(point["t"]).astimezone(ZoneInfo(prefs.timezone)).date().isoformat()
        days[day].append(point)
    if not days:
        raise ValueError("No usable location fixes in the selected month; previous location history was preserved")
    # Partial exports add to a month; missing days must not erase older routes.
    for day in sorted(days):
        merged = merge_day(session, source, day, days[day], prefs)
        save_day(session, source, day, merged, rejected if day == min(days) else 0)
    rebuild_outings(session, month, source)


@handler("roadtrip_import_file")
def handle_import_file(job_id, payload, ctx):
    with _LOCATION_LOCK:
        path = Path(payload["path"])
        try:
            if not path.resolve().is_relative_to(_cache_root() / "imports"):
                raise ValueError("Invalid location import")
            with session_scope() as session:
                prefs = preferences(session)
                points, rejected = parse_import(path, payload["month"], prefs)
                if not points:
                    raise ValueError("No usable location fixes in the selected month; previous location history was preserved")
                _cancel(ctx)
                _import_points(session, payload["source"], payload["month"], points, rejected)
            ctx.set_progress(1, f"Indexed {len(points)} location fixes; rejected {rejected}")
        finally:
            path.unlink(missing_ok=True)


def _handle_import_ha_month(job_id, payload, ctx):
    with _LOCATION_LOCK:
        with session_scope() as session:
            prefs = preferences(session)
            discover_entities(session)
            if prefs.home_lat is None:
                raise ValueError("Home Assistant zone.home has no coordinates")
            settings = get_app_settings(session)
            if not settings.ha_base_url or not settings.ha_token:
                raise ValueError("Configure Home Assistant in Settings first")
            entity = payload.get("entity_id") or prefs.entity_id
            if not re.fullmatch(r"(?:person|device_tracker)\.[a-zA-Z0-9_]+", entity or ""):
                raise ValueError("Choose a Home Assistant person or device tracker")
            prefs.entity_id = entity
            timezone = prefs.timezone
            base_url, headers = _base_url(settings), _headers(settings)
        source = "ha:" + entity
        month = payload["month"]
        month_bounds(month, timezone)
        year, number = map(int, month.split("-"))
        count = calendar.monthrange(year, number)[1]
        report = {"month": month, "imported_days": 0, "empty_days": 0, "preserved_days": 0, "point_count": 0, "rejected_count": 0}
        # Commit each day separately; restart safely re-fetches bounded days.
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            for day in range(1, count + 1):
                _cancel(ctx)
                local_start = dt.datetime(year, number, day, tzinfo=ZoneInfo(timezone))
                start, end = local_start.astimezone(UTC), (local_start + dt.timedelta(days=1)).astimezone(UTC)
                with client.stream("GET", base_url + "/api/history/period/" + iso(start),
                    headers=headers, params={"filter_entity_id": entity, "end_time": iso(end), "significant_changes_only": "0"}) as response:
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes(65536):
                        _cancel(ctx)
                        body.extend(chunk)
                        if len(body) > MAX_IMPORT_BYTES:
                            raise ValueError("A day of HA history exceeds 16 MB; export fewer GPS fixes")
                raw = json.loads(body)
                del body
                if not isinstance(raw, list) or any(not isinstance(part, list) for part in raw):
                    raise ValueError("Home Assistant returned an unsupported history response")
                points, unusable = [], 0
                for part in raw:
                    for row in part:
                        point = normalize_point(row, timezone) if isinstance(row, dict) else None
                        if point is None:
                            unusable += 1
                        elif start <= timestamp(point["t"]) < end:
                            points.append(point)
                del raw
                with session_scope() as session:
                    clean, rejected = clean_points(points, preferences(session))
                    rejected += unusable
                    day_key = local_start.date().isoformat()
                    existing = session.query(LocationDay).filter_by(source=source, day=day_key).one_or_none()
                    # An expired recorder window is not evidence the old trip
                    # never happened. Preserve useful cached GPS on empty data.
                    if clean:
                        merged = merge_day(session, source, day_key, clean, preferences(session))
                        save_day(session, source, day_key, merged, rejected)
                        report["imported_days"] += 1
                        report["point_count"] += len(clean)
                    elif existing and existing.point_count:
                        report["preserved_days"] += 1
                    else:
                        save_day(session, source, day_key, [], rejected)
                        report["empty_days"] += 1
                    report["rejected_count"] += rejected
                ctx.set_progress(day / (count + 1), f"Imported location day {day}/{count}")
        with session_scope() as session:
            rebuild_outings(session, month, source)
        ctx.set_progress(1, f"Location month indexed: {report['imported_days']} days, {report['empty_days']} empty, {report['preserved_days']} preserved")
        return report


def months_between(start_month, end_month=None):
    month_bounds(start_month)
    month_bounds(end_month or start_month)
    start = dt.date.fromisoformat(start_month + "-01")
    end = dt.date.fromisoformat((end_month or start_month) + "-01")
    if end < start:
        raise ValueError("End month must be on or after the first month")
    result = []
    while start <= end:
        result.append(start.strftime("%Y-%m"))
        if len(result) > 12:
            raise ValueError("Import at most 12 months at a time")
        start = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return result


@handler("roadtrip_import_ha")
def handle_import_ha(job_id, payload, ctx):
    months = months_between(payload["month"], payload.get("end_month"))
    class MonthContext:
        def __init__(self, index):
            self.index = index
        def is_cancelled(self):
            return ctx.is_cancelled()
        def set_progress(self, value, detail=None):
            ctx.set_progress((self.index + value) / len(months), f"{months[self.index]}: {detail or ''}")
    reports = []
    for index, month in enumerate(months):
        _cancel(ctx)
        reports.append(_handle_import_ha_month(job_id, payload | {"month": month}, MonthContext(index)))
    with session_scope() as session:
        row = session.get(Job, job_id)
        if row:
            row.result = json.dumps({"months": reports})


def folder_month(path: str) -> tuple[str | None, str]:
    """The archive's year/month wins over a camera clock, even on wrong days."""
    match = re.search(r"(?:^|[/\\])(20\d{2})[/\\](0?[1-9]|1[0-2])(?:[/\\]|$)", path)
    if match:
        return f"{int(match[1]):04d}-{int(match[2]):02d}", "archive_folder"
    return None, "unknown"


def _camera_order(clip):
    path = clip.get("source_path") or clip.get("path") or clip["filename"]
    folder = re.search(r"(?:^|/)(\d{3})[^/]*MEDIA(?:/|$)", path, re.IGNORECASE)
    counter = re.search(r"(?:DVR|VID|MOV)[_-]?(\d+)", clip["filename"], re.IGNORECASE)
    if counter:
        return (int(folder[1]) if folder else 0, int(counter[1]), clip["filename"])
    return (0, 0, clip["filename"])


def clip_groups(session, month: str, album_id: int | None = None, media_ids: list[int] | None = None, clip_ids: list[str] | None = None):
    month_bounds(month)
    from .archive_models import ArchiveFile
    # Scalar projections avoid eager media/upload-state trees on the Pi.
    media_selection = album_id is not None or media_ids is not None
    selected = media_selection or clip_ids is not None
    allowed = set(media_ids or [])
    if album_id:
        allowed = {r[0] for r in session.query(AlbumItem.media_id).filter_by(album_id=album_id)}
    if media_selection and not allowed:
        return []
    media = {r.id: r for r in session.query(MediaItem.id, MediaItem.filename, MediaItem.path,
        MediaItem.duration_s, MediaItem.capture_time, MediaItem.checksum).filter(
        MediaItem.kind == "video", MediaItem.derived.is_(False))}
    ledger = {r.id: r for r in session.query(UploadedClip.id, UploadedClip.source_media_id,
        UploadedClip.remote_path, UploadedClip.filename, UploadedClip.checksum).filter(UploadedClip.status == "done")}
    checksums = {r.checksum: r for r in media.values() if r.checksum}
    metadata = {row.archive_id: row for row in session.query(RoadtripClipMetadata)}
    clips, seen = [], set()
    for archived in session.query(ArchiveFile.id, ArchiveFile.path, ArchiveFile.ledger_id, ArchiveFile.size_bytes, ArchiveFile.mtime_ns, ArchiveFile.ctime_ns, ArchiveFile.device, ArchiveFile.inode).filter(
            ArchiveFile.present.is_(True), ArchiveFile.kind == "video").order_by(ArchiveFile.path):
        entry = ledger.get(archived.ledger_id)
        item = media.get(entry.source_media_id) if entry else None
        if not item and entry:
            item = checksums.get(entry.checksum)
        path_month, basis = folder_month(archived.path)
        if path_month != month or (media_selection and (not item or item.id not in allowed)):
            continue
        # Different archive copies of one source clip should not inflate a trip.
        identity = f"media:{item.id}" if item else f"archive:{archived.id}"
        if identity in seen:
            continue
        seen.add(identity)
        cached = metadata.get(archived.id)
        if cached and cached.identity != _metadata_identity(archived):
            cached = None
        capture = iso(item.capture_time) if item and item.capture_time else (iso(cached.observed_time) if cached and cached.observed_time else None)
        clips.append({"id": identity, "media_id": item.id if item else None,
            "archive_id": archived.id, "path": archived.path,
            "source_path": item.path if item else None,
            "filename": item.filename if item else Path(archived.path).name,
            "duration_s": item.duration_s if item and item.duration_s else (cached.duration_s if cached else None), "observed_time": capture,
            "month_basis": basis})
    # A not-yet-inventoried upload still has reliable year/month in its ledger.
    for entry in ledger.values():
        item = media.get(entry.source_media_id) or checksums.get(entry.checksum)
        identity = f"media:{item.id}" if item else f"ledger:{entry.id}"
        path_month, basis = folder_month(entry.remote_path or "")
        if identity in seen or path_month != month or media_selection and (not item or item.id not in allowed):
            continue
        seen.add(identity)
        clips.append({"id": identity, "media_id": item.id if item else None, "archive_id": None,
            "path": entry.remote_path, "source_path": item.path if item else None,
            "filename": item.filename if item else entry.filename,
            "duration_s": item.duration_s if item else None,
            "observed_time": iso(item.capture_time) if item and item.capture_time else None,
            "month_basis": basis})
    if clip_ids is not None:
        selected_clips = set(clip_ids)
        if selected_clips - {c["id"] for c in clips}:
            raise ValueError("Selected clips changed; refresh the candidates")
        clips = [c for c in clips if c["id"] in selected_clips]
    if selected:
        groups = [sorted(clips, key=_camera_order)] if clips else []
    else:
        # Wrong clocks still often preserve relative sequence/gaps. Separate
        # camera-day bursts, retaining counter order and 999->next-folder rollovers.
        dated = defaultdict(list)
        for clip in clips:
            observed = clip.get("observed_time")
            key = timestamp(observed).date().isoformat() if observed else "unknown-date"
            dated[key].append(clip)
        groups = []
        for key in sorted(dated):
            ordered = sorted(dated[key], key=_camera_order)
            current, previous = [], None
            for clip in ordered:
                a, b = previous and previous.get("observed_time"), clip.get("observed_time")
                gap = (timestamp(b) - timestamp(a)).total_seconds() if a and b else None
                if current and gap is not None and gap > 7200:
                    groups.append(current)
                    current = []
                current.append(clip)
                previous = clip
            if current:
                groups.append(current)
    result = []
    for clips in groups:
        recorded = sum(max(0, c.get("duration_s") or 0) for c in clips)
        known = [c for c in clips if c.get("observed_time") and (c.get("duration_s") or 0) > 0]
        span, relative = None, False
        if len(known) == len(clips) and known:
            origin = timestamp(clips[0]["observed_time"])
            offsets = [(timestamp(c["observed_time"]) - origin).total_seconds() for c in clips]
            relative = all(0 <= n <= 24 * 3600 for n in offsets) and all(
                b >= a + (clips[i].get("duration_s") or 0) - 2
                for i, (a, b) in enumerate(zip(offsets, offsets[1:])))
            if relative:
                span = max(n + c["duration_s"] for n, c in zip(offsets, clips))
        key = hashlib.sha256(json.dumps([month, [c["id"] for c in clips]]).encode()).hexdigest()[:24]
        result.append({"key": key, "label": f"{clips[0]['filename']} – {clips[-1]['filename']}",
            "month": month, "month_basis": "archive_folder", "clips": clips, "clip_count": len(clips),
            "recorded_duration_s": recorded, "observed_span_s": span,
            "relative_timing_available": relative,
            "timing_warning": "Camera clock is unverified. Folder month is used; unrecorded gaps may be unknown."})
    return result


def rank_candidates(group: dict, outings: list[dict]) -> list[dict]:
    result = []
    recorded = group.get("recorded_duration_s") or 0
    span = group.get("observed_span_s")
    for outing in outings:
        if outing["month"] != group["month"]:
            continue
        duration = outing["duration_s"]
        if duration <= 0:
            continue
        ratio = recorded / duration
        # Recorded video is only a lower bound: stopping the camera does not
        # shorten the outing. A broad duration score is evidence, never proof.
        if recorded > duration + max(1800, duration * .5):
            continue
        duration_score = max(0, 1 - abs(math.log(max(ratio, .01))) / 3)
        basis = span if span else recorded
        span_score = max(0, 1 - abs(math.log(max(basis / duration, .01))) / 2)
        score = .35 + .25 * duration_score + .20 * span_score
        reasons = ["NAS folder year/month matches", f"{recorded / 60:.0f} min recorded / {duration / 60:.0f} min away"]
        if span:
            reasons.append(f"Unverified camera sequence spans {span / 60:.0f} min including gaps")
        else:
            reasons.append("Recording gaps are unknown; summed clip duration is only a lower bound")
        if outing["complete"]:
            score += .05
            reasons.append("Home departure and return both observed")
        if outing["gap_count"]:
            score -= min(.15, outing["gap_count"] * .025)
            reasons.append("GPS gaps reduce route coverage")
        if any(not c.get("duration_s") for c in group["clips"]):
            score = min(score, .49)
            reasons.append("Some clip durations are unavailable")
        result.append({"group_key": group["key"], "outing_id": outing["id"], "score": round(max(0, score), 3),
            "confidence": "medium" if score >= .65 else "low", "reasons": reasons,
            "provisional": True, "outing": outing})
    result.sort(key=lambda item: (-item["score"], item["outing"]["start_time"]))
    if len(result) >= 2 and result[0]["score"] - result[1]["score"] < .12:
        for item in result:
            item["confidence"] = "low"
            item["reasons"].append("Multiple outings have similar duration; choose a date manually")
    return result[:5]


def candidates(session, month, album_id=None, media_ids=None, clip_ids=None):
    groups = clip_groups(session, month, album_id, media_ids, clip_ids)
    outings = [outing_dict(row) for row in session.query(RoadtripOuting).filter_by(month=month, active=True).order_by(RoadtripOuting.start_time)]
    rejected = {(r.group_key, r.outing_id) for r in session.query(RoadtripRejection).filter_by(month=month)}
    rejected.update((r.group_key, r.outing_id) for r in session.query(RoadtripMatch).filter_by(month=month, status="rejected"))
    return {"groups": groups, "outings": outings,
        "suggestions": [suggestion for group in groups for suggestion in rank_candidates(group,
            [o for o in outings if (group["key"], o["id"]) not in rejected])],
        "warning": "Suggestions are provisional. Confirm dates and clip anchors before relying on routes or speeds."}


def _track(points, prefs):
    points, rejected = clean_points(points, prefs)
    segments, current, previous = [], [], None
    for point in points:
        if previous and (timestamp(point["t"]) - timestamp(previous["t"])).total_seconds() > prefs.max_gap_s:
            if len(current) > 1:
                segments.append(current)
            current = []
        current.append(point)
        previous = point
    if len(current) > 1:
        segments.append(current)
    return segments


def clip_speed(segments, start: dt.datetime, duration: float):
    if duration <= 0:
        return {"coverage": 0, "covered_s": 0, "distance_m": None, "average_speed_kmh": None}
    end = start + dt.timedelta(seconds=duration)
    length, covered, fixes = 0.0, 0.0, set()
    for segment in segments:
        for a, b in zip(segment, segment[1:]):
            at, bt = timestamp(a["t"]), timestamp(b["t"])
            left, right = max(start, at), min(end, bt)
            elapsed = (bt - at).total_seconds()
            if left < right and elapsed > 0:
                overlap = (right - left).total_seconds()
                length += distance(a, b) * overlap / elapsed
                covered += overlap
                fixes.update((a["t"], b["t"]))
    coverage = min(1, covered / duration)
    # Incomplete tracks cannot support a whole-clip average. Keep partial
    # distance visible and explain the missing speed instead of extrapolating.
    speed = length / covered * 3.6 if len(fixes) >= 2 and coverage >= .8 else None
    return {"coverage": round(coverage, 3), "covered_s": round(covered, 1),
        "distance_m": round(length, 1) if covered else None,
        "average_speed_kmh": round(speed, 1) if speed is not None else None,
        "speed_note": "Estimate from GPS over covered time" if speed is not None else "Insufficient GPS coverage (80% required)"}


def preview(session, month, group_key, outing_id, start_time=None, clip_offsets=None, group_snapshot=None, confirmed=False, album_id=None, media_ids=None, clip_ids=None, *, track_points=None):
    prefs = preferences(session)
    outing = session.get(RoadtripOuting, outing_id)
    if outing is None or outing.month != month:
        raise ValueError("Choose an outing from this month")
    group = group_snapshot or next((g for g in clip_groups(session, month, album_id, media_ids, clip_ids) if g["key"] == group_key), None)
    if group is None:
        raise ValueError("Camera group changed; refresh the candidates")
    start, end = timestamp(outing.start_time), timestamp(outing.end_time)
    # Collection maps load one bounded source/month bucket, then reuse its
    # immutable points across outings without repeated NAS mount/path checks.
    points = track_points if track_points is not None else load_month(session, month, outing.source, start_time=start, end_time=end)
    points = [p for p in points if start <= timestamp(p["t"]) <= end]
    segments = _track(points, prefs)
    anchor = timestamp(start_time, prefs.timezone) if start_time else start
    if anchor < start - dt.timedelta(hours=12) or anchor > end + dt.timedelta(hours=12):
        raise ValueError("Choose a clip start close to this outing")
    offsets = clip_offsets or {}
    if not set(offsets).issubset({c["id"] for c in group["clips"]}):
        raise ValueError("A clip anchor no longer belongs to this group")
    if any(not math.isfinite(float(v)) or abs(float(v)) > 172800 for v in offsets.values()):
        raise ValueError("Clip offsets must be finite and within two days")
    clips, sequential = [], 0
    origin = timestamp(group["clips"][0]["observed_time"]) if group["relative_timing_available"] else None
    for clip in group["clips"]:
        duration = max(0, clip.get("duration_s") or 0)
        offset = float(offsets[clip["id"]]) if clip["id"] in offsets else (
            (timestamp(clip["observed_time"]) - origin).total_seconds() if origin else sequential)
        clip_start = anchor + dt.timedelta(seconds=offset)
        clips.append(clip | {"start_time": iso(clip_start), "end_time": iso(clip_start + dt.timedelta(seconds=duration)),
            "offset_s": offset, "provisional": not confirmed,
            "timing_basis": "manual" if clip["id"] in offsets else ("camera_relative" if origin else "continuous_recording_assumption"),
            **clip_speed(segments, clip_start, duration)})
        sequential += duration
    series, distance_so_far = [], 0.0
    for index, segment in enumerate(segments):
        for i, point in enumerate(segment):
            speed = None
            if i:
                length = distance(segment[i - 1], point)
                seconds = (timestamp(point["t"]) - timestamp(segment[i - 1]["t"])).total_seconds()
                distance_so_far += length
                speed = round(length / seconds * 3.6, 1) if seconds else None
            series.append({"time": point["t"], "latitude": point["lat"], "longitude": point["lon"],
                "distance_m": round(distance_so_far, 1), "speed_kmh": speed, "segment": index})
    from .roadtrip_map import annotate_detail
    return annotate_detail({"group": group, "outing": outing_dict(outing), "provisional": not confirmed,
        "start_time": iso(anchor), "clips": clips,
        "geojson": {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"segment": i},
            "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in segment]}}
            for i, segment in enumerate(segments)]}, "time_series": series,
        "warning": "GPS is approximate. Missing locations are not joined. Clip positions assume the displayed timing until you confirm or adjust anchors."}, prefs.timezone, prefs.max_gap_s)


def match_dict(row):
    return {"id": row.id, "month": row.month, "group_key": row.group_key, "outing_id": row.outing_id,
        "name": row.name, "status": row.status, "updated_at": iso(row.updated_at),
        "created_at": iso(row.created_at), "start_time": json.loads(row.mapping or "{}").get("start_time")}


def save_match(session, request):
    if request.status == "rejected":
        outing = session.get(RoadtripOuting, request.outing_id)
        if outing is None or outing.month != request.month:
            raise ValueError("Choose an outing from this month")
        rejection = session.query(RoadtripRejection).filter_by(month=request.month, group_key=request.group_key, outing_id=request.outing_id).one_or_none()
        if rejection is None:
            rejection = RoadtripRejection(month=request.month, group_key=request.group_key, outing_id=request.outing_id)
            session.add(rejection)
        existing = session.query(RoadtripMatch).filter_by(month=request.month, group_key=request.group_key).one_or_none()
        if existing and existing.outing_id == request.outing_id:
            existing.status = "rejected"
            existing.updated_at = utcnow()
        session.flush()
        return {"status": "rejected", "month": request.month, "group_key": request.group_key, "outing_id": request.outing_id}
    detail = preview(session, request.month, request.group_key, request.outing_id,
        request.start_time, request.clip_offsets, album_id=getattr(request, "album_id", None), media_ids=getattr(request, "media_ids", None), clip_ids=getattr(request, "clip_ids", None))
    row = session.query(RoadtripMatch).filter_by(month=request.month, group_key=request.group_key).one_or_none()
    if row is None:
        row = RoadtripMatch(month=request.month, group_key=request.group_key, outing_id=request.outing_id)
        session.add(row)
    session.query(RoadtripRejection).filter_by(month=request.month, group_key=request.group_key, outing_id=request.outing_id).delete()
    row.outing_id, row.name, row.status = request.outing_id, request.name[:256], request.status
    row.mapping = json.dumps({"group": detail["group"], "start_time": detail["start_time"], "clip_offsets": request.clip_offsets})
    row.updated_at = utcnow()
    session.flush()
    return match_dict(row)


def state(session):
    prefs = preferences(session)
    ha = get_app_settings(session)
    rows = session.query(LocationDay.day, func.sum(LocationDay.point_count), func.sum(LocationDay.rejected_count)).group_by(LocationDay.day).all()
    monthly = defaultdict(lambda: {"days": 0, "point_count": 0, "rejected_count": 0})
    for day, count, rejected in rows:
        monthly[day[:7]]["days"] += 1
        monthly[day[:7]]["point_count"] += count
        monthly[day[:7]]["rejected_count"] += rejected
    for month, count in session.query(RoadtripOuting.month, func.count()).filter_by(active=True).group_by(RoadtripOuting.month):
        monthly[month]["outing_count"] = count
    from .archive_models import ArchiveFile
    months = set(monthly)
    for (path,) in session.query(ArchiveFile.path).filter(ArchiveFile.present.is_(True), ArchiveFile.kind == "video"):
        month, _ = folder_month(path)
        if month:
            months.add(month)
    for (path,) in session.query(UploadedClip.remote_path).filter(UploadedClip.status == "done"):
        month, _ = folder_month(path or "")
        if month:
            months.add(month)
    return {"configured_ha": bool(ha.ha_base_url and ha.ha_token), "settings": preferences_dict(prefs),
        "available_months": sorted(months, reverse=True),
        "imports": [{"month": month, **values} for month, values in sorted(monthly.items(), reverse=True)],
        "active_jobs": [{"id": row.id, "kind": row.kind, "status": row.status, "progress": row.progress} for row in session.query(Job).filter(Job.kind.in_(("roadtrip_import_ha", "roadtrip_import_file", "roadtrip_probe")), Job.status.in_(("queued", "running", "paused")))],
        "matches": [match_dict(row) for row in session.query(RoadtripMatch).order_by(RoadtripMatch.updated_at.desc()).limit(100)]}


def _metadata_identity(row):
    return hashlib.sha256(json.dumps([row.size_bytes, row.mtime_ns, row.ctime_ns, str(row.device), str(row.inode)]).encode()).hexdigest()


def _probe_file(path: Path, ctx):
    command = [get_settings().ffprobe, "-v", "error", "-threads", "1", "-show_entries",
        "format=duration:format_tags=creation_time,date:stream_tags=creation_time", "-of", "json", str(path)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 30
    try:
        while True:
            _cancel(ctx)
            if time.monotonic() > deadline:
                raise ValueError("Metadata probe timed out")
            try:
                output, _ = process.communicate(timeout=.25)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            raise ValueError("Video metadata could not be read")
        if len(output) > 1024 * 1024:
            raise ValueError("Video metadata exceeds the safe limit")
        data = json.loads(output)
        duration = float(data.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration <= 0 or duration > 7 * 24 * 3600:
            raise ValueError("Video duration is unavailable or invalid")
        tags = data.get("format", {}).get("tags", {})
        when = tags.get("creation_time") or tags.get("date")
        if not when:
            when = next((s.get("tags", {}).get("creation_time") for s in data.get("streams", []) if s.get("tags", {}).get("creation_time")), None)
        try:
            observed = timestamp(when).replace(tzinfo=None) if when else None
        except (ValueError, TypeError):
            observed = None
        return duration, observed
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@handler("roadtrip_probe")
def handle_probe(job_id, payload, ctx):
    from .archive import _safe_root, _safe_stat, stat_identity
    from .archive_models import ArchiveFile
    month = payload["month"]
    month_bounds(month)
    # Share the decoder slot, then the upload slot, matching other NAS metadata
    # work. No two probes or NAS transfer-heavy jobs run concurrently.
    report = {"month": month, "probed": 0, "cached": 0, "errors": 0}
    pending = []
    def flush_pending():
        if not pending:
            return
        with session_scope() as session:
            for archive_id, identity, duration, observed, error in pending:
                row = session.get(RoadtripClipMetadata, archive_id)
                if row is None:
                    row = RoadtripClipMetadata(archive_id=archive_id, identity=identity)
                    session.add(row)
                row.identity, row.duration_s, row.observed_time, row.error = identity, duration, observed, error
                row.probed_at = utcnow()
        pending.clear()
    with _LOCATION_LOCK, ctx.ffmpeg_semaphore, ctx.upload_semaphore:
        with session_scope() as session:
            ids = [row.id for row in session.query(ArchiveFile.id, ArchiveFile.path).filter(
                ArchiveFile.present.is_(True), ArchiveFile.kind == "video") if folder_month(row.path)[0] == month]
        for index, archive_id in enumerate(ids):
            _cancel(ctx)
            with session_scope() as session:
                archived = session.get(ArchiveFile, archive_id)
                if archived is None or not archived.present:
                    continue
                identity = _metadata_identity(archived)
                cached = session.get(RoadtripClipMetadata, archive_id)
                if cached and cached.identity == identity and not cached.error:
                    report["cached"] += 1
                    continue
                root, relative = Path(archived.root_path), archived.path
            duration, observed, error = None, None, None
            try:
                root = _safe_root(root)
                current = stat_identity(_safe_stat(root, relative))
                actual = SimpleIdentity(current)
                if _metadata_identity(actual) != identity:
                    raise ValueError("File changed; refresh the archive inventory first")
                duration, observed = _probe_file(root / relative, ctx)
                after = SimpleIdentity(stat_identity(_safe_stat(root, relative)))
                if _metadata_identity(after) != identity:
                    raise ValueError("File changed while probing; refresh the archive inventory")
                report["probed"] += 1
            except JobCancelled:
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                # Do not include video tags, raw subprocess output or GPS data.
                error = str(exc)[:512]
                report["errors"] += 1
            pending.append((archive_id, identity, duration, observed, error))
            if len(pending) >= 25:
                flush_pending()
            ctx.set_progress((index + 1) / len(ids) if ids else 1, f"Read {index + 1}/{len(ids)} clip headers; {report['errors']} unreadable")
        flush_pending()
    with session_scope() as session:
        row = session.get(Job, job_id)
        if row:
            row.result = json.dumps(report)
    ctx.set_progress(1, f"Clip details ready: {report['probed']} read, {report['cached']} cached, {report['errors']} unreadable")


class SimpleIdentity:
    def __init__(self, values):
        for key in ("size_bytes", "mtime_ns", "ctime_ns", "device", "inode"):
            setattr(self, key, values[key])
