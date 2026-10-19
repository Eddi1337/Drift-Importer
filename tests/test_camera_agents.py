"""Real TLS gateway and central upload tests, including reconnect/resume safety."""
import copy
import hashlib
import json
import os
import ssl
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.camera_agent import CameraInventory, Gateway
from app import camera_agents as agents, camera_paths, media, workflow
from app.destinations.local import LocalBackend
from app.models import Destination


@pytest.fixture
def gateway(tmp_path):
    camera=tmp_path/'media'/'camera'/'DCIM';camera.mkdir(parents=True)
    data=bytes(range(256))*4097
    file=camera/'one.MP4';file.write_bytes(data)
    inv=CameraInventory(tmp_path/'media',require_mount=False);inv.scan()
    cert=tmp_path/'agent.crt';key=tmp_path/'agent.key'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-subj','/CN=localhost','-addext','subjectAltName=IP:127.0.0.1',
        '-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
    server=Gateway(('127.0.0.1',0),inv,'test-token-'+'x'*40)
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key)
    server.socket=ctx.wrap_socket(server.socket,server_side=True,do_handshake_on_connect=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    cfg={'id':'one','name':'Pi','url':f'https://127.0.0.1:{server.server_port}',
         'root':'/media','token':server.token,'ca':str(cert)}
    with agents._lock:old=dict(agents._nodes);agents._nodes.clear()
    agents.install_snapshot(cfg,inv.payload)
    try:yield SimpleNamespace(inv=inv,cfg=cfg,server=server,file=file,data=data,
                            path=camera_paths.Path('/media/camera/DCIM/one.MP4'),tmp=tmp_path)
    finally:
        server.shutdown();server.server_close();thread.join(2)
        with agents._lock:agents._nodes.clear();agents._nodes.update(old)


def test_tls_auth_and_safe_paths(gateway):
    g=gateway
    with httpx.Client(verify=ssl.create_default_context(cafile=g.cfg['ca']),trust_env=False) as c:
        assert c.get(g.cfg['url']+'/manifest').status_code==401
        headers={'Authorization':'Bearer '+g.cfg['token']}
        assert c.get(g.cfg['url']+'/manifest',headers=headers).status_code==200
        for path in ('../secret','camera/../../secret','/etc/passwd','camera/DCIM/no.MP4'):
            assert c.get(g.cfg['url']+'/file',params={'path':path},headers=headers).status_code==404
        assert c.get(g.cfg['url']+'/file',params={'path':'camera/DCIM/one.MP4'},headers=headers).status_code==412
    with pytest.raises(httpx.ConnectError):
        with httpx.Client(trust_env=False) as c:c.get(g.cfg['url']+'/manifest')


def test_metadata_seek_and_nas_copy(gateway,monkeypatch):
    g=gateway
    assert workflow.scan_videos(camera_paths.Path('/media/camera'))==[g.path]
    assert g.path.stat().st_size==len(g.data)
    assert agents.devices()[0]['agent_id']=='one'
    with g.path.open('rb') as f:
        assert f.read(257)==g.data[:257]
        f.seek(-333,2);assert f.read()==g.data[-333:]
        f.seek(111);assert f.read(31)==g.data[111:142]
    assert media.checksum(g.path)==media.checksum(g.file)
    nas=g.tmp/'NAS';nas.mkdir()
    backend=LocalBackend(Destination(id=1,name='NAS',type='local',base_path=str(nas)))
    result=Path(backend.upload(g.path,'2026/10','one.MP4'))
    assert hashlib.sha256(result.read_bytes()).digest()==hashlib.sha256(g.data).digest()
    assert not list(nas.rglob('*.part'))
    with pytest.raises(OSError):g.path.open('wb')
    with pytest.raises(OSError):g.path.unlink()


def test_disconnect_retains_partial_and_resumes(gateway):
    g=gateway;nas=g.tmp/'NAS';nas.mkdir()
    backend=LocalBackend(Destination(id=1,name='NAS',type='local',base_path=str(nas)))
    partial=nas/'clip.MP4.part';partial.write_bytes(g.data[:345678])
    with agents._lock:agents._nodes['one']['online']=False
    with pytest.raises(agents.AgentUnavailable):backend.upload(g.path,'','clip.MP4',start_offset=345678)
    assert partial.read_bytes()==g.data[:345678] and not (nas/'clip.MP4').exists()
    agents.install_snapshot(g.cfg,g.inv.payload)
    offset=backend.get_resume_offset('','clip.MP4',len(g.data));assert offset==345678
    backend.upload(g.path,'','clip.MP4',start_offset=offset)
    assert (nas/'clip.MP4').read_bytes()==g.data and not partial.exists()


def test_changed_source_and_symlink_never_committed(gateway):
    g=gateway;nas=g.tmp/'NAS';nas.mkdir()
    backend=LocalBackend(Destination(id=1,name='NAS',type='local',base_path=str(nas)))
    partial=nas/'clip.MP4.part';partial.write_bytes(g.data[:100])
    g.file.write_bytes(b'changed')
    with pytest.raises(OSError):backend.upload(g.path,'','clip.MP4',start_offset=100)
    assert not (nas/'clip.MP4').exists() and partial.exists()
    g.file.unlink();g.file.symlink_to('/etc/passwd')
    with pytest.raises(OSError):g.inv.open_file('camera/DCIM/one.MP4',f'{len(g.data)}:0')


def test_full_size_partial_still_requires_source_identity(gateway):
    g=gateway;nas=g.tmp/'NAS';nas.mkdir()
    backend=LocalBackend(Destination(id=1,name='NAS',type='local',base_path=str(nas)))
    partial=nas/'clip.MP4.part';partial.write_bytes(g.data)
    g.file.unlink()
    with pytest.raises(agents.AgentUnavailable):backend.upload(g.path,'','clip.MP4',start_offset=len(g.data))
    assert partial.exists() and not (nas/'clip.MP4').exists()


@pytest.mark.parametrize('field,value',[('path','../outside'),('size',-1),('inode','no'),('device',{}),('mtime_ns',2**90)])
def test_invalid_inventory_rejected(gateway,field,value):
    payload=copy.deepcopy(gateway.inv.payload);payload['files'][0][field]=value
    with pytest.raises(ValueError):agents.validate_manifest(payload)


def test_multiple_agents_isolate_same_filenames(gateway):
    g=gateway
    cfg=g.cfg|{'id':'two','root':'/media/agents/two'}
    agents.install_snapshot(cfg,g.inv.payload)
    second=camera_paths.Path('/media/agents/two/camera/DCIM/one.MP4')
    assert agents.lookup(second)[0]['config']['id']=='two'
    assert second.read_bytes()==g.data
    assert agents.lookup(g.path)[0]['config']['id']=='one'


def test_bridge_ffprobe_and_offline_status(gateway,monkeypatch):
    import shutil
    from app import agent_bridge
    if not shutil.which('ffmpeg'):pytest.skip('ffmpeg unavailable')
    g=gateway
    subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x120:d=1',
        '-c:v','mpeg4',str(g.file)],check=True,capture_output=True)
    g.inv.scan();agents.install_snapshot(g.cfg,g.inv.payload)
    server=agent_bridge.ThreadingHTTPServer(('127.0.0.1',0),agent_bridge.Handler)
    server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(camera_paths,'get_settings',lambda:SimpleNamespace(agent_bridge_port=server.server_port))
    try:
        result=media.probe(g.path)
        assert result['width']==160 and .9<float(result['duration_s'])<1.2
        with agents._lock:agents._nodes['one']['online']=False
        assert httpx.get(f'http://127.0.0.1:{server.server_port}/source',params={'path':str(g.path)},trust_env=False).status_code==503
        assert httpx.get(f'http://127.0.0.1:{server.server_port}/source',params={'path':'/etc/passwd'},trust_env=False).status_code==404
    finally:server.shutdown();server.server_close();thread.join(2)


def test_reconnect_automatically_enqueues_existing_workflow(gateway,monkeypatch):
    from app import devicescan,tasks
    from app.database import Base
    from app.models import AppSettings
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine);maker=sessionmaker(bind=engine,expire_on_commit=False)
    @contextmanager
    def scope():
        with maker() as s:yield s;s.commit()
    with scope() as s:s.add(AppSettings(id=1,auto_import_on_connect=True,auto_upload_on_import=True,default_destination_ids='1'))
    monkeypatch.setattr(devicescan,'session_scope',scope)
    from app import config
    monkeypatch.setattr(config,'get_settings',lambda:SimpleNamespace(processing_role='server'))
    monkeypatch.setattr(devicescan.DeviceMonitor,'_maybe_date_check',lambda *a:None)
    calls=[]
    monkeypatch.setattr(tasks,'enqueue_device_import',lambda root,**kw:(calls.append((root,kw)) or (42,1)))
    monitor=devicescan.DeviceMonitor();monitor._scan();monitor._scan()
    assert len(calls)==1 and calls[0][1]['auto_upload'] and calls[0][1]['destination_ids']==[1]
    assert calls[0][1]['paths']==[str(gateway.path)]
    gateway.inv.connections.clear();gateway.inv.scan();agents.install_snapshot(gateway.cfg,gateway.inv.payload)
    monitor._scan();assert len(calls)==2


def test_midstream_source_loss_keeps_partial_until_reconnect(gateway,monkeypatch):
    from app.destinations import local
    g=gateway;nas=g.tmp/'NAS';nas.mkdir();original_stat=g.file.stat()
    settings=SimpleNamespace(upload_chunk_bytes=65536,require_nas_mount=False)
    monkeypatch.setattr(local,'get_settings',lambda:settings)
    backend=LocalBackend(Destination(id=1,name='NAS',type='local',base_path=str(nas)))
    removed=False
    def progress(sent,total):
        nonlocal removed
        if not removed:g.file.unlink();removed=True
    with pytest.raises(agents.AgentUnavailable):backend.upload(g.path,'','clip.MP4',progress)
    partial=nas/'clip.MP4.part'
    assert 0<partial.stat().st_size<len(g.data) and not (nas/'clip.MP4').exists()
    g.file.write_bytes(g.data);os.utime(g.file,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
    g.inv.scan();agents.install_snapshot(g.cfg,g.inv.payload)
    backend.upload(g.path,'','clip.MP4',start_offset=partial.stat().st_size)
    assert (nas/'clip.MP4').read_bytes()==g.data and not partial.exists()


def test_worker_defers_source_loss_and_offline_does_not_block_nas_work(gateway,monkeypatch):
    import datetime as dt
    from app import jobs
    from app.database import Base
    from app.models import Job,MediaItem,utcnow
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine);maker=sessionmaker(bind=engine,expire_on_commit=False)
    @contextmanager
    def scope():
        with maker() as s:yield s;s.commit()
    monkeypatch.setattr(jobs,'session_scope',scope)
    real=jobs.get_settings()
    monkeypatch.setattr(jobs,'get_settings',lambda:real.model_copy(update={'processing_role':'server'}))
    manager=jobs.JobManager()
    def lost(*args):raise agents.AgentUnavailable('offline')
    monkeypatch.setitem(jobs._HANDLERS,'test_agent_loss',lost)
    with scope() as s:
        s.add(Job(id=1,kind='test_agent_loss',status='running'))
        s.add(MediaItem(id=1,path=str(gateway.path),filename='one.MP4'))
        s.add(MediaItem(id=2,path='/mnt/NAS/known.MP4',filename='known.MP4'))
        s.add(Job(id=2,kind='upload',status='queued',payload=json.dumps({'media_ids':[1]})))
        s.add(Job(id=3,kind='upload',status='queued',payload=json.dumps({'media_ids':[2]})))
    manager._run_job(1)
    with scope() as s:
        row=s.get(Job,1);assert row.status=='queued' and row.retry_after>utcnow().replace(tzinfo=None) and not row.error
    with agents._lock:agents._nodes['one']['online']=False
    assert manager._claim_next()==3


def test_central_migration_keeps_upload_settings_and_processor_rides(tmp_path):
    from app import archive_models,roadtrip_models,auto_ride_jobs,processing_sync
    from deploy.migrations.centralize import build
    from app.database import Base
    from app.models import AppSettings,Job
    from app.roadtrip_models import RoadtripSettings
    from app.auto_ride_models import AutoRideDiscovery
    paths=[tmp_path/name for name in ('source.db','processor.db')]
    for path in paths:
        engine=create_engine('sqlite:///'+str(path));Base.metadata.create_all(engine)
        maker=sessionmaker(bind=engine)
        with maker() as s:
            s.add(AppSettings(id=1,auto_import_on_connect=path==paths[0],ha_token='encrypted-existing-secret' if path==paths[0] else None))
            s.add(RoadtripSettings(id=1,home_lat=1.5 if path==paths[0] else None,home_lon=2.5 if path==paths[0] else None))
            if path==paths[0]:s.add(Job(id=99,kind='upload',status='running',payload='{"media_ids":[77]}'))
            else:s.add(AutoRideDiscovery(id=1,input_signature='keep-me',report='{"rides":2}'))
            s.commit()
        engine.dispose()
    output=tmp_path/'central.db';build(*paths,output)
    import sqlite3
    with sqlite3.connect(output) as db:
        assert db.execute('select auto_import_on_connect,ha_token from app_settings').fetchone()==(1,'encrypted-existing-secret')
        assert db.execute('select id,status,payload from jobs').fetchone()==(99,'queued','{"media_ids":[77]}')
        assert db.execute('select home_lat,home_lon from roadtrip_settings').fetchone()==(1.5,2.5)
        assert db.execute('select input_signature from auto_ride_discovery').fetchone()==('keep-me',)
    with pytest.raises(RuntimeError):build(*paths,output)


def test_unregistered_camera_job_is_not_consumed_as_missing(monkeypatch):
    from app import jobs
    from app.database import Base
    from app.models import Job,MediaItem
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine);maker=sessionmaker(bind=engine,expire_on_commit=False)
    @contextmanager
    def scope():
        with maker() as s:yield s;s.commit()
    monkeypatch.setattr(jobs,'session_scope',scope)
    settings=jobs.get_settings().model_copy(update={'processing_role':'server'})
    monkeypatch.setattr(jobs,'get_settings',lambda:settings)
    with scope() as s:
        s.add(MediaItem(id=5,path='/media/not-paired/DCIM/a.MP4',filename='a.MP4'))
        s.add(Job(id=5,kind='upload',status='queued',payload=json.dumps({'media_ids':[5]})))
    assert jobs.JobManager()._claim_next() is None
    with scope() as s:assert s.get(Job,5).status=='queued'
