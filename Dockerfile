# Drift-Import container image.
# Multi-arch: builds for linux/arm64 (Pi Zero 2 W / Pi 3+ 64-bit OS) and amd64.
FROM python:3.11-slim

# ffmpeg/ffprobe are required for probing, thumbnails, timestamps and merging.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DRIFT_DATA_DIR=/data \
    DRIFT_WORKING_DIR=/working \
    DRIFT_THUMBNAIL_DIR=/data/thumbnails \
    DRIFT_PORT=8080

WORKDIR /app

COPY requirements.txt .
# piwheels serves prebuilt ARM (armv6l/armv7l) wheels for the Rust/C packages
# here — cryptography, pydantic-core, uvloop, httptools, watchfiles — so the
# 32-bit Raspberry Pi build needs no compiler toolchain. It is harmless on other
# architectures: pip finds no matching wheels there and falls back to PyPI.
RUN pip install --no-cache-dir \
      --extra-index-url https://www.piwheels.org/simple \
      -r requirements.txt

COPY app ./app
COPY run.py .

# Persisted state lives on mounted volumes.
VOLUME ["/data", "/working"]
EXPOSE 8080

# Single worker: the in-process job threads share the SQLite DB.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
