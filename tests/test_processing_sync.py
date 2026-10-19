"""The processing service gets upload evidence, never secrets or live GPS data."""
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

from app import processing_sync as sync
from app.archive_models import ArchiveFile
from app.auto_ride_jobs import AutoRideRender
from app.auto_ride_models import AutoRide
from app.database import Base
from app.models import AppSettings, CorrectedArchive, Destination, Job, MediaItem, RecordingDate, UploadedClip
from app.roadtrip_models import LocationDay, RoadtripClipMetadata, RoadtripOuting, RoadtripSettings

NOW = dt.datetime(2026, 10, 18, 12, tzinfo=dt.timezone.utc)


def outing(identifier=1, signature="source-outing", source="ha:phone"):
    return RoadtripOuting(id=identifier, signature=signature, source=source, month="2026-10", start_time=NOW,
        end_time=NOW+dt.timedelta(hours=1), duration_s=3600, distance_m=1000, point_count=30, gap_count=0, complete=True)


@pytest.fixture
def databases(monkeypatch):
    monkeypatch.setattr(sync, "get_settings", lambda: SimpleNamespace(nas_root=Path("/mnt/NAS")))
    engines = [create_engine("sqlite://") for _ in range(2)]
    for engine in engines:
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        Base.metadata.create_all(engine)
    with Session(engines[0]) as source, Session(engines[1]) as target:
        source.add_all([Destination(id=1,name="NAS",type="local",base_path="/mnt/NAS",secret_enc="NAS_SECRET",username="NAS_USERNAME"),
            Destination(id=2,name="Remote",type="nextcloud",host="PRIVATE_HOST",username="PRIVATE_USERNAME",secret_enc="DESTINATION_SECRET",base_path="https://user:URL_SECRET@example.test"),
            Destination(id=3,name="Other local disk",type="local",base_path="/tmp/not-NAS",secret_enc="OTHER_SECRET")])
        source.add(MediaItem(id=1,path="/camera/DCIM/DVR001.MP4",filename="DVR001.MP4",kind="video",size_bytes=100,
            capture_time=NOW,duration_s=600,checksum="a"*64))
        source.add(RecordingDate(id=1,camera_root="/camera/DCIM",path="/camera/DCIM/DVR001.MP4",relative_path="DVR001.MP4",size_bytes=100,
            mtime_ns=1,checksum="a"*64,original_time=NOW,corrected_time=NOW,duration_s=600,time_source="metadata",status="confirmed",revision=1))
        source.add(RoadtripSettings(id=1,entity_id="device_tracker.phone",timezone="Europe/London",home_lat=51.5,home_lon=-.1))
        source.add(LocationDay(id=1,source="ha:phone",day="2026-10-18",cache_path="/mnt/NAS/.drift/locations/day.json.gz",point_count=30))
        source.add(outing())
        source.add(AppSettings(id=1,ha_base_url="https://private-ha.test",ha_token="HA_SECRET"))
        source.add(Job(id=1,kind="upload",status="running",payload='{"token":"JOB_SECRET"}'))
        source.flush()
        source.add_all([UploadedClip(id=1,destination_id=1,source_media_id=1,filename="DVR001.MP4",size_bytes=100,checksum="a"*64,status="done",remote_path="/mnt/NAS/2026/10/DVR001.MP4"),
            UploadedClip(id=2,destination_id=2,source_media_id=1,filename="DVR001.MP4",size_bytes=100,checksum="a"*64,status="done",remote_path="https://user:REMOTE_SECRET@example.test/file")])
        source.flush()
        source.add_all([ArchiveFile(id=1,destination_id=1,root_path="/mnt/NAS",path="2026/10/DVR001.MP4",size_bytes=100,mtime_ns=1,ctime_ns=1,device="1",inode="1",ledger_id=1),
            ArchiveFile(id=2,destination_id=2,root_path="https://user:ARCHIVE_SECRET@example.test",path="file",size_bytes=100),
            CorrectedArchive(id=1,recording_id=1,destination_id=1,corrected_time=NOW,original_path="/mnt/NAS/2026/10/DVR001.MP4",corrected_path="/mnt/NAS/Corrected/2026/10/DVR001.MP4"),
            CorrectedArchive(id=2,recording_id=1,destination_id=2,corrected_time=NOW,original_path="https://user:CORRECTION_SECRET@example.test/file")])
        source.flush()
        source.add(RoadtripClipMetadata(archive_id=1,identity="identity",observed_time=NOW,duration_s=600))
        source.commit()
        yield source,target


def changes(session):
    return session.connection().exec_driver_sql("SELECT total_changes()").scalar()


def scalar(session, model, field, identifier=1):
    table=model.__table__
    key=next(iter(table.primary_key.columns))
    return session.connection().execute(select(table.c[field]).where(key==identifier)).scalar()


def test_export_is_scalar_only_metadata_without_secrets_or_files(databases, monkeypatch):
    source,_=databases
    calls=[]
    event.listen(source.bind,"before_cursor_execute",lambda _,__,statement,*args:calls.append(statement))
    with monkeypatch.context() as patch:
        patch.setattr(Path,"open",lambda *a,**k:pytest.fail("Snapshot must not read NAS files"))
        patch.setattr(source,"query",lambda *a,**k:pytest.fail("Snapshot must not load ORM relationship trees"))
        payload=sync.export_snapshot(source)
    text=json.dumps(payload)
    assert all(value not in text for value in ("SECRET","PRIVATE_USERNAME","PRIVATE_HOST","home_lat","home_lon","ha_token","secret_enc","jobs"))
    assert [r['id'] for r in payload['rows']['destinations']]==[1]
    assert [r['id'] for r in payload['rows']['uploaded_clips']]==[1]
    assert [r['id'] for r in payload['rows']['archive_files']]==[1]
    assert [r['id'] for r in payload['rows']['corrected_archives']]==[1]
    assert not any('upload_states' in query or 'media_tags' in query for query in calls)
    assert payload==sync.export_snapshot(source)


def test_import_is_idempotent_and_updates_only_changed_source_fields(databases):
    source,target=databases
    payload=sync.export_snapshot(source)
    result=sync.import_snapshot(target,payload)
    target.commit()
    assert result['changed'] and result['inserted']>0 and result['updated']==0
    before=changes(target)
    assert not sync.import_snapshot(target,payload)['changed']
    assert changes(target)==before
    source.get(MediaItem,1).duration_s=720
    source.commit()
    result=sync.import_snapshot(target,sync.export_snapshot(source))
    assert result['updated']==1 and result['inserted']==0
    assert scalar(target,MediaItem,'duration_s')==720
    assert scalar(target,Destination,'secret_enc') is None


def test_processor_jobs_rides_renders_and_ha_credentials_are_preserved(databases):
    source,target=databases
    target.add_all([AppSettings(id=1,ha_token="CENTRAL_HA_SECRET",ha_base_url="https://central-ha.test"),
        Job(id=77,kind="auto_ride_prepare",status="running",progress=.5,payload='{"processor":"owned"}'),outing(77,"processor-outing","ha:central")])
    target.flush()
    target.add(AutoRide(id=77,signature="ride",input_signature="processor-input",source="ha:central",outing_id=77,name="Processor ride",start_time=NOW,end_time=NOW+dt.timedelta(hours=1),clip_count=2,duration_s=600,distance_m=1000,confidence=.96))
    target.flush()
    target.add(AutoRideRender(ride_id=77,signature="render",input_signature="processor-input",state_json='{"status":"rendering"}'))
    target.commit()
    sync.import_snapshot(target,sync.export_snapshot(source))
    assert scalar(target,AppSettings,'ha_token')=="CENTRAL_HA_SECRET"
    assert scalar(target,Job,'progress',77)==.5
    assert scalar(target,AutoRide,'name',77)=="Processor ride"
    assert scalar(target,AutoRideRender,'state_json',77)=='{"status":"rendering"}'
    assert target.connection().execute(select(Job.__table__.c.id)).scalars().all()==[77]


def test_ha_settings_and_history_are_seeded_once_without_overwriting_central_imports(databases):
    source,target=databases
    sync.import_snapshot(target,sync.export_snapshot(source));target.commit()
    assert scalar(target,RoadtripSettings,'entity_id')=="device_tracker.phone"
    assert scalar(target,RoadtripSettings,'home_lat') is None
    target.get(RoadtripSettings,1).entity_id="device_tracker.central"
    target.get(LocationDay,1).point_count=999
    target.get(RoadtripOuting,1).distance_m=9999
    target.add(LocationDay(id=77,source="ha:central",day="2026-10-19",cache_path="/mnt/NAS/.drift/locations/central.json.gz",point_count=100))
    target.commit()
    source.get(LocationDay,1).point_count=10
    source.add(LocationDay(id=2,source="ha:phone",day="2026-10-19",cache_path="/mnt/NAS/.drift/locations/pi.json.gz",point_count=20))
    source.get(MediaItem,1).size_bytes=200
    source.commit()
    sync.import_snapshot(target,sync.export_snapshot(source))
    assert scalar(target,RoadtripSettings,'entity_id')=="device_tracker.central"
    assert scalar(target,LocationDay,'point_count')==999
    assert scalar(target,RoadtripOuting,'distance_m')==9999
    assert scalar(target,LocationDay,'point_count',77)==100
    assert scalar(target,LocationDay,'point_count',2) is None


def test_initial_seed_does_not_replace_preexisting_central_rows_or_natural_keys(databases):
    source,target=databases
    target.add(RoadtripSettings(id=1,entity_id="device_tracker.central",home_lat=1,home_lon=2))
    target.add(LocationDay(id=77,source="ha:phone",day="2026-10-18",cache_path="/mnt/NAS/central.json.gz",point_count=999))
    target.add(outing(77,"source-outing","ha:central"));target.commit()
    sync.import_snapshot(target,sync.export_snapshot(source))
    assert scalar(target,RoadtripSettings,'home_lat')==1
    assert scalar(target,RoadtripSettings,'entity_id')=="device_tracker.central"
    assert scalar(target,LocationDay,'point_count',77)==999
    assert scalar(target,LocationDay,'point_count',1) is None
    assert scalar(target,RoadtripOuting,'source',77)=="ha:central"


def test_missing_source_archives_are_retired_without_deleting_processor_rows(databases):
    source,target=databases
    sync.import_snapshot(target,sync.export_snapshot(source));target.commit()
    target.add(ArchiveFile(id=77,destination_id=1,root_path="/mnt/NAS",path="2026/10/processor-owned.mp4",present=True));target.commit()
    source.execute(delete(ArchiveFile).where(ArchiveFile.id==1));source.commit()
    result=sync.import_snapshot(target,sync.export_snapshot(source))
    assert result['retired_archives']==1
    assert scalar(target,ArchiveFile,'present',1) is False
    assert scalar(target,ArchiveFile,'present',77) is True
    assert scalar(target,MediaItem,'filename')=="DVR001.MP4"


def test_higher_processor_date_revision_and_corrected_copy_are_not_reverted(databases):
    source,target=databases
    sync.import_snapshot(target,sync.export_snapshot(source));target.commit()
    corrected=NOW+dt.timedelta(days=1)
    target.get(RecordingDate,1).revision=4
    target.get(RecordingDate,1).corrected_time=corrected
    target.get(CorrectedArchive,1).corrected_time=corrected
    target.get(CorrectedArchive,1).corrected_path="/mnt/NAS/Corrected/processor-choice.mp4"
    target.commit()
    source.get(MediaItem,1).size_bytes=200
    source.commit()
    result=sync.import_snapshot(target,sync.export_snapshot(source))
    assert result['preserved_date_revisions']==1
    assert scalar(target,RecordingDate,'revision')==4
    assert scalar(target,RecordingDate,'corrected_time')==corrected.replace(tzinfo=None)
    assert scalar(target,CorrectedArchive,'corrected_path')=="/mnt/NAS/Corrected/processor-choice.mp4"


@pytest.mark.parametrize('mutation',[
    lambda p:p['rows']['destinations'][0].update(secret_enc="DO_NOT_IMPORT"),
    lambda p:p['rows']['roadtrip_settings'][0].update(home_lat=51.5),
    lambda p:p['rows']['uploaded_clips'][0].update(destination_id=999),
    lambda p:p['rows'].pop('archive_files'),
])
def test_invalid_or_unsafe_payload_never_partially_updates_target(databases,mutation):
    source,target=databases
    payload=copy.deepcopy(sync.export_snapshot(source));mutation(payload)
    payload['digest']=sync._digest(payload['rows'])
    before=changes(target)
    with pytest.raises(ValueError):sync.import_snapshot(target,payload)
    assert changes(target)==before


def test_digest_must_match_full_payload(databases):
    source,target=databases
    payload=sync.export_snapshot(source);payload['rows']['media_items'][0]['filename']='tampered.mp4'
    with pytest.raises(ValueError,match="digest"):sync.import_snapshot(target,payload)
    assert changes(target)==0


def test_conditional_http_sync_uses_etag_and_unchanged_payload_causes_no_writes(databases):
    source,target=databases
    payload=sync.export_snapshot(source);tag='"'+payload['digest']+'"';requests=[]
    def response(request):
        requests.append(request)
        assert str(request.url)=="http://drift-pi.local:8080/api/roadtrips/processor-input"
        if request.headers.get('if-none-match')==tag:return httpx.Response(304)
        return httpx.Response(200,json=payload,headers={'ETag':tag})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert sync.sync_from_source(target,"http://drift-pi.local:8080",client=client)['changed']
        target.commit();before=changes(target)
        assert sync.sync_from_source(target,"http://drift-pi.local:8080/",client=client)['not_modified']
        assert changes(target)==before
    assert len(requests)==2


def test_unchanged_body_does_not_write_even_if_http_etag_changes(databases):
    source,target=databases
    payload=sync.export_snapshot(source);sync.import_snapshot(target,payload,etag='"old"');target.commit();before=changes(target)
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload,headers={'ETag':'"new"'}))) as client:
        assert not sync.sync_from_source(target,"http://drift-pi.local:8080/api/roadtrips/processor-input",client=client)['changed']
    assert changes(target)==before


def test_stream_limit_applies_without_content_length(databases,monkeypatch):
    _,target=databases
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x'*40
            yield b'y'*40
    monkeypatch.setattr(sync,'MAX_SNAPSHOT_BYTES',64)
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=Chunks()))) as client:
        with pytest.raises(ValueError,match="16 MB"):sync.sync_from_source(target,"http://drift-pi.local:8080",client=client)
    assert changes(target)==0


@pytest.mark.parametrize('url',['file:///mnt/NAS/data','http://user:secret@pi.local','http://pi.local/?token=secret'])
def test_source_url_rejects_credentials_and_non_http_resources(databases,url):
    _,target=databases
    with pytest.raises(ValueError):sync.sync_from_source(target,url)
    assert changes(target)==0


def test_export_holds_one_sqlite_snapshot_while_an_upload_commits(tmp_path,monkeypatch):
    """A new ledger row must not outrun the media table's earlier SELECT."""
    monkeypatch.setattr(sync,"get_settings",lambda:SimpleNamespace(nas_root=Path("/mnt/NAS")))
    engine=create_engine("sqlite:///"+str(tmp_path/'concurrent.sqlite'))
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(Destination.__table__.insert().values(id=1,name='NAS',type='local',base_path='/mnt/NAS'))
        connection.execute(MediaItem.__table__.insert().values(id=1,path='/camera/first.mp4',filename='first.mp4'))
        connection.execute(UploadedClip.__table__.insert().values(id=1,destination_id=1,source_media_id=1,checksum='first',filename='first.mp4'))
    committed=[]
    def upload_between_reads(connection,cursor,statement,*args):
        if 'FROM uploaded_clips' in statement and not committed:
            committed.append(True)
            with engine.begin() as writer:
                writer.execute(MediaItem.__table__.insert().values(id=2,path='/camera/new.mp4',filename='new.mp4'))
                writer.execute(UploadedClip.__table__.insert().values(id=2,destination_id=1,source_media_id=2,checksum='new',filename='new.mp4'))
    event.listen(engine,'before_cursor_execute',upload_between_reads)
    with Session(engine) as session:
        payload=sync.export_snapshot(session)
        assert [r['id'] for r in payload['rows']['media_items']]==[1]
        assert [r['id'] for r in payload['rows']['uploaded_clips']]==[1]
        assert session.connection().connection.driver_connection.in_transaction
    assert committed
    with engine.connect() as connection:
        assert connection.execute(select(MediaItem.__table__.c.id).order_by(MediaItem.__table__.c.id)).scalars().all()==[1,2]


def test_export_reuses_and_never_commits_an_existing_caller_transaction(databases):
    source,_=databases
    source.connection().execute(Destination.__table__.update().where(Destination.id==1).values(name='Uncommitted caller choice'))
    payload=sync.export_snapshot(source)
    assert payload['rows']['destinations'][0]['name']=='Uncommitted caller choice'
    assert source.connection().connection.driver_connection.in_transaction
    source.rollback()
    assert scalar(source,Destination,'name')=='NAS'
