"""Ports (interfaces) for the `doctor` bounded context."""

from collections.abc import Callable, Iterable, Mapping
import os
from typing import Any, BinaryIO, Protocol, TextIO

from doctor.domain import Deployment, OstreeAnswer, Result


class DoctorHostPort(Protocol):
    """Read-only view of the host: files under root and query commands."""

    container: bool
    root: str
    # The cst command for hints ("cst", or what CST_PROG names).
    cst: str
    now: float
    uid: int
    state_home: str
    _ostree: OstreeAnswer
    _ostree_list: list[Deployment]
    _domains: tuple[list[str] | None, str]
    _dumpxml: dict[str, tuple[int, str, str]]

    def path(self, p: str) -> str:
        """Map an absolute host path to this process's view (e.g. /run/host)."""
        ...

    def run(self, cmd: list[str], timeout: float = 60) -> tuple[int, str, str]:
        """Run a read-only query command on the host; return (rc, stdout, stderr)."""
        ...

    def repo_path(self, rel: str) -> str:
        """Where the CST checkout's file `rel` is (it may not exist)."""
        ...

    # Files, by paths already mapped with path(). Each raises OSError like the os call it stands for.
    def open_text(self, path: str) -> TextIO:
        """Open for reading as UTF-8 (undecodable bytes replaced)."""
        ...

    def open_bytes(self, path: str) -> BinaryIO:
        """Open for reading as bytes."""
        ...

    def listdir(self, path: str) -> list[str]:
        """The names in a folder."""
        ...

    def exists(self, path: str) -> bool:
        """True when path exists (following links)."""
        ...

    def isdir(self, path: str) -> bool:
        """True for a folder (following links)."""
        ...

    def isfile(self, path: str) -> bool:
        """True for a regular file (following links)."""
        ...

    def islink(self, path: str) -> bool:
        """True for a symbolic link."""
        ...

    def stat(self, path: str) -> os.stat_result:
        """os.stat of path."""
        ...

    def lstat(self, path: str) -> os.stat_result:
        """os.lstat of path."""
        ...

    def realpath(self, path: str) -> str:
        """The path with every link resolved."""
        ...


class CheckFn(Protocol):
    """A registered check callable: host -> iterable of Result."""

    __name__: str

    def __call__(self, host: DoctorHostPort, /) -> Iterable[Result]:
        """Run one check against the given host adapter."""
        ...


class VmToolsPort(Protocol):
    """The host side of the vm package the VM checks reuse (vm.adapters):
    the knobs of /etc/cst/vm.conf and the probe of the VM image folders."""

    @property
    def system_images(self) -> str:
        """libvirt's system image folder (vm.adapters.SYSTEM_IMAGES)."""
        ...

    def knobs(self, path: str) -> dict[str, bool]:
        """{knob: on} from a vm.conf (missing file or knob: on)."""
        ...

    def image_dirs(self) -> list[dict[str, str]]:
        """[{scope, path, what}] of every known VM image folder."""
        ...

    def probe_dir(self, d: Mapping[str, Any], readable: Callable[[str], str]) -> dict[str, Any]:
        """What an image folder looks like (readable maps a path to where this process reads it)."""
        ...
