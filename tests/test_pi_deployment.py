"""Exercise the deployed backup step against real SQLite state without Docker."""
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest


DEPLOY = Path(__file__).resolve().parents[1] / 'deploy' / 'deploy-to-pi.sh'


@pytest.mark.parametrize('container_state', ['running', 'created', 'exited'])
def test_deployment_preserves_database_when_container_is_not_running(tmp_path, container_state):
    data = tmp_path / 'persistent data'
    data.mkdir()
    with sqlite3.connect(data / 'drift.db') as db:
        db.execute('create table uploads (id integer, status text)')
        db.execute('insert into uploads values (1, "uploading")')
    with sqlite3.connect(data / 'drift.predeploy.db') as db:
        db.execute('create table obsolete (id integer)')
    commands = tmp_path / 'bin'
    commands.mkdir()
    docker = commands / 'docker'
    docker.write_text('''#!/usr/bin/env bash
set -eu
if [ "$1" = inspect ]; then
  if [ "${2:-}" = --format ]; then printf '%s\\n' "$TEST_DATA_DIR"; fi
  exit 0
fi
echo "Cannot execute inside a $TEST_CONTAINER_STATE container" >&2
exit 1
''')
    sudo = commands / 'sudo'
    sudo.write_text('#!/usr/bin/env bash\n[ "$1" = -n ] && shift\nexec "$@"\n')
    docker.chmod(0o755)
    sudo.chmod(0o755)
    source = DEPLOY.read_text()
    backup = source.split('if docker inspect drift-import >/dev/null 2>&1; then', 1)[1]
    backup = 'if docker inspect drift-import >/dev/null 2>&1; then' + backup.split('\nif ! docker compose up -d;', 1)[0]
    result = subprocess.run(['bash', '-euc', backup], capture_output=True, text=True,
        env={**os.environ, 'PATH': f'{commands}:{os.environ["PATH"]}',
             'TEST_DATA_DIR': str(data), 'TEST_CONTAINER_STATE': container_state})
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(data / 'drift.predeploy.db') as db:
        assert db.execute('select * from uploads').fetchall() == [(1, 'uploading')]
        assert db.execute("select count(*) from sqlite_master where name='obsolete'").fetchone()[0] == 0
