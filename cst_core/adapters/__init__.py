# SPDX-License-Identifier: MPL-2.0
"""Concrete infrastructure adapters implementing cst_core ports (the runner: cst_core.adapters.runner; virsh: cst_core.adapters.virsh; files: cst_core.adapters.files; zipapps: cst_core.adapters.zipapp)."""

from collections.abc import Mapping, Sequence
import time
from typing import TypedDict, Unpack

from cst_core.domain.units import parse_show_props, parse_show_units, show_manager_value_argv, show_units_argv
from cst_core.ports import CommandResult, CommandRunnerPort, OutputMode, StdinMode, SystemctlHost, UnitName


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


class _ScopeOptions(TypedDict, total=False):
    """The runner options of an adapter's scope: only those it sets, so an adapter built without them
    calls the runner as it always did."""

    on_host: bool
    base_env: Mapping[str, str]
    stdin: StdinMode
    output: OutputMode


class _SystemctlScope(TypedDict, total=False):
    """Which manager an adapter reaches and how (see SystemctlAdapter): one keyword bag, so that the
    constructor's own arguments stay few."""

    user: bool
    program: str | None
    on_host: SystemctlHost
    base_env: Mapping[str, str] | None
    stdin: StdinMode


class SystemctlAdapter:
    """SystemdPort implementation delegating to systemctl and systemd-escape.

    timeout and locale_c apply to every systemctl call (show: 20 s unless a timeout is given, and
    always the C locale); by default none (no limit, the caller's locale). A caller that keeps its own words for a systemctl that is
    missing or does not answer gives the runner its words (SubprocessCommandRunner(words=...)).

    The scope, every option off by default but on_host: user (`systemctl --user`: the user manager);
    program (a stub run instead of systemctl, never on the host); on_host (a SystemctlHost: which calls
    go to the host's systemctl from a toolbox, through the runner's on_host: none (LOCAL), show only
    (SHOW_ONLY, the default) or every call (EVERY); systemd-escape never does); base_env and stdin
    (passed to the runner on every systemctl call). The CommandResult calls also take a per-call
    `output` (OutputMode; CAPTURE by default)."""

    def __init__(
        self,
        runner: CommandRunnerPort,
        *,
        timeout: float | None = None,
        locale_c: bool = False,
        **scope: Unpack[_SystemctlScope],
    ) -> None:
        self._runner = runner
        self._timeout = timeout
        self._locale_c = locale_c
        program, user = scope.get("program"), scope.get("user", False)
        self._head = [program or "systemctl", *(["--user"] if user else [])]
        self._stub = program is not None
        # A stub never runs on the host. show always names on_host to the runner; the other calls name
        # it only when every call goes to the host.
        policy = SystemctlHost.LOCAL if self._stub else scope.get("on_host", SystemctlHost.SHOW_ONLY)
        self._show_on_host = policy != SystemctlHost.LOCAL
        self._scope = _ScopeOptions()
        if policy == SystemctlHost.EVERY:
            self._scope["on_host"] = True
        if (base_env := scope.get("base_env")) is not None:
            self._scope["base_env"] = base_env
        if (stdin := scope.get("stdin", "devnull")) != "devnull":
            self._scope["stdin"] = stdin

    def _systemctl(self, *args: str, check: bool = False, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        opts = _ScopeOptions(self._scope)
        if output != OutputMode.CAPTURE:
            opts["output"] = output
        return self._runner.run(
            [*self._head, *args], check=check, timeout=self._timeout, locale_c=self._locale_c, **opts
        )

    def daemon_reload(self) -> None:
        """Run `systemctl daemon-reload` (CommandFailed when it fails)."""
        self._systemctl("daemon-reload", check=True)

    def daemon_reload_result(self, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """`systemctl daemon-reload`: its exit status and output (a fault with its cause), never raised."""
        return self._systemctl("daemon-reload", output=output)

    def is_active(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """`systemctl is-active unit`: the state on stdout; exit 3 for a unit that is not active, 4 for one
        the manager does not know."""
        return self._systemctl("is-active", unit, output=output)

    def is_enabled(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """`systemctl is-enabled unit`: its exit status and output."""
        return self._systemctl("is-enabled", unit, output=output)

    def show_environment(self, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """`systemctl show-environment`: the manager's environment, one KEY=VALUE per line."""
        return self._systemctl("show-environment", output=output)

    def show_value(self, unit: str | None, prop: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """`systemctl show -p PROP --value unit`, with the adapter's own timeout and locale (show() has the
        C locale; a value shown to the user, such as a timestamp, keeps the caller's). unit None: a
        property of the manager itself (show_manager_value_argv, such as UnitPath), asked as show asks
        (on the host from a toolbox unless on_host is LOCAL, the C locale, 20 s at most or the adapter's
        timeout): CST reads it, and nobody sees it."""
        if unit is None:
            return self._show(show_manager_value_argv(self._head, prop), output=output)
        return self._systemctl("show", "-p", prop, "--value", unit, output=output)

    def start(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """Start unit; its exit status and output (the caller shows systemctl's message)."""
        return self._systemctl("start", unit, output=output)

    def stop(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """Stop unit; its exit status and output."""
        return self._systemctl("stop", unit, output=output)

    def restart(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """Restart unit; its exit status and output."""
        return self._systemctl("restart", unit, output=output)

    def enable(self, unit: str, *, now: bool = False, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """Enable unit (and start it, with now); its exit status and output."""
        return self._systemctl("enable", *(["--now"] if now else []), unit, output=output)

    def disable(self, unit: str, *, now: bool = False, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """Disable unit (and stop it, with now); its exit status and output."""
        return self._systemctl("disable", *(["--now"] if now else []), unit, output=output)

    def clean(self, unit: str, *, what: str, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """Remove what systemd keeps for unit (what: state, cache, logs, ...); its exit status and output."""
        return self._systemctl("clean", f"--what={what}", unit, output=output)

    def _show(self, argv: list[str], *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        """A `systemctl show` argv as show and show_units run it: on_host as the adapter's policy says for
        show, the C locale, 20 s at most or the adapter's timeout."""
        timeout = 20 if self._timeout is None else self._timeout
        scope = _ScopeOptions(self._scope, on_host=self._show_on_host)
        if output != OutputMode.CAPTURE:
            scope["output"] = output
        return self._runner.run(argv, check=False, timeout=timeout, locale_c=True, **scope)

    def show_units(self, pattern: str, props: Sequence[str]) -> list[dict[str, str]] | None:
        """`systemctl show --state=loaded -p PROP... PATTERN` (show_units_argv): one {prop: value} per unit
        the manager has loaded (active or not: without --state=loaded a glob hides the inactive units with no
        job queued) whose name matches the glob (a glob loads no unit on demand, as a unit name or a path
        would; ask for `Id` to know which unit each is), in systemctl's order; [] when it lists none; None
        when systemctl fails or cannot start (no answer). As show: on the host from a toolbox unless the
        adapter's on_host is LOCAL, the C locale, 20 s at most or the adapter's timeout."""
        res = self._show(show_units_argv(self._head, pattern, props))
        if res.returncode != 0:
            return None
        return parse_show_units(res.stdout)

    def escape_path(self, path: str, *, suffix: str) -> UnitName:
        """Escape a filesystem path into a systemd unit name via systemd-escape."""
        res = self._runner.run(["systemd-escape", "-p", f"--suffix={suffix}", path], check=True)
        return UnitName(res.stdout.strip())

    def show(self, unit: UnitName, props: Sequence[str], *, user: bool = False) -> dict[str, str]:
        """`systemctl show -p PROP... unit` as {prop: value}, on the host from a toolbox unless the
        adapter's on_host is LOCAL (read-only, so also from plan and status there); user: the unit of the
        user's own manager (`systemctl --user`; an adapter whose scope is user already has it); {} when
        systemctl fails or cannot start. 20 s at most, or the adapter's timeout when it has one."""
        head = [*self._head, "--user"] if user and "--user" not in self._head else self._head
        res = self._show([*head, "show", *(a for p in props for a in ("-p", p)), unit])
        return parse_show_props(res.stdout) if res.returncode == 0 else {}
