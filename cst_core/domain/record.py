# SPDX-License-Identifier: MPL-2.0
"""The install record: what CST installed, where, from which checkout (pure rules, no I/O).

Two JSON files, written by the installer's use case (InstallService) after it
writes or finds current each file: /var/lib/cst/installed.json for root's
files and ~/.local/state/cst/installed.json for a user's, each by the
account that owns the files. Format 1:

    {"format": 1,
     "files": {DEST: {"src": SRC | null, "component": C, "kind": K,
                      "sha256": HEX, "mode": "0644", "owner": "root" | "user",
                      "values": {KEY: VALUE} | null,
                      "cst": {"commit": HEX | "unknown", "tree": HEX}}},
     "components": {C: "pending" | "done"}}

DEST is the destination as the manifest names it, `~/` expanded (never a
staging folder). src is null for generated text; values are a template's
--set values. A component is "pending" from the first write of one of its
files until its setup command finishes and marks it "done" (`installer.py
mark`): a crash in between leaves it pending, which cst doctor reports as a
partial install. "tree" identifies the payloads of the component's files in
that install (sha256 of their sorted `SRC SHA256` lines), "commit" the
checkout's commit when it could be read.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
import json
from typing import Final

from cst_core.domain.contracts import StateFormatKey, state_format
from cst_core.domain.install import Owner
from cst_core.domain.shapes import json_object, parse_json

_FORMAT: Final = state_format(StateFormatKey.INSTALL_RECORD).version
ROOT_RECORD: Final = state_format(StateFormatKey.INSTALL_RECORD).path
# Relative to the user's home.
USER_RECORD_REL: Final = ".local/state/cst/installed.json"


class ComponentState(StrEnum):
    """A component's state in the record: pending from a write until its setup command marks it done."""

    PENDING = "pending"
    DONE = "done"


class RecordFormatError(ValueError):
    """A record that cannot be read as format 1 (newer is True: written by a newer CST)."""

    def __init__(self, message: str, *, newer: bool = False) -> None:
        super().__init__(message)
        self.newer: bool = newer


@dataclass(frozen=True, slots=True)
class RecordedFile:
    """One file as CST installed it."""

    src: str | None
    component: str
    kind: str
    sha256: str
    mode: int
    owner: Owner
    values: Mapping[str, str] | None = None
    commit: str = "unknown"
    tree: str = ""

    def same_install(self, other: RecordedFile) -> bool:
        """The same bytes, mode, owner and origin (the stamp of the checkout aside)."""
        return replace(self, commit="", tree="") == replace(other, commit="", tree="")


@dataclass(frozen=True, slots=True)
class InstallRecord:
    """Every recorded file by destination, and the state of each component."""

    files: Mapping[str, RecordedFile]
    components: Mapping[str, ComponentState]


EMPTY_RECORD: Final = InstallRecord({}, {})

_OWNERS: Final[Mapping[str, Owner]] = {o.value: o for o in Owner}
_STATES: Final[Mapping[str, ComponentState]] = {s.value: s for s in ComponentState}


def _string(d: Mapping[str, object], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise RecordFormatError(f"{key!r} is not a string")
    return v


def _file(d: object) -> RecordedFile:
    if not isinstance(d, dict):
        raise RecordFormatError("a file entry is not an object")
    src = d.get("src")
    if src is not None and not isinstance(src, str):
        raise RecordFormatError("'src' is not a string or null")
    values = d.get("values")
    if values is not None and not (
        isinstance(values, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())
    ):
        raise RecordFormatError("'values' is not an object of strings or null")
    cst = d.get("cst") or {}
    if not isinstance(cst, dict):
        raise RecordFormatError("'cst' is not an object")
    owner = _OWNERS.get(_string(d, "owner"))
    if owner is None:
        raise RecordFormatError("'owner' is neither root nor user")
    try:
        mode = int(_string(d, "mode"), 8)
    except ValueError:
        raise RecordFormatError("'mode' is not an octal number") from None
    return RecordedFile(
        src=src,
        component=_string(d, "component"),
        kind=_string(d, "kind"),
        sha256=_string(d, "sha256"),
        mode=mode,
        owner=owner,
        values=dict(values) if values is not None else None,
        commit=str(cst.get("commit") or "unknown"),
        tree=str(cst.get("tree") or ""),
    )


def parse_record(text: str) -> InstallRecord:
    """The record in text; RecordFormatError when it is not one (newer=True for a newer format)."""
    try:
        doc = parse_json(text)
    except ValueError as e:
        raise RecordFormatError(f"not JSON ({e})") from None
    if not json_object(doc):
        raise RecordFormatError("not a JSON object")
    fmt = doc.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        raise RecordFormatError("no format number")
    if fmt > _FORMAT:
        raise RecordFormatError(f"format {fmt}, written by a newer CST (this one reads format {_FORMAT})", newer=True)
    files = doc.get("files") or {}
    comps = doc.get("components") or {}
    if not isinstance(files, dict) or not isinstance(comps, dict):
        raise RecordFormatError("'files' or 'components' is not an object")
    states: dict[str, ComponentState] = {}
    for name, state in comps.items():
        st = _STATES.get(state) if isinstance(state, str) else None
        if st is None:
            raise RecordFormatError(f"component {name!r}: state {state!r} is neither pending nor done")
        states[str(name)] = st
    return InstallRecord({str(k): _file(v) for k, v in files.items()}, states)


def render_record(rec: InstallRecord) -> str:
    """The record as its file holds it (sorted keys: the same record, the same bytes)."""
    files = {
        dest: {
            "src": f.src,
            "component": f.component,
            "kind": f.kind,
            "sha256": f.sha256,
            "mode": f"{f.mode:04o}",
            "owner": f.owner,
            "values": dict(f.values) if f.values is not None else None,
            "cst": {"commit": f.commit, "tree": f.tree},
        }
        for dest, f in rec.files.items()
    }
    doc = {"format": _FORMAT, "files": files, "components": dict(rec.components)}
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def with_files(rec: InstallRecord, files: Mapping[str, tuple[RecordedFile, bool]]) -> InstallRecord:
    """rec with each {dest: (file, written)} recorded.

    A component becomes "pending" when one of its files was written, or is
    new to the record or recorded differently (an install from before the
    record, adopted): its setup command marks it done when it finishes. A
    file found as recorded leaves its component's state alone, so a second
    run that changes nothing changes nothing in the record either.
    """
    out = dict(rec.files)
    states = dict(rec.components)
    for dest, (f, written) in files.items():
        old = out.get(dest)
        if written or old is None or not old.same_install(f):
            states[f.component] = ComponentState.PENDING
        out[dest] = f
    return InstallRecord(out, states)


def without_files(rec: InstallRecord, dests: list[str]) -> InstallRecord:
    """rec without these destinations; a component with no file left loses its state too."""
    out = {d: f for d, f in rec.files.items() if d not in dests}
    left = {f.component for f in out.values()}
    return InstallRecord(out, {c: s for c, s in rec.components.items() if c in left})


def with_state(rec: InstallRecord, component: str, state: ComponentState) -> InstallRecord:
    """rec with component in state; unchanged when the record has none of its files."""
    if not any(f.component == component for f in rec.files.values()):
        return rec
    return InstallRecord(rec.files, {**rec.components, component: state})


def tree_lines(files: Mapping[str, str]) -> str:
    """The text whose sha256 is a component's "tree": {src: sha256} as sorted `SRC SHA256` lines."""
    return "".join(f"{src} {sha}\n" for src, sha in sorted(files.items()))
