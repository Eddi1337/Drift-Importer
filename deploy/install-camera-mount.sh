#!/usr/bin/env bash
# Shared by native installation and Docker deployment. Run as root on the Pi.
set -euo pipefail
files=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

# Application releases usually do not change device setup. Avoid rewriting SD
# files and retriggering every disk (or waiting for unrelated udev work) while
# the camera is actively uploading.
if ! cmp -s "$files/drift-camera-mount.sh" /usr/local/libexec/drift-camera-mount; then
  install -D -m 0755 "$files/drift-camera-mount.sh" /usr/local/libexec/drift-camera-mount
fi
if ! cmp -s "$files/drift-camera-mount.service" /etc/systemd/system/drift-camera-mount.service; then
  install -D -m 0644 "$files/drift-camera-mount.service" /etc/systemd/system/drift-camera-mount.service
  systemctl daemon-reload
fi
if ! cmp -s "$files/99-drift-camera.rules" /etc/udev/rules.d/99-drift-camera.rules; then
  install -D -m 0644 "$files/99-drift-camera.rules" /etc/udev/rules.d/99-drift-camera.rules
  udevadm control --reload-rules
  udevadm trigger --subsystem-match=block --action=add
  udevadm settle --timeout=30
fi
# SYSTEMD_WANTS only starts units for newly active devices. Explicitly start
# for a camera already connected when its rule is first installed.
if [ -b /dev/drift-camera ] && ! systemctl is-active --quiet drift-camera-mount.service; then
  systemctl start drift-camera-mount.service
fi
