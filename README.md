# Drift-Import

## Roadtrip location history

Trips can associate archived clips with journeys reconstructed from Home
Assistant GPS history. Choose a tracker under **Trips → Location sources**,
then import the footage month, or a range of up to twelve months. The app uses
the existing HA connection from Settings, includes location attribute updates,
and fetches one day at a time. A device tracker's GPS history is usually more
detailed than a person entity. Empty recorder windows preserve previously
imported tracks; they do not imply that no journey happened.

The NAS YYYY/MM folder is the month anchor when camera dates are unreliable.
Sequence, recording duration, plausible camera-relative gaps, and time away
from home rank possible outings. These are advisory matches: compare the
alternatives, choose a smaller clip sequence if a folder spans several rides,
and adjust the first-clip time or individual clip offsets before confirming.
Confirmation saves an association; it does not rewrite video files or dates.
Use **Index archive clip details** to read missing duration metadata from
older NAS footage. Metadata probes and location imports are explicit,
serialized background jobs.

The route map and speed/time graph share a time slider; clicking a clip selects
its location interval. The route works without an external basemap. **Show
street map** loads OpenStreetMap tiles in the browser with attribution.
GPS gaps are not bridged, poor fixes and implausible jumps are filtered, and
per-clip average speeds remain unavailable below 80% GPS coverage. Speeds are
GPS estimates for the displayed alignment, not calibrated road measurements.

Location fixes live in private, guarded NAS files under .drift/locations
at roughly one-minute resolution. SQLite holds only compact indexes and
confirmed associations. A 2 MiB RAM cache avoids rereading GPS days during
preview adjustments; imports invalidate changed days. A cold preview can take
30–60 seconds on a slow NAS, and the page shows that it is loading.
GPX and Google Timeline JSON imports are supported
for a selected month, with a 16 MB input limit to bound Pi memory. Google
Timeline is stored on the phone; a Google Maps API key does not grant access
to personal Timeline history. Export it from the phone if HA no longer retains
the required dates. Home Assistant retention varies; long-term numeric
statistics do not replace GPS attribute history.

Self-hosted camera offload & upload manager for the **Drift XL** action camera,
designed to run on a **Raspberry Pi Zero 2 W**. Plug the camera in over USB,
browse the clips in a web GUI, fix timestamps, merge the 5-minute segments,
tag/organise into albums, and upload to one or more destinations (Nextcloud,
SFTP, or a local/NAS path).

## Features

- **Overview homepage** — connected-camera video count/capacity, NAS space,
  archived totals, recording days, Pi CPU/RAM/temperature/network and activity.
- **Import & verify** — stream all videos from the entire attached camera
  (including EVENT recordings) to a chosen NAS. **Verify all NAS copies** runs
  a background full SHA-256 comparison of every video, not just upload status
  or the sampled dedup hash. The dated report lists missing/mismatched files;
  importing again repairs those copies. Keep the camera attached throughout.
  New imports fingerprint camera files again and add a content suffix to uploaded
  filenames, so recycled camera counters and identically named EVENT/DCIM clips
  cannot overwrite a different recording.
- **Back up first, review dates later** — every camera video is saved even when
  its timestamp is uncertain. Questionable dates go to `Holding` on the NAS,
  with filename/recording-order best guesses, confidence and reasons in the
  Import page. Review the NAS video, adjust a suggested time, and preview before
  applying corrected dates and metadata. Saved backups remain reviewable after
  unplugging the camera. Original backups retain their exact bytes; optional
  metadata-corrected copies are stored separately.
- **Named trips and dated maps** — select backed-up videos across days or months
  and save them as one named trip. Reopen the trip to edit its files, filter its
  map and timeline by date, and select numbered video markers to see their
  filenames, dates and GPS coordinates. Locations require a confirmed GPS
  association; unmatched videos and gaps remain visible without invented pins.
  Camera timestamps stay labelled as unverified, and grouping only saves file
  references. Street-map tiles can be enabled explicitly in the map.
- **Suggested trip movies** — every indexed recording day appears on Trips. Create a
  day movie or select several days for one movie in recording order. This uses
  completed NAS copies, so the camera can be unplugged after offload. Finished
  movies are kept separately in `<destination>/Trips/<year>/`, with Watch and
  Download controls. Original clips are untouched.

- **Device detection & import** — scans mounted DCIM volumes; one-click
  *Import* or *Upload Everything*.
- **Web GUI** with thumbnail gallery and in-browser video playback using HTTP
  Range streaming (the server never buffers a whole clip in RAM).
- **Filter by Year/Month**, tag, album, and upload status.
- **File management** — rename / delete (library-only or with the file).
- **Timestamp correction** — absolute set or relative batch shift; updates the
  DB, file mtime, and embedded metadata (stream-copy, no re-encode).
- **Make a latest-day movie** — one Library action groups all original clips
  from the newest day and joins them in capture order with **stream-copy** (no
  re-encode → fast and low-CPU on the Pi), leaving the originals untouched.
- **Tags & albums** with reordering (album order drives merges).
- **Multiple destinations** configured in the GUI: Nextcloud (WebDAV), SFTP, and
  local/NAS path. Per-destination upload status, "test connection", and
  per-destination remote path templating (e.g. `{year}/{month:02d}`).
- **Background jobs** with progress, cancel, and persistence across restarts.
- **Home Assistant progress** — one overall percentage for uploads, imports,
  verification and trip movies, refreshed every five seconds. A native horizontal
  bar appears only while tasks run, and hides for idle, queued-only or paused
  work. The token is encrypted with the existing data-directory secret key and
  is never returned by the settings API.

### Home Assistant dashboard

Set the Home Assistant URL (including `:8123` where needed), token and optional
entity prefix in Settings. Add [the native progress card](deploy/home-assistant-progress.yaml)
to your Home view. It uses `sensor.drift_import_progress` and
`binary_sensor.drift_import_active`; `sensor.drift_import_camera` reports the
attached camera. The percentage measures completion of the current task batch,
including terminated tasks; the Import page's verification report remains the
source of truth for successful camera backups. Tap the card or its bar to open
Home Assistant's recorded progress history; hold it to open Pi Jobs. Keep the
progress entity included in HA Recorder for this history. The card also shows
an estimated finish for the current upload queue, calculated from recent
committed byte checkpoints. Camera indexing can add more queued files, and the
estimate excludes indexing, verification and movie creation. Until there is
enough throughput data, while paused/stalled, or when a transfer needs review,
the ETA stays unknown. `sensor.drift_import_upload_eta` exposes the estimated
finish timestamp; progress attributes include remaining bytes and estimate
scope. Estimates use bounded RAM only and reuse existing ledger checkpoints,
so this feature adds no sample writes to the Pi's SD card.

Blank token input keeps the saved credential; use **Remove the saved Home
Assistant token** to disconnect it. Existing plaintext credentials are migrated
once, and transient publish failures retry on the next tick.

## Design notes for the Pi Zero 2 W (512 MB RAM)

- FastAPI + a single Uvicorn worker. No Celery/Redis — background work runs on a
  small in-process thread pool backed by SQLite.
- Uploads and playback **stream from disk in chunks**; whole files never hit RAM.
- Concurrency is capped (default: 1 upload, 1 ffmpeg at a time) — see `.env`.
- Pi deployment puts temporary movies/concat manifests at `/mnt/NAS/.drift/tmp`
  and thumbnails at `/mnt/NAS/.drift/thumbnails`. `/tmp` is a bounded RAM tmpfs.
  The durable SQLite database remains local; WAL SQLite is not placed on NFS.
  Upload checkpoints persist every 30 seconds on the Pi (and always at terminal
  states), generic job progress every 15 seconds, and system history every five
  minutes. Transfer resume uses the NAS partial file's actual length.
- Pi host setup replaces SD-backed swap with 256 MiB compressed RAM swap.
  System and container logs use a bounded volatile journal (8 MiB); Uvicorn
  access logs and the duplicate app file log are disabled. Thumbnail FFmpeg
  threads are limited to one; the app container is capped at two CPU cores.
- Consistent recovery snapshots are published every 15 minutes to
  `/mnt/NAS/.drift/state/drift.db`, alongside the private `secret.key` needed to
  decrypt saved integration credentials. SQLite's backup API includes committed
  WAL records; temporary snapshots and atomic publication happen entirely on
  the mounted NAS. Startup continues if the NAS is offline. After replacing an
  SD card, stop the app and restore both files to `/data` before starting it.
  Logs in RAM do not survive a reboot; the durable database and NAS snapshots do.
- `DRIFT_REQUIRE_NAS_MOUNT=true` checks the actual filesystem is NFS/CIFS before
  writes. An existing directory or autofs placeholder on the SD card is rejected.
  The web dashboard still starts while the NAS is unavailable.
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

Both `./install.sh` and the GitHub Actions Docker deployment install a
udev-triggered systemd mount for the Ghost XL card labelled `Drift Card`.
It mounts read-only at `/media/drift-camera` whenever the camera is connected,
so the app's existing auto-import/upload settings can
start work without a desktop session. The app looks for a `DCIM` folder under
each mounted volume, falling back to mounted folders that directly contain
video files.

The rule matches udev's normalised label `Drift_Card` and creates the stable
`/dev/drift-camera` alias. The mount follows that alias when USB device names
change, and detaches on unplug so old mounts cannot hide a reconnected card.
The read-only mount protects recordings; timestamp edits to camera originals
require a separately managed writable mount. Archived trip outputs remain on
the NAS.

If your card has a different filesystem label or camera USB ID, adjust
`deploy/99-drift-camera.rules` before installing. To update only the camera
mount on an existing Pi, run `sudo bash deploy/install-camera-mount.sh`.

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
# Build for the Pi (32-bit arm/v7) and push to Harbor.
# Harbor here is HTTP-only, so the builder needs an insecure-registry config.
docker login 192.168.10.155
docker buildx build --platform linux/arm/v7 \
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
every push to `main` (tests first run on a GitHub-hosted runner):

1. registers ARM QEMU and a `buildx` builder (configured for the HTTP Harbor
   registry),
2. logs in to Harbor and builds + pushes the 32-bit ARM image
   (`linux/arm/v7`), using the Harbor `buildcache` tag to reuse layers between
   runner builds,
3. deploys to the Pi over SSH (`deploy/deploy-to-pi.sh`): ships
   `deploy/docker-compose.pi.yml`, logs the Pi into Harbor, `docker compose pull`
   + `up -d`, checks HTTP health, and performs a real container NAS write/read.
   Deployments use the commit's immutable 12-character image tag; overlapping
   workflow runs are serialized to avoid deploying an older image over a newer one.

### Configurable deploy target

The deploy host is the **`DEPLOY_HOST`** variable (default `ed@drift-pi.local`),
read by `deploy/deploy-to-pi.sh`. The main workflow explicitly targets this
hostname; manual deployments can override it. Point
it at any Docker host with the deploy SSH key authorised to deploy elsewhere.
The runner must resolve `drift-pi.local`. If mDNS does not cross LAN segments,
provide a LAN DNS entry or a runner `/etc/hosts` entry for the Pi's reserved IP.

### Runner / credentials provisioning

Because this pushes to a private Harbor over a deploy key (no GitHub PAT for
repo secrets), credentials live in the **runner's `.env`** (loaded into every
job's environment), not in GitHub Secrets:

```
# /home/github/actions-runner-drift/.env   (chmod 600, owned by the runner user)
HARBOR_REGISTRY=192.168.10.155
HARBOR_ROBOT_USER=robot$drift-import+drift-pusher
HARBOR_ROBOT_TOKEN=********
DEPLOY_HOST=ed@drift-pi.local
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

### Camera clock checks and historical date corrections

Import backs up every video, including recordings whose dates need review.
It checks Drift `NNNMEDIA/DVRxxxxx.MP4` recording order numerically, including
folder rollover, for backwards counters, regressing timestamps, missing metadata
and newly seen rides ending outside the connection day or previous day in
Europe/London. Normal recordings and `EVENT/E_DVRxxxxx.MP4` are both checked.
Checks read MP4 movie headers with small seeks, without decoding videos.

Recordings with uncertain dates upload to
`Holding/<camera-label>/<camera-relative-folder>/<stem>_<content-hash>.MP4` on
the NAS. The relative folder and content suffix keep different DCIM/EVENT clips
and recycled filenames separate. These original backups preserve every byte.
Date uncertainty never blocks the backup; it only delays filing that recording
under an approved date. Existing completed backups are reused.

On **Import → Recording dates**, choose a folder (or all DCIM folders), select the
first and last video, and provide a known start or end time. Each selected video
shows its current date, suggested date, confidence, reason and NAS backup state.
**Watch NAS copy** opens a saved video for review, including after the camera is
unplugged. **Use best guess in editable time** fills the selected anchor time;
adjust it before previewing. Suggestions use available neighbouring recordings
and sequence evidence; a USB connection time alone does not establish the exact
recording time, and no guess is automatically approved.

A constant offset preserves real recording breaks. Split ranges at any clock
reset. Preview shows corrected dates and matching NAS paths; nothing moves until
the preview is confirmed.
The optional reusable offset applies to subsequent recordings only when sequence,
clock and recent-ride checks pass. A reset stops this reuse.

Confirmed jobs move existing originals into the corrected destination folders,
requiring full camera/NAS SHA-256 equality and refusing to overwrite other files.
The move journal and upload ledger support retry after interruption. Older
unindexed copies are also matched by filename and full hash in the original date
folder; unrelated files are left alone. Saved Holding and indexed NAS originals
can be reviewed and corrected with the camera unplugged. A clip still awaiting
backup needs the camera connected.

Camera files stay read-only. Original NAS backups retain their video bytes and
embedded metadata; an adjacent `.dates.json` records the confirmed correction.
With **Create metadata-corrected copies** enabled, ffmpeg stream-copies the original
into `Corrected/YYYY/MM`, updates container and stream creation times, validates
streams/duration/date, and publishes the result. MP4 creation-time fields have
whole-second precision; the correction record retains fractional clip-end offsets. Scratch files remain in
`/mnt/NAS/.drift/tmp`; this requires extra NAS space, but no re-encoding or video
writes to the Pi SD card. Trip grouping and new trip metadata use confirmed dates.
Failed corrections remain held; retry their job after resolving the reported error.

For future rides, the advanced clock button can send the Pi's current UK time to
a supported Drift camera reachable over Wi-Fi. It cannot set the clock through
USB mass storage. Check a new recording afterwards; firmware support varies.

### NAS inventory and duplicate reports

Stats now includes a saved-file inventory independent of the camera library and
upload ledger. **Scan NAS** discovers existing video and image files in enabled
mounted/local destinations, including older files and Holding. It excludes
hidden caches, symbolic links and incomplete `.part` uploads. Missing expected
uploads are only marked after an entire successful directory scan; an offline
NAS or interrupted scan preserves the previous report. New successful uploads
join the inventory with a cheap filesystem stat, without another directory scan.

The **Duplicates** page first groups equal-sized files and reads at most three
64 KiB samples per candidate. Unique sizes are never read, and unchanged cached
samples are reused. These are possible matches, not proof. **Verify full
content** reads SHA-256 one file at a time and separates different full hashes;
its cache is invalidated when inode, size, modification time or change time
changes. Read-only report refreshes never start scans or hashing.

Hardlinking is a separate, explicitly confirmed job. Choose a kept copy and
copies within the same configured destination. The job reads both complete
files again, requires the same filesystem and matching timestamps, ownership,
permissions and extended attributes/ACLs, and atomically replaces each selected duplicate with a link
while preserving both filenames. Hardlinked names subsequently share edits and
metadata. Files with different dates or permissions remain separate. Keep other
NAS clients from editing selected files while the operation runs. No automatic
hardlinking or deletion is performed; NAS snapshots/compression can affect the
actual saving.

Archive work shares the upload/metadata semaphore, runs one audit at a time,
and is prioritized after the current transfer instead of waiting behind an
entire camera backlog. Cancellation is supported; inventory/sample cache writes
are batched, and no new media copies are staged on the Pi SD card.
