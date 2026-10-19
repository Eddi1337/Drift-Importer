#!/usr/bin/env python3
"""Build a central DB from consistent stopped-uploader/processor snapshots.

Never edits either input. Keep both snapshots and the uploader's encryption key
until the migration has been verified. Run with services stopped and owner-only
permissions; the output retains ALL uploader settings and encrypted credentials.
"""
import argparse
import os
import sqlite3
from pathlib import Path

PROCESSOR_TABLES=(
 'roadtrip_settings','location_days','roadtrip_outings','roadtrip_matches',
 'roadtrip_rejections','roadtrip_clip_metadata','roadtrip_collections',
 'roadtrip_collection_clips','auto_rides','auto_ride_clips',
 'auto_ride_discovery','auto_ride_ha_refresh','auto_ride_renders')


def build(uploader,processor,output):
    output=Path(output)
    if output.exists():raise RuntimeError('Refusing to overwrite an existing migration output')
    with sqlite3.connect(f'file:{Path(uploader).resolve()}?mode=ro',uri=True) as src, sqlite3.connect(output) as db:
        src.backup(db)
        db.execute('ATTACH DATABASE ? AS processor',(str(Path(processor).resolve()),))
        # The old metadata sync preserves source IDs. Fail before copying if a
        # processor reference now points at a different archive in the source.
        conflicts=db.execute('SELECT count(*) FROM processor.archive_files p LEFT JOIN main.archive_files m ON m.id=p.id WHERE m.id IS NULL OR m.path!=p.path OR m.destination_id!=p.destination_id').fetchone()[0]
        if conflicts:raise RuntimeError('Archive identities changed; explicit reconciliation is required')
        private_home=db.execute('SELECT home_lat,home_lon FROM main.roadtrip_settings WHERE id=1').fetchone()
        db.execute('PRAGMA foreign_keys=OFF')
        for table in reversed(PROCESSOR_TABLES):db.execute(f'DELETE FROM main.{table}')
        counts={}
        for table in PROCESSOR_TABLES:
            expected=[row[1] for row in db.execute(f'PRAGMA main.table_info({table})')]
            actual=[row[1] for row in db.execute(f'PRAGMA processor.table_info({table})')]
            if expected!=actual:raise RuntimeError(f'Schema mismatch for {table}')
            db.execute(f'INSERT INTO main.{table} SELECT * FROM processor.{table}')
            counts[table]=db.execute(f'SELECT count(*) FROM main.{table}').fetchone()[0]
        # Old metadata sync deliberately omitted home coordinates. Restore
        # them from the authoritative uploader if the processor had none.
        if private_home:
            db.execute('UPDATE roadtrip_settings SET home_lat=COALESCE(home_lat,?),home_lon=COALESCE(home_lon,?) WHERE id=1',private_home)
        db.execute("UPDATE jobs SET status='queued',error=NULL,finished_at=NULL WHERE status='running'")
        db.execute('DELETE FROM processing_sync_state')
        db.commit()
        errors=db.execute('PRAGMA foreign_key_check').fetchall()
        if errors:raise RuntimeError(f'Migration has {len(errors)} broken references')
        if db.execute('PRAGMA integrity_check').fetchone()!=('ok',):raise RuntimeError('Migration integrity check failed')
        db.execute('PRAGMA journal_mode=DELETE')
    output.chmod(0o600)
    return counts


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--uploader',required=True);p.add_argument('--processor',required=True);p.add_argument('--output',required=True)
    a=p.parse_args()
    print(build(a.uploader,a.processor,a.output))
