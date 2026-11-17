"""Install use case: write manifest files (and generated text) only when they changed.

Output lines are the ones the setup scripts always printed:
`unchanged: DEST` and `written:   DEST`; a plan prints
`would write: DEST` instead and changes nothing. Every entry's bytes are
built and checked (templates) before the first write, so a bad call writes
nothing; then one line comes per file, as it is written.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from cst_core.domain.files import checked_path
from cst_core.domain.install import InstallEntry, Owner, expand_dest, is_current, render_template, template_keys
from cst_core.domain.values import DomainValidationError
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
    as_root: bool = False


@dataclass(frozen=True, slots=True)
class _Prepared:
    entry: InstallEntry
    path: str
    data: bytes


def staged(root: str, path: str) -> str:
    """`path` under the staging folder `root` ('' = the real root); both plain absolute paths."""
    checked_path(path)
    if not root:
        return path
    return checked_path(root.rstrip("/") or "/").rstrip("/") + path


class InstallService:
    """Writes manifest entries and generated text through an InstallTargetPort."""

    def __init__(self, target: InstallTargetPort, payload: PayloadFn) -> None:
        self._target = target
        self._payload = payload

    def desired(self, entry: InstallEntry, values: Mapping[str, str] | None = None) -> bytes:
        """The bytes the entry's dest must hold (DomainValidationError: a template value missing or unused)."""
        return self._render([entry], values)[0]

    def _render(self, entries: Sequence[InstallEntry], values: Mapping[str, str] | None) -> list[bytes]:
        """Every entry's bytes; a --set value no template of the call uses is an error (a misspelt KEY)."""
        values = values or {}
        used: set[str] = set()
        out: list[bytes] = []
        for e in entries:
            data = self._payload(e)
            if e.is_template:
                text = data.decode("utf-8")
                used |= template_keys(text)
                try:
                    data = render_template(text, values).encode("utf-8")
                except DomainValidationError as exc:
                    raise DomainValidationError(f"{e.src}: {exc}") from None
            out.append(data)
        unused = sorted(values.keys() - used)
        if unused:
            raise DomainValidationError(
                "--set "
                + ", ".join(unused)
                + ": no template among these files uses "
                + ", ".join(f"@{k}@" for k in unused)
            )
        return out

    def destination(self, entry: InstallEntry, req: InstallRequest) -> str:
        """Where the entry goes for this request (`~/` expanded, staged)."""
        return staged(req.root, expand_dest(entry.dest, req.home))

    def _check(self, entries: Sequence[InstallEntry], req: InstallRequest) -> None:
        for entry in entries:
            if entry.owner == "user" and req.as_root and not req.root:
                raise InstallRefused(
                    f"{entry.src}: belongs to the user; install it as the user (cst_install_as USER), not as root"
                )

    def _prepare(self, entries: Sequence[InstallEntry], req: InstallRequest) -> list[_Prepared]:
        self._check(entries, req)
        paths = [self.destination(e, req) for e in entries]
        return [_Prepared(e, p, d) for e, p, d in zip(entries, paths, self._render(entries, req.values), strict=True)]

    def is_current(self, path: str, data: bytes, mode: int, owner: Owner = "root", *, as_root: bool = False) -> bool:
        """True when path already holds exactly data with mode (links and owner: domain.install.is_current)."""
        return is_current(self._target.current(path), data, mode, owner, as_root=as_root)

    def write_if_changed(
        self, path: str, data: bytes, mode: int, owner: Owner = "root", *, as_root: bool = False
    ) -> str:
        """Write path unless it is current; the report line either way (WriteFailed when it cannot)."""
        if self.is_current(path, data, mode, owner, as_root=as_root):
            return f"unchanged: {path}"
        self._target.write(path, data, mode)
        return f"written:   {path}"

    def install(self, entries: Sequence[InstallEntry], req: InstallRequest) -> Iterator[str]:
        """Bring every entry's dest up to date; one report line each, yielded as it is written.

        Everything is checked first (here, before the iterator is returned):
        a refused entry or a bad template value raises before any write.
        """
        return self._write_all(self._prepare(entries, req), req.as_root)

    def _write_all(self, todo: Sequence[_Prepared], as_root: bool) -> Iterator[str]:
        for p in todo:
            yield self.write_if_changed(p.path, p.data, p.entry.mode.bits, p.entry.owner, as_root=as_root)

    def plan(self, entries: Sequence[InstallEntry], req: InstallRequest) -> tuple[list[str], int]:
        """What install would do, changing nothing: (report lines, number of files to write)."""
        lines: list[str] = []
        todo = 0
        for p in self._prepare(entries, req):
            if self.is_current(p.path, p.data, p.entry.mode.bits, p.entry.owner, as_root=req.as_root):
                lines.append(f"unchanged: {p.path}")
            else:
                lines.append(f"would write: {p.path}")
                todo += 1
        return lines, todo
