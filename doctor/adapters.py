# SPDX-License-Identifier: MPL-2.0
"""Adapters implementing the `doctor` ports against Linux/toolbox hosts."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
import hashlib
import os
import pwd
import time
import zipfile
from typing import BinaryIO, TextIO

from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.virsh import SubprocessVirshAdapter
from cst_core.adapters.files import open_regular_text
from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.zipapp import entry_payload
from cst_core.domain.install import manifest_by_src, render_template
from cst_core.domain.record import InstallRecord
from cst_core.domain.units import UNIT_PATH_PROP, show_manager_value_argv, user_unit_dir
from cst_core.ports import CommandFault, CommandResult, CommandRunnerPort, ContainerProbePort, VirshPort
from doctor.domain import NAME, VIRSH_URI, HostPath, ViewPath
from vm import adapters as vm_adapters
from vm.adapters import images as vm_images
from vm.domain import ImageDir, ImageProbe, VmSettings


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
        # The user manager's own unit folder, asked on first use (user_unit_dir).
        self._user_unit_dir: HostPath | None = None
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

    def open_regular_text(self, path: ViewPath) -> TextIO:
        """open_text's text of a regular file; OSError(EINVAL) at once for anything else (cst_core's
        open_regular_text, as cst swap reads its files)."""
        return open_regular_text(path, errors="replace", newline=None)

    def open_bytes(self, path: ViewPath) -> BinaryIO:
        """open(path, "rb")."""
        return open(path, "rb")

    def zip_text(self, path: ViewPath, member: str, limit: int) -> str:
        """zipfile's read of one member stored uncompressed (as CST's zipapps store theirs), its first limit
        bytes decoded (bad bytes replaced). Anything else raises OSError, like a file that cannot be read:
        no zip archive, no such member, a member compressed or encrypted, a damaged archive."""
        try:
            with zipfile.ZipFile(path) as zf:
                info = zf.getinfo(member)
                if info.compress_type != zipfile.ZIP_STORED:
                    raise OSError(f"{path}: {member}: compressed, CST's zipapps store their files as they are")
                with zf.open(info) as f:
                    return f.read(limit).decode("utf-8", "replace")
        # What zipfile raises on an archive it cannot read: KeyError for a missing member, ValueError for a
        # name that is not UTF-8, EOFError for one cut short, RuntimeError (NotImplementedError among them)
        # for flags it does not support or an encrypted member.
        except (zipfile.BadZipFile, KeyError, ValueError, EOFError, RuntimeError) as e:
            raise OSError(f"{path}: {member}: {e}") from e

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

    def user_unit_dir(self) -> HostPath:
        """The folder the user's manager names first for the user's own units (systemctl --user show -p
        UnitPath --value, through run(): C locale, 20 s), asked once: not the shell's XDG_CONFIG_HOME, which
        the manager does not read and sudo's step down drops. ~/.config/systemd/user when it cannot be asked."""
        if self._user_unit_dir is None:
            argv = show_manager_value_argv(["systemctl", "--user"], UNIT_PATH_PROP)
            rc, out, _ = self.run(argv, timeout=20, locale_c=True)
            self._user_unit_dir = HostPath(user_unit_dir(out if rc == 0 else None, self.home))
        return self._user_unit_dir

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
        except OSError, ValueError, SyntaxError:  # ValueError covers DomainValidationError and UnicodeDecodeError
            return None
        return hashlib.sha256(data).hexdigest()


def host_words(argv: Sequence[str], r: CommandResult, timeout: float | None) -> CommandResult:
    """The doctor's text for a query that could not run (SubprocessCommandRunner's words); no output kept."""
    spawned = r.argv[0] if r.argv else argv[0]
    reason = r.cause.strerror if isinstance(r.cause, OSError) else ""
    if r.fault == CommandFault.MISSING:
        text = f"{spawned}: not found ({reason})"
    elif r.fault == CommandFault.TIMEOUT:
        text = f"{' '.join(argv)}: no answer within {timeout} s"
    else:
        text = f"{spawned}: {reason}"
    return CommandResult(r.returncode, "", text, argv=r.argv, fault=r.fault, cause=r.cause)


class VmTools:
    """VmToolsPort over the checkout's vm package, with the vm settings of this process
    (vm.wiring.vm_settings); the functions are looked up at each call, so a test that
    patches vm.adapters or vm.adapters.images reaches them."""

    def __init__(self, settings: VmSettings) -> None:
        self.settings = settings

    @property
    def system_images(self) -> str:
        """The settings' system image folder (CST_VM_SYSTEM_IMAGES, else libvirt's)."""
        return self.settings.paths.system_images

    def knobs(self, path: str) -> dict[str, bool]:
        """vm.adapters.knobs of the vm.conf at path."""
        return vm_adapters.knobs(replace(self.settings.paths, vm_conf=path))

    def image_dirs(self) -> list[ImageDir]:
        """vm.adapters.images.image_dirs."""
        return vm_images.image_dirs(self.settings)

    def probe_dir(self, d: ImageDir, readable: Callable[[str], str]) -> ImageProbe:
        """vm.adapters.images.probe_dir."""
        return vm_images.probe_dir(d, readable)
