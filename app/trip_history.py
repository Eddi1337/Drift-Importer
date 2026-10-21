"""Cached trip presentation and offline place labels; private GPS stays local."""
from __future__ import annotations
from collections import defaultdict
from functools import lru_cache
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

from . import roadtrips as rt
from .config import get_settings


@lru_cache(maxsize=1)
def _places():
    grid = defaultdict(list)
    for row in json.loads(Path(__file__).with_name('gazetteer').joinpath('places.json').read_text()):
        grid[(math.floor(row[1]), math.floor(row[2]))].append(row)
    return grid


@lru_cache(maxsize=2048)
def place_name(lat, lon):
    """Nearby settlement, not an address. Prefer a nearby major city in metros."""
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    candidates = []
    width = min(180, math.ceil(1 / max(.01, math.cos(math.radians(lat)))))
    for y in range(max(-90, math.floor(lat)-1), min(89, math.floor(lat)+1)+1):
        for x in range(math.floor(lon)-width, math.floor(lon)+width+1):
            for row in _places().get((y, (x+180)%360-180), []):
                distance = rt.distance({'lat':lat,'lon':lon}, {'lat':row[1],'lon':row[2]})
                if distance <= 40000:
                    candidates.append((distance, row))
    if not candidates:
        return None
    metros = [item for item in candidates if item[1][3] >= 500000 and item[0] <= 20000]
    _, row = min(metros or candidates, key=lambda item:item[0])
    return row[0]


def dated_title(start, timezone, origin=None, destination=None):
    date = rt.timestamp(start).astimezone(ZoneInfo(timezone))
    n = date.day
    suffix = 'th' if 10 < n % 100 < 14 else {1:'st',2:'nd',3:'rd'}.get(n % 10, 'th')
    route = (origin + ' to ' + destination if origin != destination else origin + ' area ride') if origin and destination else 'Route not yet located'
    return f'{n}{suffix} {date:%B %Y} – {route}'


def presentation(clips, prefs, tracks=None):
    origin = destination = None
    if tracks:
        from .roadtrip_map import TrackIndex
        series = [{'time':p['t'],'latitude':p['lat'],'longitude':p['lon'],'segment':i} for i,piece in enumerate(tracks) for p in piece]
        index = TrackIndex(series)
        pieces = [piece for c in clips for piece in index.interval(rt.timestamp(c['start_time']), rt.timestamp(c['end_time']))]
        points = sorted((p for piece in pieces for p in piece), key=lambda p:p['time'])
        if points:
            origin = place_name(round(points[0]['latitude'],4), round(points[0]['longitude'],4))
            destination = place_name(round(points[-1]['latitude'],4), round(points[-1]['longitude'],4))
    first = clips[0]
    key = hashlib.sha256(('cover-v1:' + first['file_identity'] + ':' + first['path']).encode()).hexdigest()
    return {'origin':origin,'destination':destination,'place_basis':'Nearby towns and cities from recorded GPS fixes' if origin or destination else None,
            'title':dated_title(first['start_time'],prefs.timezone,origin,destination),'cover_key':key}


def recording_groups(clips, used, prefs):
    from .auto_rides import _cluster, _hash
    groups = []
    # A gap of an hour separates recording runs. These are explicitly unlocated,
    # never GPS associations or inferred corrections to unreliable camera dates.
    for group in _cluster([c for c in clips if c['archive_id'] not in used]):
        groups.append({'id':_hash([c['archive_id'] for c in group])[:24],
            'association_kind':'recording_only','start_time':group[0]['start_time'],'end_time':max(c['end_time'] for c in group),
            'clip_count':len(group),'duration_s':sum(c['duration_s'] for c in group),'distance_m':None,
            'clip_ids':[c['archive_id'] for c in group],'input_signature':_hash([c['input_signature'] for c in group]),
            **presentation(group,prefs)})
    return sorted(groups,key=lambda g:g['start_time'],reverse=True)


def coverage_summary(session, clips, used):
    from .archive_models import ArchiveFile
    from .roadtrip_models import LocationDay, RoadtripClipMetadata
    from .auto_rides import _rows
    probes = {p['archive_id']:p for p in _rows(session,RoadtripClipMetadata)}
    dated = {c['archive_id'] for c in clips}
    months = defaultdict(lambda:{'video_count':0,'indexed_count':0,'dated_count':0,'matched_count':0,'gps_days':0,'gps_points':0})
    for a in _rows(session,ArchiveFile):
        month = rt.folder_month(a['path'])[0]
        if not a['present'] or a['kind']!='video' or not month or 'holding' in [p.lower() for p in Path(a['path']).parts[:-1]]:
            continue
        row=months[month];row['video_count']+=1
        row['indexed_count']+=int(a['id'] in probes)
        row['dated_count']+=int(a['id'] in dated)
        row['matched_count']+=int(a['id'] in used)
    source='ha:'+rt.preferences(session).entity_id
    for day in _rows(session,LocationDay):
        if day['source']==source and day['day'][:7] in months:
            row=months[day['day'][:7]];row['gps_days']+=int(day['point_count']>0);row['gps_points']+=day['point_count']
    return [{'month':month,**values} for month,values in sorted(months.items(),reverse=True)]


def cover_directory():
    from .storage import require_storage
    root=require_storage(Path(get_settings().nas_root))
    directory=root/'.drift'/'ride-renders'/'covers'
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Unsafe trip picture directory')
    return directory


def prepare_covers(session, ctx):
    """One small frame per recording run, independent of full film rendering."""
    from .auto_rides import eligible_clips, _json
    from .auto_ride_models import AutoRide, AutoRideClip, AutoRideDiscovery
    from .archive import _safe_root, _safe_stat, stat_identity
    clips={c['archive_id']:c for c in eligible_clips(session)[0]}
    targets=[]
    for row in session.query(AutoRide).filter_by(active=True,status='ready'):
        member=session.query(AutoRideClip).filter_by(ride_id=row.id).order_by(AutoRideClip.position).first()
        key=_json(row.payload,{}).get('cover_key')
        if member and key and member.archive_id in clips:targets.append((key,clips[member.archive_id]))
    state=session.get(AutoRideDiscovery,1)
    for group in _json(state.report if state else None,{}).get('recording_groups',[]):
        if group['clip_ids'][0] in clips:targets.append((group['cover_key'],clips[group['clip_ids'][0]]))
    session.commit()  # never hold SQLite's writer lock over NAS or ffmpeg calls
    directory=cover_directory();directory.mkdir(parents=True,exist_ok=True)
    completed=0
    with ctx.ffmpeg_semaphore,ctx.upload_semaphore:
        for i,(key,clip) in enumerate(targets):
            rt._cancel(ctx)
            target=directory/(key+'.jpg');temp=directory/(key+'.part.jpg')
            if target.is_file() and not target.is_symlink():continue
            path=Path(clip['path'])
            try:
                root=_safe_root(Path(get_settings().nas_root));relative=str(path.relative_to(root))
                before=stat_identity(_safe_stat(root,relative))
                # The uploader's NFS device number differs; all portable identity
                # fields must match before reading, and local identity after it.
                if any(str(before[k])!=str(clip[k]) for k in ('size_bytes','mtime_ns','ctime_ns','inode')):
                    continue
                command=[get_settings().ffmpeg,'-nostdin','-v','error','-threads','1','-ss',str(min(5,clip['duration_s']/3)),'-i',str(path),
                    '-frames:v','1','-vf','scale=320:-2','-threads','1','-q:v','5','-y',str(temp)]
                p=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);deadline=time.monotonic()+20
                try:
                    while p.poll() is None:
                        rt._cancel(ctx)
                        if time.monotonic()>deadline:raise ValueError('Cover read timed out')
                        time.sleep(.1)
                    if p.returncode or stat_identity(_safe_stat(root,relative))!=before:continue
                    if temp.is_file() and 0<temp.stat().st_size<512*1024:
                        temp.replace(target);completed+=1
                finally:
                    if p.poll() is None:p.kill();p.wait()
            except (OSError,ValueError,RuntimeError):
                pass  # A missing cover must not hide or invalidate a trip.
            finally:
                try:temp.unlink(missing_ok=True)
                except OSError:pass
            ctx.set_progress((i+1)/max(1,len(targets)),f'Preparing trip pictures {i+1}/{len(targets)}')
    return completed
