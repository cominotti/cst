# SPDX-License-Identifier: MPL-2.0
"""Install use case: write manifest files (and generated text) only when they changed, and record them.

Output lines are the ones the setup scripts always printed:
`unchanged: DEST` and `written:   DEST`; a plan prints
`would write: DEST` instead and changes nothing; a removal
`removed:   DEST`. Every entry's bytes are built and checked (templates)
before the first write, so a bad call writes nothing; then one line comes
per file, as it is written.

Every file written or found current is listed in the install record of its
owner (cst_core.domain.record): what it came from, its sha256, mode and
component, and which checkout installed it. A file found current that the
record did not list yet (installed before the record existed) is adopted.
A component is "pending" from a write until its setup command marks it
done (mark).
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import itertools
import posixpath

from cst_core.domain.errors import Refused
from cst_core.domain.files import checked_path
from cst_core.domain.install import (
    InstallEntry,
    Owner,
    expand_dest,
    is_current,
    render_template,
    template_keys,
)
from cst_core.domain.settings_steps import SETTINGS_STEPS, SettingsStep
from cst_core.domain.record import (
    ROOT_RECORD,
    USER_RECORD_REL,
    ComponentState,
    InstallRecord,
    RecordedFile,
    RecordFormatError,
    tree_lines,
    with_files,
    with_state,
    without_files,
)
from cst_core.domain.values import DomainValidationError
from cst_core.ports import InstallRecordPort, InstallTargetPort, SettingsStepPort, UnitVerifierPort

# The bytes install writes for an entry (a zipapp for a python-entry, the
# file for the rest), before template rendering.
type PayloadFn = Callable[[InstallEntry], bytes]


class InstallRefused(Refused):
    """An install that must not run as asked (wrong user, bad arguments, a record of a newer CST).

    A Refused: a module that writes through InstallService (swap, vm, toolchains) reports it as its
    own refusals."""


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """How to place the entries: home for `~/` dests, values for templates, a staging root."""

    home: str | None = None
    values: Mapping[str, str] | None = None
    root: str = ""
    as_root: bool = False


@dataclass(frozen=True, slots=True)
class Placement:
    """Where one file is written, and how the install record lists it."""

    path: str  # where it is written (under a staging root, or where a module's tests put it)
    dest: str  # its destination as the record lists it (`~/` expanded, never staged)
    record: str  # the record file that lists it ("": none)
    component: str
    src: str | None  # the manifest source; None for generated text
    kind: str
    owner: Owner
    mode: int
    values: Mapping[str, str] | None = None


def staged(root: str, path: str) -> str:
    """`path` under the staging folder `root` ('' = the real root); both plain absolute paths."""
    checked_path(path)
    if not root:
        return path
    return checked_path(root.rstrip("/") or "/").rstrip("/") + path


def record_file(owner: Owner, home: str | None, root: str = "") -> str:
    """The install record of root's files, or of the user's whose home this is (staged under root)."""
    if owner == "root":
        return staged(root, ROOT_RECORD)
    if not home:
        raise ValueError("a home folder is needed for the user's install record")
    return staged(root, home.rstrip("/") + "/" + USER_RECORD_REL)


class InstallService:
    """Writes manifest entries and generated text through an InstallTargetPort, and records them
    through an InstallRecordPort (None: nothing is recorded; a module's tests)."""

    def __init__(
        self,
        target: InstallTargetPort,
        payload: PayloadFn,
        *,
        record: InstallRecordPort | None = None,
        commit: str = "unknown",
        verifier: UnitVerifierPort | None = None,
        steps: SettingsStepPort | None = None,
    ) -> None:
        self._target = target
        self._payload = payload
        self._record = record
        self._commit = commit
        self._verifier = verifier
        self._steps = steps

    def desired(self, entry: InstallEntry, values: Mapping[str, str] | None = None) -> bytes:
        """The bytes the entry's dest must hold (DomainValidationError: a template value missing or unused)."""
        return self._render([entry], values)[0][0]

    def _render(
        self, entries: Sequence[InstallEntry], values: Mapping[str, str] | None
    ) -> list[tuple[bytes, Mapping[str, str] | None]]:
        """Every entry's bytes, and a template's values (the --set values it uses); a --set value no
        template of the call uses is an error (a misspelt KEY)."""
        values = values or {}
        used: set[str] = set()
        out: list[tuple[bytes, Mapping[str, str] | None]] = []
        for e in entries:
            data = self._payload(e)
            mine: Mapping[str, str] | None = None
            if e.is_template:
                text = data.decode("utf-8")
                keys = template_keys(text)
                used |= keys
                try:
                    data = render_template(text, values).encode("utf-8")
                except DomainValidationError as exc:
                    raise DomainValidationError(f"{e.src}: {exc}") from None
                mine = {k: values[k] for k in sorted(keys)}
            out.append((data, mine))
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

    def placement(self, entry: InstallEntry, req: InstallRequest, values: Mapping[str, str] | None = None) -> Placement:
        """Where the entry is written for this request, and its line in the record (values: a template's)."""
        dest = expand_dest(entry.dest, req.home)
        return Placement(
            path=staged(req.root, dest),
            dest=dest,
            record=record_file(entry.owner, req.home, req.root) if self._record else "",
            component=entry.component,
            src=entry.src,
            kind=entry.kind,
            owner=entry.owner,
            mode=entry.mode.bits,
            values=values,
        )

    def _check(self, entries: Sequence[InstallEntry], req: InstallRequest) -> None:
        for entry in entries:
            if entry.owner == "user" and req.as_root and not req.root:
                raise InstallRefused(
                    f"{entry.src}: belongs to the user; install it as the user (cst_install_as USER), not as root"
                )

    def _prepare(self, entries: Sequence[InstallEntry], req: InstallRequest) -> list[tuple[Placement, bytes]]:
        self._check(entries, req)
        for e in entries:
            self.destination(e, req)  # every destination checked before any payload is built
        places = [
            (self.placement(e, req, values), data)
            for e, (data, values) in zip(entries, self._render(entries, req.values), strict=True)
        ]
        self._check_records({p.record for p, _ in places})
        return places

    def _check_records(self, records: set[str]) -> None:
        """A record of a newer CST stops the call before any write (it is never rewritten by an older one)."""
        if self._record is None:
            return
        for path in sorted(r for r in records if r):
            try:
                self._record.read(path)
            except RecordFormatError as e:
                if e.newer:
                    raise InstallRefused(f"{path}: {e}: nothing was changed") from None
            except OSError:
                continue  # unreadable here: the update below says why, after the files

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
        a refused entry, a bad template value or a record of a newer CST
        raises before any write. Programs are written before units (a unit
        is never there before what it runs). The record is updated after
        the last file, and also when a write fails (with the files before it).
        """
        todo = self._prepare(entries, req)
        todo.sort(key=lambda pd: pd[0].kind == "unit")
        # Only a program with a SettingsStep is read here (is_current reads the whole file).
        replaced = [
            p
            for p, data in todo
            if (p.src or "") in SETTINGS_STEPS
            and not self.is_current(p.path, data, p.mode, p.owner, as_root=req.as_root)
        ]
        done = self._settings_steps(replaced)
        return itertools.chain(done, self._write_all(todo, req))

    def _due_steps(self, places: Sequence[Placement]) -> list[tuple[Placement, SettingsStep, str]]:
        """The places with a SettingsStep whose settings file is missing, with that step and that file:
        staged under the same root as the place (its path is that root followed by its dest)."""
        if self._steps is None:
            return []
        due: list[tuple[Placement, SettingsStep, str]] = []
        for p in places:
            step = SETTINGS_STEPS.get(p.src or "")
            if step is None or not p.path.endswith(p.dest):
                continue
            settings = staged(p.path.removesuffix(p.dest), step.settings_file)
            if self._target.current(settings) is None:
                due.append((p, step, settings))
        return due

    def _settings_steps(self, places: Sequence[Placement]) -> list[str]:
        """Run the SettingsStep of each place about to be replaced or removed while its settings file
        is missing; their `written:` lines. A step that fails raises InstallRefused: nothing else changed."""
        lines: list[str] = []
        runner = self._steps
        if runner is None:
            return lines
        for p, step, settings in self._due_steps(places):
            r = runner.run(p.src or "", step.args, {step.env_program: p.path, step.env_file: settings})
            out = [line for line in r.stdout.splitlines() if line.strip()]
            if r.returncode != 0:
                raise InstallRefused(
                    f"{p.path}: its settings {settings} could not be written from it (exit {r.returncode}); "
                    "nothing was changed" + "".join(f"\n  {line}" for line in out)
                )
            lines += [line for line in out if line.startswith("written:")]
        return lines

    def _write_all(self, todo: Sequence[tuple[Placement, bytes]], req: InstallRequest) -> Iterator[str]:
        trees = self._trees(todo)
        done: list[tuple[Placement, bytes, bool]] = []
        verified = False
        try:
            for p, data in todo:
                if p.kind == "unit" and not verified:
                    # The programs are in place: the units that change are verified before any is written.
                    verified = True
                    note = self._verify_units(todo, req)
                    if note:
                        yield f"note:      {note}"
                line = self.write_if_changed(p.path, data, p.mode, p.owner, as_root=req.as_root)
                done.append((p, data, line.startswith("written")))
                yield line
        finally:
            self._save(done, trees)

    def _verify_units(self, todo: Sequence[tuple[Placement, bytes]], req: InstallRequest) -> str:
        """systemd-analyze verify of the units this install changes, as root on the real root only;
        InstallRefused (nothing of them written) when they fail; the verifier's note, if any.

        Not verified: drop-ins (a NAME.d/ file is no unit of its own), templates (NAME@.service: only
        an instance can be verified), and the units of a staged install (--root: the programs they
        run are not where systemd looks)."""
        if self._verifier is None or not req.as_root or req.root:
            return ""
        units = {
            posixpath.basename(p.dest): data
            for p, data in todo
            if p.kind == "unit"
            and not posixpath.dirname(p.dest).endswith(".d")
            and "@." not in posixpath.basename(p.dest)
            and not self.is_current(p.path, data, p.mode, p.owner, as_root=req.as_root)
        }
        verdict = self._verifier.verify(units)
        if not verdict.ok:
            raise InstallRefused(
                f"systemd-analyze verify failed, none of these units written: {', '.join(sorted(units))}"
                + "".join(f"\n  {line}" for line in verdict.lines)
            )
        return verdict.note

    def _trees(self, todo: Sequence[tuple[Placement, bytes]]) -> dict[str, str]:
        if self._record is None:
            return {}
        by_component: dict[str, dict[str, str]] = {}
        for p, data in todo:
            by_component.setdefault(p.component, {})[p.src or p.dest] = self._record.sha256(data)
        return {c: self._record.sha256(tree_lines(f).encode()) for c, f in by_component.items()}

    def _save(self, done: Sequence[tuple[Placement, bytes, bool]], trees: Mapping[str, str]) -> None:
        """Record what was written or found current, one update per record file."""
        if self._record is None:
            return
        per_record: dict[str, dict[str, tuple[RecordedFile, bool]]] = {}
        for p, data, written in done:
            if not p.record:
                continue
            sha = self._record.sha256(data)
            f = RecordedFile(
                p.src, p.component, p.kind, sha, p.mode, p.owner, p.values, self._commit, trees.get(p.component, sha)
            )
            per_record.setdefault(p.record, {})[p.dest] = (f, written)
        for path, files in sorted(per_record.items()):
            self._update(path, _adding(files))

    def _update(self, path: str, change: Callable[[InstallRecord], InstallRecord]) -> None:
        if self._record is None:
            return
        try:
            self._record.update(path, change)
        except RecordFormatError as e:
            raise InstallRefused(f"{path}: {e}: not changed") from None

    def put(self, place: Placement, data: bytes, *, as_root: bool) -> bool:
        """Write one file a module checked itself (its own refusals first), unless current; record it.
        True when it was written."""
        self._check_records({place.record})
        written = self.write_if_changed(place.path, data, place.mode, place.owner, as_root=as_root).startswith(
            "written"
        )
        self._save([(place, data, written)], {})
        return written

    def mark(self, record: str, component: str, state: ComponentState = "done") -> None:
        """The component's state in the record (done: its setup command finished)."""
        if record:
            self._update(record, lambda rec: with_state(rec, component, state))

    def forget(self, record: str, dests: Sequence[str]) -> None:
        """Drop these destinations from the record (their files were removed by their module)."""
        if record and dests:
            self._update(record, lambda rec: without_files(rec, list(dests)))

    def remove(self, places: Sequence[Placement]) -> Iterator[str]:
        """Remove each file (a link itself) and its line in the record; `removed:   PATH` for each that
        was there. Works without a record (an install from before it): the files are the manifest's.
        A program with a SettingsStep that is there runs it first, while its settings file is missing."""
        yield from self._settings_steps(
            [p for p in places if (p.src or "") in SETTINGS_STEPS and self._target.current(p.path) is not None]
        )
        gone: list[Placement] = []
        try:
            for p in places:
                existed = self._target.remove(p.path)
                gone.append(p)
                if existed:
                    yield f"removed:   {p.path}"
        finally:
            for record in sorted({p.record for p in gone if p.record}):
                self.forget(record, [p.dest for p in gone if p.record == record])

    def plan(self, entries: Sequence[InstallEntry], req: InstallRequest) -> tuple[list[str], int]:
        """What install would do, changing nothing: (report lines, number of files to write)."""
        lines: list[str] = []
        replaced: list[Placement] = []
        for p, data in self._prepare(entries, req):
            if self.is_current(p.path, data, p.mode, p.owner, as_root=req.as_root):
                lines.append(f"unchanged: {p.path}")
            else:
                lines.append(f"would write: {p.path}")
                replaced.append(p)
        # A settings file install would write first, from the program it replaces (SettingsStep).
        settings = [
            f"would write: {path} (from {p.path}, before it is replaced)" for p, _, path in self._due_steps(replaced)
        ]
        return settings + lines, len(replaced) + len(settings)


def _adding(files: Mapping[str, tuple[RecordedFile, bool]]) -> Callable[[InstallRecord], InstallRecord]:
    """The record change that lists these files (with_files)."""
    return lambda rec: with_files(rec, files)
