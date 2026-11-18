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
    """SystemdPort implementation delegating to systemctl and systemd-escape.

    timeout and locale_c apply to every systemctl call (show: 20 s unless a timeout is given, and
    always the C locale); by default none (no limit, the caller's locale). A caller that keeps its own words for a systemctl that is
    missing or does not answer gives the runner its words (SubprocessCommandRunner(words=...))."""

    def __init__(self, runner: CommandRunnerPort, *, timeout: float | None = None, locale_c: bool = False) -> None:
        self._runner = runner
        self._timeout = timeout
        self._locale_c = locale_c

    def _systemctl(self, *args: str, check: bool = False) -> CommandResult:
        return self._runner.run(["systemctl", *args], check=check, timeout=self._timeout, locale_c=self._locale_c)

    def daemon_reload(self) -> None:
        """Run `systemctl daemon-reload` (CommandFailed when it fails)."""
        self._systemctl("daemon-reload", check=True)

    def start(self, unit: str) -> CommandResult:
        """Start unit; its exit status and output (the caller shows systemctl's message)."""
        return self._systemctl("start", unit)

    def stop(self, unit: str) -> CommandResult:
        """Stop unit; its exit status and output."""
        return self._systemctl("stop", unit)

    def restart(self, unit: str) -> CommandResult:
        """Restart unit; its exit status and output."""
        return self._systemctl("restart", unit)

    def enable(self, unit: str, *, now: bool = False) -> CommandResult:
        """Enable unit (and start it, with now); its exit status and output."""
        return self._systemctl("enable", *(["--now"] if now else []), unit)

    def disable(self, unit: str, *, now: bool = False) -> CommandResult:
        """Disable unit (and stop it, with now); its exit status and output."""
        return self._systemctl("disable", *(["--now"] if now else []), unit)

    def clean(self, unit: str, *, what: str) -> CommandResult:
        """Remove what systemd keeps for unit (what: state, cache, logs, ...); its exit status and output."""
        return self._systemctl("clean", f"--what={what}", unit)

    def escape_path(self, path: str, *, suffix: str) -> UnitName:
        """Escape a filesystem path into a systemd unit name via systemd-escape."""
        res = self._runner.run(["systemd-escape", "-p", f"--suffix={suffix}", path], check=True)
        return UnitName(res.stdout.strip())

    def show(self, unit: UnitName, props: Sequence[str], *, on_host: bool = True) -> dict[str, str]:
        """`systemctl show -p PROP... unit` as {prop: value}, on the host from a toolbox (read-only, so
        also from plan and status there; on_host=False: where the caller runs); {} when systemctl fails
        or cannot start. 20 s at most, or the adapter's timeout when it has one."""
        argv = ["systemctl", "show", *(a for p in props for a in ("-p", p)), unit]
        timeout = 20 if self._timeout is None else self._timeout
        res = self._runner.run(argv, check=False, timeout=timeout, locale_c=True, on_host=on_host)
        if res.returncode != 0:
            return {}
        return dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
