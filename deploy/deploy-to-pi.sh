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

: "${HARBOR_REGISTRY:?HARBOR_REGISTRY not set}"
: "${HARBOR_ROBOT_USER:?HARBOR_ROBOT_USER not set}"
: "${HARBOR_ROBOT_TOKEN:?HARBOR_ROBOT_TOKEN not set}"

SSH=(ssh -i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

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
retry scp -i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
  deploy/docker-compose.pi.yml "$DEPLOY_HOST:~/${DEPLOY_DIR}/docker-compose.yml"

{
# Send credentials over encrypted stdin, rather than remote command arguments.
printf 'export HARBOR_REGISTRY=%q HARBOR_ROBOT_USER=%q HARBOR_ROBOT_TOKEN=%q IMAGE_TAG=%q DEPLOY_DIR=%q\n' \
  "$HARBOR_REGISTRY" "$HARBOR_ROBOT_USER" "$HARBOR_ROBOT_TOKEN" "$IMAGE_TAG" "$DEPLOY_DIR"
cat <<'REMOTE'
set -euo pipefail
cd ~/${DEPLOY_DIR}
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
# Back up the live SQLite database consistently before additive migrations.
if docker inspect drift-import >/dev/null 2>&1; then
  docker exec drift-import python -c 'import sqlite3; source=sqlite3.connect("/data/drift.db"); target=sqlite3.connect("/data/drift.predeploy.db"); source.backup(target); target.close(); source.close()'
fi
docker compose up -d
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
} | "${SSH[@]}" "$DEPLOY_HOST" bash -se

echo ">> Done."
