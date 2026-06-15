#!/usr/bin/env bash
# Deploy the freshly-built image to the Raspberry Pi.
#
# Runs on the GitHub Actions runner. It copies the Pi compose file to the
# deploy host, logs that host into Harbor, and runs `docker compose up`.
#
# The deploy target is CONFIGURABLE via the DEPLOY_HOST variable (set in the
# runner .env, default below). Point it at any host running Docker + an
# SSH-authorised key to deploy somewhere else.
set -euo pipefail

# --- configurable deploy target --------------------------------------------
DEPLOY_HOST="${DEPLOY_HOST:-ed@192.168.3.188}"        # user@host of the Pi
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/drift_deploy}"
DEPLOY_DIR="${DEPLOY_DIR:-drift-import}"               # dir on the deploy host
IMAGE_TAG="${IMAGE_TAG:-latest}"

# --- registry credentials (from runner .env) -------------------------------
: "${HARBOR_REGISTRY:?HARBOR_REGISTRY not set}"
: "${HARBOR_ROBOT_USER:?HARBOR_ROBOT_USER not set}"
: "${HARBOR_ROBOT_TOKEN:?HARBOR_ROBOT_TOKEN not set}"

SSH=(ssh -i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)
SCP=(scp -i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)

echo ">> Deploying ${HARBOR_REGISTRY}/drift-import/drift-import:${IMAGE_TAG} to ${DEPLOY_HOST}"

# 1) Ship the compose file.
"${SSH[@]}" "$DEPLOY_HOST" "mkdir -p ~/${DEPLOY_DIR}"
"${SCP[@]}" deploy/docker-compose.pi.yml "$DEPLOY_HOST:~/${DEPLOY_DIR}/docker-compose.yml"

# 2) Run on the Pi: write env, login, pull, up. Credentials are passed on the
#    remote command line (not committed anywhere) and the .env is chmod 600.
"${SSH[@]}" "$DEPLOY_HOST" \
  "HARBOR_REGISTRY='${HARBOR_REGISTRY}' \
   HARBOR_ROBOT_USER='${HARBOR_ROBOT_USER}' \
   HARBOR_ROBOT_TOKEN='${HARBOR_ROBOT_TOKEN}' \
   IMAGE_TAG='${IMAGE_TAG}' \
   DEPLOY_DIR='${DEPLOY_DIR}' bash -se" <<'REMOTE'
set -euo pipefail
cd ~/"${DEPLOY_DIR}"
umask 077
cat > .env <<EOF
HARBOR_REGISTRY=${HARBOR_REGISTRY}
IMAGE_TAG=${IMAGE_TAG}
EOF
echo "${HARBOR_ROBOT_TOKEN}" | docker login "${HARBOR_REGISTRY}" -u "${HARBOR_ROBOT_USER}" --password-stdin
docker compose pull
docker compose up -d
docker image prune -f >/dev/null 2>&1 || true
echo ">> Deployed. Containers:"
docker compose ps
REMOTE

echo ">> Done."
