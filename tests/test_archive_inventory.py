"""Inventory is rebuildable, batched, incremental and never trusts samples as proof."""
import contextlib
import hashlib
import json
import os
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import archive, archive_tasks, jobs
from app.archive_models import ArchiveFile, ArchiveScan
from app.database import Base
from app.models import Destination, Job, UploadedClip
from app.routers import archive as routes


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    @contextlib.contextmanager
    def scope():
        with sessions() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
    for module in (archive, archive_tasks, routes, jobs):
        monkeypatch.setattr(module, 'session_scope', scope)
    monkeypatch.setattr(archive, 'require_storage', lambda p: p)
    from app import archive_links
    monkeypatch.setattr(archive_links, 'require_storage', lambda p: p)
    monkeypatch.setattr(archive_links, 'READ_PAUSE_SECONDS', 0)
    if not hasattr(archive_links.os, 'listxattr'):
        monkeypatch.setattr(archive_links.os, 'listxattr', lambda descriptor: [], raising=False)
        monkeypatch.setattr(archive_links.os, 'getxattr', lambda descriptor, name: b'', raising=False)
    root = tmp_path / 'NAS'
    root.mkdir()
    with scope() as session:
        d = Destination(name='NAS', type='local', base_path=str(root))
        session.add(d)
        session.flush()
        did = d.id
    yield SimpleNamespace(scope=scope, root=root, did=did, engine=engine)
    engine.dispose()


def put(store, name, content=b'one video'):
    path = store.root / name
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(content)
    return path


def context():
    return SimpleNamespace(upload_semaphore=threading.Semaphore(1), is_cancelled=lambda: False, set_progress=lambda *args: None)


def report(store):
    with store.scope() as session:
        return archive.duplicate_report(session)


def test_scan_discovers_legacy_holding_and_ledger_missing_and_excludes_temporary(store):
    put(store, '2025/09/old.mp4')
    put(store, 'Holding/camera/saved.MP4')
    put(store, '.drift/private.mp4')
    put(store, '2025/open.mp4.part')
    put(store, 'readme.txt')
    (store.root / 'shortcut.mp4').symlink_to(store.root / '2025/09/old.mp4')
    with store.scope() as session:
        session.add(UploadedClip(destination_id=store.did, checksum='missing', filename='lost.mp4',
                                 size_bytes=24, status='done', remote_path=str(store.root / '2025/lost.mp4')))
    result = archive.scan_archive()
    assert result['present_files'] == 2
    assert result['total_files'] == 3
    assert result['missing_files'] == 1
    assert result['unchecked_files'] == 0
    with store.scope() as session:
        found = archive.list_files(session)
        assert {f['path'] for f in found['files']} == {'2025/09/old.mp4', 'Holding/camera/saved.MP4', '2025/lost.mp4'}
        assert archive.list_files(session, status='missing')['files'][0]['status'] == 'missing'
        assert archive.archive_summary(session)['total_bytes'] == len(b'one video') * 2


def test_interrupted_scan_keeps_previous_presence_and_reports_error(store, monkeypatch):
    put(store, 'old.mp4')
    archive.scan_archive()
    def broken(*args):
        raise OSError('NAS disconnected')
        yield
    monkeypatch.setattr(archive, '_walk', broken)
    result = archive.scan_archive()
    assert result['present_files'] == 1 and result['missing_files'] == 0
    assert result['errors'][0]['error'] == 'NAS disconnected'
    with store.scope() as session:
        assert session.get(ArchiveScan, store.did).status == 'error'


def test_unique_sizes_are_never_read_and_unchanged_samples_cached(store, monkeypatch):
    put(store, 'unique.mp4', b'only one unique size')
    put(store, 'a.mp4', b'same')
    put(store, 'b.mp4', b'same')
    original = archive.sample_fingerprint
    reads = []
    def sampled(root, relative, *args):
        reads.append(relative)
        return original(root, relative, *args)
    monkeypatch.setattr(archive, 'sample_fingerprint', sampled)
    archive.scan_archive()
    assert set(reads) == {'a.mp4', 'b.mp4'}
    reads.clear()
    archive.scan_archive()
    assert reads == []
    assert report(store)['summary']['candidate_groups'] == 1


def test_ctime_invalidates_hash_even_when_mtime_and_size_restored(store):
    path = put(store, 'a.mp4', b'AAAA')
    put(store, 'b.mp4', b'AAAA')
    archive.scan_archive()
    with store.scope() as session:
        row = session.query(ArchiveFile).filter_by(path='a.mp4').one()
        old = row.ctime_ns
        row.full_sha256 = hashlib.sha256(b'AAAA').hexdigest()
    stamp = path.stat().st_mtime_ns
    path.write_bytes(b'BBBB')
    os.utime(path, ns=(stamp, stamp))
    assert path.stat().st_ctime_ns != old
    archive.scan_archive()
    with store.scope() as session:
        assert session.query(ArchiveFile).filter_by(path='a.mp4').one().full_sha256 is None
    assert report(store)['groups'] == []


def test_full_verification_splits_sample_collision_instead_of_false_duplicate(store, monkeypatch):
    # Simulate a match in cheap samples while complete contents differ.
    put(store, 'a.mp4', b'AAAA')
    put(store, 'b.mp4', b'BBBB')
    put(store, 'c.mp4', b'AAAA')
    monkeypatch.setattr(archive, 'sample_fingerprint', lambda *args: 'c' * 64)
    archive.scan_archive()
    group = report(store)['groups'][0]
    queued = routes.verify(group['id'])
    with store.scope() as session:
        payload = json.loads(session.get(Job, queued['job_id']).payload)
    archive_tasks.run_verify(queued['job_id'], payload, context())
    groups = report(store)['groups']
    assert len(groups) == 1 and groups[0]['status'] == 'verified'
    assert {f['path'] for f in groups[0]['files']} == {'a.mp4', 'c.mp4'}
    assert groups[0]['reclaimable_bytes'] == 4


def test_upload_incremental_hook_adds_stats_without_directory_or_content_reads(store, monkeypatch):
    path = put(store, '2026/10/saved.mp4')
    monkeypatch.setattr(archive, '_walk', lambda *args: pytest.fail('must not walk'))
    monkeypatch.setattr(archive, 'sample_fingerprint', lambda *args: pytest.fail('must not hash'))
    with store.scope() as session:
        clip = UploadedClip(destination_id=store.did, checksum='uploaded', filename=path.name,
                            size_bytes=path.stat().st_size, status='done', remote_path=str(path))
        session.add(clip)
        session.flush()
        archive.record_uploaded_file(session, session.get(Destination, store.did), clip)
        assert archive.archive_summary(session)['present_files'] == 1
    with store.scope() as session:
        assert session.query(ArchiveFile).one().ledger_id == clip.id


def test_batching_samples_does_not_commit_each_file(store):
    for i in range(12):
        put(store, f'{i}.mp4', b'same')
    commits = []
    event.listen(store.engine, 'commit', lambda connection: commits.append(1))
    archive.scan_archive()
    # One initial scan state, one final inventory, one sample cache batch;
    # read-only session commits are ignored by the engine if no transaction.
    assert len(commits) < 10


def test_scan_enqueue_deduplicates_and_serializes_verify_requests(store):
    first = routes.scan(routes.ScanRequest())
    second = routes.scan(routes.ScanRequest())
    assert second == {'job_id': first['job_id'], 'existing': True}
    with pytest.raises(HTTPException) as exc:
        routes.verify('not-a-group')
    assert exc.value.status_code == 409


def test_hardlink_request_needs_group_scoped_ids_and_explicit_confirmation(store):
    a = put(store, 'a.mp4', b'identical')
    b = put(store, 'b.mp4', b'identical')
    os.utime(b, ns=(a.stat().st_mtime_ns, a.stat().st_mtime_ns))
    archive.scan_archive()
    with store.scope() as session:
        for row in session.query(ArchiveFile):
            row.full_sha256 = hashlib.sha256(b'identical').hexdigest()
    group = report(store)['groups'][0]
    ids = [f['id'] for f in group['files']]
    with pytest.raises(HTTPException) as exc:
        routes.hardlink(group['id'], routes.HardlinkRequest(keeper_id=ids[0], file_ids=[ids[1]], confirm=False))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        routes.hardlink(group['id'], routes.HardlinkRequest(keeper_id=ids[0], file_ids=[9999], confirm=True))
    assert exc.value.status_code == 400
    queued = routes.hardlink(group['id'], routes.HardlinkRequest(keeper_id=ids[0], file_ids=[ids[1]], confirm=True))
    assert a.stat().st_ino != b.stat().st_ino  # queueing never performs the operation
    with store.scope() as session:
        payload = json.loads(session.get(Job, queued['job_id']).payload)
    archive_tasks.run_hardlink(queued['job_id'], payload, context())
    assert a.stat().st_ino == b.stat().st_ino
    assert report(store)['groups'][0]['status'] == 'already_linked'


def test_changed_group_rejected_at_execution_and_unrelated_files_preserved(store):
    a = put(store, 'a.mp4', b'identical')
    b = put(store, 'b.mp4', b'identical')
    archive.scan_archive()
    group = report(store)['groups'][0]
    queued = routes.verify(group['id'])
    with store.scope() as session:
        payload = json.loads(session.get(Job, queued['job_id']).payload)
    b.write_bytes(b'different')
    with pytest.raises(RuntimeError, match='changed since'):
        archive_tasks.run_verify(queued['job_id'], payload, context())
    assert a.read_bytes() == b'identical' and b.read_bytes() == b'different'


def test_claim_prioritizes_only_one_archive_audit(store):
    with store.scope() as session:
        upload = Job(kind='upload', status='queued')
        audit = Job(kind='archive_scan', status='queued')
        audit2 = Job(kind='archive_verify', status='queued')
        session.add_all([upload, audit, audit2])
        session.flush()
        uid, aid = upload.id, audit.id
    manager = jobs.JobManager()
    assert manager._claim_next() == aid
    assert manager._claim_next() == uid
    assert manager._claim_next() is None


def verified_group(store):
    with store.scope() as session:
        for row in session.query(ArchiveFile):
            row.full_sha256 = hashlib.sha256(b'identical').hexdigest()
    return report(store)['groups'][0]


def test_savings_exclude_undiscovered_hardlink_aliases(store):
    a = put(store, 'a.mp4', b'identical')
    b = put(store, 'b.mp4', b'identical')
    # Hidden or non-media names are intentionally outside the inventory.
    os.link(a, store.root / 'a.hidden-copy')
    os.link(b, store.root / 'b.hidden-copy')
    archive.scan_archive()
    group = verified_group(store)
    assert group['reclaimable_bytes'] == 0
    assert report(store)['summary']['reclaimable_bytes'] == 0


def test_linking_multiple_old_inode_aliases_refreshes_both_inode_link_counts(store):
    a = put(store, 'a.mp4', b'identical')
    b = put(store, 'b.mp4', b'identical')
    c = store.root / 'c.mp4'
    os.link(b, c)
    os.utime(b, ns=(a.stat().st_mtime_ns, a.stat().st_mtime_ns))
    archive.scan_archive()
    group = verified_group(store)
    assert group['reclaimable_bytes'] == len(b'identical')
    ids = {f['path']: f['id'] for f in group['files']}
    payload = {'group_id': group['id'], 'keeper_id': ids['a.mp4'], 'file_ids': [ids['b.mp4'], ids['c.mp4']], 'confirm': True}
    with store.scope() as session:
        job = Job(kind='archive_hardlink', status='running', payload=json.dumps(payload))
        session.add(job)
        session.flush()
        jid = job.id
    archive_tasks.run_hardlink(jid, payload, context())
    assert a.stat().st_ino == b.stat().st_ino == c.stat().st_ino
    with store.scope() as session:
        rows = session.query(ArchiveFile).all()
        assert all(row.nlink == 3 and row.ctime_ns == a.stat().st_ctime_ns for row in rows)
        assert archive.archive_summary(session)['physical_files'] == 1
        result = json.loads(session.get(Job, jid).result)
        assert result['bytes_saved'] == len(b'identical')
    assert report(store)['summary']['reclaimable_bytes'] == 0


def test_hardlink_execution_rejects_reconfigured_destination(store):
    put(store, 'a.mp4', b'identical')
    put(store, 'b.mp4', b'identical')
    archive.scan_archive()
    group = verified_group(store)
    ids = [f['id'] for f in group['files']]
    with store.scope() as session:
        session.get(Destination, store.did).enabled = False
    with pytest.raises(RuntimeError, match='destination changed'):
        archive_tasks.run_hardlink(1, {'group_id': group['id'], 'keeper_id': ids[0], 'file_ids': [ids[1]], 'confirm': True}, context())
    assert (store.root / 'a.mp4').stat().st_ino != (store.root / 'b.mp4').stat().st_ino


def test_random_samples_disable_linux_readahead_without_changing_fingerprint(store, monkeypatch):
    path = put(store, 'sample.mp4', b'video' * 60000)
    calls = []
    monkeypatch.setattr(archive.os, 'POSIX_FADV_RANDOM', 1, raising=False)
    monkeypatch.setattr(archive.os, 'posix_fadvise', lambda *args: calls.append(args), raising=False)
    expected = archive.stat_identity(path.stat())
    digest = archive.sample_fingerprint(store.root, path.name, expected)
    assert len(calls) == 1 and calls[0][1:] == (0, 0, 1)
    def unsupported(*args):
        raise OSError('random advice unsupported')
    monkeypatch.setattr(archive.os, 'posix_fadvise', unsupported)
    assert archive.sample_fingerprint(store.root, path.name, expected) == digest
    monkeypatch.delattr(archive.os, 'posix_fadvise')
    assert archive.sample_fingerprint(store.root, path.name, expected) == digest
