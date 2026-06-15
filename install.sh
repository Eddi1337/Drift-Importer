#!/usr/bin/env bash
# Install Drift-Import on Raspberry Pi OS as a systemd service.
set -euo pipefail

APP_DIR=/opt/drift-import
SERVICE=drift-import

echo ">> Installing system dependencies (ffmpeg)…"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg

echo ">> Copying app to ${APP_DIR}…"
sudo mkdir -p "${APP_DIR}"
sudo cp -r app run.py requirements.txt "${APP_DIR}/"
[ -f "${APP_DIR}/.env" ] || sudo cp .env.example "${APP_DIR}/.env"

echo ">> Creating virtualenv…"
sudo python3 -m venv "${APP_DIR}/.venv"
sudo "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo ">> Installing systemd service…"
sudo cp deploy/${SERVICE}.service /etc/systemd/system/${SERVICE}.service
sudo chown -R pi:pi "${APP_DIR}"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE}"
sudo systemctl restart "${SERVICE}"

echo ">> Done. Edit ${APP_DIR}/.env then: sudo systemctl restart ${SERVICE}"
echo ">> Logs: journalctl -u ${SERVICE} -f"
