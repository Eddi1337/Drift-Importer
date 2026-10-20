"""Exercise the mount helper without requiring root or touching real disks."""
import os
from pathlib import Path
import subprocess

import pytest


HELPER = Path(__file__).resolve().parents[1] / "deploy" / "drift-camera-mount.sh"


def test_unchanged_camera_install_does_not_write_or_retrigger_disks(tmp_path):
    commands = tmp_path / 'bin'
    commands.mkdir()
    actions = tmp_path / 'actions'
    for name in ('install', 'systemctl', 'udevadm'):
        executable = commands / name
        executable.write_text('#!/usr/bin/env bash\necho "${0##*/} $*" >> "$ACTIONS"\n')
        executable.chmod(0o755)
    compare = commands / 'cmp'
    compare.write_text('#!/usr/bin/env bash\nexit 0\n')
    compare.chmod(0o755)
    result = subprocess.run(['bash', str(HELPER.with_name('install-camera-mount.sh'))],
        env={**os.environ, 'PATH': f'{commands}:{os.environ["PATH"]}', 'ACTIONS': str(actions)},
        text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    # The existing camera service and mount remain untouched. On a host with
    # its camera attached, checking whether the service is active is harmless.
    lines = actions.read_text().splitlines() if actions.exists() else []
    assert all(line == 'systemctl is-active --quiet drift-camera-mount.service' for line in lines)


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
