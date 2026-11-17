# SPDX-License-Identifier: MPL-2.0
"""Concrete infrastructure adapters implementing cst_core ports (files: cst_core.adapters.files; zipapps: cst_core.adapters.zipapp)."""

from collections.abc import Callable, Mapping, Sequence
import os
import subprocess
import time

from cst_core.ports import CommandFailed, CommandResult, CommandRunnerPort, ExecutionContextPort


def _decode_stream(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data


def c_locale(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """ENV (nothing when None) with LC_ALL=C on top: for a command whose
    answers CST parses (virsh translates its answers)."""
    return {**(env or {}), "LC_ALL": "C"}


class SubprocessCommandRunner:
    """CommandRunnerPort backed by subprocess.run with explicit stdin handling."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run argv with capture_output=True and DEVNULL stdin unless input_text is given.

        Output that is not UTF-8 is decoded with U+FFFD for the bad bytes,
        never a UnicodeDecodeError out of run().
        """
        cmd = list(argv)
        merged_env = None if env is None else {**os.environ, **env}
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                # No stdin of ours leaks into the command: /dev/null, or input_text.
                stdin=subprocess.DEVNULL if input_text is None else None,
                input=input_text,
                env=merged_env,
            )
        except FileNotFoundError as exc:
            if check:
                raise CommandFailed(cmd, 127, "", str(exc)) from None
            return CommandResult(returncode=127, stdout="", stderr=str(exc), argv=tuple(cmd))
        except subprocess.TimeoutExpired as exc:
            msg = f"timed out after {timeout}s" if timeout is not None else "command timed out"
            out = _decode_stream(exc.stdout)
            err = _decode_stream(exc.stderr) or msg
            if check:
                raise CommandFailed(cmd, 124, out, err) from None
            return CommandResult(returncode=124, stdout=out, stderr=err, argv=tuple(cmd))
        except OSError as exc:
            if check:
                raise CommandFailed(cmd, 126, "", str(exc)) from None
            return CommandResult(returncode=126, stdout="", stderr=str(exc), argv=tuple(cmd))
        if check and proc.returncode != 0:
            raise CommandFailed(cmd, proc.returncode, proc.stdout, proc.stderr)
        return CommandResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, argv=tuple(cmd))


class HostExecutionContext:
    """ExecutionContextPort and HostPathPort detecting host vs toolbox/container environments."""

    def __init__(
        self,
        *,
        exists: Callable[[str], bool] | None = None,
        euid: Callable[[], int] | None = None,
        host_root: str = "/run/host",
    ) -> None:
        self._exists = os.path.exists if exists is None else exists
        self._euid = os.geteuid if euid is None else euid
        self._host_root = host_root.rstrip("/") or "/run/host"

    def is_container(self) -> bool:
        """Return True when running inside a Podman toolbox or Docker container."""
        return self._exists("/run/.containerenv") or self._exists("/.dockerenv")

    def is_root(self) -> bool:
        """Return True when effective UID is 0."""
        return self._euid() == 0

    def path(self, host_path: str) -> str:
        """Map an absolute host path into /run/host when inside a container."""
        if self.is_container() and host_path.startswith("/"):
            return f"{self._host_root}/{host_path.lstrip('/')}"
        return host_path

    def etc_dir(self) -> str:
        """Return the host's /etc path as readable from the current context."""
        return f"{self._host_root}/etc" if self.is_container() else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        """Return a host /run subdirectory as readable from the current context."""
        cleaned = subpath.strip("/")
        base = f"{self._host_root}/run" if self.is_container() else "/run"
        return f"{base}/{cleaned}" if cleaned else base


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

    def escape_path(self, path: str, *, suffix: str) -> str:
        """Escape a filesystem path into a systemd unit name via systemd-escape."""
        res = self._runner.run(["systemd-escape", "-p", f"--suffix={suffix}", path], check=True)
        return res.stdout.strip()


class SubprocessVirshAdapter:
    """VirshPort adapter running `virsh` on the host (via `flatpak-spawn --host` in containers)."""

    def __init__(
        self,
        uri: str = "qemu:///system",
        runner: CommandRunnerPort | None = None,
        ctx: ExecutionContextPort | None = None,
    ) -> None:
        self._uri = uri
        self._runner = SubprocessCommandRunner() if runner is None else runner
        self._ctx = HostExecutionContext() if ctx is None else ctx

    def call(
        self,
        *args: str,
        check: bool = False,
        timeout: float | None = 60,
        start_new_session: bool = False,
    ) -> CommandResult:
        """Execute `virsh -c <uri> *args` (using direct subprocess when start_new_session=True)."""
        base = ["virsh", "-c", self._uri, *args]
        cmd = ["flatpak-spawn", "--host", *base] if self._ctx.is_container() else base
        if start_new_session:
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=timeout,
                    stdin=subprocess.DEVNULL,
                    env=c_locale(os.environ),
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                if check:
                    raise CommandFailed(cmd, 127, "", str(exc)) from None
                return CommandResult(127, "", str(exc), argv=tuple(cmd))
            except subprocess.TimeoutExpired:
                if check:
                    raise CommandFailed(cmd, 124, "", f"timed out after {timeout}s") from None
                return CommandResult(124, "", f"timed out after {timeout}s", argv=tuple(cmd))
            if check and proc.returncode != 0:
                raise CommandFailed(cmd, proc.returncode, proc.stdout, proc.stderr)
            return CommandResult(proc.returncode, proc.stdout, proc.stderr, argv=tuple(cmd))
        return self._runner.run(cmd, check=check, timeout=timeout, env=c_locale())

    def domains(self, *, all_domains: bool = True) -> list[str]:
        """Return configured domain names, or empty list on failure."""
        args = ["list", "--all", "--name"] if all_domains else ["list", "--name"]
        res = self.call(*args, check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def dumpxml(self, domain: str, *, inactive: bool = False) -> str | None:
        """Return XML definition of `domain`, or None on failure."""
        args = ["dumpxml", "--inactive", domain] if inactive else ["dumpxml", domain]
        res = self.call(*args, check=False)
        return res.stdout if res.returncode == 0 else None
