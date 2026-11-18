# SPDX-License-Identifier: MPL-2.0
"""Concrete infrastructure adapters implementing cst_core ports (files: cst_core.adapters.files; zipapps: cst_core.adapters.zipapp)."""

from collections.abc import Callable, Mapping, Sequence
import functools
import os
import shutil
import subprocess
import time
from typing import TypedDict

from cst_core.ports import (
    CommandFailed,
    CommandFault,
    CommandResult,
    CommandRunnerPort,
    ContainerProbePort,
    DecodeErrors,
    OutputMode,
    StdinMode,
    UnitName,
    VmName,
)


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


class _RunKwargs(TypedDict, total=False):
    """The subprocess.run arguments of one call: only those an option sets, so
    a call with the defaults reaches subprocess.run as it always did."""

    timeout: float
    stdin: int
    input: str
    stdout: int
    env: dict[str, str]
    stderr: int
    capture_output: bool
    text: bool
    preexec_fn: Callable[[], None]
    errors: str
    cwd: str
    start_new_session: bool


def _renice(increment: int) -> None:
    """In the child, before exec: nice(increment); a refusal is ignored (the command still runs)."""
    try:
        os.nice(increment)
    except OSError:
        pass


def _additions(env: Mapping[str, str] | None, *, locale_c: bool) -> dict[str, str]:
    """The variables a call sets on top of its base environment: LC_ALL=C first, so env wins."""
    return {**(c_locale() if locale_c else {}), **(env or {})}


def _child_env(
    env: Mapping[str, str] | None, unset: Sequence[str], base_env: Mapping[str, str] | None, *, locale_c: bool
) -> dict[str, str] | None:
    """The command's whole environment; None (inherit ours) when no option changes it."""
    if env is None and not unset and base_env is None and not locale_c:
        return None
    merged = {**(os.environ if base_env is None else base_env), **_additions(env, locale_c=locale_c)}
    for name in unset:
        merged.pop(name, None)
    return merged


def _stream_kwargs(
    input_text: str | None, stdin: StdinMode, output: OutputMode, decode_errors: DecodeErrors
) -> _RunKwargs:
    """stdin, stdout, stderr and decoding."""
    if input_text is not None and stdin != "devnull":
        raise ValueError("input_text is the command's stdin: stdin must stay 'devnull'")
    kw: _RunKwargs = {}
    if input_text is not None:
        kw["input"] = input_text
    elif stdin == "devnull":
        kw["stdin"] = subprocess.DEVNULL
    if output == "capture":
        kw["capture_output"] = True
    elif output == "merge":
        kw["stdout"], kw["stderr"] = subprocess.PIPE, subprocess.STDOUT
    elif output == "stdout":
        kw["stdout"], kw["stderr"] = subprocess.PIPE, subprocess.DEVNULL
    if output != "inherit" or input_text is not None:
        kw["text"] = True
        if decode_errors == "replace":
            kw["errors"] = "replace"
    return kw


def _fault_result(
    cmd: Sequence[str], fault: CommandFault, cause: Exception, stdout: str = "", stderr: str = ""
) -> CommandResult:
    rc = {"missing": 127, "unstartable": 126, "timeout": 124}[fault]
    return CommandResult(rc, stdout, stderr or str(cause), argv=tuple(cmd), fault=fault, cause=cause)


# A caller's own words for a fault: (argv as the caller gave it, the
# result, the timeout) -> the result to return instead.
type _FaultWords = Callable[[Sequence[str], CommandResult, float | None], CommandResult]


def on_host_argv(argv: Sequence[str], settings: Mapping[str, str]) -> list[str]:
    """argv run on the host from a container (flatpak-spawn --host), with settings (--env=K=V).

    The host command does not get flatpak-spawn's own environment, only the
    settings named here; they are on flatpak-spawn's command line (ps shows
    them): never a secret.
    """
    return ["flatpak-spawn", "--host", *[f"--env={k}={v}" for k, v in settings.items()], *argv]


def can_spawn_on_host(path: str | None) -> bool:
    """flatpak-spawn is on path (None: the default PATH), as `command -v flatpak-spawn` tells."""
    return shutil.which("flatpak-spawn", path=path) is not None


class SubprocessCommandRunner:
    """CommandRunnerPort backed by subprocess.run with explicit stdin handling.

    subprocess.run gets only the arguments an option sets (see _RunKwargs).
    """

    def __init__(
        self,
        ctx: ContainerProbePort | None = None,
        *,
        base_env: Mapping[str, str] | None = None,
        words: _FaultWords | None = None,
    ) -> None:
        """ctx: asked at each on_host call (inside a container or not).
        base_env: the environment a call starts from when it names none (None:
        this process's). words: rewrites a fault's result in the caller's own
        words (before check)."""
        self._ctx = HostExecutionContext() if ctx is None else ctx
        self._base_env = base_env
        self._words = words

    def _argv(
        self, argv: Sequence[str], env: Mapping[str, str] | None, unset: Sequence[str], *, locale_c: bool, on_host: bool
    ) -> list[str]:
        """argv, behind flatpak-spawn --host when on_host inside a container. The
        host command does not get flatpak-spawn's environment: what the call sets
        goes in --env."""
        if not (on_host and self._ctx.is_container()):
            return list(argv)
        if unset:
            raise ValueError("unset cannot reach a command run on the host from a container")
        return on_host_argv(argv, _additions(env, locale_c=locale_c))

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        unset: Sequence[str] = (),
        base_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        locale_c: bool = False,
        new_session: bool = False,
        on_host: bool = False,
        stdin: StdinMode = "devnull",
        output: OutputMode = "capture",
        decode_errors: DecodeErrors = "replace",
        nice: int | None = None,
    ) -> CommandResult:
        """Run argv (see CommandRunnerPort for the options).

        A command that is missing (127), cannot start (126) or times out (124)
        gives a CommandResult with fault and cause set (CommandFailed with
        check); with decode_errors="strict", output that is not UTF-8 raises
        UnicodeDecodeError.
        """
        cmd = self._argv(argv, env, unset, locale_c=locale_c, on_host=on_host)
        kw = _stream_kwargs(input_text, stdin, output, decode_errors)
        child_env = _child_env(env, unset, self._base_env if base_env is None else base_env, locale_c=locale_c)
        if child_env is not None:
            kw["env"] = child_env
        if timeout is not None:
            kw["timeout"] = timeout
        if cwd is not None:
            kw["cwd"] = cwd
        if new_session:
            kw["start_new_session"] = True
        if nice:
            kw["preexec_fn"] = functools.partial(_renice, nice)
        res = self._spawn(cmd, kw, timeout)
        if res.fault is not None and self._words is not None:
            res = self._words(argv, res, timeout)
        if check and res.returncode != 0:
            raise CommandFailed(cmd, res.returncode, res.stdout, res.stderr)
        return res

    @staticmethod
    def _spawn(cmd: Sequence[str], kw: _RunKwargs, timeout: float | None) -> CommandResult:
        try:
            proc = subprocess.run(cmd, check=False, **kw)
        except FileNotFoundError as exc:
            # The cwd folder is missing (subprocess names it): the program may exist, it could not start there.
            fault: CommandFault = (
                "unstartable" if exc.filename is not None and exc.filename == kw.get("cwd") else "missing"
            )
            return _fault_result(cmd, fault, exc)
        except subprocess.TimeoutExpired as exc:
            msg = f"timed out after {timeout}s" if timeout is not None else "command timed out"
            return _fault_result(cmd, "timeout", exc, _decode_stream(exc.stdout), _decode_stream(exc.stderr) or msg)
        except OSError as exc:
            return _fault_result(cmd, "unstartable", exc)
        return CommandResult(proc.returncode, _decode_stream(proc.stdout), _decode_stream(proc.stderr), argv=tuple(cmd))


class HostExecutionContext:
    """ExecutionContextPort detecting host vs toolbox/container environments (and mapping a host path into this view)."""

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

    def escape_path(self, path: str, *, suffix: str) -> UnitName:
        """Escape a filesystem path into a systemd unit name via systemd-escape."""
        res = self._runner.run(["systemd-escape", "-p", f"--suffix={suffix}", path], check=True)
        return UnitName(res.stdout.strip())


class SubprocessVirshAdapter:
    """VirshPort adapter: `virsh [-r] -c URI ARGS` in the C locale, through a CommandRunnerPort.

    on_host: inside a container, virsh runs on the host (flatpak-spawn --host);
    read_only: -r, for the queries libvirt answers on a read-only connection.
    Output that is not UTF-8 is decoded as the runner does by default (U+FFFD).
    ctx only builds the default runner (the runner decides on_host): a ctx
    next to a runner is refused, since it would be ignored.
    """

    def __init__(
        self,
        uri: str = "qemu:///system",
        runner: CommandRunnerPort | None = None,
        ctx: ContainerProbePort | None = None,
        *,
        on_host: bool = True,
        read_only: bool = False,
    ) -> None:
        if runner is not None and ctx is not None:
            raise ValueError("SubprocessVirshAdapter: ctx only builds the default runner; give the runner its ctx")
        self._uri = uri
        self._runner = SubprocessCommandRunner(ctx) if runner is None else runner
        self._on_host = on_host
        self._read_only = read_only

    def call(
        self,
        *args: str,
        check: bool = False,
        timeout: float | None = 60,
        start_new_session: bool = False,
    ) -> CommandResult:
        """Run `virsh -c <uri> *args`; start_new_session: in a session of its own."""
        cmd = ["virsh", *(["-r"] if self._read_only else []), "-c", self._uri, *args]
        return self._runner.run(
            cmd,
            check=check,
            timeout=timeout,
            locale_c=True,
            new_session=start_new_session,
            on_host=self._on_host,
        )

    def domains(self, *, all_domains: bool = True) -> list[VmName]:
        """Return configured domain names, or empty list on failure."""
        args = ["list", "--all", "--name"] if all_domains else ["list", "--name"]
        res = self.call(*args, check=False)
        if res.returncode != 0:
            return []
        return [VmName(line.strip()) for line in res.stdout.splitlines() if line.strip()]

    def dumpxml(self, domain: str, *, inactive: bool = False) -> str | None:
        """Return XML definition of `domain`, or None on failure."""
        args = ["dumpxml", "--inactive", domain] if inactive else ["dumpxml", domain]
        res = self.call(*args, check=False)
        return res.stdout if res.returncode == 0 else None
