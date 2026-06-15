#!/usr/bin/env bash
# Build the Drift-Import image for the target architecture and push it to a
# Harbor registry. Creates the Harbor project first if it does not exist.
#
# Usage:
#   export HARBOR_USER=admin HARBOR_PASS=...        # or run `docker login` first
#   ./deploy/harbor-push.sh [registry] [project] [tag] [platform]
#
# Defaults match the user's setup:
set -euo pipefail

REGISTRY="${1:-192.168.10.155}"
PROJECT="${2:-drift-import}"
TAG="${3:-latest}"
# Pi Zero 2 W: linux/arm64 for 64-bit Raspberry Pi OS, linux/arm/v7 for 32-bit.
PLATFORM="${4:-linux/arm64}"
IMAGE="${REGISTRY}/${PROJECT}/drift-import:${TAG}"

# Harbor runs over HTTP here; use http:// for the API.
HARBOR_SCHEME="${HARBOR_SCHEME:-http}"
API="${HARBOR_SCHEME}://${REGISTRY}/api/v2.0"

echo ">> Target image: ${IMAGE} (platform ${PLATFORM})"

# 1) Ensure the Harbor project exists (needs API credentials).
if [[ -n "${HARBOR_USER:-}" && -n "${HARBOR_PASS:-}" ]]; then
  echo ">> Ensuring Harbor project '${PROJECT}' exists…"
  code=$(curl -s -o /dev/null -w "%{http_code}" -u "${HARBOR_USER}:${HARBOR_PASS}" \
    "${API}/projects?project_name=${PROJECT}")
  if [[ "${code}" == "200" ]]; then
    curl -s -u "${HARBOR_USER}:${HARBOR_PASS}" -X POST "${API}/projects" \
      -H "Content-Type: application/json" \
      -d "{\"project_name\":\"${PROJECT}\",\"public\":false}" \
      -o /dev/null -w ">> create project HTTP %{http_code}\n" || true
  fi
  echo ">> Logging in to registry…"
  echo "${HARBOR_PASS}" | docker login "${REGISTRY}" -u "${HARBOR_USER}" --password-stdin
else
  echo ">> HARBOR_USER/HARBOR_PASS not set; assuming you already ran 'docker login ${REGISTRY}'"
  echo ">> and that the '${PROJECT}' project already exists in Harbor."
fi

# 2) Build for the target platform (BuildKit + qemu handles cross-arch).
echo ">> Building…"
DOCKER_BUILDKIT=1 docker build --platform "${PLATFORM}" -t "${IMAGE}" .

# 3) Push.
echo ">> Pushing ${IMAGE}…"
docker push "${IMAGE}"

echo ">> Done. On the Pi:  docker compose pull && docker compose up -d"
