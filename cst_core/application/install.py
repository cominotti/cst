"""Install use case: write manifest files (and generated text) only when they changed.

Output lines are the ones the setup scripts always printed:
`unchanged: DEST` and `written:   DEST`; a plan prints
`would write: DEST` instead and changes nothing.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from cst_core.domain.install import InstallEntry, expand_dest, render_template
from cst_core.ports import InstallTargetPort

# The bytes install writes for an entry (a zipapp for a python-entry, the
# file for the rest), before template rendering.
type PayloadFn = Callable[[InstallEntry], bytes]


class InstallRefused(Exception):
    """An install that must not run as asked (wrong user, bad arguments)."""


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """How to place the entries: home for `~/` dests, values for templates, a staging root."""

    home: str | None = None
    values: Mapping[str, str] | None = None
    root: str = ""
    dest: str | None = None
    as_root: bool = False


def staged(root: str, path: str) -> str:
    """`path` under the staging folder `root` ('' = the real root)."""
    return root.rstrip("/") + path if root else path


class InstallService:
    """Writes manifest entries and generated text through an InstallTargetPort."""

    def __init__(self, target: InstallTargetPort, payload: PayloadFn) -> None:
        self._target = target
        self._payload = payload

    def desired(self, entry: InstallEntry, values: Mapping[str, str] | None = None) -> bytes:
        """The bytes the entry's dest must hold."""
        data = self._payload(entry)
        if entry.is_template:
            return render_template(data.decode("utf-8"), values or {}).encode("utf-8")
        return data

    def destination(self, entry: InstallEntry, req: InstallRequest) -> str:
        """Where the entry goes for this request (dest override, `~/` expanded, staged)."""
        return staged(req.root, req.dest or expand_dest(entry.dest, req.home))

    def _check(self, entries: Sequence[InstallEntry], req: InstallRequest) -> None:
        if req.dest is not None and len(entries) != 1:
            raise InstallRefused("--dest names the destination of exactly one file")
        for entry in entries:
            if entry.owner == "user" and req.as_root and not req.root:
                raise InstallRefused(
                    f"{entry.src}: belongs to the user; install it as the user (cst_install_as USER), not as root"
                )

    def is_current(self, path: str, data: bytes, mode: int) -> bool:
        """True when path already holds exactly data with mode."""
        return self._target.current(path) == (data, mode)

    def write_if_changed(self, path: str, data: bytes, mode: int) -> str:
        """Write path unless it is current; the report line either way."""
        if self.is_current(path, data, mode):
            return f"unchanged: {path}"
        self._target.write(path, data, mode)
        return f"written:   {path}"

    def install(self, entries: Sequence[InstallEntry], req: InstallRequest) -> list[str]:
        """Bring every entry's dest up to date; one report line each."""
        self._check(entries, req)
        return [
            self.write_if_changed(self.destination(e, req), self.desired(e, req.values), e.mode.bits) for e in entries
        ]

    def plan(self, entries: Sequence[InstallEntry], req: InstallRequest) -> tuple[list[str], int]:
        """What install would do, changing nothing: (report lines, number of files to write)."""
        self._check(entries, req)
        lines: list[str] = []
        todo = 0
        for e in entries:
            path = self.destination(e, req)
            if self.is_current(path, self.desired(e, req.values), e.mode.bits):
                lines.append(f"unchanged: {path}")
            else:
                lines.append(f"would write: {path}")
                todo += 1
        return lines, todo
