"""Concrete adapters implementing swap ports on Linux."""

import re

from cst_core.ports import CommandRunnerPort, FileSystemPort


class LinuxSwapSystemAdapter:
    """SwapSystemPort backed by blockdev, blkid, modprobe, sysctl, zramctl, and swapon."""

    def __init__(self, runner: CommandRunnerPort, fs: FileSystemPort) -> None:
        self._runner = runner
        self._fs = fs

    def blockdev_size_bytes(self, dev_path: str) -> int | None:
        """Query exact block device size via `blockdev --getsize64`, or None if unreadable."""
        res = self._runner.run(["blockdev", "--getsize64", dev_path], check=False)
        if res.returncode != 0:
            return None
        out = res.stdout.strip()
        return int(out) if out.isdigit() else None

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

    def apply_sysctl_file(self, sysctl_conf: str, *, exclude_key: str | None = None) -> None:
        """Apply a sysctl.d file with `sysctl -q -p`, optionally skipping one key."""
        if exclude_key is None:
            self._runner.run(["sysctl", "-q", "-p", sysctl_conf], check=True)
            return
        text = self._fs.read_text(sysctl_conf) or ""
        pattern = re.compile(rf"^\s*{re.escape(exclude_key)}\s*=")
        filtered = "\n".join(line for line in text.splitlines() if not pattern.match(line)) + "\n"
        self._runner.run(["sysctl", "-q", "-p", "-"], check=True, input_text=filtered)

    def reset_zram0(self) -> None:
        """Best-effort reset of /dev/zram0 after stopping its swap unit."""
        self._runner.run(["zramctl", "--reset", "/dev/zram0"], check=False)

    def swapon_show(self) -> str:
        """Return `swapon --show` output, or '' if the command fails."""
        res = self._runner.run(["swapon", "--show"], check=False)
        return res.stdout if res.returncode == 0 else ""


__all__ = ["LinuxSwapSystemAdapter"]
