"""Clip locations from covered GPS intervals; never join a missing track."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import datetime as dt
import math
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc


def _stamp(value):
    if not isinstance(value, dt.datetime):
        value = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return value.replace(tzinfo=value.tzinfo or UTC).timestamp()


def _iso(seconds):
    return dt.datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def _interpolate(a, b, fraction):
    longitude = (b["longitude"] - a["longitude"] + 180) % 360 - 180
    return (a["latitude"] + (b["latitude"] - a["latitude"]) * fraction,
            (a["longitude"] + longitude * fraction + 180) % 360 - 180)


class TrackIndex:
    """Binary search within each continuous segment, bounded by one outing."""

    def __init__(self, series, max_gap_s=900):
        groups = []
        previous, current = None, []
        for point in sorted(series, key=lambda item: _stamp(item["time"])):
            if not all(math.isfinite(point.get(key, float("nan"))) for key in ("latitude", "longitude")):
                continue
            value = _stamp(point["time"])
            if previous is not None and (point.get("segment", 0) != previous.get("segment", 0)
                                         or value - _stamp(previous["time"]) > max_gap_s):
                if len(current) > 1:
                    groups.append(current)
                current = []
            if not current or value > _stamp(current[-1]["time"]):
                current.append(point)
            previous = point
        if len(current) > 1:
            groups.append(current)
        self.segments = [(points, [_stamp(point["time"]) for point in points]) for points in groups]

    def interval(self, start, end):
        if start is None or end is None:
            return []
        start, end = _stamp(start), _stamp(end)
        if not end > start:
            return []
        pieces = []
        for segment, times in self.segments:
            if end <= times[0] or start >= times[-1]:
                continue
            first = max(0, bisect_right(times, start) - 1)
            last = min(len(times) - 1, bisect_left(times, end))
            piece = []
            for i in range(first, last):
                left, right = max(start, times[i]), min(end, times[i + 1])
                if not right > left:
                    continue
                for value in (left, right):
                    if piece and piece[-1]["time"] == _iso(value):
                        continue
                    lat, lon = _interpolate(segment[i], segment[i + 1], (value - times[i]) / (times[i + 1] - times[i]))
                    piece.append({"time": _iso(value), "latitude": lat, "longitude": lon,
                                  "speed_kmh": segment[i + 1].get("speed_kmh")})
            if len(piece) > 1:
                pieces.append(piece)
        return pieces


def annotate_detail(detail, timezone="Europe/London", max_gap_s=900):
    """Add dated pins and clipped paths without persisting coordinates in SQL."""
    index = TrackIndex(detail.get("time_series", []), max_gap_s)
    zone = ZoneInfo(timezone)
    features, dates = [], {}
    located = 0
    for number, clip in enumerate(detail.get("clips", []), 1):
        clip["number"] = number
        pieces = index.interval(clip.get("start_time"), clip.get("end_time"))
        date = dt.datetime.fromtimestamp(_stamp(clip["start_time"]), zone).date().isoformat() if clip.get("start_time") else None
        clip["location_date"] = date
        clip["location"] = None
        clip["location_status"] = "no_gps"
        if pieces:
            first = pieces[0][0]
            clip["location"] = {key: first[key] for key in ("latitude", "longitude", "time")}
            clip["location"]["method"] = "GPS estimate"
            clip["location_status"] = "proposed" if detail.get("provisional", True) else "confirmed"
            located += 1
        if date:
            counts = dates.setdefault(date, {"date": date, "clip_count": 0, "located_clip_count": 0})
            counts["clip_count"] += 1
            counts["located_clip_count"] += bool(pieces)
        for segment, piece in enumerate(pieces):
            features.append({"type": "Feature", "properties": {"clip_id": clip["id"], "number": number,
                "filename": clip.get("filename", ""), "date": date, "segment": segment,
                "start_time": piece[0]["time"], "end_time": piece[-1]["time"]},
                "geometry": {"type": "LineString", "coordinates": [[p["longitude"], p["latitude"]] for p in piece]}})
    detail["clip_geojson"] = {"type": "FeatureCollection", "features": features}
    detail["location_dates"] = sorted(dates.values(), key=lambda value: value["date"])
    detail["located_clip_count"] = located
    detail["unlocated_clip_count"] = len(detail.get("clips", [])) - located
    return detail
