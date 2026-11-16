"""Port interfaces (typing.Protocol) and shared boundary errors for CST adapters."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result from executing a system command."""

    returncode: int
    stdout: str
    stderr: str


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


class CommandRunnerPort(Protocol):
    """Executes external commands without leaking shell or stdin state."""

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
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


class ExecutionContextPort(Protocol):
    """Describes the execution environment (host vs container/toolbox, privilege level)."""

    def is_container(self) -> bool: ...

    def is_root(self) -> bool: ...

    def etc_dir(self) -> str: ...

    def host_run_dir(self, subpath: str) -> str: ...


class HostPathPort(Protocol):
    """Maps an absolute host path into this process's container or host view."""

    def path(self, host_path: str) -> str: ...


class ClockPort(Protocol):
    """Provides wall-clock, monotonic timestamps, and sleeping without direct `time` imports."""

    def now(self) -> float: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class VirshPort(Protocol):
    """Consolidated interface to libvirt (`virsh`) domain queries and control."""

    def call(
        self,
        *args: str,
        check: bool = False,
        timeout: float | None = 60,
        start_new_session: bool = False,
    ) -> CommandResult: ...

    def domains(self, *, all_domains: bool = True) -> list[str]: ...

    def dumpxml(self, domain: str, *, inactive: bool = False) -> str | None: ...


class CstDispatchPort(Protocol):
    """Host capabilities required by the top-level `cst` dispatcher."""

    def is_file(self, path: str) -> bool: ...

    def is_container(self) -> bool: ...

    def euid(self) -> int: ...

    def environ(self) -> Mapping[str, str]: ...

    def user_passwd(self, user: str) -> tuple[int, str]: ...

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int: ...


__all__ = [
    "ClockPort",
    "CommandFailed",
    "CommandResult",
    "CommandRunnerPort",
    "CstDispatchPort",
    "ExecutionContextPort",
    "FileSystemPort",
    "HostPathPort",
    "InstallTargetPort",
    "SystemdPort",
    "VirshPort",
]


class SystemdPort(Protocol):
    """Manages systemd units and path escaping."""

    def daemon_reload(self) -> None: ...

    def start(self, unit: str) -> bool: ...

    def stop(self, unit: str) -> bool: ...

    def restart(self, unit: str) -> bool: ...

    def escape_path(self, path: str, *, suffix: str) -> str: ...


class InstallTargetPort(Protocol):
    """The files `install` reads and writes (the real root, or a staging folder in tests)."""

    def current(self, path: str) -> tuple[bytes, int] | None:
        """(content, permission bits) of the regular file at path; None when absent or not a regular file."""
        ...

    def write(self, path: str, data: bytes, mode: int) -> None:
        """Replace path atomically with data and mode (parent folders created 0755)."""
        ...
