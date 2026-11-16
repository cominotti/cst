#!/usr/bin/python3 -I
"""cst installer: writes the files of INSTALL_MANIFEST (cst_core/domain/install.py).

The one way the setup scripts install files (through cst-install.bash).
A python-entry is written as a self-contained zipapp; a `*.in` source with
every @KEY@ replaced (--set KEY=VALUE); the rest as it is. A file is
written only when its content or mode differs, atomically; as root it is
owned by root and relabelled. User files (dest `~/...`) are installed by a
process running as that user, with --home naming the home folder.

Usage:
  installer.py install [OPTIONS] SRC...   unchanged:/written: per file
  installer.py plan [OPTIONS] SRC...      unchanged:/would write: per file; changes nothing
  installer.py check [OPTIONS] SRC...     exit 0 when every file is current, else 1; prints nothing
  installer.py text DEST MODE [--root DIR]
                                          generated content (stdin) to DEST, same rules
  installer.py payload SRC                the bytes install writes for SRC, to stdout
  installer.py dests [--home DIR] [--module M] [--kind K]
                                          the destination of each matching entry, one per line
OPTIONS: --home DIR, --root DIR (a staging folder), --dest PATH (one SRC only),
         --set KEY=VALUE (templates), --module M / --kind K / --owner root|user
         (select entries instead of naming them)
SRC is a path relative to the CST folder, as in the manifest (btrfs/docker-gc).
"""

import os.path as _cst_path
import sys as _cst_sys

# CST bootstrap (the same in every entry point; tests/test_entrypoints.py checks
# it): from a checkout, cst_core and the module packages are in the folder above
# this file; inside an installed zipapp, sys.path already holds the archive.
_cst_root = _cst_path.dirname(_cst_path.dirname(_cst_path.realpath(__file__)))
if not _cst_path.isfile(_cst_path.dirname(__file__)) and _cst_root not in _cst_sys.path:
    _cst_sys.path.insert(0, _cst_root)

import argparse
from collections.abc import Sequence
import os
import sys
from typing import NoReturn

from cst_core.adapters.install import LocalInstallTarget
from cst_core.adapters.runtime import checkout_root
from cst_core.adapters.zipapp import entry_payload
from cst_core.application.install import InstallRefused, InstallRequest, InstallService, staged
from cst_core.domain.install import INSTALL_MANIFEST, ArtifactKind, InstallEntry, Owner, expand_dest, select_entries
from cst_core.domain.values import DomainValidationError, OctalMode

NAME = "cst installer"


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        """Raise instead of printing the usage and exiting."""
        raise _UsageError(message)


def _parser() -> _Parser:
    p = _Parser(prog=NAME, add_help=False)
    p.add_argument("command", choices=["install", "plan", "check", "text", "payload", "dests", "help"])
    p.add_argument("args", nargs="*")
    p.add_argument("--home")
    p.add_argument("--root", default="")
    p.add_argument("--dest")
    p.add_argument("--set", action="append", default=[], dest="values")
    p.add_argument("--module")
    p.add_argument("--kind", choices=["python-entry", "shell", "unit", "data"])
    p.add_argument("--owner", choices=["root", "user"])
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
    srcs: Sequence[str], module: str | None, kind: ArtifactKind | None, owner: Owner | None = None
) -> tuple[InstallEntry, ...]:
    by_src = {e.src: e for e in INSTALL_MANIFEST}
    if srcs and (module or kind or owner):
        raise _UsageError("name the files (SRC...) or select them (--module/--kind/--owner), not both")
    if not srcs:
        if not (module or kind or owner):
            raise _UsageError("no file named")
        return tuple(
            e for e in select_entries(INSTALL_MANIFEST, module=module, kind=kind) if owner is None or e.owner == owner
        )
    unknown = [s for s in srcs if s not in by_src]
    if unknown:
        raise _UsageError(f"not in the install manifest: {', '.join(unknown)}")
    return tuple(by_src[s] for s in srcs)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one installer command; 0 ok, 1 refused or not current, 2 usage."""
    try:
        a = _parser().parse_intermixed_args(list(sys.argv[1:] if argv is None else argv))
        if a.command == "help":
            print(__doc__)
            return 0
        root = checkout_root(__file__)
        if root is None:
            raise _UsageError("runs from a CST checkout only")
        service = InstallService(LocalInstallTarget(), lambda e: entry_payload(root, e))
        if a.command == "text":
            if len(a.args) != 2:
                raise _UsageError("text DEST MODE")
            mode = OctalMode.parse(a.args[1]).bits
            print(service.write_if_changed(staged(a.root, a.args[0]), sys.stdin.buffer.read(), mode))
            return 0
        if a.command == "dests":
            for e in _entries(a.args, a.module, a.kind, a.owner):
                print(expand_dest(e.dest, a.home) if e.dest.startswith("~") and a.home else e.dest)
            return 0
        entries = _entries(a.args, a.module, a.kind, a.owner)
        if a.command == "payload":
            if len(entries) != 1:
                raise _UsageError("payload SRC (one file)")
            sys.stdout.buffer.write(service.desired(entries[0], _values(a.values)))
            return 0
        req = InstallRequest(home=a.home, values=_values(a.values), root=a.root, dest=a.dest, as_root=os.geteuid() == 0)
        if a.command == "install":
            for line in service.install(entries, req):
                print(line, flush=True)
            return 0
        lines, todo = service.plan(entries, req)
        if a.command == "plan":
            print("\n".join(lines))
            return 0
        return 0 if todo == 0 else 1
    except (_UsageError, DomainValidationError, ValueError) as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return 2
    except InstallRefused as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
