# SPDX-License-Identifier: MPL-2.0
"""Concrete adapters implementing swap ports on Linux."""

import re

from cst_core.adapters import SystemClockAdapter
from cst_core.ports import CommandResult, CommandRunnerPort, FileSystemPort


class LinuxSwapSystemAdapter:
    """SwapSystemPort backed by blockdev, blkid, modprobe, sysctl, zramctl, and swapon."""

    def __init__(self, runner: CommandRunnerPort, fs: FileSystemPort) -> None:
        self._runner = runner
        self._fs = fs
        self._clock = SystemClockAdapter()

    def blockdev_size(self, dev_path: str) -> str:
        """The size `blockdev --getsize64` prints; CommandFailed when it fails (apply stops, as under set -e)."""
        return self._runner.run(["blockdev", "--getsize64", dev_path], check=True).stdout.strip()

    def blkid_type(self, dev_path: str) -> str:
        """Return `blkid -o value -s TYPE` for dev_path, or '' when absent."""
        res = self._runner.run(["blkid", "-o", "value", "-s", "TYPE", dev_path], check=False)
        if res.returncode != 0:
            return ""
        return res.stdout.strip()

    def modprobe(self, module: str) -> None:
        """Best-effort kernel module load before inspecting /proc/crypto."""
        # Built-in algorithms have no module to load; preconditions() checks /proc/crypto right after.
        self._runner.run(["modprobe", module], check=False)

    def apply_sysctl_file(self, sysctl_conf: str, *, exclude_key: str | None = None) -> CommandResult:
        """Apply a sysctl.d file with `sysctl -q -p`, optionally skipping one key; its exit status and output."""
        if exclude_key is None:
            return self._runner.run(["sysctl", "-q", "-p", sysctl_conf], check=False)
        text = self._fs.read_text(sysctl_conf) or ""
        pattern = re.compile(rf"^\s*{re.escape(exclude_key)}\s*=")
        filtered = "\n".join(line for line in text.splitlines() if not pattern.match(line)) + "\n"
        return self._runner.run(["sysctl", "-q", "-p", "-"], check=False, input_text=filtered)

    def reset_zram0(self) -> None:
        """Best-effort reset of /dev/zram0 after stopping its swap unit."""
        self._runner.run(["zramctl", "--reset", "/dev/zram0"], check=False)

    def swapon_show(self) -> CommandResult:
        """`swapon --show`: shown whatever its exit status (`swapon --show || true` before)."""
        return self._runner.run(["swapon", "--show"], check=False)

    def sleep(self, seconds: float) -> None:
        """Pause `seconds` while waiting for an asynchronous unit update."""
        self._clock.sleep(seconds)
