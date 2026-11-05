"""Conservative return-journey alignment for an already confirmed camera run.

This estimates association times only. Camera files, date approvals and archive
metadata are never changed. An ambiguous fit remains for manual review.
"""
from bisect import bisect_right
from collections import defaultdict
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

from . import roadtrips as rt

RETURN_GAP_S = 60 * 60
MAX_SHIFT_S = 3 * 60 * 60
CAMERA_UTC_SHIFTS_S = (-60 * 60, 60 * 60)
MIN_CAMERA_SHIFT_GAIN = 3


def align_camera_offset(clips, outings, min_coverage):
    """Remove a whole-hour camera clock offset that the GPS outings disprove.

    The camera keeps a fixed offset from UTC from whenever it was last set and
    does not follow daylight saving, so neither the calendar nor the configured
    timezone says what it is. Let the month's outings decide, and only when one
    shift clearly places more recordings inside an outing than no shift does.
    Confirmed corrections are already real UTC times and are left alone.
    """
    camera = [c for c in clips if c['timing_basis'] != 'confirmed_date_correction']
    spans = [(rt.timestamp(o.start_time), rt.timestamp(o.end_time)) for o in outings]
    if not camera or not spans:
        return clips

    def placed(shift):
        found = set()
        for clip in camera:
            start = rt.timestamp(clip['start_time']) + dt.timedelta(seconds=shift)
            end = rt.timestamp(clip['end_time']) + dt.timedelta(seconds=shift)
            inside = [max(0, (min(end, right) - max(start, left)).total_seconds()) / clip['duration_s'] for left, right in spans]
            if sum(value >= min_coverage for value in inside) == 1:
                found.add(clip['id'])
        return found

    fits = {shift: placed(shift) for shift in (0, *CAMERA_UTC_SHIFTS_S)}
    shift = max(CAMERA_UTC_SHIFTS_S, key=lambda value: len(fits[value]))
    other = -shift
    reachable = len(set().union(*fits.values()))
    gain = len(fits[shift]) - len(fits[0])
    if gain < max(MIN_CAMERA_SHIFT_GAIN, reachable * .1) or len(fits[shift]) <= len(fits[other]):
        return clips
    moved = {c['id'] for c in camera}
    adjusted = [clip if clip['id'] not in moved else clip | {
        'start_time': rt.iso(rt.timestamp(clip['start_time']) + dt.timedelta(seconds=shift)),
        'end_time': rt.iso(rt.timestamp(clip['end_time']) + dt.timedelta(seconds=shift)),
        'unadjusted_start_time': clip['start_time'], 'unadjusted_end_time': clip['end_time'],
        'association_offset_s': shift, 'alignment_basis': 'camera_utc_offset',
    } for clip in clips]
    return sorted(adjusted, key=lambda c: (c['start_time'], c['id']))


def align_confirmed_returns(clips, outings, points, prefs):
    if prefs.home_lat is None or prefs.home_lon is None or not points:
        return clips
    zone = ZoneInfo(prefs.timezone)
    times = [rt.timestamp(p['t']) for p in points]
    groups = defaultdict(list)
    for clip in clips:
        parent = Path(clip.get('source_path') or clip['path']).parent
        groups[(str(parent), rt.timestamp(clip['start_time']).astimezone(zone).date())].append(clip)
    replacements = {}
    for (_, day), run in groups.items():
        clusters, last = [], None
        for clip in sorted(run, key=lambda c: (c['start_time'], c['id'])):
            if last is None or (rt.timestamp(clip['start_time']) - last).total_seconds() > RETURN_GAP_S:
                clusters.append([])
            clusters[-1].append(clip)
            last = max(last or rt.timestamp(clip['end_time']), rt.timestamp(clip['end_time']))
        for cluster in clusters:
            if len(cluster) < 3 or not all(c['approved_date'] and c['timing_basis'] == 'confirmed_date_correction' for c in cluster):
                continue
            start = min(rt.timestamp(c['start_time']) for c in cluster)
            end = max(rt.timestamp(c['end_time']) for c in cluster)
            span = (end - start).total_seconds()
            if not 30 * 60 <= span <= 12 * 3600:
                continue
            fits = []
            for outing in outings:
                arrival = rt.timestamp(outing.end_time)
                shift = (arrival - end).total_seconds()
                # A corrected clock running late can truncate a complete return.
                # Do not shift raw/unapproved dates or make up a different day.
                # `complete` also requires observing the outbound departure
                # from home, which may be days earlier and absent from HA.
                # Validate the actual return-home fix below instead.
                if not -MAX_SHIFT_S <= shift < -5 * 60 or arrival.astimezone(zone).date() != day:
                    continue
                departure = start + dt.timedelta(seconds=shift)
                if departure < rt.timestamp(outing.start_time) or departure.astimezone(zone).date() != day:
                    continue
                i = bisect_right(times, departure) - 1
                if i < 0 or (departure - times[i]).total_seconds() > 5 * 60:
                    continue
                origin = points[i]
                # The departure must be at a real long stop, not an arbitrary
                # point halfway down the motorway. Allow a large venue/car park.
                dwell_start = times[i]
                for p, t in zip(reversed(points[:i]), reversed(times[:i])):
                    if (departure - t).total_seconds() > 24 * 3600 or rt.distance(p, origin) > 1500:
                        break
                    dwell_start = t
                dwell = (departure - dwell_start).total_seconds() >= 45 * 60
                moving = any(departure < t <= departure + dt.timedelta(minutes=15) and rt.distance(p, origin) >= 3000
                    for p, t in zip(points[i + 1:], times[i + 1:]))
                home = {'lat': prefs.home_lat, 'lon': prefs.home_lon}
                arrived = any(abs((t - arrival).total_seconds()) <= 60 and rt.distance(p, home) <= prefs.home_radius_m
                    for p, t in zip(points, times))
                if not (dwell and moving and arrived):
                    continue
                track = rt._track([p for p, t in zip(points, times) if departure - dt.timedelta(minutes=15) <= t <= arrival], prefs)
                coverage = sum(rt.clip_speed(track, rt.timestamp(c['start_time']) + dt.timedelta(seconds=shift), c['duration_s'])['coverage'] * c['duration_s']
                    for c in cluster) / sum(c['duration_s'] for c in cluster)
                if coverage >= .8:
                    fits.append((shift, outing.id))
            if len(fits) != 1:
                continue
            shift, outing_id = fits[0]
            for clip in cluster:
                replacements[clip['id']] = clip | {
                    'start_time': rt.iso(rt.timestamp(clip['start_time']) + dt.timedelta(seconds=shift)),
                    'end_time': rt.iso(rt.timestamp(clip['end_time']) + dt.timedelta(seconds=shift)),
                    'unadjusted_start_time': clip['start_time'], 'unadjusted_end_time': clip['end_time'],
                    'association_offset_s': shift, 'alignment_outing_id': outing_id,
                    'alignment_basis': 'confirmed_return_duration',
                }
    return sorted((replacements.get(c['id'], c) for c in clips), key=lambda c: (c['start_time'], c['id']))
