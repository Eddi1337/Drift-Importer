#!/usr/bin/env bash
# Native deployment into the dedicated LXC; never run on the Proxmox host.
set -euo pipefail
: "${PROCESSOR_HOST:?Set PROCESSOR_HOST to the dedicated Drift LXC SSH target}"
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/drift_deploy}"
PROCESSOR_REVISION="${GITHUB_SHA:-$(git rev-parse HEAD)}"
case "$PROCESSOR_REVISION" in
  *[!a-f0-9]*|'') echo 'Expected a Git SHA for PROCESSOR_REVISION' >&2; exit 1 ;;
esac
bundle="$(mktemp /tmp/drift-processor.XXXXXX.tar.gz)"
trap 'rm -f -- "$bundle"' EXIT
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' -czf "$bundle" app requirements.txt deploy/drift-processor.service deploy/drift-processor-render.service deploy/register-camera-agent.py
ssh_options=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=${PROCESSOR_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}"
  -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
scp "${ssh_options[@]}" "$bundle" "$PROCESSOR_HOST:/tmp/drift-processor-${PROCESSOR_REVISION}.tar.gz"
ssh "${ssh_options[@]}" "$PROCESSOR_HOST" bash -se -- "$PROCESSOR_REVISION" <<'REMOTE'
set -euo pipefail
revision="$1"
# Explicit guard against accidentally configuring the hypervisor as target.
test "$(systemd-detect-virt --container)" = lxc
test -f /etc/drift-importer.env
install -d -m 700 /opt/drift-importer/releases /var/lib/drift-importer
release="/opt/drift-importer/releases/$revision"
install -d "$release"
tar -xzf "/tmp/drift-processor-${revision}.tar.gz" -C "$release"
rm "/tmp/drift-processor-${revision}.tar.gz"
test -x /opt/drift-importer/venv/bin/python || python3 -m venv /opt/drift-importer/venv
/opt/drift-importer/venv/bin/pip install --disable-pip-version-check -q -r "$release/requirements.txt"
cd "$release"
set -a
. /etc/drift-importer.env
set +a
# Back up only this guest's database, never overwrite the Pi's NAS recovery set.
/opt/drift-importer/venv/bin/python - <<'PY'
import sqlite3
from app.config import get_settings
from app.database import init_db
p = get_settings().db_path
if p.exists():
    with sqlite3.connect(p) as source, sqlite3.connect(p.with_suffix('.predeploy.db')) as target:
        source.backup(target)
init_db()
PY
install -m 644 deploy/drift-processor.service deploy/drift-processor-render.service /etc/systemd/system/
ln -sfn "$release" /opt/drift-importer/current.next
mv -Tf /opt/drift-importer/current.next /opt/drift-importer/current
systemctl daemon-reload
systemctl enable drift-processor.service drift-processor-render.service
systemctl restart drift-processor.service drift-processor-render.service
for attempt in $(seq 1 30); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:8080/healthz >/dev/null; then
    systemctl is-active --quiet drift-processor.service drift-processor-render.service
    echo "Processor $revision healthy; app and renderer running in the LXC"
    exit 0
  fi
  sleep 2
done
echo 'Processor health check failed' >&2
exit 1
REMOTE
