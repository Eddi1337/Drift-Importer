#!/usr/bin/env bash
# Compressed RAM swap avoids wearing the Pi's system SD card.
set -euo pipefail
/sbin/modprobe zram num_devices=1
if /usr/sbin/swapon --noheadings --show=NAME | /usr/bin/grep -qx /dev/zram0; then
  exit 0
fi
if [ "$(cat /sys/block/zram0/disksize)" != 0 ]; then
  echo "zram0 is configured by another service; refusing to reset it" >&2
  exit 1
fi
echo lz4 > /sys/block/zram0/comp_algorithm
echo 268435456 > /sys/block/zram0/disksize
/usr/sbin/mkswap --label drift-zram /dev/zram0
/usr/sbin/swapon --priority 100 /dev/zram0
