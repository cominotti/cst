# SPDX-License-Identifier: MPL-2.0
"""The install area of `cst doctor`: the files CST installed, against its install records and this checkout."""

from collections.abc import Iterator
from dataclasses import dataclass, field

from cst_core.domain.install import INSTALL_MANIFEST, InstallEntry, Owner, expand_dest, manifest_by_src
from cst_core.domain.record import (
    ROOT_RECORD,
    USER_RECORD_REL,
    ComponentState,
    InstallRecord,
    RecordedFile,
    RecordFormatError,
)
from doctor.application.shared import check, refresh, runs
from doctor.domain import INFO, OK, WARN, HostPath, Result, ViewPath, host_join, is_unit_file, unit_exec_paths
from doctor.ports import CheckFn, DoctorHostPort, InstalledFilesPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


@dataclass(slots=True)
class _InstallFindings:
    """What one record's files showed: problems as they come, the older files per component."""

    results: list[Result] = field(default_factory=list)
    older: dict[str, list[str]] = field(default_factory=dict)
    fine: int = 0


def _file_findings(
    host: DoctorHostPort, files: InstalledFilesPort, dest: str, f: RecordedFile, home: str, out: _InstallFindings
) -> None:
    """One recorded file: missing, differing from its record (changed by hand, or written by an install that
    could not update the record), not readable, mode or owner, older than this checkout, the programs its
    unit runs."""
    view = host.path(HostPath(dest))
    fix = refresh(host, f.component)
    try:
        st = host.stat(view)
    except FileNotFoundError:
        out.results.append(Result(WARN, f"{dest}: missing (CST installed it, {f.component})", fix))
        return
    except OSError:
        st = None
    try:
        sha: str | None = files.file_sha256(view)
    except OSError:
        sha = None
    problem = False
    if sha is None:
        out.results.append(
            Result(INFO, f"{dest}: not readable as your user (compared the record with this checkout only)")
        )
    elif sha != f.sha256:
        out.results.append(
            Result(
                WARN,
                f"{dest}: differs from its install record ({f.component}): changed by hand, or written by an "
                "install that could not update the record",
                fix,
            )
        )
        problem = True
    if st is not None and st.st_mode & 0o7777 != f.mode:
        out.results.append(Result(INFO, f"{dest}: mode {st.st_mode & 0o7777:04o}, CST installed it {f.mode:04o}"))
        problem = True
    if sha is not None and is_unit_file(dest):
        problem = _unit_findings(host, view, dest, f, home, out) or problem
    if _older(files, f):
        out.older.setdefault(f.component, []).append(dest)
    elif not problem:
        out.fine += 1


def _older(files: InstalledFilesPort, f: RecordedFile) -> bool:
    """The record's bytes differ from what this checkout installs (a manifest file only; a template with
    its recorded values; never a file its module renders itself, such as the sccache wrapper)."""
    entry = manifest_by_src().get(f.src) if f.src is not None and f.src != "" else None
    if entry is None or (f.values is not None and not entry.is_template):
        return False
    want = files.desired_sha256(entry.src, f.values)
    return want is not None and want != f.sha256


def _unit_findings(
    host: DoctorHostPort, view: ViewPath, dest: str, f: RecordedFile, home: str, out: _InstallFindings
) -> bool:
    """WARN for each CST program the unit runs that is not there, or not executable."""
    try:
        with host.open_text(view) as fh:
            text = fh.read(65536)
    except OSError:
        return False
    bad = False
    for prog in unit_exec_paths(text, home):
        if not runs(host, HostPath(prog)):
            out.results.append(
                Result(WARN, f"{dest} runs {prog}, which is missing or not executable", refresh(host, f.component))
            )
            bad = True
    return bad


def _record_results(
    host: DoctorHostPort, files: InstalledFilesPort, rec: InstallRecord, where: str, home: str
) -> Iterator[Result]:
    """Every finding of one record (home: the user's, for %h in a user's units; "" for root's), then its
    summary lines."""
    out = _InstallFindings()
    for component, state in sorted(rec.components.items()):
        if state == ComponentState.PENDING:
            fix = refresh(host, component)
            out.results.append(
                Result(
                    WARN,
                    f"{component}: installed only in part (a file of it was recorded, or a removal of it began, "
                    f"and `{fix or component}` has not finished since)",
                    fix,
                )
            )
    for dest, f in sorted(rec.files.items()):
        _file_findings(host, files, dest, f, home, out)
    yield from out.results
    for component, dests in sorted(out.older.items()):
        yield Result(
            INFO,
            f"{component}: {len(dests)} file(s) older than this checkout ({', '.join(dests[:3])}"
            f"{', ...' if len(dests) > 3 else ''}): {refresh(host, component)} installs them again",
        )
    if out.fine:
        yield Result(OK, f"{out.fine} file(s) as CST installed them ({where})")


def _listed_without_record(e: InstallEntry, owner: Owner, recorded: frozenset[str]) -> bool:
    """An entry of owner's whose component the record does not list, and that this checkout can compare
    without the install's values: no template, not the rustc wrapper toolchains writes itself."""
    return (
        e.owner == owner and e.component not in recorded and not e.is_template and e.src != "toolchains/rustc-wrapper"
    )


def _differing(host: DoctorHostPort, files: InstalledFilesPort, entries: list[InstallEntry]) -> tuple[int, list[str]]:
    """(how many of the entries' files are installed and judged by this checkout, [the installed paths of
    those that differ from it])."""
    found = 0
    differ: list[str] = []
    for e in entries:
        dest = expand_dest(e.dest, host.home)
        try:
            sha = files.file_sha256(host.path(HostPath(dest)))
        except OSError:
            continue
        want = files.desired_sha256(e.src, None)
        if want is None:
            continue  # this checkout cannot say (no checkout, a source that does not build)
        found += 1
        if sha != want:
            differ.append(dest)
    return found, differ


def _without_record(
    host: DoctorHostPort, files: InstalledFilesPort, owner: Owner, recorded: frozenset[str] = frozenset()
) -> Iterator[Result]:
    """The components the record does not list (none: no record; installs from before it): each one's
    installed files against this checkout, as INFO."""
    by_component: dict[str, list[InstallEntry]] = {}
    for e in INSTALL_MANIFEST:
        if _listed_without_record(e, owner, recorded):
            by_component.setdefault(e.component, []).append(e)
    for component, entries in by_component.items():
        found, differ = _differing(host, files, entries)
        if differ:
            yield Result(
                INFO,
                f"{component}: installed before the install record; {len(differ)} of {found} file(s) differ from "
                f"this checkout ({', '.join(differ[:3])}{', ...' if len(differ) > 3 else ''}): "
                f"{refresh(host, component)} installs them again and records them",
            )


@check(CHECKS, "install")
def check_install(host: DoctorHostPort, *, installed: InstalledFilesPort | None = None) -> Iterator[Result]:
    """The files CST installed (its install records): missing, changed since, partial installs, older
    than this checkout, units whose programs are gone; without a record, the payload comparison."""
    if installed is None:
        return
    for owner, path in ((Owner.ROOT, HostPath(ROOT_RECORD)), (Owner.USER, host_join(host.home, USER_RECORD_REL))):
        try:
            rec = installed.record(host.path(path))
        except RecordFormatError as e:
            why = "written by a newer CST: update this checkout (git pull)" if e.newer else "it is no install record"
            yield Result(WARN, f"{path}: {e}; {why}")
            continue
        except OSError as e:
            yield Result(INFO, f"{path}: not readable as your user ({e.strerror or e})")
            continue
        if rec is None:
            yield from _without_record(host, installed, owner)
        else:
            yield from _record_results(host, installed, rec, path, host.home if owner == Owner.USER else "")
            yield from _without_record(host, installed, owner, frozenset(f.component for f in rec.files.values()))
