# SPDX-License-Identifier: MPL-2.0
"""Ports (interfaces) for the `doctor` bounded context."""

from collections.abc import Callable, Iterable, Mapping
import os
from typing import BinaryIO, Protocol, TextIO

from cst_core.domain.record import InstallRecord
from cst_core.ports import VirshPort
from doctor.domain import HostPath, Result, ViewPath
from vm.domain import ImageDir, ImageProbe


class DoctorHostPort(Protocol):
    """Read-only view of the host: files under root and query commands."""

    container: bool
    root: str
    # The cst command for hints ("cst", or what CST_PROG names).
    cst: str
    now: float
    uid: int
    # The machine's architecture (uname -m), e.g. x86_64.
    arch: str
    # The doctor's own XDG state folder (a path of this process: never mapped).
    state_home: ViewPath
    # The home folder of the user it runs as, on the host (where that user's install record is).
    home: HostPath

    def path(self, p: HostPath) -> ViewPath:
        """Map an absolute host path to this process's view (e.g. /run/host)."""
        ...

    def run(self, cmd: list[str], timeout: float = 60, *, locale_c: bool = False) -> tuple[int, str, str]:
        """Run a read-only query command on the host (with LC_ALL=C when locale_c); return (rc, stdout, stderr)."""
        ...

    def virsh(self, *, read_only: bool) -> VirshPort:
        """virsh on the host's system connection (-r when read_only), with run()'s answers for a fault."""
        ...

    def repo_path(self, rel: str) -> ViewPath:
        """Where the CST checkout's file `rel` is (it may not exist)."""
        ...

    # Files, by paths already mapped with path(). Each raises OSError like the os call it stands for.
    def open_text(self, path: ViewPath) -> TextIO:
        """Open for reading as UTF-8 (undecodable bytes replaced)."""
        ...

    def open_bytes(self, path: ViewPath) -> BinaryIO:
        """Open for reading as bytes."""
        ...

    def zip_text(self, path: ViewPath, member: str, limit: int) -> str:
        """The first limit bytes of one member of the zip archive at path (an installed zipapp), as UTF-8
        with undecodable bytes replaced. OSError also when path is no zip archive or has no such member."""
        ...

    def listdir(self, path: ViewPath) -> list[str]:
        """The names in a folder."""
        ...

    def exists(self, path: ViewPath) -> bool:
        """True when path exists (following links)."""
        ...

    def isdir(self, path: ViewPath) -> bool:
        """True for a folder (following links)."""
        ...

    def isfile(self, path: ViewPath) -> bool:
        """True for a regular file (following links)."""
        ...

    def islink(self, path: ViewPath) -> bool:
        """True for a symbolic link."""
        ...

    def stat(self, path: ViewPath) -> os.stat_result:
        """os.stat of path."""
        ...

    def lstat(self, path: ViewPath) -> os.stat_result:
        """os.lstat of path."""
        ...

    def realpath(self, path: ViewPath) -> ViewPath:
        """The path with every link resolved."""
        ...

    def user_name(self, uid: int) -> str:
        """The login name of uid ("" when the account is unknown)."""
        ...

    def user_id(self, name: str) -> int | None:
        """The uid of the account name (None when it is unknown)."""
        ...


class CheckFn(Protocol):
    """A registered check callable: host -> iterable of Result."""

    __name__: str

    def __call__(self, host: DoctorHostPort, /) -> Iterable[Result]:
        """Run one check against the given host adapter."""
        ...


class VmToolsPort(Protocol):
    """The host side of the vm package the VM checks reuse (vm.adapters, vm.adapters.images):
    the knobs of /etc/cst/vm.conf and the probe of the VM image folders."""

    @property
    def system_images(self) -> str:
        """libvirt's system image folder (vm.adapters.SYSTEM_IMAGES)."""
        ...

    def knobs(self, path: str) -> dict[str, bool]:
        """{knob: on} from a vm.conf (missing file or knob: on)."""
        ...

    def image_dirs(self) -> list[ImageDir]:
        """Every known VM image folder."""
        ...

    def probe_dir(self, d: ImageDir, readable: Callable[[str], str]) -> ImageProbe:
        """What an image folder looks like (readable maps a path to where this process reads it)."""
        ...


class InstalledFilesPort(Protocol):
    """The install records (cst_core.domain.record) and what this checkout would install now."""

    def record(self, path: ViewPath) -> InstallRecord | None:
        """The record at path; None when there is none. RecordFormatError when it is not one (newer:
        written by a newer CST); OSError when it cannot be read."""
        ...

    def file_sha256(self, path: ViewPath) -> str:
        """The sha256 of the file at path; OSError when it cannot be read."""
        ...

    def desired_sha256(self, src: str, values: Mapping[str, str] | None) -> str | None:
        """The sha256 of what this checkout installs for src (a template rendered with values); None
        when it cannot say (no checkout, no such source, a template without its values)."""
        ...
