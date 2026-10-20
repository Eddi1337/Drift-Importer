#!/usr/bin/env bash
# Deploy the freshly-built image to the Raspberry Pi Docker host.
#
# The deploy target is CONFIGURABLE via the DEPLOY_HOST variable (set in the
# runner .env, default below). Point it at any Docker host with an
# SSH-authorised key to deploy elsewhere.
set -euo pipefail

# --- configurable deploy target --------------------------------------------
DEPLOY_HOST="${DEPLOY_HOST:-ed@drift-pi.local}"       # user@host of the Pi
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/drift_deploy}"
DEPLOY_DIR="${DEPLOY_DIR:-drift-import}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Once migrated, this host is a camera agent. A transport failure must fail
# the deployment, rather than falling through and resurrecting the old server.
agent_check=0
ssh -i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o ConnectTimeout=20 "$DEPLOY_HOST" \
  'test -f /etc/drift-camera-agent.env' || agent_check=$?
if [ "$agent_check" -eq 0 ]; then
  export DEPLOY_HOST DEPLOY_SSH_KEY
  exec ./deploy/deploy-agent.sh
elif [ "$agent_check" -ne 1 ]; then
  exit "$agent_check"
fi

: "${HARBOR_REGISTRY:?HARBOR_REGISTRY not set}"
: "${HARBOR_ROBOT_USER:?HARBOR_ROBOT_USER not set}"
: "${HARBOR_ROBOT_TOKEN:?HARBOR_ROBOT_TOKEN not set}"

# Reuse one authenticated connection for all transfers and commands. A broken
# transport stays bounded, with enough headroom for the low-memory Pi to finish
# an I/O-heavy startup or close an exec session without repeating deployment.
SSH_CONTROL_DIR="$(mktemp -d /tmp/drift-deploy.XXXXXX)"
SSH_OPTIONS=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20
  -o ServerAliveInterval=15 -o ServerAliveCountMax=8
  -o ControlMaster=auto -o ControlPersist=60
  -o "ControlPath=${SSH_CONTROL_DIR}/ssh")
SSH=(ssh "${SSH_OPTIONS[@]}")
SCP=(scp "${SSH_OPTIONS[@]}")
cleanup() {
  "${SSH[@]}" -O exit "$DEPLOY_HOST" >/dev/null 2>&1 || true
  rm -rf -- "$SSH_CONTROL_DIR"
}
trap cleanup EXIT

retry() {
  local attempt
  for attempt in 1 2 3; do
    "$@" && return 0
    if [ "$attempt" -lt 3 ]; then
      echo ">> Connection attempt ${attempt} failed; retrying..." >&2
      sleep 3
    fi
  done
  return 1
}

echo ">> Deploying ${HARBOR_REGISTRY}/drift-import/drift-import:${IMAGE_TAG} to ${DEPLOY_HOST}"

retry "${SSH[@]}" "$DEPLOY_HOST" "mkdir -p ~/${DEPLOY_DIR}"
retry "${SCP[@]}" \
  deploy/docker-compose.pi.yml "$DEPLOY_HOST:~/${DEPLOY_DIR}/docker-compose.yml"
retry "${SCP[@]}" \
  deploy/drift-camera-mount.service deploy/99-drift-camera.rules \
  deploy/drift-camera-mount.sh deploy/install-camera-mount.sh "$DEPLOY_HOST:~/${DEPLOY_DIR}/"
retry "${SCP[@]}" \
  deploy/drift-zram-start.sh deploy/drift-zram.service deploy/install-low-write.sh \
  "$DEPLOY_HOST:~/${DEPLOY_DIR}/"

deploy_remote() {
{
# Send credentials over encrypted stdin, rather than remote command arguments.
printf 'export HARBOR_REGISTRY=%q HARBOR_ROBOT_USER=%q HARBOR_ROBOT_TOKEN=%q IMAGE_TAG=%q DEPLOY_DIR=%q\n' \
  "$HARBOR_REGISTRY" "$HARBOR_ROBOT_USER" "$HARBOR_ROBOT_TOKEN" "$IMAGE_TAG" "$DEPLOY_DIR"
cat <<'REMOTE'
set -euo pipefail
cd ~/${DEPLOY_DIR}
sudo -n bash ./install-camera-mount.sh
sudo -n bash ./install-low-write.sh
umask 077
cat > .env <<EOF
HARBOR_REGISTRY=${HARBOR_REGISTRY}
IMAGE_TAG=${IMAGE_TAG}
EOF
# Harbor intentionally serves this private-LAN registry over HTTP. Supplying
# the scheme is required for docker login; image pulls use the Pi daemon's
# matching insecure-registry setting.
echo "${HARBOR_ROBOT_TOKEN}" | docker login "http://${HARBOR_REGISTRY}" -u "${HARBOR_ROBOT_USER}" --password-stdin
docker compose pull
# Read the actual data mount so a retry also works when the replacement has
# already been created but not started. SQLite's backup API remains consistent
# while the old uploader is running; it does not need a running container.
if docker inspect drift-import >/dev/null 2>&1; then
  data_dir="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' drift-import)"
  sudo -n python3 - "$data_dir" <<'PY'
import sqlite3
import sys
from pathlib import Path
root = Path(sys.argv[1])
if not root.is_absolute() or not (root / 'drift.db').is_file():
    raise SystemExit('Cannot locate the existing uploader database; deployment stopped')
source = sqlite3.connect((root / 'drift.db').as_uri() + '?mode=ro', uri=True, timeout=30)
target = sqlite3.connect(root / 'drift.predeploy.db')
try:
    source.backup(target)
finally:
    target.close()
    source.close()
print('Consistent pre-deployment database backup saved')
PY
fi
if ! docker compose up -d; then
  # A slow NAS syscall can delay the engine's exit event beyond its stop
  # deadline. Reconcile once only after the old container is actually stopped;
  # never repeat installation, credentials, pulls or database backup writes.
  stopped=false
  for attempt in $(seq 1 15); do
    if [ "$(docker inspect --format '{{.State.Running}}' drift-import 2>/dev/null)" = false ]; then
      stopped=true
      break
    fi
    sleep 2
  done
  if [ "$stopped" != true ]; then
    echo "Container reconciliation failed while the old container is still active" >&2
    exit 1
  fi
  echo "Old container has stopped; finishing replacement once"
  docker compose up -d
fi
# Compose can retain its temporary replacement name after recovering a failed
# stop. Restore the configured name before exec-based health/NAS checks.
if ! docker inspect drift-import >/dev/null 2>&1; then
  recovered_id="$(docker compose ps -q drift-import)"
  case "$recovered_id" in
    ''|*$'\n'*) echo "Cannot identify one replacement container" >&2; exit 1 ;;
  esac
  docker rename "$recovered_id" drift-import
fi
docker image prune -f >/dev/null 2>&1 || true
docker compose ps
# Do not report success until the app is healthy and real container NAS I/O works.
healthy=false
for attempt in $(seq 1 30); do
  if docker exec drift-import python -c 'import urllib.request; urllib.request.urlopen("http://localhost:8080/healthz", timeout=5).read()' >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 2
done
if [ "$healthy" != true ]; then
  echo "Application health check failed" >&2
  exit 1
fi
docker exec -i drift-import python - <<'PY'
import tempfile
from pathlib import Path
from app.storage import require_storage
root = require_storage(Path('/mnt/NAS'))
with tempfile.TemporaryDirectory(prefix='.drift-deploy-', dir=root) as scratch:
    probe = Path(scratch) / 'probe'
    probe.write_bytes(b'drift NAS deployment check')
    assert probe.read_bytes() == b'drift NAS deployment check'
print('Application healthy; container NAS write/read passed')
PY
REMOTE
} | "${SSH[@]}" "$DEPLOY_HOST" "cd ~/${DEPLOY_DIR} && flock -w 180 .deploy.lock bash -se"
}
for attempt in 1 2 3; do
  if deploy_remote; then
    break
  else
    deploy_status=$?
  fi
  # An application/install error needs diagnosis, rather than repeating writes.
  if [ "$deploy_status" -ne 255 ] || [ "$attempt" -eq 3 ]; then
    exit "$deploy_status"
  fi
  echo ">> SSH connection lost; retrying deployment..." >&2
  sleep 3
done

echo ">> Done."
