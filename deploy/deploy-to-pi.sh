#!/usr/bin/env bash
# Deploy the freshly-built image to the Raspberry Pi Docker host.
#
# The deploy target is CONFIGURABLE via the DEPLOY_HOST variable (set in the
# runner .env, default below). Point it at any Docker host with an
# SSH-authorised key to deploy elsewhere.
set -euo pipefail

# --- configurable deploy target --------------------------------------------
DEPLOY_HOST="${DEPLOY_HOST:-ed@192.168.3.188}"        # user@host of the Pi
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

"${SSH[@]}" "$DEPLOY_HOST" \
  "HARBOR_REGISTRY='${HARBOR_REGISTRY}' HARBOR_ROBOT_USER='${HARBOR_ROBOT_USER}' HARBOR_ROBOT_TOKEN='${HARBOR_ROBOT_TOKEN}' IMAGE_TAG='${IMAGE_TAG}' DEPLOY_DIR='${DEPLOY_DIR}' bash -se" <<'REMOTE'
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
docker compose up -d
docker image prune -f >/dev/null 2>&1 || true
docker compose ps
REMOTE

echo ">> Done."
