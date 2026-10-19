import sqlite3
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import archive_models,roadtrip_models,auto_ride_jobs,processing_sync,camera_agents
from app.database import Base
from app.models import Job,JobLog,MediaItem,Destination,UploadedClip,AppSettings
from deploy.migrations.split_state import build


@pytest.mark.parametrize('role,kinds',[('uploader',['upload']),('processor',['auto_rides_discover']),('standalone',['upload','auto_rides_discover'])])
def test_reverse_migration_keeps_new_uploads_settings_and_job_ownership(tmp_path,role,kinds):
    source=tmp_path/'source.db';output=tmp_path/'split.db'
    engine=create_engine('sqlite:///'+str(source));Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Destination(id=1,name='NAS',type='local'))
        s.add(MediaItem(id=1,path='/media/drift-camera/a.MP4',filename='a.MP4'))
        s.add(AppSettings(id=1,auto_import_on_connect=True,ha_token='encrypted-existing'))
        s.add_all([Job(id=1,kind='upload',status='running'),Job(id=2,kind='auto_rides_discover',status='queued')]);s.commit()
        s.add_all([JobLog(job_id=1,message='partial retained'),JobLog(job_id=2,message='trip work')])
        s.add(UploadedClip(destination_id=1,source_media_id=1,checksum='fresh',filename='a.MP4',status='done',size_bytes=100,bytes_uploaded=100,remote_path='/mnt/NAS/a.MP4'))
        s.commit()
    engine.dispose()
    build(source,output,role)
    with sqlite3.connect(output) as db:
        assert [r[0] for r in db.execute('SELECT kind FROM jobs ORDER BY id')]==kinds
        assert db.execute('SELECT status,bytes_uploaded FROM uploaded_clips').fetchone()==('done',100)
        assert db.execute('SELECT ha_token,auto_import_on_connect FROM app_settings').fetchone()==('encrypted-existing',1)
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    with sqlite3.connect(source) as db:assert db.execute('SELECT status FROM jobs WHERE id=1').fetchone()==('running',)
    with pytest.raises(RuntimeError):build(source,output,role)
