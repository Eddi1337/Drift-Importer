#!/usr/bin/env python3
"""Private stdout tar for an authorized migration; run after stopping the app.

Stream directly over SSH into the new server's root-only staging directory.
Never save this archive in a public location, print it, or add it to Git.
"""
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile


def main():
    root=Path('/var/lib/drift-import/data')
    config=json.loads(subprocess.check_output(['docker','inspect','drift-import']))[0]
    if config['State']['Running']:raise SystemExit('Stop the uploader before its final migration snapshot')
    allowed={'DRIFT_AUTH_USERNAME','DRIFT_AUTH_PASSWORD','DRIFT_MAX_CONCURRENT_UPLOADS',
             'DRIFT_MAX_CONCURRENT_FFMPEG','DRIFT_UPLOAD_CHUNK_BYTES',
             'DRIFT_UPLOAD_PROGRESS_INTERVAL_S','DRIFT_JOB_PROGRESS_INTERVAL_S'}
    env=dict(value.split('=',1) for value in config['Config']['Env'] if '=' in value)
    env={key:value for key,value in env.items() if key in allowed}
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='drift-migrate-',dir='/run') as folder:
        snapshot=Path(folder)/'uploader.db'
        with sqlite3.connect('file:'+str(root/'drift.db')+'?mode=ro',uri=True) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()!=('ok',):raise RuntimeError('Source database integrity check failed')
        with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
            for name,path in (('uploader.db',snapshot),('secret.key',root/'secret.key')):
                archive.add(path,arcname=name,recursive=False)
            data=json.dumps(env).encode();info=tarfile.TarInfo('runtime.json');info.mode=0o600;info.size=len(data)
            archive.addfile(info,io.BytesIO(data))


if __name__=='__main__':main()
