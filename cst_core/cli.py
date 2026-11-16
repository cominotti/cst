#!/usr/bin/python3 -I
"""Hexagonal CLI dispatcher for the top-level `cst` command."""

from collections.abc import Mapping, Sequence
import os
import pwd
import shutil
import subprocess
import sys
from typing import TextIO

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from cst_core.domain.dispatch import MODULE_BY_NAME, MODULES, format_cst_usage
from cst_core.ports import CstDispatchPort


class LocalCstDispatchAdapter:
    """Concrete `CstDispatchPort` backed by the host OS and subprocess stdio passthrough."""

    def is_file(self, path: str) -> bool:
        """Return True when `path` exists as a regular file."""
        return os.path.isfile(path)

    def is_container(self) -> bool:
        """Return True inside a Podman toolbox or Docker container."""
        return os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")

    def euid(self) -> int:
        """Return effective user ID."""
        return os.geteuid()

    def environ(self) -> Mapping[str, str]:
        """Return a snapshot of the process environment."""
        return dict(os.environ)

    def user_passwd(self, user: str) -> tuple[int, str]:
        """Return `(uid, home_dir)` for `user`."""
        entry = pwd.getpwnam(user)
        return entry.pw_uid, entry.pw_dir

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Run `argv` inheriting terminal streams and return its exit code."""
        merged = None if env is None else dict(env)
        proc = subprocess.run(list(argv), check=False, env=merged)
        return proc.returncode


class CstDispatcher:
    """Application orchestrator for `cst help|status|doctor|MODULE`."""

    def __init__(
        self,
        *,
        port: CstDispatchPort,
        repo_dir: str,
        self_path: str,
        arg0: str,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._port = port
        self._repo_dir = repo_dir
        self._self_path = self_path
        self._arg0 = arg0
        self._stdout = sys.stdout if stdout is None else stdout
        self._stderr = sys.stderr if stderr is None else stderr
        self._cst_name = self._resolve_cst_name()

    def _resolve_cst_name(self) -> str:
        env = self._port.environ()
        configured = env.get("CST_NAME", "")
        if configured:
            return configured
        on_path = shutil.which("cst", path=env.get("PATH"))
        if on_path is not None and os.path.realpath(on_path) == os.path.realpath(self._self_path):
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
            print(
                f"{self._cst_name}: the '{module_name}' module is not in this copy ({script_path} is missing)",
                file=self._stderr,
                flush=True,
            )
            return 1
        env = dict(self._port.environ())
        env["CST_PROG"] = f"{self._cst_name} {module_name}"
        return self._port.run_passthrough(["bash", script_path, *args], env=env)

    def run_doctor(self, args: Sequence[str]) -> int:
        """Run `doctor/cst-doctor` as the invoking user, stepping down from sudo if needed."""
        doctor = os.path.join(self._repo_dir, "doctor", "cst-doctor")
        if not self._port.is_file(doctor):
            print(
                f"{self._cst_name}: doctor is not in this copy ({doctor} is missing)",
                file=self._stderr,
                flush=True,
            )
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
                    doctor,
                    *args,
                ]
            )
        env["CST_PROG"] = f"{self._cst_name} doctor"
        return self._port.run_passthrough(["python3", "-I", doctor, *args], env=env)

    def status_all(self) -> int:
        """Run read-only `status` across all modules (elevating once if needed) and finish with `doctor --brief`."""
        need_root = any(
            spec.status_needs_root and self._port.is_file(os.path.join(self._repo_dir, spec.script)) for spec in MODULES
        )
        if need_root and self._port.euid() != 0 and not self._port.is_container():
            print("(some modules need root for their status: asking sudo once)", file=self._stderr, flush=True)
            return self._port.run_passthrough(
                ["sudo", "--", "env", f"CST_NAME={self._cst_name}", self._self_path, "status"]
            )

        rc = 0
        for spec in MODULES:
            script_path = os.path.join(self._repo_dir, spec.script)
            print(f"\n==== {spec.name} ({self._cst_name} {spec.name} status)", file=self._stdout, flush=True)
            if not self._port.is_file(script_path):
                print(f"skipped: not in this copy ({script_path} is missing)", file=self._stdout, flush=True)
            elif spec.status_needs_root and self._port.euid() != 0:
                print(
                    f"skipped: its status needs root on the host (run '{self._cst_name} status' on the host, not in a toolbox)",
                    file=self._stdout,
                    flush=True,
                )
            else:
                env = dict(self._port.environ())
                env["CST_PROG"] = f"{self._cst_name} {spec.name}"
                if self._port.run_passthrough(["bash", script_path, "status"], env=env) != 0:
                    rc = 1

        print(f"\n==== doctor ({self._cst_name} doctor: the full list)", file=self._stdout, flush=True)
        if self.run_doctor(["--brief"]) != 0:
            rc = 1
        return rc

    def dispatch(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments after `cst`."""
        cmd = argv[0] if argv else "help"
        rest = argv[1:]
        if cmd in ("help", "-h", "--help"):
            print(self.usage(), file=self._stdout, flush=True)
            return 0
        if cmd == "status":
            if rest:
                print(
                    f"{self._cst_name} status: no arguments (a module's own: {self._cst_name} MODULE status)",
                    file=self._stderr,
                    flush=True,
                )
                return 2
            return self.status_all()
        if cmd == "doctor":
            return self.run_doctor(rest)
        if cmd in MODULE_BY_NAME:
            return self.run_module(cmd, rest)

        print(f"{self._cst_name}: unknown command or module '{cmd}'", file=self._stderr, flush=True)
        print(self.usage(), file=self._stderr, flush=True)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `cst`."""
    repo_dir = os.environ.get("CST_DIR") or _REPO_ROOT
    self_path = os.environ.get("CST_SELF") or os.path.join(repo_dir, "cst")
    arg0 = os.environ.get("CST_ARG0") or (sys.argv[0] if sys.argv else "cst")
    dispatcher = CstDispatcher(
        port=LocalCstDispatchAdapter(),
        repo_dir=repo_dir,
        self_path=self_path,
        arg0=arg0,
    )
    return dispatcher.dispatch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
