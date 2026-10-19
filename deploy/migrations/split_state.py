#!/usr/bin/env python3
"""Create an ownership-specific DB copy without losing uploaded-file history.

Input must be a consistent stopped-service snapshot. The uploader keeps the
camera queue; the map processor keeps only its own jobs. Media, settings, review
records, GPS and ride indexes remain intact. Source and existing output are
never overwritten.
"""
import argparse
from pathlib import Path
import sqlite3

from app.jobs import PROCESSOR_KINDS


def build(source, output, role):
    if role not in ('uploader', 'processor', 'standalone'):
        raise ValueError('Choose uploader, processor or standalone')
    output=Path(output)
    if output.exists():raise RuntimeError('Refusing to overwrite an existing split output')
    with sqlite3.connect(f'file:{Path(source).resolve()}?mode=ro',uri=True) as src,sqlite3.connect(output) as db:
        src.backup(db)
        marks=','.join('?' for _ in PROCESSOR_KINDS)
        if role!='standalone':
            operator='IN' if role=='uploader' else 'NOT IN'
            where=f'kind {operator} ({marks})'
            db.execute(f'DELETE FROM job_logs WHERE job_id IN (SELECT id FROM jobs WHERE {where})',PROCESSOR_KINDS)
            db.execute(f'DELETE FROM jobs WHERE {where}',PROCESSOR_KINDS)
        db.execute("UPDATE jobs SET status='queued',error=NULL,finished_at=NULL WHERE status='running'")
        db.execute("UPDATE upload_states SET status='queued' WHERE status='uploading'")
        db.execute("UPDATE uploaded_clips SET status='pending' WHERE status='uploading'")
        db.execute('DELETE FROM processing_sync_state')
        db.execute('DELETE FROM camera_agents')
        db.commit()
        if db.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('Split has broken references')
        if db.execute('PRAGMA integrity_check').fetchone()!=('ok',):raise RuntimeError('Split database integrity check failed')
        db.execute('PRAGMA journal_mode=DELETE')
        counts={name:db.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                for name in ('media_items','uploaded_clips','recording_dates','auto_rides','jobs')}
    output.chmod(0o600)
    return counts


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--output',required=True)
    p.add_argument('--role',required=True,choices=['uploader','processor','standalone'])
    a=p.parse_args();print(build(a.source,a.output,a.role))
