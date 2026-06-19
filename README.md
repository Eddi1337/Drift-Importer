# Drift-Import

Self-hosted camera offload & upload manager for the **Drift XL** action camera,
designed to run on a **Raspberry Pi Zero 2 W**. Plug the camera in over USB,
browse the clips in a web GUI, fix timestamps, merge the 5-minute segments,
tag/organise into albums, and upload to one or more destinations (Nextcloud,
SFTP, or a local/NAS path).

## Features

- **Device detection & import** — scans mounted DCIM volumes; one-click
  *Import* or *Upload Everything*.
- **Web GUI** with thumbnail gallery and in-browser video playback using HTTP
  Range streaming (the server never buffers a whole clip in RAM).
- **Filter by Year/Month**, tag, album, and upload status.
- **File management** — rename / delete (library-only or with the file).
- **Timestamp correction** — absolute set or relative batch shift; updates the
  DB, file mtime, and embedded metadata (stream-copy, no re-encode).
- **Merge clips** in order via ffmpeg concat **stream-copy** (no re-encode →
  fast and low-CPU on the Pi).
- **Tags & albums** with reordering (album order drives merges).
- **Multiple destinations** configured in the GUI: Nextcloud (WebDAV), SFTP, and
  local/NAS path. Per-destination upload status, "test connection", and
  per-destination remote path templating (e.g. `{year}/{month:02d}`).
- **Background jobs** with progress, cancel, and persistence across restarts.

## Design notes for the Pi Zero 2 W (512 MB RAM)

- FastAPI + a single Uvicorn worker. No Celery/Redis — background work runs on a
  small in-process thread pool backed by SQLite.
- Uploads and playback **stream from disk in chunks**; whole files never hit RAM.
- Concurrency is capped (default: 1 upload, 1 ffmpeg at a time) — see `.env`.
- Merging uses ffmpeg `-c copy`; if clips' codecs/resolutions differ the merge
  is rejected with an explanation rather than silently re-encoding (which would
  be painfully slow on this hardware).

## Quick start (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # edit as needed
python run.py                 # http://localhost:8080
```

Requires `ffmpeg`/`ffprobe` on PATH (`sudo apt-get install ffmpeg`).

## Install on Raspberry Pi OS (systemd)

```bash
git clone <this repo> && cd Drift-Import
./install.sh                  # installs ffmpeg, venv, deps, systemd service
sudoedit /opt/drift-import/.env
sudo systemctl restart drift-import
journalctl -u drift-import -f
```

### Auto-mounting the camera

Raspberry Pi OS Desktop auto-mounts USB media under `/media/<user>/…`. On Lite,
install `udisks2`/`usbmount` or add an `/etc/fstab` entry, then point
`DRIFT_MOUNT_PATHS` at the mount base. The app looks for a `DCIM` folder under
each mounted volume, falling back to mounted folders that directly contain
video files.

### Mounting your NAS (recommended for the "local" destination)

Mount the NAS at the OS level and use a **local** destination pointing at it —
simpler and more robust than in-app SMB:

```
# /etc/fstab
//nas.local/camera  /mnt/nas/camera  cifs  credentials=/etc/nas.cred,uid=pi,gid=pi  0  0
```

Then add a destination of type *Local / NAS* with base path `/mnt/nas/camera`.

## Configuration

All settings come from environment / `.env` (see `.env.example`). Notable ones:

| Variable | Purpose |
|---|---|
| `DRIFT_MOUNT_PATHS` | Comma-separated base paths scanned for the camera |
| `DRIFT_WORKING_DIR` | Where merged/derived clips are written |
| `DRIFT_AUTH_PASSWORD` | Set to enable HTTP Basic login (blank = no auth) |
| `DRIFT_MAX_CONCURRENT_UPLOADS` | Parallel uploads (default 1) |
| `DRIFT_MAX_CONCURRENT_FFMPEG` | Parallel ffmpeg jobs (default 1) |

Destination credentials are encrypted at rest with a Fernet key stored in
`DRIFT_DATA_DIR/secret.key` (mode 600) — never in plaintext in the database.

## Docker

```bash
docker compose up -d --build      # http://<host>:8080
```

The compose file mounts `/media` and `/mnt` from the host with slave mount
propagation so the container can see cameras mounted after it starts. State is
kept in named volumes `drift-data` and `drift-working`.

### Build & push to Harbor (manual)

```bash
# Build for the Pi (32-bit arm/v7 + 64-bit arm64) and push to Harbor.
# Harbor here is HTTP-only, so the builder needs an insecure-registry config.
docker login 192.168.10.155
docker buildx build --platform linux/arm64,linux/arm/v7 \
  -t 192.168.10.155/drift-import/drift-import:latest --push .
```

Then on the Pi: `docker compose pull && docker compose up -d`.

> **32-bit note:** the Pi Zero 2 W runs 32-bit Raspberry Pi OS (`armv7l`).
> `cryptography`, `pydantic-core` etc. have no 32-bit ARM wheels on PyPI, so the
> Dockerfile uses **piwheels** for those, and `uvicorn` (not `uvicorn[standard]`)
> to avoid the compiled `uvloop`/`httptools`. The image then builds with no
> compiler toolchain.

## CI/CD (GitHub Actions → Harbor → Pi)

`.github/workflows/build-deploy.yml` runs on a **self-hosted runner** and, on
every push to `main`:

1. registers QEMU and a `buildx` builder (configured for the HTTP Harbor
   registry),
2. logs in to Harbor and builds + pushes the multi-arch image
   (`linux/arm64,linux/arm/v7`),
3. deploys to the Pi over SSH (`deploy/deploy-to-pi.sh`): ships
   `deploy/docker-compose.pi.yml`, logs the Pi into Harbor, `docker compose pull`
   + `up -d`.

### Configurable deploy target

The deploy host is the **`DEPLOY_HOST`** variable (default `ed@192.168.3.188`),
read by `deploy/deploy-to-pi.sh` and overridable from the runner `.env`. Point
it at any Docker host with the deploy SSH key authorised to deploy elsewhere.

### Runner / credentials provisioning

Because this pushes to a private Harbor over a deploy key (no GitHub PAT for
repo secrets), credentials live in the **runner's `.env`** (loaded into every
job's environment), not in GitHub Secrets:

```
# /home/github/actions-runner-drift/.env   (chmod 600, owned by the runner user)
HARBOR_REGISTRY=192.168.10.155
HARBOR_ROBOT_USER=robot$drift-import+drift-pusher
HARBOR_ROBOT_TOKEN=********
DEPLOY_HOST=ed@192.168.3.188
DEPLOY_SSH_KEY=/home/github/.ssh/drift_deploy
```

The runner is registered to the repo with labels `self-hosted,drift,docker`
and installed as a systemd service like the host's other runners. The Pi
authorises `DEPLOY_SSH_KEY`'s public key for the deploy user.

## Tests

```bash
pip install pytest
pytest -q
```

Covers timestamp shifting, remote-path templating, merge command construction
and concat-list escaping, and credential encryption.

## API

The GUI is driven by a JSON API under `/api` (FastAPI auto-docs at `/docs`):
`/api/devices`, `/api/import-device`, `/api/media`, `/api/media/{id}/stream`,
`/api/destinations`, `/api/upload`, `/api/timestamp`, `/api/merge`,
`/api/albums`, `/api/jobs`, …
```
