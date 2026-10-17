#!/usr/bin/env bash
# Idempotent Pi host configuration. Keep durable app state on SD; logs and swap
# use RAM, while the app publishes consistent recovery snapshots on the NAS.
set -euo pipefail
cd "$(dirname "$0")"
install -Dm755 drift-zram-start.sh /usr/local/libexec/drift-zram-start
install -Dm644 drift-zram.service /etc/systemd/system/drift-zram.service
systemctl daemon-reload
# Start RAM swap before stopping the old swap file, so memory can be reclaimed
# safely even during a transfer. If setup fails, leave the SD swap enabled.
systemctl enable --now drift-zram.service
/usr/sbin/swapon --noheadings --show=NAME | /usr/bin/grep -qx /dev/zram0
if systemctl list-unit-files dphys-swapfile.service --no-legend | /usr/bin/grep -q dphys-swapfile; then
  systemctl disable --now dphys-swapfile.service
fi
if /usr/sbin/swapon --noheadings --show=NAME | /usr/bin/grep -qx /var/swap; then
  /usr/sbin/swapoff /var/swap
fi
install -d -m755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/90-drift-low-write.conf <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=8M
RuntimeKeepFree=32M
Compress=yes
EOF
systemctl restart systemd-journald
echo "RAM swap and bounded volatile logs enabled; SD swap disabled"
