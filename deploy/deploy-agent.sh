#!/usr/bin/env bash
# Updates an already enrolled camera-only Pi. Never contains credentials.
set -euo pipefail
: "${DEPLOY_HOST:?Set DEPLOY_HOST to the enrolled Pi SSH target}"
DEPLOY_SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/drift_deploy}"
opts=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=${DEPLOY_KNOWN_HOSTS_FILE:-$HOME/.ssh/known_hosts}"
  -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
# A private, random staging directory prevents collisions between deployments.
stage=$(ssh "${opts[@]}" "$DEPLOY_HOST" 'test -f /etc/drift-camera-agent.env && mktemp -d /tmp/drift-agent.XXXXXXXX')
[[ "$stage" =~ ^/tmp/drift-agent\.[a-zA-Z0-9]+$ ]] || exit 1
scp "${opts[@]}" agent/camera_agent.py deploy/drift-camera-agent.service "$DEPLOY_HOST:$stage/"
ssh "${opts[@]}" "$DEPLOY_HOST" bash -se -- "$stage" <<'REMOTE'
set -euo pipefail
stage="$1"
trap 'rm -rf -- "$stage"' EXIT
sudo -n test -f /etc/drift-camera-agent.env
sudo -n install -D -m 644 "$stage/camera_agent.py" /opt/drift-camera-agent/camera_agent.py
sudo -n install -m 644 "$stage/drift-camera-agent.service" /etc/systemd/system/drift-camera-agent.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable drift-camera-agent.service
sudo -n systemctl restart drift-camera-agent.service
for attempt in $(seq 1 15); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8080/healthz | python3 -c 'import json,sys; assert json.load(sys.stdin)["role"]=="camera-agent"'; then
    sudo -n systemctl is-active --quiet drift-camera-agent.service
    echo 'Camera agent healthy; existing server pairing preserved'
    exit 0
  fi
  sleep 1
done
exit 1
REMOTE
