# SPDX-License-Identifier: MPL-2.0
"""Adapters implementing the `doctor` ports against Linux/toolbox hosts."""

from collections.abc import Callable, Mapping, Sequence
import hashlib
import os
import pwd
import time
from typing import BinaryIO, TextIO

from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.virsh import SubprocessVirshAdapter
from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.zipapp import entry_payload
from cst_core.domain.install import manifest_by_src, render_template
from cst_core.domain.record import InstallRecord
from cst_core.domain.values import DomainValidationError
from cst_core.ports import CommandResult, CommandRunnerPort, ContainerProbePort, VirshPort
from doctor.domain import NAME, VIRSH_URI, HostPath, ViewPath
from vm import adapters as vm_adapters
from vm.domain import ImageDir, ImageProbe


class Host:
    """How to reach the host: directly, or from a toolbox (flatpak-spawn
    --host for commands, /run/host for files). Tests set CST_DOCTOR_CONTAINER
    (1 or 0), CST_DOCTOR_ROOT (the host's / for files) and CST_DOCTOR_NOW.
    Without CST_DOCTOR_CONTAINER, probe tells (None: HostExecutionContext,
    cst_core's one host-or-toolbox detection)."""

    def __init__(
        self, env: Mapping[str, str] | None = None, repo: str = "", *, probe: ContainerProbePort | None = None
    ) -> None:
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
            self.container = (HostExecutionContext() if probe is None else probe).is_container()
        self.root = env.get("CST_DOCTOR_ROOT") or ("/run/host" if self.container else "/")
        self.now = float(env.get("CST_DOCTOR_NOW") or time.time())
        self.uid = os.getuid()
        self.arch = os.uname().machine
        self.state_home = ViewPath(
            env.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        )
        # The user's home on the host (the same path in a toolbox).
        self.home = HostPath(env.get("HOME") or _home(self.uid))
        # Every command (virsh too) starts from self.env, in doctor's words for a fault.
        self._commands: CommandRunnerPort = SubprocessCommandRunner(self, base_env=env, words=host_words)

    def path(self, p: str) -> ViewPath:
        """A host path, as this process can read it."""
        return ViewPath(os.path.join(self.root, p.lstrip("/")))

    def repo_path(self, rel: str) -> ViewPath:
        """The checkout's file rel (a path that does not exist when there is no checkout)."""
        return ViewPath(os.path.join(self.repo, rel) if self.repo else os.path.join("/nonexistent-cst-checkout", rel))

    def open_text(self, path: ViewPath) -> TextIO:
        """open(path) as UTF-8, undecodable bytes replaced."""
        return open(path, encoding="utf-8", errors="replace")

    def open_bytes(self, path: ViewPath) -> BinaryIO:
        """open(path, "rb")."""
        return open(path, "rb")

    def listdir(self, path: ViewPath) -> list[str]:
        """os.listdir."""
        return os.listdir(path)

    def exists(self, path: ViewPath) -> bool:
        """os.path.exists."""
        return os.path.exists(path)

    def isdir(self, path: ViewPath) -> bool:
        """os.path.isdir."""
        return os.path.isdir(path)

    def isfile(self, path: ViewPath) -> bool:
        """os.path.isfile."""
        return os.path.isfile(path)

    def islink(self, path: ViewPath) -> bool:
        """os.path.islink."""
        return os.path.islink(path)

    def stat(self, path: ViewPath) -> os.stat_result:
        """os.stat."""
        return os.stat(path)

    def lstat(self, path: ViewPath) -> os.stat_result:
        """os.lstat."""
        return os.lstat(path)

    def realpath(self, path: ViewPath) -> ViewPath:
        """os.path.realpath."""
        return ViewPath(os.path.realpath(path))

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

    def is_container(self) -> bool:
        """Inside a toolbox (CST_DOCTOR_CONTAINER, or detected): commands then run on the host."""
        return self.container

    def run(self, cmd: list[str], timeout: float = 60, *, locale_c: bool = False) -> tuple[int, str, str]:
        """(rc, stdout, stderr) of a read-only query command on the host;
        rc 127 when it cannot be started, 124 on a timeout; bytes that are not
        UTF-8 become U+FFFD. locale_c: LC_ALL=C
        on top of the Host's environment; in a toolbox it goes through
        flatpak-spawn --env (the host command does not get flatpak-spawn's own)."""
        r = self._commands.run(cmd, timeout=timeout, locale_c=locale_c, on_host=True)
        return r.returncode, r.stdout, r.stderr

    def virsh(self, *, read_only: bool) -> VirshPort:
        """virsh on the system connection, through the same commands as run()."""
        return SubprocessVirshAdapter(VIRSH_URI, self._commands, on_host=True, read_only=read_only)


def _home(uid: int) -> str:
    """uid's home folder from the password database ("/" for an unknown uid)."""
    try:
        return pwd.getpwuid(uid).pw_dir
    except KeyError:
        return "/"


class InstalledFiles:
    """InstalledFilesPort: the install records (read as JSON), files hashed as they are, and what the
    checkout at repo would install (built once per source and values; "" = no checkout)."""

    def __init__(self, repo: str) -> None:
        self._repo = repo
        self._desired: dict[tuple[str, tuple[tuple[str, str], ...]], str | None] = {}

    def record(self, path: ViewPath) -> InstallRecord | None:
        """JsonInstallRecord.read."""
        return JsonInstallRecord().read(path)

    def file_sha256(self, path: ViewPath) -> str:
        """sha256 of the file's bytes (OSError when it cannot be read)."""
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def desired_sha256(self, src: str, values: Mapping[str, str] | None) -> str | None:
        """sha256 of the installer's bytes for src from this checkout (a template rendered with values)."""
        key = (src, tuple(sorted((values or {}).items())))
        if key not in self._desired:
            self._desired[key] = self._build(src, values)
        return self._desired[key]

    def _build(self, src: str, values: Mapping[str, str] | None) -> str | None:
        entry = manifest_by_src().get(src)
        if not self._repo or entry is None or (entry.is_template and values is None):
            return None
        try:
            data = entry_payload(self._repo, entry)
            if entry.is_template:
                data = render_template(data.decode("utf-8"), values or {}).encode("utf-8")
        except OSError, ValueError, SyntaxError, DomainValidationError:
            return None
        return hashlib.sha256(data).hexdigest()


def host_words(argv: Sequence[str], r: CommandResult, timeout: float | None) -> CommandResult:
    """The doctor's text for a query that could not run (SubprocessCommandRunner's words); no output kept."""
    spawned = r.argv[0] if r.argv else argv[0]
    reason = r.cause.strerror if isinstance(r.cause, OSError) else ""
    if r.fault == "missing":
        text = f"{spawned}: not found ({reason})"
    elif r.fault == "timeout":
        text = f"{' '.join(argv)}: no answer within {timeout} s"
    else:
        text = f"{spawned}: {reason}"
    return CommandResult(r.returncode, "", text, argv=r.argv, fault=r.fault, cause=r.cause)


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

    def image_dirs(self) -> list[ImageDir]:
        """vm.adapters.image_dirs."""
        return vm_adapters.image_dirs()

    def probe_dir(self, d: ImageDir, readable: Callable[[str], str]) -> ImageProbe:
        """vm.adapters.probe_dir."""
        return vm_adapters.probe_dir(d, readable)
