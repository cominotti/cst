"""Adapters implementing the `doctor` ports against Linux/toolbox hosts."""

from collections.abc import Callable, Mapping
import os
import pwd
import subprocess
import time
from typing import Any, BinaryIO, TextIO

from doctor.domain import NAME, Deployment, OstreeAnswer
from vm import adapters as vm_adapters


class Host:
    """How to reach the host: directly, or from a toolbox (flatpak-spawn
    --host for commands, /run/host for files). Tests set CST_DOCTOR_CONTAINER
    (1 or 0), CST_DOCTOR_ROOT (the host's / for files) and CST_DOCTOR_NOW."""

    _ostree: OstreeAnswer
    _ostree_list: list[Deployment]
    _domains: tuple[list[str] | None, str]
    _dumpxml: dict[str, tuple[int, str, str]]

    def __init__(self, env: Mapping[str, str] | None = None, repo: str = "") -> None:
        env = os.environ if env is None else env
        # The commands run with this environment (its PATH finds them), not
        # with the process's: a test's stub folder reaches every command.
        self.env = env
        # The checkout the doctor runs from ("" = none: repo_path then names nothing that exists).
        self.repo = repo
        prog = env.get("CST_PROG") or NAME
        # The cst command, for hints: "cst" from "cst doctor".
        self.cst = prog[: -len(" doctor")] if prog.endswith(" doctor") else "cst"
        forced = env.get("CST_DOCTOR_CONTAINER")
        if forced in ("0", "1"):
            self.container = forced == "1"
        else:
            self.container = os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")
        self.root = env.get("CST_DOCTOR_ROOT") or ("/run/host" if self.container else "/")
        self.now = float(env.get("CST_DOCTOR_NOW") or time.time())
        self.uid = os.getuid()
        self.arch = os.uname().machine
        self.state_home = env.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")

    def path(self, p: str) -> str:
        """A host path, as this process can read it."""
        return os.path.join(self.root, p.lstrip("/"))

    def repo_path(self, rel: str) -> str:
        """The checkout's file rel (a path that does not exist when there is no checkout)."""
        return os.path.join(self.repo, rel) if self.repo else os.path.join("/nonexistent-cst-checkout", rel)

    def open_text(self, path: str) -> TextIO:
        """open(path) as UTF-8, undecodable bytes replaced."""
        return open(path, encoding="utf-8", errors="replace")

    def open_bytes(self, path: str) -> BinaryIO:
        """open(path, "rb")."""
        return open(path, "rb")

    def listdir(self, path: str) -> list[str]:
        """os.listdir."""
        return os.listdir(path)

    def exists(self, path: str) -> bool:
        """os.path.exists."""
        return os.path.exists(path)

    def isdir(self, path: str) -> bool:
        """os.path.isdir."""
        return os.path.isdir(path)

    def isfile(self, path: str) -> bool:
        """os.path.isfile."""
        return os.path.isfile(path)

    def islink(self, path: str) -> bool:
        """os.path.islink."""
        return os.path.islink(path)

    def stat(self, path: str) -> os.stat_result:
        """os.stat."""
        return os.stat(path)

    def lstat(self, path: str) -> os.stat_result:
        """os.lstat."""
        return os.lstat(path)

    def realpath(self, path: str) -> str:
        """os.path.realpath."""
        return os.path.realpath(path)

    def user_name(self, uid: int) -> str:
        """pwd.getpwuid(uid).pw_name, or "" for an unknown uid."""
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return ""

    def user_id(self, name: str) -> int | None:
        """pwd.getpwnam(name).pw_uid, or None for an unknown name."""
        try:
            return pwd.getpwnam(name).pw_uid
        except KeyError:
            return None

    def run(self, cmd: list[str], timeout: float = 60) -> tuple[int, str, str]:
        """(rc, stdout, stderr) of a read-only query command on the host;
        rc 127 when it cannot be started, 124 on a timeout."""
        full = (["flatpak-spawn", "--host"] + cmd) if self.container else cmd
        try:
            p = subprocess.run(
                full,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env=dict(self.env),
            )
        except FileNotFoundError as e:
            return 127, "", f"{full[0]}: not found ({e.strerror})"
        except subprocess.TimeoutExpired:
            return 124, "", f"{' '.join(cmd)}: no answer within {timeout} s"
        except OSError as e:
            return 126, "", f"{full[0]}: {e.strerror}"
        return p.returncode, p.stdout, p.stderr


class VmTools:
    """VmToolsPort over the checkout's vm package (looked up at each call, so
    a test that patches vm.adapters reaches it)."""

    @property
    def system_images(self) -> str:
        """vm.adapters.SYSTEM_IMAGES."""
        return vm_adapters.SYSTEM_IMAGES

    def knobs(self, path: str) -> dict[str, bool]:
        """vm.adapters.knobs."""
        return vm_adapters.knobs(path)

    def image_dirs(self) -> list[dict[str, str]]:
        """vm.adapters.image_dirs."""
        return vm_adapters.image_dirs()

    def probe_dir(self, d: Mapping[str, Any], readable: Callable[[str], str]) -> dict[str, Any]:
        """vm.adapters.probe_dir."""
        return vm_adapters.probe_dir(d, readable)
