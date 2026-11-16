"""Ports (interfaces) for the `doctor` bounded context."""

from collections.abc import Iterable
from typing import Protocol

from doctor.domain import Deployment, OstreeAnswer, Result


class DoctorHostPort(Protocol):
    """Read-only view of the host: files under root and query commands."""

    container: bool
    root: str
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


class CheckFn(Protocol):
    """A registered check callable: host -> iterable of Result."""

    __name__: str

    def __call__(self, host: DoctorHostPort, /) -> Iterable[Result]:
        """Run one check against the given host adapter."""
        ...
