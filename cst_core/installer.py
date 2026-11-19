#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""cst installer: writes the files of INSTALL_MANIFEST (cst_core/domain/install.py).

The one way the setup scripts install files (through cst-install.bash).
A python-entry is written as a self-contained zipapp; a `*.in` source with
every @KEY@ replaced (--set KEY=VALUE); the rest as it is. A file is
written only when its content or mode differs, atomically; as root it is
owned by root and relabelled. User files (dest `~/...`) are installed by a
process running as that user, with --home naming the home folder.

Usage:
  installer.py install [OPTIONS] SRC...   unchanged:/written: per file, as it goes
  installer.py plan [OPTIONS] SRC...      unchanged:/would write: per file (would record: for a
                                          current file the install record does not list yet,
                                          or lists otherwise); changes nothing
  installer.py check [OPTIONS] SRC...     exit 0 when every file is current (also one only the
                                          record lacks), else 1; prints nothing
  installer.py text DEST MODE [--root DIR] [--component C]
                                          generated content (stdin) to DEST, same rules; with
                                          --component, listed in the install record under C
  installer.py remove [OPTIONS] SRC...    removes the files (a link itself) and their record lines:
                                          removed: per file that was there
  installer.py remove-text DEST... [--home DIR] [--root DIR]
                                          the same for generated files (--home: run as a user,
                                          the record in that home)
  installer.py forget [OPTIONS] SRC...    drops the files' record lines and leaves the files as they
                                          are (a file a setup script keeps); prints nothing
  installer.py forget-text DEST... [--home DIR] [--root DIR]
                                          the same for generated files
  installer.py mark --component C done    C's setup command finished (the record says done; a
                                          component stays "pending" from a write until then)
  installer.py text-plan DEST MODE [--root DIR] [--component C]
                                          unchanged:/would write: for that content (with --component,
                                          would record: for a current file the record does not
                                          list so, as text would); changes nothing
  installer.py payload SRC                the bytes install writes for SRC, to stdout
  installer.py dests [--home DIR] [--module M] [--kind K] [--component C]
                                          the destination of each matching entry, one per line
  installer.py srcs [--module M] [--kind K] [--component C]
                                          the source (in the CST folder) of each matching entry
OPTIONS: --home DIR, --root DIR (a staging folder, an absolute path),
         --set KEY=VALUE (templates), --module M / --kind K / --owner root|user /
         --component C (select entries instead of naming them; C as in
         COMPONENTS, e.g. btrfs:core: every file one command installs;
         remove and forget refuse a C of GENERATED_COMPONENTS, whose files
         are generated text: name them, remove-text or forget-text DEST...)
SRC is a path relative to the CST folder, as in the manifest (btrfs/docker-gc).
Every file install writes or finds current is listed in the install record
(/var/lib/cst/installed.json for root's files, ~/.local/state/cst/installed.json
for the user's, under --root when staged): see cst_core/domain/record.py.
A @KEY@ of a template without a --set value, or a --set KEY no template of the
call uses, is a usage error (exit 2) and nothing is written. Programs are
written before units; as root on the real root (no --root), the units an
install changes are first checked with `systemd-analyze verify
--recursive-errors=no` on copies under their own names (drop-ins and
template units are not), and when that fails none of them is written (exit 1,
the lines about them on stderr; a note when systemd-analyze is missing). A file that
cannot be written ends the run with "cst installer: cannot write DEST: REASON"
(exit 1), after the lines of the files written before it.
Before a program of SETTINGS_STEPS (cst_core/domain/settings_steps.py: btrfs/docker-gc)
is replaced by install or deleted by remove, while its settings file is missing
(under --root when staged), the checkout's copy of it writes that file from
the installed copy (docker-gc: `settings --write`, the registries an older
docker-gc had built in); plan lists that file as "would write:". When it
fails (exit 78: an installed program it does not know) nothing is changed
(exit 1, its lines on stderr).
"""

import os.path as _cst_path
import sys as _cst_sys

# CST bootstrap (the same in every entry point; tests/test_entrypoints.py checks
# it): from a checkout, cst_core and the module packages are in the folder above
# this file, and Python writes no bytecode there (no __pycache__ in the checkout,
# even for a program run by its shebang as root); inside an installed zipapp,
# sys.path already holds the archive.
_cst_root = _cst_path.dirname(_cst_path.dirname(_cst_path.realpath(__file__)))
if not _cst_path.isfile(_cst_path.dirname(__file__)) and _cst_root not in _cst_sys.path:
    _cst_sys.dont_write_bytecode = True
    _cst_sys.path.insert(0, _cst_root)

import argparse
from collections.abc import Iterable, Sequence
from enum import StrEnum
import sys
from typing import NoReturn, override

from cst_core import wiring
from cst_core.application.install import (
    InstallRefused,
    InstallRequest,
    InstallService,
    Placement,
    record_file,
    staged,
)
from cst_core.domain.install import (
    COMPONENTS,
    GENERATED_COMPONENTS,
    INSTALL_MANIFEST,
    ArtifactKind,
    GeneratedKind,
    InstallEntry,
    Owner,
    expand_dest,
    select_entries,
)
from cst_core.domain.values import DomainValidationError, OctalMode
from cst_core.ports import WriteFailed

NAME = "cst installer"


class _UsageError(Exception):
    pass


class InstallerCommand(StrEnum):
    """The installer's commands (its first argument), in the order its usage lists them."""

    INSTALL = "install"
    PLAN = "plan"
    CHECK = "check"
    TEXT = "text"
    TEXT_PLAN = "text-plan"
    PAYLOAD = "payload"
    DESTS = "dests"
    SRCS = "srcs"
    REMOVE = "remove"
    REMOVE_TEXT = "remove-text"
    FORGET = "forget"
    FORGET_TEXT = "forget-text"
    MARK = "mark"
    HELP = "help"


class _Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        """Raise instead of printing the usage and exiting."""
        raise _UsageError(message)


def _parser() -> _Parser:
    p = _Parser(prog=NAME, add_help=False)
    p.add_argument(
        "command",
        choices=[c.value for c in InstallerCommand],
    )
    p.add_argument("args", nargs="*")
    p.add_argument("--home")
    p.add_argument("--root", default="")
    p.add_argument("--set", action="append", default=[], dest="values")
    p.add_argument("--module")
    p.add_argument("--kind", choices=[k.value for k in ArtifactKind])
    p.add_argument("--owner", choices=[o.value for o in Owner])
    p.add_argument("--component")
    return p


def _values(pairs: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise _UsageError(f"--set {pair!r}: expected KEY=VALUE")
        out[key] = value
    return out


def _entries(
    srcs: Sequence[str],
    module: str | None,
    kind: ArtifactKind | None,
    owner: Owner | None = None,
    component: str | None = None,
) -> tuple[InstallEntry, ...]:
    by_src = {e.src: e for e in INSTALL_MANIFEST}
    named = (module is not None and module != "") or (component is not None and component != "")
    selected = named or kind is not None or owner is not None
    if srcs and selected:
        raise _UsageError("name the files (SRC...) or select them (--module/--kind/--owner/--component), not both")
    if component is not None and component not in COMPONENTS:
        raise _UsageError(f"--component {component!r}: no such component (known: {', '.join(COMPONENTS)})")
    if not srcs:
        if not selected:
            raise _UsageError("no file named")
        return tuple(
            e
            for e in select_entries(INSTALL_MANIFEST, module=module, kind=kind, component=component)
            if owner is None or e.owner == owner
        )
    unknown = [s for s in srcs if s not in by_src]
    if unknown:
        raise _UsageError(f"not in the install manifest: {', '.join(unknown)}")
    return tuple(by_src[s] for s in srcs)


def _own() -> tuple[Owner, str, bool]:
    """Who this process writes as: root (root's record) or a user (their record, in their home)."""
    as_root, home = wiring.installer_identity()
    return (Owner.ROOT if as_root else Owner.USER), home, as_root


def _text_place(dest: str, mode: int, component: str | None, a: argparse.Namespace, *, recorded: bool) -> Placement:
    """Where generated text goes (staged under --root), and its record (when recorded)."""
    owner, home, _ = _own()
    return Placement(
        path=staged(a.root, dest),
        dest=dest,
        record=record_file(owner, a.home or home, a.root) if recorded else "",
        component=component or "",
        src=None,
        kind=GeneratedKind.TEXT,
        owner=owner,
        mode=mode,
    )


def _text(service: InstallService, a: argparse.Namespace) -> str:
    """text (write) or text-plan (only judge) generated content from stdin; the report line."""
    if len(a.args) != 2:
        raise _UsageError(f"{a.command} DEST MODE")
    if a.component is not None and a.component not in COMPONENTS:
        raise _UsageError(f"--component {a.component!r}: no such component")
    mode = OctalMode.parse(a.args[1]).bits
    # Generated text as root goes to a root destination; as a user (cst_install_text DEST MODE USER), to theirs.
    place = _text_place(a.args[0], mode, a.component, a, recorded=a.component is not None)
    as_root = place.owner == Owner.ROOT
    data = sys.stdin.buffer.read()
    if a.command == InstallerCommand.TEXT:
        written = service.put(place, data, as_root=as_root)
        return f"{'written:  ' if written else 'unchanged:'} {place.path}"
    return service.plan_put(place, data, as_root=as_root)


def _component_owner(component: str) -> Owner:
    """Whose record lists the component: its manifest files' owner, or GENERATED_COMPONENTS' for one whose
    files are all generated text."""
    owners = {e.owner for e in INSTALL_MANIFEST if e.component == component}
    return owners.pop() if owners else GENERATED_COMPONENTS[component]


def _mark(service: InstallService, a: argparse.Namespace) -> int:
    """mark --component C done: the record of C's owner says done."""
    if a.args != ["done"] or a.component not in COMPONENTS:
        raise _UsageError("mark --component C done")
    owner = _component_owner(a.component)
    _, home, as_root = _own()
    if owner == Owner.USER and as_root and not a.root:
        raise InstallRefused(f"{a.component}: belongs to the user; mark it as the user, not as root")
    service.mark(record_file(owner, a.home or home, a.root), a.component)
    return 0


def _listing(command: str, entries: Sequence[InstallEntry], home: str | None) -> int:
    """dests or srcs: one line per entry."""
    for e in entries:
        if command == InstallerCommand.SRCS:
            print(e.src)
        else:
            print(expand_dest(e.dest, home) if e.dest.startswith("~") and home is not None and home != "" else e.dest)
    return 0


def _stream(lines: Iterable[str]) -> int:
    """Print each report line as it comes (a later failure still leaves the earlier ones on screen)."""
    for line in lines:
        print(line, flush=True)
    return 0


def _remove_or_forget(service: InstallService, command: str, places: Sequence[Placement]) -> int:
    """remove or remove-text: the files and their record lines ('removed:' per file that was there);
    forget or forget-text: only the record lines dropped, the files left alone."""
    if command not in (InstallerCommand.FORGET, InstallerCommand.FORGET_TEXT):
        return _stream(service.remove(places))
    for record in sorted({p.record for p in places if p.record}):
        service.forget(record, [p.dest for p in places if p.record == record])
    return 0


def _files(service: InstallService, a: argparse.Namespace, entries: Sequence[InstallEntry]) -> int:
    """install, remove, forget, plan or check of manifest entries."""
    req = InstallRequest(home=a.home, values=_values(a.values), root=a.root, as_root=_own()[2])
    if a.command in (InstallerCommand.REMOVE, InstallerCommand.FORGET):
        if any(e.owner == Owner.USER for e in entries) and req.as_root and not req.root:
            how = (
                "removed as the user (cst_remove_as USER)"
                if a.command == InstallerCommand.REMOVE
                else "forgotten as the user"
            )
            raise InstallRefused(f"files of the user are {how}, not as root")
        return _remove_or_forget(service, a.command, [service.placement(e, req) for e in entries])
    if a.command == InstallerCommand.INSTALL:
        return _stream(service.install(entries, req))
    lines, todo = service.plan(entries, req)
    if a.command == InstallerCommand.PLAN:
        print("\n".join(lines))
        return 0
    return 0 if todo == 0 else 1


def _refuse_generated(a: argparse.Namespace) -> None:
    """remove or forget --component C of a component whose files are all generated text (GENERATED_COMPONENTS):
    no manifest file names them, so the call would select nothing and say nothing; a usage error that names
    the DEST form (the paths its setup script generates; never paths read from a record a user can edit)."""
    if a.command not in (InstallerCommand.REMOVE, InstallerCommand.FORGET) or a.component not in GENERATED_COMPONENTS:
        return
    text = InstallerCommand.REMOVE_TEXT if a.command == InstallerCommand.REMOVE else InstallerCommand.FORGET_TEXT
    as_user = " --home DIR, as the user" if GENERATED_COMPONENTS[a.component] == Owner.USER else ""
    raise _UsageError(
        f"{a.command} --component {a.component}: its files are generated text, not manifest files; "
        f"name them: {text} DEST...{as_user}"
    )


def _command(service: InstallService, a: argparse.Namespace) -> int:
    """One command of the parsed command line; its exit status."""
    if a.command in (InstallerCommand.TEXT, InstallerCommand.TEXT_PLAN):
        print(_text(service, a))
        return 0
    if a.command == InstallerCommand.MARK:
        return _mark(service, a)
    if a.command in (InstallerCommand.REMOVE_TEXT, InstallerCommand.FORGET_TEXT):
        if not a.args:
            raise _UsageError(f"{a.command} DEST...")
        return _remove_or_forget(service, a.command, [_text_place(d, 0, None, a, recorded=True) for d in a.args])
    _refuse_generated(a)
    entries = _entries(a.args, a.module, a.kind, a.owner, a.component)
    if a.command in (InstallerCommand.DESTS, InstallerCommand.SRCS):
        return _listing(a.command, entries, a.home)
    if a.command == InstallerCommand.PAYLOAD:
        if len(entries) != 1:
            raise _UsageError("payload SRC (one file)")
        sys.stdout.buffer.write(service.desired(entries[0], _values(a.values)))
        return 0
    return _files(service, a, entries)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one installer command; 0 ok, 1 refused or not current, 2 usage."""
    # A reader that stops early (`cmp -s <(installer.py payload ...)`) ends this quietly, as it ended cp or cat;
    # the commands it runs get the caller's LC_CTYPE (cst-install.bash hands it over).
    wiring.start_installer()
    try:
        a = _parser().parse_intermixed_args(list(sys.argv[1:] if argv is None else argv))
        if a.command == InstallerCommand.HELP:
            print(__doc__)
            return 0
        service = wiring.install_service()
        if service is None:
            raise _UsageError("runs from a CST checkout only")
        return _command(service, a)
    except (_UsageError, DomainValidationError, ValueError) as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return 2
    except (InstallRefused, WriteFailed) as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return 1
    except OSError as e:  # a source file of the checkout (writes raise WriteFailed)
        print(f"{NAME}: cannot read {e.filename or 'a source file'}: {e.strerror or e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
