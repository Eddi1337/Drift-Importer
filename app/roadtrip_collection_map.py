"""Bounded collection maps showing only confirmed, selected video intervals."""
from __future__ import annotations

from collections import defaultdict

from . import roadtrip_collections as collections
from . import roadtrips as rt
from .roadtrip_map import TrackIndex
from .roadtrip_models import RoadtripMatch, RoadtripOuting

MAX_MAP_POINTS = 8000


def _aliases(clip):
    values = set(clip.get("aliases", [])) | {clip["id"]}
    for kind in ("media", "archive", "ledger"):
        if clip.get(f"{kind}_id"):
            values.add(f"{kind}:{clip[f'{kind}_id']}")
    if clip.get("current_id"):
        values.add(clip["current_id"])
    return values


def _unlocated(clip, number):
    return clip | {"number": number, "start_time": None, "end_time": None, "location": None,
        "location_date": None, "provisional": False, "coverage": 0, "covered_s": 0,
        "distance_m": None, "average_speed_kmh": None,
        "location_status": "ambiguous" if clip.get("alignment_conflict") else "unmatched",
        "speed_note": "No confirmed GPS location"}


def _limited(points, remaining):
    """Only simplify within a continuous piece; never connect across a gap."""
    if len(points) <= remaining:
        return list(points), False
    if remaining < 2:
        return [], True
    indices = sorted({round(i * (len(points) - 1) / (remaining - 1)) for i in range(remaining)})
    return [points[i] for i in indices], True


def _merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def _bucket_series(points, prefs):
    series = []
    for segment, piece in enumerate(rt._track(points, prefs)):
        previous = None
        for point in piece:
            speed = None
            if previous:
                seconds = (rt.timestamp(point["t"]) - rt.timestamp(previous["t"])).total_seconds()
                speed = round(rt.distance(previous, point) / seconds * 3.6, 1) if seconds else None
            series.append({"time": point["t"], "latitude": point["lat"], "longitude": point["lon"],
                "speed_kmh": speed, "segment": segment})
            previous = point
    return series


def collection_map(session, row, *, max_points=MAX_MAP_POINTS):
    if max_points < 2:
        raise ValueError("A map needs room for at least two route points")
    detail = collections.collection_detail(session, row)
    prefs = rt.preferences(session)
    clips = [_unlocated(clip, i) for i, clip in enumerate(detail["clips"], 1)]
    # A clip with conflicting saved dates is intentionally excluded, even if
    # either route could independently produce a plausible marker.
    selected = {clip["id"]: clip for clip in detail["clips"]
        if clip.get("alignment") and not clip.get("alignment_conflict")}
    by_match = defaultdict(list)
    for clip in selected.values():
        by_match[clip["alignment"]["match_id"]].append(clip)
    output = {clip["id"]: clip for clip in clips}
    buckets, errors = defaultdict(list), []
    for match_id, targets in by_match.items():
        match = session.get(RoadtripMatch, match_id)
        outing = session.get(RoadtripOuting, match.outing_id) if match and match.status == "confirmed" else None
        if outing is None:
            errors.append({"match_id": match_id, "message": "The confirmed journey is no longer available."})
            continue
        mapping = collections._json(match.mapping, {})
        group = mapping.get("group")
        if not isinstance(group, dict) or not isinstance(group.get("clips"), list):
            errors.append({"match_id": match_id, "message": "The saved journey needs its clip alignment reviewed."})
            continue
        # Keep confirmed timing visible even if the NAS is disconnected.
        for target in targets:
            output[target["id"]].update({"start_time": target["alignment"]["start_time"],
                "end_time": target["alignment"]["end_time"], "location_status": "no_gps",
                "timing_basis": target["alignment"]["timing_basis"]})
        buckets[(match.month, outing.source)].append((match, outing, mapping, targets))

    features, series, source_coverage = [], [], []
    route_points, distance_m, segment_number = 0, 0.0, 0
    truncated = False
    for (month, source), jobs in sorted(buckets.items()):
        first = min(rt.timestamp(outing.start_time) for _, outing, _, _ in jobs)
        last = max(rt.timestamp(outing.end_time) for _, outing, _, _ in jobs)
        try:
            # Date ranges are UTC-aware; load_month selects local cache days,
            # including DST and month boundaries. One bucket is held at a time.
            points = rt.load_month(session, month, source, start_time=first, end_time=last)
        except (ValueError, RuntimeError, OSError, KeyError, TypeError, OverflowError) as exc:
            errors.append({"month": month, "match_ids": [m.id for m, _, _, _ in jobs],
                "message": f"GPS history for {month} could not be read. Check the NAS or reimport this month's history.",
                "reason": type(exc).__name__})
            continue
        if not points:
            errors.append({"month": month, "match_ids": [m.id for m, _, _, _ in jobs],
                "message": f"No saved GPS locations are available for {month}; reimport this month's history."})
            continue
        try:
            bucket_series = _bucket_series(points, prefs)
        except (ValueError, KeyError, TypeError, OverflowError, AttributeError) as exc:
            errors.append({"month": month, "match_ids": [m.id for m, _, _, _ in jobs],
                "message": f"Saved GPS data for {month} could not be interpreted; reimport this month's history.",
                "reason": type(exc).__name__})
            continue
        covered_intervals = []
        for match, outing, mapping, targets in jobs:
            aliases = {alias: target for target in targets for alias in _aliases(target)}
            selected_snapshot, offsets, source_to_target = [], {}, {}
            try:
                anchor = rt.timestamp(mapping["start_time"])
                for source_clip in mapping["group"]["clips"]:
                    target = next((aliases[a] for a in _aliases(source_clip) if a in aliases), None)
                    if target is None:
                        continue
                    selected_snapshot.append(source_clip)
                    offsets[source_clip["id"]] = (rt.timestamp(target["alignment"]["start_time"]) - anchor).total_seconds()
                    source_to_target[source_clip["id"]] = target
                if not selected_snapshot:
                    raise ValueError("Saved files are no longer in the confirmed alignment")
                group = mapping["group"] | {"clips": selected_snapshot, "relative_timing_available": False,
                    "clip_count": len(selected_snapshot)}
                preview = rt.preview(session, match.month, match.group_key, match.outing_id,
                    mapping["start_time"], offsets, group, True, track_points=points)
            except (ValueError, RuntimeError, KeyError, TypeError, OverflowError) as exc:
                errors.append({"match_id": match.id,
                    "message": "The saved clip alignment needs review before its locations can be displayed.",
                    "reason": type(exc).__name__})
                continue
            for located in preview["clips"]:
                target = source_to_target[located["id"]]
                current = output[target["id"]]
                # Retain the collection's stable id, inventory metadata and
                # numbering; preview contributes GPS-derived fields only.
                current.update({key: located.get(key) for key in (
                    "start_time", "end_time", "location", "location_date", "location_status", "coverage",
                    "covered_s", "distance_m", "average_speed_kmh", "speed_note", "offset_s")})
                current["timing_basis"] = target["alignment"]["timing_basis"]
                covered_intervals.append((max(rt.timestamp(current["start_time"]), rt.timestamp(outing.start_time)),
                    min(rt.timestamp(current["end_time"]), rt.timestamp(outing.end_time))))
            for feature in preview["clip_geojson"]["features"]:
                target = source_to_target[feature["properties"]["clip_id"]]
                coordinates, cut = _limited(feature["geometry"]["coordinates"], max_points - route_points)
                truncated |= cut
                if coordinates:
                    properties = feature["properties"] | {"clip_id": target["id"],
                        "number": output[target["id"]]["number"], "match_id": match.id}
                    features.append(feature | {"properties": properties,
                        "geometry": {"type": "LineString", "coordinates": coordinates}})
                    route_points += len(coordinates)
        # The timeline and distance cover the union of filmed GPS intervals,
        # so simultaneous cameras or overlapping matches do not double distance.
        index = TrackIndex(bucket_series, prefs.max_gap_s)
        for start, end in _merge_intervals(covered_intervals):
            for piece in index.interval(start, end):
                source_coverage.append((rt.timestamp(piece[0]["time"]), rt.timestamp(piece[-1]["time"]), source))
                previous, piece_distance = None, 0.0
                for point in piece:
                    if previous:
                        increment = rt.distance({"lat": previous["latitude"], "lon": previous["longitude"]},
                            {"lat": point["latitude"], "lon": point["longitude"]})
                        distance_m += increment
                        piece_distance += increment
                    # Preserve true distance through later display decimation,
                    # then accumulate it in chronological order across sources.
                    point["_segment_distance_m"] = piece_distance
                    point["segment"] = segment_number
                    previous = point
                kept, cut = _limited(piece, max_points - len(series))
                truncated |= cut
                series.extend(kept)
                segment_number += 1
        # No location points survive in a process-wide collection cache.
        del points

    series.sort(key=lambda p: p["time"])
    latest_by_source, source_conflict = {}, False
    for start_time, end_time, source in sorted(source_coverage):
        if any(other != source and latest > start_time for other, latest in latest_by_source.items()):
            source_conflict = True
            break
        latest_by_source[source] = max(end_time, latest_by_source.get(source, end_time))
    segment_distances, chronological_distance = {}, 0.0
    for point in series:
        segment = point["segment"]
        local = point.pop("_segment_distance_m")
        chronological_distance += max(0, local - segment_distances.get(segment, 0.0))
        segment_distances[segment] = local
        point["distance_m"] = None if source_conflict else round(chronological_distance, 1)
    bounds = [clip for clip in clips if clip.get("start_time")]
    start = min((clip["start_time"] for clip in bounds), default=None)
    end = max((clip["end_time"] for clip in bounds), default=None)
    dates = {}
    located_count = 0
    for clip in clips:
        if clip.get("location"):
            located_count += 1
        if clip.get("location_date"):
            counts = dates.setdefault(clip["location_date"], {"date": clip["location_date"],
                "clip_count": 0, "located_clip_count": 0})
            counts["clip_count"] += 1
            counts["located_clip_count"] += bool(clip.get("location"))
    warning = "Only selected videos with an unambiguous confirmed GPS association are placed. GPS gaps remain empty; video dates and files are unchanged. Distance covers filmed GPS intervals."
    if errors:
        warning += " Some saved GPS routes could not be loaded; all video files remain listed."
    if truncated:
        warning += f" The displayed route and timeline are limited to {max_points:,} points; all video pins and files are retained."
    if source_conflict:
        warning += " Overlapping video intervals use different GPS sources; total distance is unavailable to avoid double-counting. Per-video speeds retain their own GPS source."
    # Avoid serializing a third complete copy of a 10,000-file member list.
    collection_summary = {key: value for key, value in detail.items() if key != "clips"}
    return {"collection": collection_summary, "group": {"label": row.name, "clips": clips},
        "outing": {"start_time": start, "end_time": end,
            "duration_s": (rt.timestamp(end) - rt.timestamp(start)).total_seconds() if start and end else 0,
            "distance_m": None if source_conflict else round(distance_m, 1),
            "distance_basis": "ambiguous_sources" if source_conflict else "covered_video_intervals"},
        "clips": clips, "geojson": {"type": "FeatureCollection", "features": features},
        "clip_geojson": {"type": "FeatureCollection", "features": features},
        "time_series": series, "start_time": start, "provisional": False,
        "located_clip_count": located_count, "unlocated_clip_count": len(clips) - located_count,
        "location_dates": sorted(dates.values(), key=lambda x: x["date"]),
        "route_truncated": truncated, "distance_conflict": source_conflict, "map_errors": errors, "warning": warning}
