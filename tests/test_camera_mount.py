"""Exercise the mount helper without requiring root or touching real disks."""
import os
from pathlib import Path
import subprocess

import pytest


HELPER = Path(__file__).resolve().parents[1] / "deploy" / "drift-camera-mount.sh"


@pytest.mark.parametrize(
    "scenario,expected_code,expected_actions",
    [
        ("new", 0, ["mount -t exfat -o ro,nosuid,nodev,noexec /dev/drift-camera /media/drift-camera"]),
        ("same", 0, []),
        ("stale", 0, ["umount -l /media/drift-camera", "mount -t exfat -o ro,nosuid,nodev,noexec /dev/drift-camera /media/drift-camera"]),
        ("other", 1, []),
        ("writable", 1, []),
        ("absent", 1, []),
    ],
)
def test_mount_helper_protects_live_devices_and_recovers_stale_mounts(
    tmp_path, scenario, expected_code, expected_actions
):
    commands = tmp_path / "bin"
    commands.mkdir()
    script = """#!/usr/bin/env bash
set -eu
case "${0##*/}" in
  readlink)
    if [ "$2" = /dev/drift-camera ]; then
      [ "$SCENARIO" != absent ] || exit 1
      echo /dev/sdb1
    elif [ "$SCENARIO" = other ]; then
      echo /dev/sdc1
    elif [ "$SCENARIO" = stale ]; then
      exit 1
    else
      echo /dev/sdb1
    fi
    ;;
  findmnt)
    [ "$SCENARIO" != new ] || exit 1
    if [ "$5" = OPTIONS ]; then
      if [ "$SCENARIO" = writable ]; then echo rw,nosuid; else echo ro,nosuid; fi
    else
      echo /dev/sda1
    fi
    ;;
  mkdir) ;;
  mount|umount) echo "${0##*/} $*" >> "$ACTIONS" ;;
esac
"""
    for name in ("readlink", "findmnt", "mkdir", "mount", "umount"):
        executable = commands / name
        executable.write_text(script)
        executable.chmod(0o755)
    actions = tmp_path / "actions"
    result = subprocess.run(
        ["bash", str(HELPER)],
        env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}", "SCENARIO": scenario, "ACTIONS": str(actions)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_code, result.stderr
    assert (actions.read_text().splitlines() if actions.exists() else []) == expected_actions
