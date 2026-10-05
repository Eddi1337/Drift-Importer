#!/usr/bin/env bash
# Shared by native installation and Docker deployment. Run as root on the Pi.
set -euo pipefail
files=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

install -D -m 0755 "$files/drift-camera-mount.sh" /usr/local/libexec/drift-camera-mount
install -D -m 0644 "$files/drift-camera-mount.service" /etc/systemd/system/drift-camera-mount.service
install -D -m 0644 "$files/99-drift-camera.rules" /etc/udev/rules.d/99-drift-camera.rules
systemctl daemon-reload
udevadm control --reload-rules
udevadm trigger --subsystem-match=block --action=add
udevadm settle --timeout=30
# SYSTEMD_WANTS only starts units for newly active devices. Explicitly start
# for a camera already connected when its rule is first installed.
if [ -b /dev/drift-camera ]; then
  systemctl start drift-camera-mount.service
fi
