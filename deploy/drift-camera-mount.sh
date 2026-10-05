#!/usr/bin/env bash
# Mount only the udev-identified camera, without changing its recordings.
set -euo pipefail

camera=/dev/drift-camera
target=/media/drift-camera
device=$(readlink -e "$camera")
mkdir -p "$target"

if source=$(findmnt -rn --mountpoint "$target" -o SOURCE); then
  # Deploying while connected is harmless: adopt the same read-only mount.
  mounted_device=$(readlink -e "$source" || true)
  if [ "$mounted_device" = "$device" ]; then
    options=$(findmnt -rn --mountpoint "$target" -o OPTIONS)
    if [[ ",$options," = *,ro,* ]]; then
      exit 0
    fi
    echo "Camera is already mounted writable; unmount it before starting this service" >&2
    exit 1
  fi
  # An unplugged card can leave an orphaned mount from an older installation.
  # Never detach a different device which is still present.
  if [ -n "$mounted_device" ]; then
    echo "Refusing to replace another device mounted at $target" >&2
    exit 1
  fi
  umount -l "$target"
fi

mount -t exfat -o ro,nosuid,nodev,noexec "$camera" "$target"
