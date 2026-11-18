# SPDX-License-Identifier: MPL-2.0
"""The files CST writes in the home folder: auto on's copies and units and the
sccache wrapper (through the installer and its record), the settings files
it appends to or rewrites, and the run summaries of the targets.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from cst_core.adapters.files import owned_file_state_at, unit_file_state_at, write_atomic
from cst_core.adapters.install import program_installer
from cst_core.application.install import InstallService, Placement, record_file
from cst_core.domain.files import OwnedState, UnitFileState
from cst_core.domain.install import INSTALL_MANIFEST, expand_dest, manifest_by_src
from toolchains.domain import (
    CONF_REL,
    MARKER,
    OFF_REL,
    OLD_CONF_REL,
    SERVICE,
    STATE_DIR_REL,
    TIMER,
    UNIT_DIR_REL,
    WRAPPER_REL,
)
from toolchains.adapters.program import Program, say


# The component of auto on's files (copies and units); the sccache wrapper's is SCCACHE_COMPONENT.
AUTO_COMPONENT = manifest_by_src()["toolchains/cst-toolchains"].component


SCCACHE_COMPONENT = manifest_by_src()["toolchains/rustc-wrapper"].component


def home_installer() -> InstallService:
    """The installer's use case for the files toolchains writes in the home (its copies, its units,
    the sccache wrapper): written only when they changed, listed in the user's install record."""
    return program_installer(Program.entry_file)


def put_home_file(
    home: str,
    path: str,
    data: bytes,
    mode: int,
    *,
    component: str,
    src: str | None,
    kind: str,
    values: Mapping[str, str] | None = None,
) -> bool:
    """Write path in home unless it is current (the caller checked conflicts and masks first) and list it
    in home's install record; True when it was written."""
    place = Placement(
        path=path,
        dest=path,
        record=record_file("user", home),
        component=component,
        src=src,
        kind=kind,
        owner="user",
        mode=mode,
        values=values,
    )
    return home_installer().put(place, data, as_root=False)


def put_auto_copy(home: str, rel: str, data: bytes) -> bool:
    """One of auto on's copies, ~/rel (its manifest entry gives mode, source and component); True when written."""
    e = next(e for e in INSTALL_MANIFEST if e.dest == "~/" + rel)
    return put_home_file(
        home, expand_dest(e.dest, home), data, e.mode.bits, component=e.component, src=e.src, kind=e.kind
    )


def put_auto_unit(home: str, name: str, text: str) -> bool:
    """One of auto on's user units (generated text); True when written."""
    path = os.path.join(home, UNIT_DIR_REL, name)
    return put_home_file(home, path, text.encode(), 0o644, component=AUTO_COMPONENT, src=None, kind="text")


def forget_auto_files(home: str) -> None:
    """auto off removed its files: the copies and the two units leave home's install record."""
    copies = [expand_dest(e.dest, home) for e in INSTALL_MANIFEST if e.component == AUTO_COMPONENT]
    forget_home_files(home, [*copies, *(os.path.join(home, UNIT_DIR_REL, n) for n in (SERVICE, TIMER))])


def put_wrapper(home: str, path: str, text: str, values: Mapping[str, str]) -> bool:
    """The sccache wrapper (rustc-wrapper with the settings written in) through the installer, listed with
    those values, its component marked done; True when written."""
    src = "toolchains/rustc-wrapper"
    e = manifest_by_src()[src]
    written = put_home_file(
        home, path, text.encode(), e.mode.bits, component=SCCACHE_COMPONENT, src=src, kind=e.kind, values=values
    )
    record_done(home, SCCACHE_COMPONENT)
    return written


def record_done(home: str, component: str) -> None:
    """component's command finished: home's install record says it is complete."""
    home_installer().mark(record_file("user", home), component)


def forget_home_files(home: str, paths: Sequence[str]) -> None:
    """These files of home were removed: its install record no longer lists them."""
    home_installer().forget(record_file("user", home), paths)


def state_path(home: str, tid: str) -> str:
    return os.path.join(home, STATE_DIR_REL, f"toolchains-{tid.replace(':', '-')}.json")


def write_state(home: str, tid: str, data: Mapping[str, Any]) -> None:
    path = state_path(home, tid)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 0600: what the temporary file it used to rename into place had.
        write_atomic(path, json.dumps(data), mode=0o600, durable=False, relabel=False)
    except OSError as e:
        say(f"{tid}: could not save the run summary: {e}", 4)


def read_state(home: str, tid: str) -> dict[str, Any] | None:
    try:
        with open(state_path(home, tid)) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except OSError, ValueError:
        return None


def file_state(path: str, wanted: str | bytes | None, marker: str = MARKER) -> OwnedState:
    """ok | change (ours: it holds MARKER, and differs) | conflict (not ours) | missing.

    Every file here is in the user's home: a symbolic link is judged by what
    it points to (cst_core.domain.install.link_read_through), and a write
    replaces the link itself."""
    want = wanted.encode() if isinstance(wanted, str) else wanted
    return owned_file_state_at(path, want, lambda data: marker.encode() in data, owner="user")


def timer_unit_state(path: str, wanted: str) -> UnitFileState:
    """file_state of a unit file `auto on` writes: also "masked" when
    `systemctl --user mask` left a link to /dev/null there (see
    cst_core.adapters.files.unit_file_state_at)."""
    return unit_file_state_at(path, wanted.encode(), lambda data: MARKER.encode() in data, owner="user")


def old_conf_note(home: str) -> None:
    if os.path.lexists(os.path.join(home, OLD_CONF_REL)):
        say(
            f"note: ~/{OLD_CONF_REL} is no longer read: put its settings into ~/{CONF_REL} "
            "with the RUST_BUILD_ prefix (e.g. RUST_BUILD_MAX_IDLE=48h), then remove it",
            4,
            err=True,
        )


def turned_off(home: str) -> bool:
    return os.path.lexists(os.path.join(home, OFF_REL))


def read_cfg(path: str) -> str | None:
    """Text of a config file; '' when missing; None when it cannot be read."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def wrapper_path(home: str) -> str:
    return os.path.join(home, WRAPPER_REL)


def write_cfg(path: str, text: str) -> str:
    """Replace a config file's text (atomic, same mode). '' or the error."""
    real = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except OSError:
        mode = 0o644
    try:
        write_atomic(real, text, mode=mode)
    except OSError as e:
        return str(e.strerror or e)
    return ""


def append_text(path: str, text: str) -> str:
    """Append TEXT to PATH (made if missing); a file not ending in a newline
    gets one first. Returns '' or the error."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "ab") as f:
            # A file not ending in a newline would glue the comment onto its last line.
            if f.tell() > 0:
                with open(path, "rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        f.write(b"\n")
            f.write(text.encode("utf-8"))
    except OSError as e:
        return str(e.strerror or e)
    return ""
