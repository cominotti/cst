# SPDX-License-Identifier: MPL-2.0
"""The vm module's reach into the host: virsh, rpm, rpm-ostree and restic (vm.adapters.restic), and
the settings files (systemctl: cst_core's SystemctlAdapter, with fault_words). vm.adapters.backup has the backups' files
and the run of a backup, vm.adapters.timer the weekly timer's files,
vm.adapters.images the VM image folders.

Where they read and write comes from the settings (vm.wiring.vm_settings).
"""

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
import os
import socket
import sys

from cpu.domain import CPU_DISABLE_DEFAULT
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.virsh import SubprocessVirshAdapter, define_from_text
from cst_core.adapters.files import (
    read_text,
)
from cst_core.adapters.runtime import restore_default_signals
from cst_core.domain.contracts import NAS_SSH_ALIAS, StateFormatKey, state_format
from cst_core.domain.errors import ExitStatus, Refused
from cst_core.domain.install import manifest_by_src
from cst_core.domain.ostree import query_status
from cst_core.domain.values import MINUTE, parse_whole_number
from cst_core.ports import CommandFault, CommandResult, CommandRunnerPort, OutputMode, VirshPort
from cst_core.domain.text import (
    NO_WORDS,
    ShellAssignments,
    describe_problems,
    first_line,
    parse_shell_assignments,
    parse_snapshots_conf,
    strip_ascii_blanks,
)
from vm.domain import (
    VmCommand,
    VmSub,
    NasRepo,
    VmSettings,
    VmPaths,
    DEFAULT_VM,
    KNOBS,
    NAS_DEFAULTS,
    NOT_INSTALLED,
    URI,
    VIRT_PACKAGES,
)
from vm.domain.backup import BootedDeployment


NAME = "cst-vm"

INDEX_FORMAT = state_format(StateFormatKey.VM_INDEX)
TORN_FORMAT = state_format(StateFormatKey.VM_NAS_TORN)

# The component of the files backup-timer on installs (the install record lists them under it).
TIMER_COMPONENT = manifest_by_src()["vm/cst-vm"].component


def fault_words(argv: Sequence[str], r: CommandResult, timeout: float | None) -> CommandResult:
    """r with cst-vm's own words for a command that is missing (127) or does not answer (124);
    one that cannot start raises (as subprocess.run did). The runner's words= for systemctl."""
    if r.fault == CommandFault.MISSING:
        return CommandResult(
            ExitStatus.NOT_FOUND, "", f"{argv[0]}: not found", argv=r.argv, fault=r.fault, cause=r.cause
        )
    if r.fault == CommandFault.TIMEOUT:
        return CommandResult(
            ExitStatus.TIMED_OUT,
            "",
            f"{argv[0]}: no answer within {timeout} s",
            argv=r.argv,
            fault=r.fault,
            cause=r.cause,
        )
    if r.cause is not None:
        raise r.cause
    return r


def _triple(r: CommandResult, name: str, timeout: float) -> tuple[int, str, str]:
    """(rc, stdout, stderr) of r in cst-vm's own words (fault_words)."""
    w = fault_words([name], r, timeout)
    return w.returncode, w.stdout, w.stderr


class VmCommands:
    """The commands cst-vm runs (where it runs: not moved to the host from a toolbox), and virsh
    over the same runner, in the C locale (virsh translates its answers: "shut off" is
    "desligado" in pt_BR). Bytes of their output that are not UTF-8 become U+FFFD.

    detached(): while it lasts (the merge back of a NAS backup), every child runs in its own
    session, so a Ctrl-C or a closed terminal cannot reach it (virsh cancels a
    `blockcommit --wait` on SIGINT). A call's own detach= wins."""

    def __init__(self, runner: CommandRunnerPort) -> None:
        self.runner = runner
        self.virsh_adapter: VirshPort = SubprocessVirshAdapter(URI, runner, on_host=False)
        self._detached = False

    @contextmanager
    def detached(self) -> Generator[None]:
        before, self._detached = self._detached, True
        try:
            yield
        finally:
            self._detached = before

    def run(
        self, cmd: Sequence[str], timeout: int, extra_env: Mapping[str, str] | None, *, detach: bool | None
    ) -> tuple[int, str, str]:
        r = self.runner.run(
            cmd,
            timeout=timeout,
            env=extra_env,
            locale_c=True,
            new_session=self._detached if detach is None else detach,
        )
        return _triple(r, cmd[0], timeout)

    def on_terminal(self, cmd: Sequence[str]) -> int:
        r = self.runner.run(cmd, locale_c=True, stdin="inherit", output=OutputMode.INHERIT)
        if r.cause is not None:
            raise r.cause
        return r.returncode

    def virsh(self, args: Sequence[str], timeout: int, *, detach: bool | None) -> tuple[int, str, str]:
        r = self.virsh_adapter.call(
            *args, timeout=timeout, start_new_session=self._detached if detach is None else detach
        )
        return _triple(r, "virsh", timeout)

    def define(self, text: str) -> tuple[int, str, str]:
        """virsh define of text, through a temporary file (removed after)."""
        return define_from_text(lambda path: self.virsh(("define", path), 60, detach=None), text, prefix="cst-vm-tune.")

    def package_versions(self) -> dict[str, str]:
        return package_versions()

    def booted_deployment(self) -> BootedDeployment | None:
        return booted_deployment()


RUNNER: CommandRunnerPort = SubprocessCommandRunner()

# The one VmCommands of this process: run, virsh and run_on_terminal below go through it.
COMMANDS_OF_VM = VmCommands(RUNNER)


def run(
    cmd: Sequence[str],
    timeout: int = 120,
    extra_env: Mapping[str, str] | None = None,
    detach: bool | None = None,
) -> tuple[int, str, str]:
    """(rc, stdout, stderr); rc 127 when the command does not exist. In the
    C locale (virsh translates its answers: "shut off" is "desligado" in
    pt_BR); extra_env on top. Bytes that are not UTF-8 become U+FFFD."""
    return COMMANDS_OF_VM.run(cmd, timeout, extra_env, detach=detach)


def run_on_terminal(cmd: Sequence[str]) -> int:
    """Exit status of cmd in the C locale, on our terminal (stdin and output:
    sudo may ask for the password). A command that cannot start raises (as
    subprocess.run did)."""
    return COMMANDS_OF_VM.on_terminal(cmd)


def virsh(*args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
    """virsh -c URI ARGS through VmCommands' virsh adapter, with run()'s answers."""
    return COMMANDS_OF_VM.virsh(args, timeout, detach=detach)


def effective_root() -> bool:
    """This process runs as root (its effective uid is 0)."""
    return os.geteuid() == 0


def read_conf(path: str, *, nas_conf: str, bash_sourced: bool = False) -> dict[str, str]:
    """The settings of a KEY=VALUE file ({} when it cannot be read), through
    cst_core's one reader (parse_shell_assignments); the last line of a key
    wins. bash_sourced: cpu-setup.conf and snapshots.conf, which bash sources
    too, read exactly as bash reads them; vm.conf (only Python reads it) also
    accepts blanks around = (`VM_NOCOW = 0`), as cst-vm always did. A line
    that is not an assignment sets nothing (prepare_command reports it).
    Undecodable bytes are replaced (main's cst-vm stopped with a traceback).
    nas_conf: the path of snapshots.conf (settings_file)."""
    return dict(settings_file(path, bash_sourced=bash_sourced, nas_conf=nas_conf).values)


def settings_file(path: str, *, bash_sourced: bool, nas_conf: str) -> ShellAssignments:
    """A settings file through cst_core's one reader ("" when it cannot be read).
    snapshots.conf (the file at nas_conf) keeps its line ends so a carriage return stays
    part of its line as in bash (matching btrfs-snapshot, btrfs-health-check, and cst doctor),
    and also takes the lines an older CST wrote there with printf %q (parse_snapshots_conf)."""
    text = read_text(path, "", keep_line_ends=path == nas_conf)
    if path == nas_conf:
        return parse_snapshots_conf(text)
    return parse_shell_assignments(text, bash_sourced=bash_sourced)


def conf_files(paths: VmPaths, cmd: VmCommand, nas: bool, vm: str | None) -> list[tuple[str, bool]]:
    """[(settings file, bash_sourced)] that a cst-vm command reads; vm: the VM named on the command line."""
    files = [(paths.vm_conf, False)]
    # cpu-setup.conf: VM_NAME when no VM is named, VM_CPU_DISABLE for status and tune.
    # A backup (or backup-timer) of a named VM reads neither: a line there must not stop it.
    if cmd != VmCommand.NOCOW and not (vm is not None and cmd in (VmCommand.BACKUP, VmCommand.BACKUP_TIMER)):
        files.append((paths.cpu_conf, True))
    if nas or cmd == VmCommand.BACKUP_TIMER:
        files.append((paths.nas_conf, True))
    return files


def prepare_command(paths: VmPaths, cmd: VmCommand, sub: VmSub | None, nas: bool, vm: str | None) -> None:
    """Before a cst-vm command runs (help: only the SIGPIPE default below). Problem lines of the settings files it
    reads: a command that changes the machine with them (backup, tune/nocow
    apply, backup-timer on) refuses to start, since a default must never
    stand in for a line that says something else; the others print them on
    stderr and go on (backup-timer off too: it reads no setting, and it is
    the way back). A command that only reads (status, list, the plans,
    backup-timer status, help) also gets SIGPIPE back at its default, so `| head`
    ends it quietly; commands that change the machine keep Python's handling
    (backup: SafeStream), so a closed pipe never cuts their cleanup short."""
    if cmd == VmCommand.HELP:
        restore_default_signals()
        return
    found = []
    for path, bash_sourced in conf_files(paths, cmd, nas, vm):
        problems = settings_file(path, bash_sourced=bash_sourced, nas_conf=paths.nas_conf).problems
        if problems:
            found.append(describe_problems(path, problems))
    changes = cmd == VmCommand.BACKUP or sub in (VmSub.APPLY, VmSub.ON, VmSub.OFF)
    if found and changes and sub != VmSub.OFF:
        raise Refused("\n".join(found) + "\nFix these lines first; nothing was changed.")
    if found:
        print("\n".join(found), file=sys.stderr)
    if not changes:
        restore_default_signals()


def conf_number(paths: VmPaths, key: str) -> int:
    """A whole number of /etc/cst/vm.conf in ASCII digits (NAS_DEFAULTS when missing or anything else)."""
    v = parse_whole_number(read_conf(paths.vm_conf, nas_conf=paths.nas_conf).get(key, ""))
    return NAS_DEFAULTS[key] if v is None else v


def default_vm(paths: VmPaths) -> str:
    return read_conf(paths.cpu_conf, nas_conf=paths.nas_conf, bash_sourced=True).get("VM_NAME") or DEFAULT_VM


def cpu_disable_list(paths: VmPaths) -> list[str]:
    """The features vm-pin keeps disabled: VM_CPU_DISABLE from cpu-setup.conf
    when set there (also empty), else vm-pin's default (cpu.domain)."""
    conf = read_conf(paths.cpu_conf, nas_conf=paths.nas_conf, bash_sourced=True)
    if "VM_CPU_DISABLE" in conf:
        return conf["VM_CPU_DISABLE"].split()
    return CPU_DISABLE_DEFAULT.split()


def package_versions() -> dict[str, str]:
    """{package: name-version-release.arch or "not installed"}, one rpm -q each
    (its exit code tells whether it is installed)."""
    out: dict[str, str] = {}
    for p in VIRT_PACKAGES:
        rc, so, _ = run(["rpm", "-q", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n", p], timeout=30)
        out[p] = first_line(so) if rc == 0 and first_line(so) else NOT_INSTALLED
    return out


def booted_deployment() -> BootedDeployment | None:
    """{version, checksum, timestamp} of the booted rpm-ostree deployment, or None: rpm-ostree
    failed, its answer is not readable, or it names no booted deployment. The reason
    query_status gives is not kept: the manifest and the index record null, as they always did."""
    status = query_status(lambda argv: run(argv, timeout=MINUTE))
    if isinstance(status, str) or status.booted is None:
        return None
    d = status.booted
    return {"version": d.get("version"), "checksum": d.get("checksum"), "timestamp": d.get("timestamp")}


def nas_settings(cfg: VmSettings) -> NasRepo:
    """{host, env, repo} for restic, from the /home NAS backup's settings.
    Refused when that backup is not set up: its setup creates the
    repository, the SSH key and the password file this one reuses."""
    conf = read_conf(cfg.paths.nas_conf, nas_conf=cfg.paths.nas_conf, bash_sourced=True)
    setup = f"{cfg.cst} btrfs snapshots nas setup"
    if conf.get("NAS_ENABLED") != "1" or not conf.get("NAS_PATH", ""):
        raise Refused(
            f"the NAS backup is not set up ({cfg.paths.nas_conf}: NAS_ENABLED=1 and NAS_PATH are needed). "
            f"Set it up first: {setup} (the VM backup shares its restic repository)"
        )
    try:
        if os.path.getsize(cfg.paths.restic_password) == 0:
            raise OSError
    except OSError:
        raise Refused(
            f"{cfg.paths.restic_password} is missing or empty: run {setup} again (or put the saved repository password there)"
        ) from None
    host = conf.get("NAS_RESTIC_HOST") or socket.gethostname().split(".")[0]
    repo = f"sftp:{NAS_SSH_ALIAS}:{conf['NAS_PATH']}"
    return {
        "host": host,
        "repo": repo,
        "env": {
            "RESTIC_REPOSITORY": repo,
            "RESTIC_PASSWORD_FILE": cfg.paths.restic_password,
            "RESTIC_CACHE_DIR": cfg.paths.restic_cache,
        },
    }


class VmConfFiles:
    """SettingsPort on this machine: the settings files of cfg's paths, read at each call."""

    def __init__(self, cfg: VmSettings) -> None:
        self.cfg = cfg

    def number(self, key: str) -> int:
        return conf_number(self.cfg.paths, key)

    def nas_repo(self) -> NasRepo:
        return nas_settings(self.cfg)

    def nas_set_up(self) -> bool:
        conf = read_conf(self.cfg.paths.nas_conf, nas_conf=self.cfg.paths.nas_conf, bash_sourced=True)
        return conf.get("NAS_ENABLED") == "1" and bool(conf.get("NAS_PATH"))

    def knobs(self) -> dict[str, bool]:
        return knobs(self.cfg.paths)

    def cpu_disable(self) -> list[str]:
        return cpu_disable_list(self.cfg.paths)

    def default_vm(self) -> str:
        return default_vm(self.cfg.paths)


def knobs(paths: VmPaths) -> dict[str, bool]:
    """{knob: bool} from /etc/cst/vm.conf; a missing file or knob is yes."""
    conf = read_conf(paths.vm_conf, nas_conf=paths.nas_conf)
    # ASCII blanks only: VM_NOCOW='no<NBSP>' is a problem line (prepare_command refuses), never "no".
    return {k: strip_ascii_blanks(conf.get(k, "yes")).lower() not in NO_WORDS for k in KNOBS}
