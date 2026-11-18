# SPDX-License-Identifier: MPL-2.0
"""Port interfaces (typing.Protocol) and shared boundary errors for CST adapters."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, NewType, Protocol

from cst_core.domain.install import OnDisk
from cst_core.domain.record import InstallRecord


# Why a command did not run to its end: not found (exit status 127), found
# but could not start (126: no permission, not an executable, its cwd
# folder missing), or stopped at its timeout (124).
type CommandFault = Literal["missing", "unstartable", "timeout"]

# The command's stdin: /dev/null (nothing of ours leaks into it), or ours.
type StdinMode = Literal["devnull", "inherit"]

# Its stdout and stderr: both captured; stderr merged into the captured
# stdout; stdout captured and stderr discarded; stdout captured and stderr
# left on ours ("stdout-show-stderr": a shell's $(cmd)); or both left on ours
# (the result's stdout and stderr are then empty).
type OutputMode = Literal["capture", "merge", "stdout", "stdout-show-stderr", "inherit"]

# Captured output that is not UTF-8: U+FFFD for the bad bytes (every CST
# caller), a UnicodeDecodeError out of run(), or the bad bytes kept as lone
# surrogates ("surrogateescape": written back with surrogateescape, or passed
# in an argv, they are the bytes the command printed, as a shell passes them on).
type DecodeErrors = Literal["replace", "strict", "surrogateescape"]


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result from executing a system command."""

    returncode: int
    stdout: str
    stderr: str
    # The command that ran, for messages (a fake may leave it empty); not compared.
    argv: tuple[str, ...] = field(default=(), compare=False)
    # Set when the command did not run to its end (returncode 127, 126 or
    # 124 then); None when it ran and exited, whatever its exit status (a
    # command may exit 127 itself). Not compared.
    fault: CommandFault | None = field(default=None, compare=False)
    # The exception behind fault (FileNotFoundError, another OSError, or
    # subprocess.TimeoutExpired): for a caller that keeps its own message, or
    # that lets the exception propagate as it did before. Not compared.
    cause: Exception | None = field(default=None, compare=False)


class CommandFailed(RuntimeError):
    """Raised when an external command fails and check=True."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.argv: tuple[str, ...] = tuple(argv)
        self.returncode: int = returncode
        self.stdout: str = stdout
        self.stderr: str = stderr
        detail = stderr.strip() or stdout.strip() or f"exit status {returncode}"
        super().__init__(f"{' '.join(self.argv)}: {detail}")


class WriteFailed(Exception):
    """A destination could not be written: "cannot write PATH: REASON"."""

    def __init__(self, path: str, reason: str) -> None:
        self.path: str = path
        self.reason: str = reason
        super().__init__(f"cannot write {path}: {reason}")


class CommandRunnerPort(Protocol):
    """Executes external commands without leaking shell or stdin state.

    Every option defaults to off, so a call names what it needs:
    - check: raise CommandFailed on a non-zero exit (or a fault);
    - timeout: seconds (None: no limit); past it, exit status 124;
    - input_text: the command's stdin (instead of /dev/null);
    - env: variables set on top of the base environment; unset: names
      removed from it; base_env: the environment to start from (None: this
      process's);
    - cwd: the folder the command runs in;
    - locale_c: LC_ALL=C, for a command whose answers are parsed (env
      still wins);
    - new_session: the command runs in a session of its own (a Ctrl-C or a
      closed terminal does not reach it);
    - on_host: inside a container, run it on the host (flatpak-spawn --host,
      with env and locale_c passed as --env); on the host, as is. The --env
      values are on flatpak-spawn's command line (ps shows them): no secrets
      there. base_env, timeout, nice and new_session then act on the local
      flatpak-spawn process, not on the host command (a timeout stops
      flatpak-spawn; the host command may run on);
    - stdin, output, decode_errors: see StdinMode, OutputMode, DecodeErrors;
    - nice: added to the command's niceness before it starts;
    - pass_fds: open file descriptors the command gets as they are (the
      others are closed): a lock this process holds, handed on;
    - on_line: called with each line of the captured stdout (output
      "merge", "stdout" or "stdout-show-stderr"; without its newline) as soon as the
      command writes it, for a caller that passes the lines on while the
      command runs; not with timeout (use timeout(1) then) or input_text.
    """

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
        pass_fds: Sequence[int] = (),
        on_line: Callable[[str], None] | None = None,
    ) -> CommandResult: ...


class FileSystemPort(Protocol):
    """Abstracts host filesystem queries and atomic updates."""

    def exists(self, path: str) -> bool: ...

    def lexists(self, path: str) -> bool: ...

    def is_dir(self, path: str) -> bool: ...

    def is_file(self, path: str) -> bool: ...

    def is_symlink(self, path: str) -> bool: ...

    def is_block_device(self, path: str) -> bool: ...

    def is_readable(self, path: str) -> bool: ...

    def read_text(self, path: str, default: str | None = None) -> str | None: ...

    def read_first_line(self, path: str, default: str | None = None) -> str | None: ...

    def readlink(self, path: str, *, canonicalize: bool = False) -> str | None: ...

    def list_dir(self, path: str) -> list[str]: ...

    def write_atomic(self, dest: str, content: str, mode: int = 0o644) -> None: ...

    def write_text_direct(self, path: str, content: str) -> None: ...

    def copy_if_absent(self, src: str, dest: str) -> None: ...

    def remove_file(self, path: str) -> None: ...

    def force_symlink(self, target: str, link_path: str) -> None: ...


class ContainerProbePort(Protocol):
    """Tells whether this process runs inside a container (a toolbox)."""

    def is_container(self) -> bool: ...


class ExecutionContextPort(Protocol):
    """Describes the execution environment (host vs container/toolbox, privilege level)."""

    def is_container(self) -> bool: ...

    def is_root(self) -> bool: ...

    def etc_dir(self) -> str: ...

    def host_run_dir(self, subpath: str) -> str: ...


class ClockPort(Protocol):
    """Provides wall-clock, monotonic timestamps, and sleeping without direct `time` imports."""

    def now(self) -> float: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


# A libvirt domain name ("win11-devbox"), as virsh lists and takes it.
VmName = NewType("VmName", str)
# A systemd unit name with its suffix ("dev-zram0.swap", "foo@x.service").
UnitName = NewType("UnitName", str)


class VirshPort(Protocol):
    """Consolidated interface to libvirt (`virsh`) domain queries and control.

    call runs `virsh -c URI ARGS` in the C locale (virsh translates its
    answers) through a CommandRunnerPort; its CommandResult carries fault.
    """

    def call(
        self,
        *args: str,
        check: bool = False,
        timeout: float | None = 60,
        start_new_session: bool = False,
    ) -> CommandResult: ...

    def domains(self, *, all_domains: bool = True) -> list[VmName]: ...

    def dumpxml(self, domain: VmName, *, inactive: bool = False) -> str | None: ...


class CstDispatchPort(Protocol):
    """Host capabilities required by the top-level `cst` dispatcher."""

    def is_file(self, path: str) -> bool: ...

    def is_container(self) -> bool: ...

    def euid(self) -> int: ...

    def environ(self) -> Mapping[str, str]: ...

    def user_passwd(self, user: str) -> tuple[int, str]: ...

    def which(self, name: str, path: str | None) -> str | None:
        """Where PATH (path; None: the default) finds the program name, or None."""
        ...

    def realpath(self, path: str) -> str:
        """path with every link resolved."""
        ...

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Run argv in the foreground (bash's semantics for signals); its exit status."""
        ...

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Replace this process with argv (bash's exec); returns only when argv cannot start."""
        ...


class ModuleRoutePort(Protocol):
    """What routing a module's command (cst_core/route.py) needs from the host."""

    def environ(self) -> Mapping[str, str]: ...

    def euid(self) -> int: ...

    def is_container(self) -> bool: ...

    def user_passwd(self, user: str) -> tuple[int, str]:
        """(uid, home) of user; KeyError for an unknown one."""
        ...

    def exec_on_host(self, argv: Sequence[str], *, settings: Mapping[str, str]) -> int | None:
        """From a container: replace this process with argv run on the host (flatpak-spawn
        --host, settings as --env); None when there is no flatpak-spawn on PATH."""
        ...

    def read_text(self, path: str) -> str:
        """A file's bytes as text (the file system's encoding, every byte kept)."""
        ...

    def say(self, stream: Literal["out", "err"], text: str) -> None:
        """Write text (encoded back the same way) to stdout or stderr, at once."""
        ...

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        """Replace this process with argv (bash's exec); returns only when argv cannot start."""
        ...


class SystemdPort(Protocol):
    """Manages systemd units and path escaping."""

    def daemon_reload(self) -> None: ...

    def start(self, unit: UnitName) -> CommandResult:
        """`systemctl start unit`: its exit status and what it printed."""
        ...

    def stop(self, unit: UnitName) -> CommandResult:
        """`systemctl stop unit`: its exit status and what it printed."""
        ...

    def restart(self, unit: UnitName) -> CommandResult:
        """`systemctl restart unit`: its exit status and what it printed."""
        ...

    def enable(self, unit: UnitName, *, now: bool = False) -> CommandResult:
        """`systemctl enable [--now] unit`: its exit status and what it printed."""
        ...

    def disable(self, unit: UnitName, *, now: bool = False) -> CommandResult:
        """`systemctl disable [--now] unit`: its exit status and what it printed."""
        ...

    def clean(self, unit: UnitName, *, what: str) -> CommandResult:
        """`systemctl clean --what=WHAT unit` (what="state": a timer's Persistent= stamp)."""
        ...

    def escape_path(self, path: str, *, suffix: str) -> UnitName: ...

    def show(self, unit: UnitName, props: Sequence[str], *, on_host: bool = True) -> dict[str, str]:
        """`systemctl show -p PROP... unit` as {prop: value}; {} when systemctl cannot answer.
        on_host: the host's systemd from a toolbox (False: the systemd where the caller runs)."""
        ...


class InstallTargetPort(Protocol):
    """The files `install` reads and writes (the real root, or a staging folder in tests)."""

    def current(self, path: str) -> OnDisk | None:
        """What is at path (a regular file read through a link); None when nothing is there."""
        ...

    def write(self, path: str, data: bytes, mode: int) -> None:
        """Replace path atomically with data and mode (parent folders created 0755).

        A failure raises WriteFailed (cst_core.ports), naming path.
        """
        ...

    def remove(self, path: str) -> bool:
        """Remove path (a link itself, never what it points to); False when nothing was there.

        A failure raises WriteFailed, naming path.
        """
        ...


@dataclass(frozen=True, slots=True)
class UnitVerdict:
    """systemd-analyze verify on units before they are written: ok, the lines about them, and a note
    when they could not be verified (no systemd-analyze)."""

    ok: bool
    lines: tuple[str, ...] = ()
    note: str = ""


class UnitVerifierPort(Protocol):
    """Checks unit files before the installer replaces the installed ones."""

    def verify(self, units: Mapping[str, bytes]) -> UnitVerdict:
        """{unit file name: bytes}, verified together (a timer next to its service)."""
        ...


class SettingsStepPort(Protocol):
    """Runs a manifest program's SettingsStep (cst_core.domain.settings_steps) from the checkout."""

    def run(self, src: str, args: Sequence[str], env: Mapping[str, str]) -> CommandResult:
        """The checkout's src with args, env added to its environment (the step's two variables: the
        installed copy and the settings file, both as the install sees them, staged under --root)."""
        ...


class InstallRecordPort(Protocol):
    """The install record files (cst_core.domain.record): read, and changed under a lock."""

    def read(self, path: str) -> InstallRecord | None:
        """The record at path; None when there is none. RecordFormatError when it is not one."""
        ...

    def update(self, path: str, change: Callable[[InstallRecord], InstallRecord]) -> bool:
        """Change the record at path under its lock (no file, or one that is not JSON: an empty one);
        True when the file changed. RecordFormatError (newer=True) for a record of a newer CST,
        which is left as it is; WriteFailed when it cannot be written."""
        ...

    def sha256(self, data: bytes) -> str:
        """The hex sha256 of data, as the record holds it."""
        ...
