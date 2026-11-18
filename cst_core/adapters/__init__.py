# SPDX-License-Identifier: MPL-2.0
"""Concrete infrastructure adapters implementing cst_core ports (the runner: cst_core.adapters.runner; virsh: cst_core.adapters.virsh; files: cst_core.adapters.files; zipapps: cst_core.adapters.zipapp)."""

from collections.abc import Sequence
import time

from cst_core.ports import CommandResult, CommandRunnerPort, UnitName


class SystemClockAdapter:
    """ClockPort delegating to the Python standard library `time` module."""

    def now(self) -> float:
        """Return wall-clock seconds since the Unix epoch."""
        return time.time()

    def monotonic(self) -> float:
        """Return monotonic seconds."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Sleep for `seconds`."""
        time.sleep(seconds)


class SystemctlAdapter:
    """SystemdPort implementation delegating to systemctl and systemd-escape."""

    def __init__(self, runner: CommandRunnerPort) -> None:
        self._runner = runner

    def daemon_reload(self) -> None:
        """Run `systemctl daemon-reload`."""
        self._runner.run(["systemctl", "daemon-reload"], check=True)

    def start(self, unit: str) -> CommandResult:
        """Start unit; its exit status and output (the caller shows systemctl's message)."""
        return self._runner.run(["systemctl", "start", unit], check=False)

    def stop(self, unit: str) -> CommandResult:
        """Stop unit; its exit status and output."""
        return self._runner.run(["systemctl", "stop", unit], check=False)

    def restart(self, unit: str) -> CommandResult:
        """Restart unit; its exit status and output."""
        return self._runner.run(["systemctl", "restart", unit], check=False)

    def escape_path(self, path: str, *, suffix: str) -> UnitName:
        """Escape a filesystem path into a systemd unit name via systemd-escape."""
        res = self._runner.run(["systemd-escape", "-p", f"--suffix={suffix}", path], check=True)
        return UnitName(res.stdout.strip())

    def show(self, unit: UnitName, props: Sequence[str]) -> dict[str, str]:
        """`systemctl show -p PROP... unit` as {prop: value}, on the host from a toolbox (read-only, so
        also from plan and status there); {} when systemctl fails or cannot start."""
        argv = ["systemctl", "show", *(a for p in props for a in ("-p", p)), unit]
        res = self._runner.run(argv, check=False, timeout=20, locale_c=True, on_host=True)
        if res.returncode != 0:
            return {}
        return dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
