"""Automatic HA ride discovery from corroborated, unshifted recording dates.

Only the explicit background discovery reads GPS. Listing and input validation
use scalar database metadata; no probes, video reads or coordinates are stored.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import roadtrips as rt
from .auto_ride_models import AutoRide, AutoRideClip, AutoRideDiscovery
from .archive_models import ArchiveFile
from .config import get_settings
from .models import CorrectedArchive, Destination, MediaItem, RecordingDate, UploadedClip, utcnow
from .roadtrip_models import LocationDay, RoadtripClipMetadata, RoadtripOuting, RoadtripSettings

CLUSTER_GAP_S = 60 * 60
GPS_NEIGHBOUR_S = 15 * 60
MIN_TEMPORAL_COVERAGE = .9
MIN_GPS_COVERAGE = .5
STRONG_GPS_COVERAGE = .8
MIN_CLUSTER_CLIPS = 2
SOFT_REVIEW_REASONS = {
    "Newest ride does not end on the connection day or previous day",
    "Historical footage needs its date confirmed separately",
}


def _json(value, fallback):
    try:
        result = json.loads(value or "null")
        return result if isinstance(result, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value):
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _absolute(root, path):
    base = os.path.abspath(root)
    result = os.path.abspath(path if os.path.isabs(path) else os.path.join(base, path))
    return result if os.path.commonpath((base, result)) == base else None


def _rows(session, model):
    """Scalar projections avoid eager ORM relationships on the Pi."""
    return session.execute(model.__table__.select()).mappings().all()


def input_fingerprint(session):
    """Cheap metadata-only change detector; includes changes lacking updated_at."""
    digest = hashlib.sha256(b'ride-policy-2-return-alignment')
    columns = {
        ArchiveFile: ("id", "destination_id", "root_path", "path", "kind", "present", "eligible", "ledger_id", "size_bytes", "mtime_ns", "ctime_ns", "device", "inode"),
        MediaItem: ("id", "path", "capture_time", "duration_s", "size_bytes", "checksum", "derived"),
        UploadedClip: ("id", "source_media_id", "destination_id", "remote_path", "status", "checksum", "size_bytes"),
        RecordingDate: ("id", "path", "size_bytes", "mtime_ns", "checksum", "original_time", "corrected_time", "duration_s", "time_source", "status", "reasons", "revision"),
        CorrectedArchive: ("id", "recording_id", "destination_id", "original_path", "corrected_path", "corrected_time"),
        RoadtripClipMetadata: ("archive_id", "identity", "observed_time", "duration_s", "error"),
        RoadtripOuting: ("id", "signature", "source", "month", "start_time", "end_time", "active", "point_count", "gap_count"),
        LocationDay: ("id", "source", "day", "point_count", "imported_at"),
        RoadtripSettings: ("id", "entity_id", "timezone", "home_lat", "home_lon", "home_radius_m", "max_gap_s", "max_accuracy_m", "max_speed_kmh"),
        Destination: ("id", "type", "base_path"),
    }
    for model, names in columns.items():
        digest.update(model.__tablename__.encode())
        query = session.query(*(getattr(model, name) for name in names)).order_by(getattr(model, names[0]))
        for row in query:
            digest.update(_dump(list(row)).encode())
    return digest.hexdigest()


def eligible_clips(session, *, now=None):
    """Return evidenced NAS dates, with explicit exclusions instead of guesses."""
    now = rt.timestamp(now or utcnow())
    prefs = rt.preferences(session)
    zone = ZoneInfo(prefs.timezone)
    nas = os.path.abspath(str(get_settings().nas_root))
    destinations = {r["id"]: r for r in _rows(session, Destination)}
    media = {r["id"]: r for r in _rows(session, MediaItem)}
    ledger = {r["id"]: r for r in _rows(session, UploadedClip) if r["status"] == "done"}
    probes = {r["archive_id"]: r for r in _rows(session, RoadtripClipMetadata)}
    dates = {r["id"]: r for r in _rows(session, RecordingDate)}
    dates_by_path = defaultdict(list)
    for row in dates.values():
        dates_by_path[row["path"]].append(row)
    corrected = {}
    for copy in _rows(session, CorrectedArchive):
        for path in (copy["original_path"], copy["corrected_path"]):
            if path:
                corrected[(copy["destination_id"], os.path.abspath(path))] = copy
    candidates, excluded, total = [], Counter(), 0
    for archived in _rows(session, ArchiveFile):
        if archived["kind"] != "video" or not archived["present"]:
            continue
        total += 1
        dest = destinations.get(archived["destination_id"])
        if not dest or dest["type"] != "local" or not dest["base_path"] or not archived["eligible"]:
            excluded["not_eligible_nas_video"] += 1
            continue
        root = os.path.abspath(archived["root_path"])
        absolute = _absolute(root, archived["path"])
        if os.path.commonpath((nas, root)) != nas or not absolute or os.path.abspath(dest["base_path"]) != root:
            excluded["outside_configured_nas"] += 1
            continue
        parts = Path(os.path.relpath(absolute, root)).parts
        if any(part.startswith(".") for part in parts) or any(p.lower() in ("trips", "renders") for p in parts[:-1]):
            excluded["derived_or_private_video"] += 1
            continue
        entry = ledger.get(archived["ledger_id"])
        item = media.get(entry["source_media_id"]) if entry else None
        if item and item["derived"]:
            excluded["derived_or_private_video"] += 1
            continue
        copy = corrected.get((archived["destination_id"], absolute))
        observation = dates.get(copy["recording_id"]) if copy else None
        if observation is None and item:
            possibilities = [r for r in dates_by_path[item["path"]]
                if r["size_bytes"] == archived["size_bytes"] and
                (not r["checksum"] or not entry["checksum"] or r["checksum"] == entry["checksum"])]
            identities = {(str(r["corrected_time"] or r["original_time"]), r["time_source"], r["status"]) for r in possibilities}
            if len(identities) > 1:
                excluded["ambiguous_date_observation"] += 1
                continue
            observation = max(possibilities, key=lambda r: (r["revision"], r["id"]), default=None)
        probe = probes.get(archived["id"])
        identity = rt._metadata_identity(SimpleNamespace(**archived))
        if probe and (probe["identity"] != identity or probe["error"]):
            probe = None
        approved = bool(observation and observation["status"] in ("trusted", "confirmed"))
        if any(p.lower() == "holding" for p in parts[:-1]) and not approved:
            excluded["unconfirmed_holding_date"] += 1
            continue
        if observation:
            reasons = _json(observation["reasons"], [])
            if observation["time_source"] != "metadata" and not (observation["status"] == "confirmed" and observation["corrected_time"]):
                excluded["mtime_only_date"] += 1
                continue
            if observation["status"] == "applying" or (not approved and (not reasons or any(r not in SOFT_REVIEW_REASONS for r in reasons))):
                excluded["reset_reused_or_uncertain_date"] += 1
                continue
            if observation["corrected_time"] is not None and not approved:
                excluded["unconfirmed_date_correction"] += 1
                continue
            value = observation["corrected_time"] or observation["original_time"]
            duration = observation["duration_s"] or (item["duration_s"] if item else None)
            provenance = "confirmed_date_correction" if observation["corrected_time"] else "camera_metadata"
        elif probe and probe["observed_time"]:
            value, duration, provenance = probe["observed_time"], probe["duration_s"], "probed_camera_metadata"
        else:
            excluded["missing_metadata_provenance"] += 1
            continue
        try:
            start, duration = rt.timestamp(value), float(duration or 0)
            end = start + dt.timedelta(seconds=duration)
            if not math.isfinite(duration) or not 0 < duration <= 24 * 3600:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            excluded["invalid_date_or_duration"] += 1
            continue
        if start.year < 2005 or end > now + dt.timedelta(minutes=5):
            excluded["reset_or_future_date"] += 1
            continue
        month, _ = rt.folder_month(archived["path"])
        if not month or start.astimezone(zone).strftime("%Y-%m") != month:
            excluded["archive_month_mismatch"] += 1
            continue
        metadata = {"id": f"archive:{archived['id']}", "archive_id": archived["id"],
            "media_id": item["id"] if item else None, "recording_id": observation["id"] if observation else None,
            "filename": Path(absolute).name, "path": absolute, "month": month,
            "source_path": item["path"] if item else observation["path"] if observation else None,
            "duration_s": duration, "start_time": rt.iso(start), "end_time": rt.iso(end),
            "observed_time": rt.iso(observation["original_time"]) if observation and observation["original_time"] else rt.iso(value),
            "timing_basis": provenance, "approved_date": approved, "file_identity": identity,
            "date_revision": observation["revision"] if observation else None,
            "size_bytes": archived["size_bytes"], "mtime_ns": archived["mtime_ns"],
            "ctime_ns": archived["ctime_ns"], "device": archived["device"], "inode": archived["inode"]}
        metadata["input_signature"] = _hash(metadata)
        candidates.append((bool(copy and copy["corrected_path"] and os.path.abspath(copy["corrected_path"]) == absolute), metadata))
    # Original/corrected metadata copies represent one recording, not two rides.
    selected, seen = [], set()
    for is_corrected, clip in sorted(candidates, key=lambda item: (not item[0], item[1]["archive_id"])):
        key = ("recording", clip["recording_id"]) if clip["recording_id"] else ("media", clip["media_id"]) if clip["media_id"] else ("archive", clip["archive_id"])
        if key in seen:
            excluded["duplicate_recording_copy"] += 1
            continue
        seen.add(key)
        selected.append(clip)
    runs, regressions = defaultdict(list), set()
    for clip in selected:
        if clip["source_path"]:
            parent = Path(clip["source_path"]).parent
            # Camera source paths retain folder/counter order; NAS filenames
            # alone cannot distinguish two different cameras reliably.
            runs[str(parent.parent if "MEDIA" in parent.name.upper() else parent)].append(clip)
    for run in runs.values():
        ordered = sorted(run, key=rt._camera_order)
        for before, after in zip(ordered, ordered[1:]):
            if rt.timestamp(after["start_time"]) < rt.timestamp(before["start_time"]) - dt.timedelta(seconds=2):
                regressions.update(c["id"] for c in (before, after) if not c["approved_date"])
    if regressions:
        selected = [c for c in selected if c["id"] not in regressions]
        excluded["camera_sequence_regression"] += len(regressions)
    return sorted(selected, key=lambda c: (c["start_time"], c["id"])), {"indexed_video_count": total,
        "eligible_clip_count": len(selected), "excluded_clip_count": sum(excluded.values()), "exclusions": dict(excluded)}


def _gps_tokens(session, source, start, end, timezone):
    zone = ZoneInfo(timezone)
    first, last = rt.timestamp(start).astimezone(zone).date().isoformat(), rt.timestamp(end).astimezone(zone).date().isoformat()
    return [list(row) for row in session.query(LocationDay.id, LocationDay.day, LocationDay.point_count, LocationDay.imported_at).filter(
        LocationDay.source == source, LocationDay.day >= first, LocationDay.day <= last).order_by(LocationDay.day)]


def _ride_input(session, source, outing, clips, prefs):
    first = min(rt.timestamp(c["start_time"]) for c in clips) - dt.timedelta(seconds=prefs.max_gap_s)
    if any(c.get("association_offset_s") for c in clips):
        first -= dt.timedelta(days=1)  # Includes the stationary-departure evidence.
    last = max(rt.timestamp(c["end_time"]) for c in clips) + dt.timedelta(seconds=prefs.max_gap_s)
    return _hash({"source": source, "outing": rt.outing_dict(outing), "settings": rt.preferences_dict(prefs),
        "clips": [[c["input_signature"], c["start_time"], c["end_time"], c.get("association_offset_s", 0)] for c in clips],
        "gps": _gps_tokens(session, source, first, last, prefs.timezone)})


def ride_summary(row):
    payload = _json(row.payload, {})
    return {"id": row.id, "name": row.name, "start_time": rt.iso(row.start_time), "end_time": rt.iso(row.end_time),
        "clip_count": row.clip_count, "duration_s": row.duration_s, "distance_m": row.distance_m,
        "confidence": row.confidence, "confidence_label": row.confidence_label, "status": row.status,
        "association_kind": "automatic", "source": row.source, "outing_id": row.outing_id,
        "input_signature": row.input_signature, "reasons": payload.get("reasons", []),
        "coverage": payload.get("coverage"), "evidence": payload.get("evidence", {}),
        "route_preview": payload.get("route_preview", []),
        "route_preview_truncated": payload.get("route_preview_truncated", False),
        "updated_at": rt.iso(row.updated_at)}


def list_rides(session):
    state = session.get(AutoRideDiscovery, 1)
    report = _json(state.report, {}) if state else {}
    rides = [ride_summary(row) for row in session.query(AutoRide).filter_by(active=True, status="ready").order_by(AutoRide.start_time.desc())]
    return {"rides": rides, "summary": report | {"ride_count": len(rides),
        "stale_count": session.query(AutoRide).filter_by(status="stale").count(),
        "last_discovered_at": rt.iso(state.updated_at) if state else None}}


def _cluster(clips):
    result, last_end = [], None
    for clip in sorted(clips, key=lambda c: (c["start_time"], c["id"])):
        if last_end is None or (rt.timestamp(clip["start_time"]) - last_end).total_seconds() > CLUSTER_GAP_S:
            result.append([])
            last_end = rt.timestamp(clip["end_time"])
        result[-1].append(clip)
        last_end = max(last_end, rt.timestamp(clip["end_time"]))
    return result


def _route_preview(tracks, clips, max_points=120):
    """A tiny abstract outline, stripped of coordinates/scale, for gallery cards."""
    from .roadtrip_map import TrackIndex
    from .roadtrip_collection_map import _merge_intervals, _limited
    series = [{"time": p["t"], "latitude": p["lat"], "longitude": p["lon"], "segment": i}
        for i, piece in enumerate(tracks) for p in piece]
    index = TrackIndex(series)
    pieces = [piece for a, b in _merge_intervals([(rt.timestamp(c["start_time"]), rt.timestamp(c["end_time"])) for c in clips])
        for piece in index.interval(a, b)]
    if not pieces:
        return [], False
    if max_points < 2:
        return [], True
    original_count = sum(len(piece) for piece in pieces)
    if len(pieces) > max_points // 2:
        count = max_points // 2
        pieces = [pieces[round(i * (len(pieces) - 1) / (count - 1))] for i in range(count)] if count > 1 else [pieces[0]]
    budgets = [2] * len(pieces)
    remaining = max_points - sum(budgets)
    while remaining > 0:
        changed = False
        for i, piece in enumerate(pieces):
            if budgets[i] < len(piece):
                budgets[i] += 1
                remaining -= 1
                changed = True
                if not remaining:
                    break
        if not changed:
            break
    sampled = [_limited(piece, limit)[0] for piece, limit in zip(pieces, budgets)]
    flat = [p for piece in sampled for p in piece]
    longitude = flat[0]["longitude"]
    cos_lat = max(.01, math.cos(math.radians(sum(p["latitude"] for p in flat) / len(flat))))
    projected = [[(((p["longitude"] - longitude + 180) % 360 - 180) * cos_lat, -p["latitude"]) for p in piece] for piece in sampled]
    xs, ys = [x for piece in projected for x, y in piece], [y for piece in projected for x, y in piece]
    mid_x, mid_y = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    scale = max(max(xs) - min(xs), max(ys) - min(ys), 1e-12)
    normalized = [[[round(.5 + .8 * (x - mid_x) / scale, 4), round(.5 + .8 * (y - mid_y) / scale, 4)] for x, y in piece] for piece in projected]
    return normalized, original_count > sum(len(piece) for piece in normalized)


def _upsert(session, source, outing, clips, coverage, distance, reasons, prefs, outline, truncated):
    first, last = clips[0]["start_time"], max(c["end_time"] for c in clips)
    signature = _hash([source, outing.signature, clips[0]["recording_id"] or clips[0]["archive_id"]])
    row = session.query(AutoRide).filter_by(signature=signature).one_or_none()
    input_signature = _ride_input(session, source, outing, clips, prefs)
    confidence = .96 if coverage >= .8 else .88 if coverage >= .5 else .85
    shift = clips[0].get("association_offset_s", 0)
    if shift:
        confidence = min(confidence, .9)
    evidence = {"temporal_coverage": min(c["temporal_coverage"] for c in clips), "gps_coverage": round(coverage, 3),
        "gps_supported_clip_count": sum(c.get("gps_coverage", 0) >= MIN_GPS_COVERAGE for c in clips), "approved_date_count": sum(c["approved_date"] for c in clips),
        "clock_shift_seconds": shift, "cluster_gap_seconds": CLUSTER_GAP_S,
        "alignment_basis": clips[0].get("alignment_basis", "evidenced_timestamp")}
    payload = _dump({"reasons": reasons, "coverage": round(coverage, 3), "evidence": evidence,
        "route_preview": outline, "route_preview_truncated": truncated})
    if row is None:
        row = AutoRide(signature=signature, input_signature=input_signature, source=source, outing_id=outing.id,
            name="Ride · " + rt.timestamp(first).astimezone(ZoneInfo(prefs.timezone)).strftime("%d %b %Y, %H:%M"),
            start_time=rt.timestamp(first), end_time=rt.timestamp(last), clip_count=len(clips),
            duration_s=sum(c["duration_s"] for c in clips), distance_m=distance, confidence=confidence,
            confidence_label="GPS-aligned estimate" if shift else "High confidence", status="ready", active=True, payload=payload)
        session.add(row)
        session.flush()
    elif row.input_signature == input_signature and row.payload == payload and row.active and row.status == "ready":
        return row
    else:
        row.input_signature, row.start_time, row.end_time = input_signature, rt.timestamp(first), rt.timestamp(last)
        if row.name.startswith("Ride · "):
            row.name = "Ride · " + rt.timestamp(first).astimezone(ZoneInfo(prefs.timezone)).strftime("%d %b %Y, %H:%M")
        row.clip_count, row.duration_s, row.distance_m = len(clips), sum(c["duration_s"] for c in clips), distance
        row.confidence, row.payload, row.active, row.status = confidence, payload, True, "ready"
        row.confidence_label = "GPS-aligned estimate" if shift else "High confidence"
        row.updated_at = utcnow()
    old = {m.clip_id: m for m in session.query(AutoRideClip).filter_by(ride_id=row.id)}
    for position, clip in enumerate(clips):
        member = old.pop(clip["id"], None)
        if member is None:
            member = AutoRideClip(ride_id=row.id, clip_id=clip["id"], archive_id=clip["archive_id"], position=position)
            session.add(member)
        member.position, member.metadata_json = position, _dump(clip)
    for member in old.values():
        session.delete(member)
    return row


def discover(session, month=None, *, now=None, ctx=None):
    if month:
        rt.month_bounds(month)
    prefs = rt.preferences(session)
    source = "ha:" + prefs.entity_id if prefs.entity_id else ""
    clips, report = eligible_clips(session, now=now)
    if month:
        clips = [c for c in clips if c["month"] == month]
    outings = list(session.query(RoadtripOuting).filter_by(source=source, active=True)) if source else []
    exclusions, review, accepted, deferred, plans = Counter(report["exclusions"]), [], set(), set(), []
    by_month = defaultdict(list)
    for clip in clips:
        by_month[clip["month"]].append(clip)
    for bucket_month, bucket in sorted(by_month.items()):
        if ctx and ctx.is_cancelled():
            from .jobs import JobCancelled
            raise JobCancelled()
        if not outings:
            exclusions["no_configured_ha_history"] += len(bucket)
            continue
        first, last = rt.timestamp(bucket[0]["start_time"]) - dt.timedelta(seconds=GPS_NEIGHBOUR_S), max(rt.timestamp(c["end_time"]) for c in bucket) + dt.timedelta(seconds=GPS_NEIGHBOUR_S)
        try:
            points = rt.load_month(session, bucket_month, source, start_time=first, end_time=last)
            points, _ = rt.clean_points(points, prefs)
        except (ValueError, RuntimeError, OSError, KeyError, TypeError):
            deferred.add(bucket_month)
            exclusions["gps_history_temporarily_unavailable"] += len(bucket)
            continue
        times = [rt.timestamp(p["t"]) for p in points]
        from .auto_ride_alignment import align_confirmed_returns
        bucket = align_confirmed_returns(bucket, outings, points, prefs)
        tracks, supported = {}, defaultdict(list)
        for clip in bucket:
            start, end = rt.timestamp(clip["start_time"]), rt.timestamp(clip["end_time"])
            candidates = []
            for outing in outings:
                if clip.get("alignment_outing_id") and clip["alignment_outing_id"] != outing.id:
                    continue
                left, right = rt.timestamp(outing.start_time), rt.timestamp(outing.end_time)
                temporal = max(0, (min(end, right) - max(start, left)).total_seconds()) / clip["duration_s"]
                if temporal >= MIN_TEMPORAL_COVERAGE:
                    candidates.append((outing, temporal))
            if len(candidates) != 1:
                exclusions["ambiguous_ha_outing" if candidates else "no_exact_ha_time_overlap"] += 1
                continue
            outing, temporal = candidates[0]
            if outing.id not in tracks:
                tracks[outing.id] = rt._track([p for p in points if rt.timestamp(outing.start_time) <= rt.timestamp(p["t"]) <= rt.timestamp(outing.end_time)], prefs)
            speed = rt.clip_speed(tracks[outing.id], start, clip["duration_s"])
            nearby = [t for t in times[bisect_left(times, start - dt.timedelta(seconds=GPS_NEIGHBOUR_S)):bisect_right(times, end + dt.timedelta(seconds=GPS_NEIGHBOUR_S))]
                if rt.timestamp(outing.start_time) <= t <= rt.timestamp(outing.end_time)]
            close = (len(nearby) >= 2 and min(nearby) <= start and max(nearby) >= end and
                min(abs((t - start).total_seconds()) for t in nearby) <= GPS_NEIGHBOUR_S and
                min(abs((t - end).total_seconds()) for t in nearby) <= GPS_NEIGHBOUR_S)
            if speed["coverage"] < MIN_GPS_COVERAGE and not (clip["approved_date"] and close) and not clip.get("alignment_basis"):
                exclusions["insufficient_gps_corroboration"] += 1
                continue
            supported[outing.id].append(clip | {"temporal_coverage": round(temporal, 3), "gps_coverage": speed["coverage"],
                "gps_distance_m": speed["distance_m"], "nearby_fix_count": len(nearby)})
        for outing_id, group in supported.items():
            outing = next(o for o in outings if o.id == outing_id)
            for cluster in _cluster(group):
                coverage = sum(c["gps_coverage"] * c["duration_s"] for c in cluster) / sum(c["duration_s"] for c in cluster)
                if len(cluster) < MIN_CLUSTER_CLIPS or (not all(c["approved_date"] for c in cluster) and coverage < STRONG_GPS_COVERAGE):
                    review.append({"start_time": cluster[0]["start_time"], "end_time": cluster[-1]["end_time"],
                        "clip_count": len(cluster), "reasons": ["Several consistent recordings and stronger GPS coverage are needed before automatic association."]})
                    continue
                reasons = [f"{len(cluster)} adjacent recordings use evidenced timestamps in their archive month.",
                    "Recording times overlap one configured Home Assistant outing without shifting the camera clock.",
                    f"Continuous GPS covers {coverage:.0%} of recorded time; nearby fixes corroborate the dates."]
                if cluster[0].get("association_offset_s"):
                    reasons[1] = "Estimated video times align the complete confirmed camera sequence with a stationary departure and HA return home; the full run has at least 80% GPS coverage."
                    reasons.append(f"The earlier date correction was {abs(cluster[0]['association_offset_s']) / 60:.1f} minutes late. This map uses estimated association times; original files and confirmed archive dates are unchanged.")
                if coverage < STRONG_GPS_COVERAGE:
                    reasons.append("GPS gaps remain unlocated; speed estimates require 80% per-video coverage.")
                # Sum only the union of recorded intervals to avoid duplicate cameras inflating distance.
                from .roadtrip_collection_map import _merge_intervals
                distance = sum(rt.clip_speed(tracks[outing_id], a, (b - a).total_seconds())["distance_m"] or 0
                    for a, b in _merge_intervals([(rt.timestamp(c["start_time"]), rt.timestamp(c["end_time"])) for c in cluster]))
                outline, cut = _route_preview(tracks[outing_id], cluster)
                plans.append((source, outing, cluster, coverage, round(distance, 1) if coverage else None, reasons, prefs, outline, cut))
        # Plans contain clip metadata and an abstract outline, never raw GPS.
        # Release one month's points before the next potentially slow NAS read.
        del points, times, tracks, supported
    # All NAS reads are complete before acquiring the SQLite writer lock.
    for plan in plans:
        row = _upsert(session, *plan)
        accepted.add(row.id)
    current_clips = {c["archive_id"]: c for c in clips}
    for row in session.query(AutoRide).filter_by(active=True):
        local_month = rt.timestamp(row.start_time).astimezone(ZoneInfo(prefs.timezone)).strftime("%Y-%m")
        safe_to_defer = local_month in deferred
        if safe_to_defer:
            members = [_json(m.metadata_json, {}) for m in session.query(AutoRideClip).filter_by(ride_id=row.id)]
            safe_to_defer = row.source == source and all(c.get("archive_id") in current_clips and
                current_clips[c["archive_id"]]["input_signature"] == c.get("input_signature") for c in members)
        if row.id not in accepted and not safe_to_defer and (not month or local_month == month):
            row.active, row.status, row.updated_at = False, "stale", utcnow()
    report.update({"exclusions": dict(exclusions), "excluded_clip_count": sum(exclusions.values()),
        "review_count": len(review), "review_candidates": review[:100],
        "association_kind": "automatic", "source": source or None, "discovered_ride_count": len(accepted),
        "deferred_months": sorted(deferred), "policy": "Evidenced dates; a unique GPS-supported complete return can realign a confirmed correction. GPS gaps stay empty."})
    state = session.get(AutoRideDiscovery, 1)
    value = _dump(report)
    fingerprint = input_fingerprint(session)
    if state is None:
        session.add(AutoRideDiscovery(id=1, input_signature=fingerprint, report=value))
    elif state.input_signature != fingerprint or state.report != value:
        state.input_signature, state.report, state.updated_at = fingerprint, value, utcnow()
    session.flush()
    return report


def ride_clips(session, row, *, revalidate=True):
    clips = [_json(m.metadata_json, {}) for m in session.query(AutoRideClip).filter_by(ride_id=row.id).order_by(AutoRideClip.position)]
    if revalidate:
        if not row.active or row.status != "ready":
            raise ValueError("Automatic ride is stale; run automatic discovery again")
        current = {c["archive_id"]: c for c in eligible_clips(session)[0]}
        if any(c["archive_id"] not in current or current[c["archive_id"]]["input_signature"] != c["input_signature"] for c in clips):
            raise ValueError("Ride footage or approved dates changed; run automatic discovery again")
        outing = session.get(RoadtripOuting, row.outing_id)
        if not outing or not outing.active or _ride_input(session, row.source, outing, clips, rt.preferences(session)) != row.input_signature:
            raise ValueError("Ride GPS history changed; run automatic discovery again")
    return clips


def ride_detail(session, ride_id):
    row = session.get(AutoRide, ride_id)
    if row is None or not row.active or row.status != "ready":
        raise ValueError("Automatic ride is unavailable or needs rediscovery")
    clips = ride_clips(session, row)
    start = rt.timestamp(row.start_time)
    prefs = rt.preferences(session)
    outing = session.get(RoadtripOuting, row.outing_id)
    points = rt.load_month(session, clips[0]["month"], row.source,
        start_time=start - dt.timedelta(seconds=prefs.max_gap_s),
        end_time=rt.timestamp(row.end_time) + dt.timedelta(seconds=prefs.max_gap_s))
    group = {"key": row.signature, "label": row.name, "clips": clips, "clip_count": len(clips),
        "month": outing.month, "relative_timing_available": False}
    result = rt.preview(session, outing.month, row.signature, row.outing_id, rt.iso(start),
        {c["id"]: (rt.timestamp(c["start_time"]) - start).total_seconds() for c in clips}, group, False, track_points=points)
    result["ride"], result["association_kind"], result["provisional"] = ride_summary(row), "automatic", False
    for clip in result["clips"]:
        original = next(c for c in clips if c["id"] == clip["id"])
        clip["timing_basis"], clip["provisional"] = original.get("alignment_basis", original["timing_basis"]), False
        if original.get("association_offset_s"):
            clip["association_offset_s"] = original["association_offset_s"]
            clip["unadjusted_start_time"] = original["unadjusted_start_time"]
        if clip.get("location"):
            clip["location_status"] = "automatic"
    # Automatic evidence is not a human confirmation, and raw GPS remains transient.
    result["warning"] = "Automatic association from evidenced recording times and Home Assistant GPS. " + " ".join(ride_summary(row)["reasons"])
    from .roadtrip_map import TrackIndex
    from .roadtrip_collection_map import _merge_intervals, _limited, MAX_MAP_POINTS
    index = TrackIndex(result["time_series"], prefs.max_gap_s)
    series, distance, segment, truncated = [], 0.0, 0, False
    for left, right in _merge_intervals([(rt.timestamp(c["start_time"]), rt.timestamp(c["end_time"])) for c in clips]):
        for piece in index.interval(left, right):
            previous = None
            for point in piece:
                if previous:
                    distance += rt.distance({"lat": previous["latitude"], "lon": previous["longitude"]},
                        {"lat": point["latitude"], "lon": point["longitude"]})
                point["distance_m"], point["segment"] = round(distance, 1), segment
                previous = point
            kept, cut = _limited(piece, MAX_MAP_POINTS - len(series))
            series.extend(kept)
            truncated |= cut
            segment += 1
    features, count = [], 0
    for feature in result["clip_geojson"]["features"]:
        coordinates, cut = _limited(feature["geometry"]["coordinates"], MAX_MAP_POINTS - count)
        truncated |= cut
        if coordinates:
            features.append(feature | {"geometry": {"type": "LineString", "coordinates": coordinates}})
            count += len(coordinates)
    result["time_series"], result["geojson"] = series, {"type": "FeatureCollection", "features": features}
    result["clip_geojson"] = result["geojson"]
    result["outing"] = {"id": row.outing_id, "source": row.source,
        "start_time": rt.iso(row.start_time), "end_time": rt.iso(row.end_time),
        "duration_s": (rt.timestamp(row.end_time) - start).total_seconds(), "distance_m": row.distance_m}
    result["route_truncated"] = truncated
    if truncated:
        result["warning"] += " Displayed route/timeline are limited to 8,000 points; all video pins remain."
    return result
