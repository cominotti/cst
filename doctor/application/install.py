# SPDX-License-Identifier: MPL-2.0
"""The install area of `cst doctor`: the files CST installed, against its install records and this checkout."""

from collections.abc import Iterator
from dataclasses import dataclass, field
import os.path
from typing import Final

from cst_core.domain.install import INSTALL_MANIFEST, Component, InstallEntry, Owner, expand_dest, manifest_by_src
from cst_core.domain.record import (
    ROOT_RECORD,
    USER_RECORD_REL,
    ComponentState,
    InstallRecord,
    RecordedFile,
    RecordFormatError,
)
from cst_core.domain.text import YES_WORDS, enum_member
from cst_core.domain.units import LoadState, parse_show_units
from doctor.application.shared import check, refresh, runs
from doctor.domain import (
    GAME_DROPIN,
    GAME_DROPIN_DIR,
    INFO,
    OK,
    SCRUB_UNIT,
    WARN,
    HostPath,
    Result,
    UnitScope,
    ViewPath,
    host_join,
    is_unit_file,
    recorded_unit,
    unit_applies,
    unit_exec_paths,
)
from doctor.ports import CheckFn, DoctorHostPort, InstalledFilesPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []

# The components whose removal command marks them pending before it changes anything (as cst names it): a
# pending state of one of them is an install or a removal that did not finish.
PENDING_REMOVALS: Final = {Component.BTRFS_IOCOST: "cst btrfs iocost revert"}

# What check_units_loaded asks of every unit a manager has loaded, and the reload that loads its files again.
LOADED_UNIT_PROPS: Final = ("Id", "LoadState", "NeedDaemonReload")
# How many of a unit's files check_units_loaded names before ", ...".
FILES_SHOWN: Final = 3
RELOAD_FIX: Final = {UnitScope.SYSTEM: "sudo systemctl daemon-reload", UnitScope.USER: "systemctl --user daemon-reload"}


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


def _pending_result(host: DoctorHostPort, component: str) -> Result:
    """The WARN of a component the record says is pending; for one whose removal marks it pending first
    (PENDING_REMOVALS), it names that removal too, which finishes the other way (the fix stays the install
    command: a fix line is one command)."""
    fix = refresh(host, component)
    known = enum_member(Component, component)
    removal = PENDING_REMOVALS.get(known) if known is not None else None
    if removal is None:
        return Result(
            WARN,
            f"{component}: installed only in part (a file of it was recorded and `{fix or component}` "
            "has not finished since)",
            fix,
        )
    undo = f"{host.cst} {removal.removeprefix('cst ')}"
    return Result(
        WARN,
        f"{component}: installed only in part (a file of it was recorded and `{fix}` has not finished since, "
        f"or `{undo}` began removing it and has not finished since: run `{undo}` again to finish the removal)",
        fix,
    )


def _record_results(
    host: DoctorHostPort, files: InstalledFilesPort, rec: InstallRecord, where: str, home: str
) -> Iterator[Result]:
    """Every finding of one record (home: the user's, for %h in a user's units; "" for root's), then its
    summary lines."""
    out = _InstallFindings()
    for component, state in sorted(rec.components.items()):
        if state == ComponentState.PENDING:
            out.results.append(_pending_result(host, component))
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


def _record_paths(host: DoctorHostPort) -> tuple[tuple[Owner, HostPath], ...]:
    """Root's install record and the user's, with their owners."""
    return ((Owner.ROOT, HostPath(ROOT_RECORD)), (Owner.USER, host_join(host.home, USER_RECORD_REL)))


@check(CHECKS, "install")
def check_install(host: DoctorHostPort, *, installed: InstalledFilesPort | None = None) -> Iterator[Result]:
    """The files CST installed (its install records): missing, changed since, partial installs, older
    than this checkout, units whose programs are gone; without a record, the payload comparison."""
    if installed is None:
        return
    for owner, path in _record_paths(host):
        try:
            rec = installed.record(host.path(path))
        except RecordFormatError as e:
            why = "written by a newer CST: update this checkout (git pull)" if e.newer else "it is no install record"
            yield Result(WARN, f"{path}: {e}; {why}")
            continue
        except PermissionError as e:
            yield Result(INFO, f"{path}: not readable as your user ({e.strerror or e})")
            continue
        except OSError as e:
            # A folder in its place, a read error: every install and removal of CST stops at it, after it has
            # changed its files (the record's update fails). No one command fixes it safely: no fix line.
            yield Result(
                WARN,
                f"{path}: the install record cannot be read ({e.strerror or e}): every CST install and removal "
                "stops at it, after it has changed its files; move it aside",
            )
            continue
        if rec is None:
            yield from _without_record(host, installed, owner)
        else:
            yield from _record_results(host, installed, rec, path, host.home if owner == Owner.USER else "")
            yield from _without_record(host, installed, owner, frozenset(f.component for f in rec.files.values()))


def _recorded_units(host: DoctorHostPort, installed: InstalledFilesPort) -> dict[UnitScope, dict[str, list[str]]]:
    """{manager: {unit name: [the recorded unit files and drop-ins of it]}} of both records. A record that
    is absent, not one or cannot be read gives nothing here: check_install reports it."""
    out: dict[UnitScope, dict[str, list[str]]] = {}
    for owner, path in _record_paths(host):
        try:
            rec = installed.record(host.path(path))
        except RecordFormatError, OSError:
            continue
        if rec is None:
            continue
        home = host.home if owner == Owner.USER else ""
        for dest in sorted(rec.files):
            unit = recorded_unit(dest, home)
            if unit is not None:
                out.setdefault(unit[0], {}).setdefault(unit[1], []).append(dest)
    return out


def _units_not_loaded(host: DoctorHostPort, scope: UnitScope) -> list[str]:
    """The Ids of the units the manager has loaded and not loaded again since one of their files changed
    (NeedDaemonReload yes), sorted. One `systemctl show --state=loaded '*'`: every loaded unit, active or
    inactive, and it loads none (a literal name would load it; a glob without --state=loaded lists only the
    active ones). [] when the manager cannot be asked."""
    user = ["--user"] if scope == UnitScope.USER else []
    cmd = ["systemctl", *user, "show", "--state=loaded", *(a for p in LOADED_UNIT_PROPS for a in ("-p", p)), "*"]
    rc, out, _ = host.run(cmd, timeout=20, locale_c=True)
    if rc != 0:
        return []
    return sorted(
        u["Id"]
        for u in parse_show_units(out)
        if u.get("Id", "") != "" and u.get("LoadState") == LoadState.LOADED and u.get("NeedDaemonReload") in YES_WORDS
    )


def _has_game_dropin(host: DoctorHostPort, unit_id: str) -> bool:
    """The game profile's runtime drop-in of a system unit is there: the profile writes it without a reload on
    purpose, so the unit's NeedDaemonReload says nothing about CST's files (the cpu rows cover it)."""
    return host.isfile(host.path(host_join(GAME_DROPIN_DIR, f"{unit_id}.d", GAME_DROPIN)))


@check(CHECKS, "install")
def check_units_loaded(host: DoctorHostPort, *, installed: InstalledFilesPort | None = None) -> Iterator[Result]:
    """The units of the unit files and drop-ins CST installed (its install records) that systemd has loaded
    and not loaded again since a file of them changed: a change stopped before its daemon-reload, or an
    edit. No systemctl call for a manager CST installed no unit file or drop-in for."""
    if installed is None:
        return
    by_scope = _recorded_units(host, installed)
    if host.exists(host.path(SCRUB_UNIT)):
        # The btrfs area's scrub row warns on its NeedDaemonReload, with what it costs.
        by_scope.get(UnitScope.SYSTEM, {}).pop(os.path.basename(SCRUB_UNIT), None)
    for scope in UnitScope:
        names = by_scope.get(scope, {})
        if not names:
            continue
        for unit_id in _units_not_loaded(host, scope):
            dests = sorted({d for name, ds in names.items() if unit_applies(name, unit_id) for d in ds})
            if not dests or (scope == UnitScope.SYSTEM and _has_game_dropin(host, unit_id)):
                continue
            shown = f"{', '.join(dests[:FILES_SHOWN])}{', ...' if len(dests) > FILES_SHOWN else ''}"
            yield Result(
                WARN,
                f"{unit_id}: systemd has not loaded its files as they are on disk (CST installed {shown}): a "
                "change stopped before its daemon-reload, or an edit; systemd runs it as it loaded it before",
                RELOAD_FIX[scope],
            )
