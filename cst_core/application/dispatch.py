# SPDX-License-Identifier: MPL-2.0
"""The `cst` dispatcher's use case: help, status across modules, doctor, and handing over to a module.

Its host side is CstDispatchPort (cst_core/ports), implemented by
cst_core/adapters/dispatch.py; cst_core/cli.py wires the two.
"""

from collections.abc import Sequence
import os.path
from typing import TextIO

from cst_core.domain.dispatch import MODULE_BY_NAME, MODULES, format_cst_usage
from cst_core.ports import CstDispatchPort


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
            return self._port.run_passthrough(
                [
                    "runuser",
                    "-u",
                    user,
                    "--",
                    "env",
                    "-u",
                    "XDG_STATE_HOME",
                    "-u",
                    "SUDO_USER",
                    f"HOME={home}",
                    f"USER={user}",
                    f"LOGNAME={user}",
                    f"XDG_RUNTIME_DIR=/run/user/{uid}",
                    f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
                    f"CST_PROG={self._cst_name} doctor",
                    "python3",
                    "-I",
                    "-B",
                    doctor,
                    *args,
                ]
            )
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
        if cmd in ("help", "-h", "--help"):
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
