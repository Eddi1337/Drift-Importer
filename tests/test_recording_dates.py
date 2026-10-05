import datetime as dt
import hashlib
import json
import shutil
import struct
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import date_review as dates, tasks, workflow
from app.database import Base
from app.models import CameraDateCheck, CorrectedArchive, DateCorrection, Destination, Job, MediaItem, RecordingDate, UploadedClip, UploadState
from app.media import checksum

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 5, 10, tzinfo=UTC)

class Context:
    def __init__(self):
        self.ffmpeg_semaphore = threading.Semaphore(1)
        self.upload_semaphore = threading.Semaphore(1)
        self.progress = []
    def is_cancelled(self): return False
    def set_progress(self, *args): self.progress.append(args)
    def log(self, *args, **kwargs): pass


def atom(name, body):
    return struct.pack('>I4s', len(body)+8, name) + body


def movie(path, when, duration=300, version=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    seconds = int((when - dt.datetime(1904, 1, 1)).total_seconds())
    header = bytes([version, 0, 0, 0]) + (struct.pack('>IIII', seconds, seconds, 1000, int(duration*1000)) if version==0 else struct.pack('>QQIQ', seconds, seconds, 1000, int(duration*1000)))
    path.write_bytes(atom(b'ftyp', b'isom0000') + atom(b'mdat', b'video payload') + atom(b'moov', atom(b'mvhd', header)))
    return path


def row(i, folder, counter, when, **kwargs):
    return RecordingDate(id=i, path=f'/camera/DCIM/{folder}MEDIA/DVR{counter:05}.MP4', original_time=when, duration_s=300, time_source='metadata', status='review', reasons='[]', **kwargs)

@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    @contextmanager
    def scope():
        with maker() as s:
            yield s
            s.commit()
    for module in (dates, tasks, workflow): monkeypatch.setattr(module,'session_scope',scope)
    camera, nas = tmp_path/'camera', tmp_path/'NAS'
    camera.mkdir(); nas.mkdir()
    monkeypatch.setattr(workflow,'get_device_monitor',lambda: SimpleNamespace(get_devices=lambda:[{'path':str(camera),'dcim_path':str(camera/'DCIM')}]))
    monkeypatch.setattr(dates,'utcnow',lambda:NOW)
    with scope() as s:
        s.add(Destination(id=1,name='NAS',type='local',base_path=str(nas),is_default=True))
        s.add(Job(id=1,kind='date_scan'))
    return maker, camera, nas

@pytest.mark.parametrize('version',[0,1])
def test_header_time_and_duration_without_reading_video(tmp_path, version):
    when = dt.datetime(2026,9,20,14,45,55)
    path = movie(tmp_path/'video.mp4',when,48.348,version)
    assert dates.mp4_clock(path)==(when,48.348)
    path.write_bytes(b'broken')
    assert dates.mp4_clock(path)==(None,None)


def test_numeric_order_rollover_and_regression():
    a=row(1,107,9999,dt.datetime(2026,10,4,17))
    b=row(2,108,1,dt.datetime(2026,10,4,17,5))
    dates.analyse([b,a],{1,2},NOW)
    assert a.status==b.status=='trusted'
    b.original_time=dt.datetime(2026,10,4,16)
    dates.analyse([b,a],{2},NOW)
    assert a.status==b.status=='review'
    assert 'higher' in b.reasons

@pytest.mark.parametrize('when,expected',[(dt.datetime(2026,10,4,17),'trusted'),(dt.datetime(2026,10,5,8),'trusted'),(dt.datetime(2026,9,20,17),'review'),(dt.datetime(2026,10,5,12),'review')])
def test_recent_ride_rule(when,expected):
    recording=row(1,107,935,when)
    dates.analyse([recording],{1},NOW)
    assert recording.status==expected


def test_historical_files_and_reused_counters_are_held():
    older=row(1,106,99,dt.datetime(2026,8,1,17))
    latest=row(2,107,935,dt.datetime(2026,10,4,17))
    dates.analyse([older,latest],{1,2},NOW)
    assert older.status=='review' and latest.status=='trusted'
    replaced=row(3,107,900,dt.datetime(2026,10,4,18))
    dates.analyse([replaced,latest],{3},NOW,[latest])
    assert replaced.status=='review' and 'reused' in replaced.reasons


def test_confirmed_offset_reused_only_with_sequence_and_recent_end():
    anchor=row(1,107,935,dt.datetime(2026,9,20,17),corrected_time=dt.datetime(2026,10,4,17),reuse_offset=True)
    anchor.status='confirmed'
    new=row(2,108,1,dt.datetime(2026,9,21,8))
    dates.analyse([anchor,new],{2},NOW)
    assert new.status=='trusted' and new.corrected_time==dt.datetime(2026,10,5,8)
    reset=row(3,108,2,dt.datetime(2026,9,1,9))
    dates.analyse([anchor,new,reset],{3},NOW)
    assert reset.status=='review' and reset.corrected_time is None
    stale=row(4,108,3,dt.datetime(2026,9,23,8))
    dates.analyse([anchor,stale],{4},NOW)
    assert stale.status=='review'


def test_local_midnight_and_dst_input():
    assert dates.parse_local('2026-10-04T18:00:00')==dt.datetime(2026,10,4,17)
    assert dates.local(dt.datetime(2026,10,3,23,30)).date()==dt.date(2026,10,4)
    with pytest.raises(ValueError, match='clock change'): dates.parse_local('2026-10-25T01:30:00')
    assert dates.parse_local('2026-10-25T01:30:00+00:00')==dt.datetime(2026,10,25,1,30)


def scan(setup, counter=908, when=dt.datetime(2026,9,20,14,45,55)):
    maker,camera,nas=setup
    path=movie(camera/f'DCIM/107MEDIA/DVR{counter:05}.MP4',when)
    dates.scan_job(1,{'camera_root':str(camera),'connected_at':NOW.isoformat()},Context())
    with maker() as s:
        observation=s.query(RecordingDate).filter_by(path=str(path)).one()
        return observation.id,path


def test_scan_gate_and_replaced_file_fingerprint(setup,monkeypatch):
    maker,camera,nas=setup
    id,path=scan(setup)
    with maker() as s:
        assert dates.review_data(s,str(camera))['held']==1
        item=MediaItem(path=str(path),filename=path.name,source='device',derived=False)
        with pytest.raises(ValueError,match='Dates need'): dates.require_approved(s,item)
        assert dates.approved_paths(s,[path])==[]
        observation=s.get(RecordingDate,id); observation.status='confirmed'; observation.corrected_time=dt.datetime(2026,10,4,17)
        s.commit()
        assert dates.require_approved(s,item).id==id
        assert item.capture_time==dt.datetime(2026,10,4,17)
        path.write_bytes(path.read_bytes()+b'replaced')
        assert dates.approved_paths(s,[path])==[]


def index(s,path,source,**kwargs):
    item=s.query(MediaItem).filter_by(path=str(path)).first()
    if not item:
        item=MediaItem(path=str(path),filename=path.name,source=source,derived=False,size_bytes=path.stat().st_size,checksum=checksum(path),duration_s=300,codec='h264')
        s.add(item); s.flush()
    return item


def preview(setup,ids,anchor=None,when='2026-10-04T18:00:00',end=False,metadata=False):
    maker,camera,nas=setup
    with maker() as s:
        result=dates.make_preview(s,str(camera),ids,anchor or ids[0],when,end,False,1,metadata,False)
        s.commit()
        return result


def test_preview_preserves_breaks_and_requires_fresh_confirmation(setup):
    maker,camera,nas=setup
    a,_=scan(setup,908)
    b,_=scan(setup,909,dt.datetime(2026,9,20,15,10,55))
    plan=preview(setup,[a,b])
    first,last=plan['entries']
    assert dt.datetime.fromisoformat(last['new_time'])-dt.datetime.fromisoformat(first['new_time'])==dt.timedelta(minutes=25)
    assert first['new_time']=='2026-10-04T17:00:00+00:00'
    with maker() as s:
        assert s.get(RecordingDate,a).corrected_time is None
        result=dates.confirm_plan(s,plan['plan_id'])
        assert s.get(Job,result['job_id']).kind=='date_correction'
        assert s.get(RecordingDate,a).status=='applying'
        assert dates.confirm_plan(s,plan['plan_id'])==result


def test_stale_preview_and_clock_reset_range_rejected(setup):
    maker,camera,nas=setup
    a,path=scan(setup)
    plan=preview(setup,[a])
    path.write_bytes(path.read_bytes()+b'changed')
    with maker() as s:
        with pytest.raises(ValueError,match='stale'): dates.confirm_plan(s,plan['plan_id'])
    b,_=scan(setup,909,dt.datetime(2026,9,1,15))
    with pytest.raises(ValueError,match='clock reset'): preview(setup,[a,b])


def test_queued_upload_cannot_bypass_gate_and_trip_days_exclude_held(setup,monkeypatch):
    maker,camera,nas=setup
    id,path=scan(setup)
    with maker() as s:
        item=index(s,path,'device'); item.capture_time=dt.datetime(2026,9,20,14,45,55); s.commit(); mid=item.id
        assert workflow.recording_days(s)=={}
    monkeypatch.setattr(tasks,'probe',lambda p:{'duration_s':300,'codec':'h264','width':1920,'height':1080,'capture_time':dt.datetime(2026,9,20,14,45,55)})
    assert tasks.enqueue_upload_jobs([mid],[1])==[]
    with pytest.raises(ValueError,match='Dates need'): tasks.handle_upload(1,{'media_ids':[mid],'destination_ids':[1]},Context())
    assert list(nas.iterdir())==[]
    with maker() as s:
        s.get(RecordingDate,id).status='confirmed'; s.get(RecordingDate,id).corrected_time=dt.datetime(2026,10,3,23,30)
        item=s.get(MediaItem,mid); item.capture_time=dt.datetime(2026,10,3,23,30); s.commit()
        assert list(workflow.recording_days(s))==['2026-10-04']


def test_confirm_waits_for_running_upload(setup):
    maker,camera,nas=setup
    id,path=scan(setup)
    plan=preview(setup,[id])
    with maker() as s:
        item=index(s,path,'device'); s.add(Job(kind='upload',status='running',payload=json.dumps({'media_ids':[item.id]}))); s.commit()
        with pytest.raises(ValueError,match='finish uploading'): dates.confirm_plan(s,plan['plan_id'])
        assert s.get(RecordingDate,id).status=='review'

@pytest.mark.parametrize('indexed',[True,False])
def test_verified_nas_move_preserves_source_and_can_retry(setup,monkeypatch,indexed):
    maker,camera,nas=setup
    id,path=scan(setup)
    before=path.read_bytes(); fp=dates.fingerprint(path)
    old=nas/'2026/09'/path.name; old.parent.mkdir(parents=True); old.write_bytes(before)
    with maker() as s:
        item=index(s,path,'device')
        if indexed:
            s.add(UploadedClip(destination_id=1,source_media_id=item.id,checksum=item.checksum,filename=old.name,size_bytes=len(before),status='done',remote_path=str(old)))
            s.add(UploadState(media_id=item.id,destination_id=1,status='done',remote_path=str(old)))
        s.commit()
    monkeypatch.setattr(tasks,'import_one',index)
    plan=preview(setup,[id])
    with maker() as s: job=dates.confirm_plan(s,plan['plan_id'])['job_id']
    payload={'plan_id':plan['plan_id'],'camera_root':str(camera)}
    dates.correction_job(job,payload,Context())
    new=nas/'2026/10'/path.name
    assert not old.exists() and new.read_bytes()==before
    assert path.read_bytes()==before and dates.fingerprint(path)==fp
    assert json.loads(new.with_name(new.name+'.dates.json').read_text())['original_bytes_preserved']
    with maker() as s:
        assert s.query(UploadedClip).one().remote_path==str(new)
        assert s.get(RecordingDate,id).status=='confirmed'
    dates.correction_job(job,payload,Context())
    assert new.read_bytes()==before


def test_wrong_nas_bytes_and_collision_never_deleted(tmp_path):
    source,target=tmp_path/'old.mp4',tmp_path/'new.mp4'
    source.write_bytes(b'original'); target.write_bytes(b'unrelated')
    digest=hashlib.sha256(b'original').hexdigest()
    with pytest.raises(RuntimeError): dates.safe_move(source,target,digest,Context())
    assert source.read_bytes()==b'original' and target.read_bytes()==b'unrelated'
    target.unlink(); source.write_bytes(b'bad')
    with pytest.raises(RuntimeError): dates.safe_move(source,target,digest,Context())
    assert source.exists() and not target.exists()
    target.write_bytes(b'original'); source.unlink()
    dates.safe_move(source,target,digest,Context()) # publication survived a crash
    assert target.read_bytes()==b'original'

@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='ffmpeg integration runs in CI and on the Pi')
def test_metadata_copy_keeps_video_packets_and_changes_all_creation_tags(tmp_path,setup):
    from app.timestamps import metadata_copy
    from app.media import probe
    from app.merge import merge_clips
    source=tmp_path/'original.mp4'; corrected=tmp_path/'corrected.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=64x64:rate=10','-f','lavfi','-i','sine=frequency=400','-t','1','-c:v','libx264','-c:a','aac','-metadata','creation_time=2026-09-20T14:45:55Z',str(source)],check=True)
    original_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    when=dt.datetime(2026,10,4,17,0,0,651708)
    metadata_copy(source,corrected,when)
    assert probe(corrected)['capture_time']==when.replace(microsecond=0)
    assert probe(corrected)['stream_signature']==probe(source)['stream_signature']
    tags=json.loads(subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',str(corrected)],capture_output=True,text=True,check=True).stdout)
    assert all(dt.datetime.fromisoformat(stream['tags']['creation_time'].replace('Z','+00:00')).replace(tzinfo=None)==when.replace(microsecond=0) for stream in tags['streams'])
    def packet_hash(path):
        return subprocess.run(['ffmpeg','-v','error','-i',str(path),'-map','0','-c','copy','-f','hash','-hash','sha256','-'],capture_output=True,check=True).stdout
    assert packet_hash(source)==packet_hash(corrected)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original_hash
    trip=tmp_path/'trip.mp4'
    merge_clips([source,source],trip,creation_time=when)
    assert probe(trip)['capture_time']==when.replace(microsecond=0) and probe(trip)['duration_s']>=2
    maker,camera,nas=setup
    original=nas/'2026/09/original.mp4'; original.parent.mkdir(parents=True); shutil.copyfile(source,original)
    with maker() as s:
        recording=RecordingDate(camera_root=str(camera),path=str(camera/'DCIM/107MEDIA/DVR00935.MP4'),relative_path='DCIM/107MEDIA/DVR00935.MP4',size_bytes=original.stat().st_size,mtime_ns=0,original_time=dt.datetime(2026,9,20,14,45,55),corrected_time=when,status='confirmed',metadata_copy=True)
        s.add(recording);s.commit();rid=recording.id
    dates.publish_metadata_copy(rid,1,original,Context())
    with maker() as s:
        stored=s.query(CorrectedArchive).one();published=Path(stored.corrected_path)
        assert published.is_relative_to(nas/'Corrected/2026/10')
        assert stored.corrected_time==when
    assert packet_hash(published)==packet_hash(source)
    assert probe(published)['capture_time']==when.replace(microsecond=0)
    assert list((nas/'.drift/tmp').iterdir())==[]
    dates.publish_metadata_copy(rid,1,original,Context()) # idempotent publication
    assert hashlib.sha256(original.read_bytes()).hexdigest()==original_hash


def test_correction_resumes_after_nas_publication_interrupted(setup,monkeypatch):
    maker,camera,nas=setup
    id,path=scan(setup)
    old=nas/'2026/09'/path.name; old.parent.mkdir(parents=True); old.write_bytes(path.read_bytes())
    with maker() as s:
        item=index(s,path,'device')
        s.add(UploadedClip(destination_id=1,source_media_id=item.id,checksum=item.checksum,filename=old.name,size_bytes=path.stat().st_size,status='done',remote_path=str(old)))
        s.commit()
    monkeypatch.setattr(tasks,'import_one',index)
    plan=preview(setup,[id])
    with maker() as s: job=dates.confirm_plan(s,plan['plan_id'])['job_id']
    publish=dates.publish_metadata_copy
    def fail(*args): raise RuntimeError('Process interrupted after original moved')
    monkeypatch.setattr(dates,'publish_metadata_copy',fail)
    payload={'plan_id':plan['plan_id'],'camera_root':str(camera)}
    with pytest.raises(RuntimeError,match='interrupted'): dates.correction_job(job,payload,Context())
    new=nas/'2026/10'/path.name
    assert new.exists() and not old.exists()
    with maker() as s:
        assert s.get(RecordingDate,id).status=='applying'
        assert s.query(UploadedClip).one().remote_path==str(new)
    monkeypatch.setattr(dates,'publish_metadata_copy',publish)
    dates.correction_job(job,payload,Context())
    with maker() as s: assert s.get(RecordingDate,id).status=='confirmed'
    assert new.read_bytes()==path.read_bytes()


def test_event_recordings_are_checked_and_held_with_bad_clock(setup):
    maker,camera,nas=setup
    movie(camera/'EVENT/E_DVR00908.MP4',dt.datetime(2026,9,20,14,45,55))
    dates.scan_job(1,{'camera_root':str(camera),'connected_at':NOW.isoformat()},Context())
    with maker() as s:
        data=dates.review_data(s,str(camera))
        assert data['total']==data['held']==1
        assert data['files'][0]['folder']=='EVENT'
        assert dates.approved_paths(s,[camera/'EVENT/E_DVR00908.MP4'])==[]


def test_reused_camera_path_does_not_preview_an_unrelated_old_backup(setup):
    maker,camera,nas=setup
    id,path=scan(setup)
    old=nas/'2026/08'/path.name;old.parent.mkdir(parents=True);old.write_bytes(b'previous recording')
    with maker() as s:
        item=index(s,path,'device');item.checksum=checksum(old);item.size_bytes=path.stat().st_size
        s.add(UploadedClip(destination_id=1,source_media_id=item.id,checksum=item.checksum,filename=old.name,size_bytes=old.stat().st_size,status='done',remote_path=str(old)))
        s.commit()
    plan=preview(setup,[id])
    assert plan['entries'][0]['moves']==[]
    assert old.read_bytes()==b'previous recording'
