# AGENTS.md

Operational notes for agents working on **Drift-Import**. See `README.md` for the
user-facing feature list; this file focuses on architecture gotchas and the live
deployment.

## What this is

Self-hosted camera offload & upload manager for the **Drift XL** action camera.
FastAPI + a single Uvicorn worker + SQLite, sized for a Raspberry Pi. Plug the
camera in over USB, browse/fix-timestamps/merge clips in a web GUI, then upload
to one or more destinations (Nextcloud / SFTP / local-or-NAS path).

## Architecture (the parts that bite)

- **In-process job system** (`app/jobs.py`) backed by SQLite — no Celery/Redis.
  Worker threads claim queued jobs; semaphores cap concurrent uploads/ffmpeg
  (default 1 each). Handlers live in `app/tasks.py`: `import`, `thumbnail`,
  `timestamp`, `merge`, `upload`.
- **Uploads stream directly from the camera card.** Import indexes each file by
  its on-camera path (`/media/<user>/<Card>/DCIM/…`, `source=device`); it does
  **not** copy footage into local storage. A clip can therefore only be uploaded
  **while the camera is physically attached**. `/working` holds only merged/
  derived clips. ⇒ If a video was never uploaded and the card is unplugged (or
  re-formatted), it cannot be uploaded and the source copy is gone.
- Per-`(media, destination)` status lives in `upload_states`; a dedup ledger in
  `uploaded_clips` is keyed on `(destination_id, checksum)`. `media.checksum()`
  is a **sampled** sha256 (file size + first/last 4 MiB), not a full hash — fast
  on the Pi, good enough for dedup.
- An upload **job** is marked `done` even if individual clips inside it errored —
  per-clip failures are caught and recorded on the clip/state, not raised as a
  job failure. **Do not read "job: done" as "all clips uploaded."** Check
  `uploaded_clips.status` / `upload_states.status` for the truth.

## Deployment — the Pi

- Host: **`ed@192.168.3.188`** (hostname `drift-pi`; 32-bit Raspberry Pi OS,
  `armv7l`). SSH in as `ed` (key auth).
- Runs as a Docker container named **`drift-import`** (root inside the
  container), pulled from the Harbor registry at `192.168.10.155` and run via
  `docker compose` using `deploy/docker-compose.pi.yml`. Web UI on **:8080**.
- Volumes: `drift-data` → `/data` (SQLite `drift.db`, `secret.key`, `logs/`),
  `drift-working` → `/working`. Host `/media` and `/mnt` are bind-mounted in as
  `rslave` so the container sees the camera (auto-mounted under `/media`) and the
  NAS — including mounts that appear after the container starts.
- CI/CD: a push to `main` triggers a self-hosted GitHub runner that builds the
  `linux/arm/v7` image, pushes to Harbor, and deploys over SSH via
  `deploy/deploy-to-pi.sh` (`DEPLOY_HOST` defaults to `ed@192.168.3.188`). See
  the README "CI/CD" section.

## Storage / the NAS (case-sensitive — easy to get wrong)

- The default upload destination is a **`local`** backend pointing at the
  NFS-mounted NAS at **`/mnt/NAS`** (uppercase). fstab:
  `192.168.10.110:/Volume1/Personal/Drift  /mnt/NAS  nfs  defaults,_netdev,nofail 0 0`
  (~8 TB total). Layout under it is `{year}/{month:02d}/`.
- `nofail` means that if the NAS is not mounted at boot, `/mnt/NAS` is just an
  empty directory on the **58 GB SD card** — writing there silently fills the
  card. `app/destinations/local.py` guards this: `_require_root()` refuses a
  missing root and `_check_free_space()` fails fast with `ENOSPC`. **But the
  destination `base_path` must match the real mountpoint exactly** — a stray
  lowercase `/mnt/nas` directory on the SD card has previously swallowed uploads
  (`[Errno 28] No space left on device`) because writes landed on the card
  instead of the NAS.

## Investigating on the Pi

```bash
ssh ed@192.168.3.188
docker logs --tail 100 drift-import                 # app logs
docker exec drift-import df -h /mnt/NAS             # NAS mounted + free space?
ls /media/ed ; find /media -iname DCIM             # is the camera attached?
# Query state (tables: media_items, destinations, jobs, job_logs,
#                       upload_states, uploaded_clips):
docker exec drift-import python -c "import sqlite3; \
db=sqlite3.connect('/data/drift.db'); \
print(db.execute(\"select status,count(*) from uploaded_clips group by status\").fetchall())"
```

## Local dev

- `python run.py` → http://localhost:8080 (needs `ffmpeg`/`ffprobe` on PATH).
- Config from `.env` (see `.env.example`). Tests: `pytest -q`.
