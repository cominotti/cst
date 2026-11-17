# SPDX-License-Identifier: MPL-2.0
"""The `cst` dispatcher's use case (help, status across modules, doctor, handing over to a
module), and the router of a shim module's command (ModuleRouter).

Its host side is CstDispatchPort (cst_core/ports), implemented by
cst_core/adapters/dispatch.py; cst_core/cli.py wires the two.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import os.path
from typing import TextIO

from cst_core.domain.dispatch import (
    MODULE_BY_NAME,
    MODULES,
    HELP_WORDS,
    ModuleRouting,
    Routed,
    format_cst_usage,
    route_command,
    script_help,
    step_down_argv,
)
from cst_core.ports import CstDispatchPort, ModuleRoutePort


def _say(stream: TextIO, text: str) -> None:
    """One line on stream, flushed at once (print(..., flush=True))."""
    stream.write(f"{text}\n")
    stream.flush()


class CstDispatcher:
    """Application orchestrator for `cst help|status|doctor|MODULE`."""

    def __init__(
        self,
        *,
        port: CstDispatchPort,
        repo_dir: str,
        self_path: str,
        arg0: str,
        stdout: TextIO,
        stderr: TextIO,
    ) -> None:
        self._port, self._stdout, self._stderr = port, stdout, stderr
        self._repo_dir, self._self_path, self._arg0 = repo_dir, self_path, arg0
        self._cst_name = self._resolve_cst_name()

    def _resolve_cst_name(self) -> str:
        env = self._port.environ()
        configured = env.get("CST_NAME", "")
        if configured:
            return configured
        on_path = self._port.which("cst", env.get("PATH"))
        if on_path is not None and self._port.realpath(on_path) == self._port.realpath(self._self_path):
            return "cst"
        return self._arg0

    def usage(self) -> str:
        """Render help text showing which modules exist in this checkout."""
        present = {spec.name: self._port.is_file(os.path.join(self._repo_dir, spec.script)) for spec in MODULES}
        return format_cst_usage(present)

    def run_module(self, module_name: str, args: Sequence[str]) -> int:
        """Execute a module entrypoint with `CST_PROG` set."""
        spec = MODULE_BY_NAME[module_name]
        script_path = os.path.join(self._repo_dir, spec.script)
        if not self._port.is_file(script_path):
            _say(
                self._stderr,
                f"{self._cst_name}: the '{module_name}' module is not in this copy ({script_path} is missing)",
            )
            return 1
        env = dict(self._port.environ())
        env["CST_PROG"] = f"{self._cst_name} {module_name}"
        return self._port.exec_replace(["bash", script_path, *args], env=env)

    def run_doctor(self, args: Sequence[str]) -> int:
        """Run `doctor/cst-doctor` as the invoking user, stepping down from sudo if needed."""
        doctor = os.path.join(self._repo_dir, "doctor", "cst-doctor")
        if not self._port.is_file(doctor):
            _say(self._stderr, f"{self._cst_name}: doctor is not in this copy ({doctor} is missing)")
            return 1
        env = dict(self._port.environ())
        user = env.get("SUDO_USER", "")
        if self._port.euid() == 0 and user and user != "root":
            uid, home = self._port.user_passwd(user)
            then = [f"CST_PROG={self._cst_name} doctor", "python3", "-I", "-B", doctor, *args]
            return self._port.run_passthrough(step_down_argv(user, uid, home, unset=("XDG_STATE_HOME",), then=then))
        env["CST_PROG"] = f"{self._cst_name} doctor"
        return self._port.run_passthrough(["python3", "-I", "-B", doctor, *args], env=env)

    def status_all(self) -> int:
        """Run read-only `status` across all modules (elevating once if needed) and finish with `doctor --brief`."""
        need_root = any(
            spec.status_needs_root and self._port.is_file(os.path.join(self._repo_dir, spec.script)) for spec in MODULES
        )
        if need_root and self._port.euid() != 0 and not self._port.is_container():
            _say(self._stderr, "(some modules need root for their status: asking sudo once)")
            return self._port.exec_replace(
                ["sudo", "--", "env", f"CST_NAME={self._cst_name}", self._self_path, "status"]
            )

        rc = 0
        for spec in MODULES:
            script_path = os.path.join(self._repo_dir, spec.script)
            _say(self._stdout, f"\n==== {spec.name} ({self._cst_name} {spec.name} status)")
            if not self._port.is_file(script_path):
                _say(self._stdout, f"skipped: not in this copy ({script_path} is missing)")
            elif spec.status_needs_root and self._port.euid() != 0:
                _say(
                    self._stdout,
                    f"skipped: its status needs root on the host (run '{self._cst_name} status' on the host, not in a toolbox)",
                )
            else:
                env = dict(self._port.environ())
                env["CST_PROG"] = f"{self._cst_name} {spec.name}"
                if self._port.run_passthrough(["bash", script_path, "status"], env=env) != 0:
                    rc = 1

        _say(self._stdout, f"\n==== doctor ({self._cst_name} doctor: the full list)")
        if self.run_doctor(["--brief"]) != 0:
            rc = 1
        return rc

    def dispatch(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments after `cst`."""
        cmd = argv[0] if argv and argv[0] else "help"  # bash's ${1:-help}: an empty word is help too
        rest = argv[1:]
        if cmd in HELP_WORDS:
            _say(self._stdout, self.usage())
            return 0
        if cmd == "status":
            if rest:
                _say(
                    self._stderr,
                    f"{self._cst_name} status: no arguments (a module's own: {self._cst_name} MODULE status)",
                )
                return 2
            return self.status_all()
        if cmd == "doctor":
            return self.run_doctor(rest)
        if cmd in MODULE_BY_NAME:
            return self.run_module(cmd, rest)

        _say(self._stderr, f"{self._cst_name}: unknown command or module '{cmd}'")
        _say(self._stderr, self.usage())
        return 2


@dataclass(frozen=True, slots=True)
class RouteSettings:
    """What a shim hands over and what the router reads from the environment once.

    script: the shim (its real path), arg0: the name it was started with
    ($0), cst_prog: CST_PROG as the caller set it ("" when unset or empty),
    container: the module's override ("1", "0", or "" to detect),
    sudo_user: SUDO_USER ("" when unset).
    """

    script: str
    arg0: str
    cst_prog: str
    container: str
    sudo_user: str


class ModuleRouter:
    """Run a shim module's command (`cst vm ...`, `cst toolchains ...`) the way its setup script did.

    The command line and the privilege of each command come from the
    module's routing table (cst_core.domain.dispatch.ROUTING). The end is
    always an exec, as in bash: the program (python3 -I -B PROGRAM ARGS),
    flatpak-spawn (a read command in a toolbox), sudo (the script again,
    as root) or runuser (the script again, as SUDO_USER).
    """

    def __init__(self, *, port: ModuleRoutePort, routing: ModuleRouting, settings: RouteSettings) -> None:
        self._port, self._routing, self._s = port, routing, settings
        self._prog = settings.cst_prog or settings.arg0  # ${CST_PROG:-$0}

    def _help(self) -> str:
        return script_help(self._port.read_text(self._s.script), self._prog)

    def _program(self, cmd: str, rest: Sequence[str]) -> list[str]:
        tool = os.path.join(os.path.dirname(self._s.script), self._routing.program)
        return ["python3", "-I", "-B", tool, cmd, *rest]

    def _run_program(self, cmd: str, rest: Sequence[str]) -> int:
        """exec the program here, with CST_PROG naming the command in its help and hints."""
        env = dict(self._port.environ())
        env["CST_PROG"] = self._prog
        return self._port.exec_replace(self._program(cmd, rest), env=env)

    def _in_container(self) -> bool:
        if self._s.container in ("0", "1"):
            return self._s.container == "1"
        return self._port.is_container()

    def route(self, argv: Sequence[str]) -> int:
        """Route argv (the command and its arguments): help, a usage error, or an exec."""
        cmd = argv[0] if argv and argv[0] else "help"  # ${1:-help}: an empty word is help too
        rest = list(argv[1:])
        return self._follow(cmd, rest, route_command(self._routing, cmd, rest, self._prog))

    def _follow(self, cmd: str, rest: Sequence[str], routed: Routed) -> int:
        if routed.action == "help":
            self._port.say("out", self._help())
            return 0
        if routed.action == "unknown":
            self._port.say("err", f"{routed.message}\n{self._help()}")
            return 2
        if routed.action == "usage":
            self._port.say("err", f"{routed.message}\n")
            return 2
        if routed.privilege == "as-user":
            return self._as_user(cmd, rest)
        if routed.privilege == "read":
            return self._read(cmd, rest)
        if self._in_container():
            words = " ".join([cmd, *rest])
            self._port.say(
                "err",
                f"'{self._prog} {words}' must run on the host, not inside a toolbox/container.\n"
                f"In a host terminal: {self._prog} {words}\n",
            )
            return 1
        if routed.privilege == "root" and self._port.euid() != 0:
            # sudo resets the environment: CST_PROG is handed over explicitly (when the caller set it).
            handed = ["env", f"CST_PROG={self._s.cst_prog}"] if self._s.cst_prog else []
            return self._port.exec_replace(["sudo", "--", *handed, self._s.script, cmd, *rest])
        return self._run_program(cmd, rest)

    def _read(self, cmd: str, rest: Sequence[str]) -> int:
        """Read-only, as the caller; from a toolbox on the host (flatpak-spawn), or here after a note."""
        if self._in_container():
            rc = self._port.exec_on_host(self._program(cmd, rest), settings={"CST_PROG": self._prog})
            if rc is not None:
                return rc
            # A container without a way to the host (CI, plain podman/docker): answer from here.
            self._port.say("out", "(cannot reach the host from this container: showing what is visible here)\n")
        return self._run_program(cmd, rest)

    def _as_user(self, cmd: str, rest: Sequence[str]) -> int:
        """The caller's own files: as root, the script runs again as SUDO_USER (runuser)."""
        if self._port.euid() != 0:
            return self._run_program(cmd, rest)
        user = self._s.sudo_user
        if not user or user == "root":
            self._port.say(
                "err",
                f"{self._prog}: ERROR: run it from your user account (needs SUDO_USER): {self._routing.user_reason}\n",
            )
            return 1
        try:
            uid, home = self._port.user_passwd(user)
        except KeyError:
            self._port.say("err", f"{self._prog}: ERROR: SUDO_USER={user}: no such user\n")
            return 1
        handed = [f"CST_PROG={self._s.cst_prog}"] if self._s.cst_prog else []
        then = [*handed, self._s.script, cmd, *rest]
        return self._port.exec_replace(step_down_argv(user, uid, home, unset=self._routing.user_unset, then=then))
