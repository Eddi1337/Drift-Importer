#!/usr/bin/env bash
# Deploy the tested application source to the Raspberry Pi's systemd service.
#
# The Pi runs Drift-Import directly from /opt/drift-import (not in Docker).
# The container images built by CI remain useful as release artefacts, but the
# deploy step deliberately syncs only application files and restarts the
# existing service. Its venv, settings, database and working media stay put.
#
# The deploy target is CONFIGURABLE via the DEPLOY_HOST variable (set in the
# runner .env, default below). Point it at any compatible systemd host with an
# SSH-authorised deployment key to deploy elsewhere.
set -euo pipefail

# --- configurable deploy target --------------------------------------------
DEPLOY_HOST="${DEPLOY_HOST:-ed@192.168.3.188}"        # user@host of the Pi
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/drift_deploy}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/drift-import}"

SSH=(ssh -i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)

echo ">> Syncing Drift-Import source to ${DEPLOY_HOST}:${DEPLOY_PATH}"

# Preserve all Pi-local state. --delete is intentionally avoided: an interrupted
# transfer must never remove a live application file or footage.
rsync -az \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='working/' \
  --exclude='thumbnails/' \
  --exclude='__pycache__/' \
  -e "${SSH[*]}" \
  ./ "${DEPLOY_HOST}:${DEPLOY_PATH}/"

"${SSH[@]}" "$DEPLOY_HOST" \
  "sudo -n /bin/systemctl restart drift-import.service && sudo -n /bin/systemctl is-active --quiet drift-import.service"

echo ">> Done."
