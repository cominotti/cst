# SPDX-License-Identifier: MPL-2.0
"""The files CST writes in the home folder: auto on's copies and units and the
sccache wrapper (through the installer and its record), the settings files
it appends to or rewrites, and the run summaries of the targets.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from toolchains.ports import RunOutputPort

from cst_core.adapters.files import owned_file_state_at, unit_file_state_at, write_atomic
from cst_core.adapters.install import program_installer
from cst_core.application.install import InstallService, Placement, record_file
from cst_core.domain.errors import Refused
from cst_core.domain.files import OwnedState, UnitFileState
from cst_core.domain.install import INSTALL_MANIFEST, expand_dest, manifest_by_src
from cst_core.domain.values import Json
from toolchains.adapters.program import program_folder
from toolchains.domain.common import (
    CONF_REL,
    DEFAULTS_NAME,
    MARKER,
    OFF_REL,
    OLD_CONF_REL,
    SERVICE,
    STATE_DIR_REL,
    TIMER,
    UNIT_DIR_REL,
)
from toolchains.domain.config import WRAPPER_REL
from toolchains.domain.prune import RunRecord, Settings, checked_settings, parse_defaults


# The component of auto on's files (copies and units); the sccache wrapper's is SCCACHE_COMPONENT.
AUTO_COMPONENT = manifest_by_src()["toolchains/cst-toolchains"].component


SCCACHE_COMPONENT = manifest_by_src()["toolchains/rustc-wrapper"].component


def home_installer(entry_file: str) -> InstallService:
    """The installer's use case for the files toolchains writes in the home (its copies, its units,
    the sccache wrapper): written only when they changed, listed in the user's install record
    (ENTRY_FILE: the running program, see program_folder)."""
    return program_installer(entry_file)


def put_home_file(
    home: str,
    path: str,
    data: bytes,
    mode: int,
    *,
    component: str,
    src: str | None,
    kind: str,
    entry_file: str,
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
    return home_installer(entry_file).put(place, data, as_root=False)


def put_auto_copy(home: str, rel: str, data: bytes, entry_file: str) -> bool:
    """One of auto on's copies, ~/rel (its manifest entry gives mode, source and component); True when written."""
    e = next(e for e in INSTALL_MANIFEST if e.dest == "~/" + rel)
    return put_home_file(
        home,
        expand_dest(e.dest, home),
        data,
        e.mode.bits,
        component=e.component,
        src=e.src,
        kind=e.kind,
        entry_file=entry_file,
    )


def put_auto_unit(home: str, name: str, text: str, entry_file: str) -> bool:
    """One of auto on's user units (generated text); True when written."""
    path = os.path.join(home, UNIT_DIR_REL, name)
    return put_home_file(
        home, path, text.encode(), 0o644, component=AUTO_COMPONENT, src=None, kind="text", entry_file=entry_file
    )


def forget_auto_files(home: str, entry_file: str) -> None:
    """auto off removed its files: the copies and the two units leave home's install record."""
    copies = [expand_dest(e.dest, home) for e in INSTALL_MANIFEST if e.component == AUTO_COMPONENT]
    forget_home_files(home, [*copies, *(os.path.join(home, UNIT_DIR_REL, n) for n in (SERVICE, TIMER))], entry_file)


def put_wrapper(home: str, path: str, text: str, values: Mapping[str, str], entry_file: str) -> bool:
    """The sccache wrapper (rustc-wrapper with the settings written in) through the installer, listed with
    those values, its component marked done; True when written."""
    src = "toolchains/rustc-wrapper"
    e = manifest_by_src()[src]
    written = put_home_file(
        home,
        path,
        text.encode(),
        e.mode.bits,
        component=SCCACHE_COMPONENT,
        src=src,
        kind=e.kind,
        entry_file=entry_file,
        values=values,
    )
    record_done(home, SCCACHE_COMPONENT, entry_file)
    return written


def record_done(home: str, component: str, entry_file: str) -> None:
    """component's command finished: home's install record says it is complete."""
    home_installer(entry_file).mark(record_file("user", home), component)


def forget_home_files(home: str, paths: Sequence[str], entry_file: str) -> None:
    """These files of home were removed: its install record no longer lists them."""
    home_installer(entry_file).forget(record_file("user", home), paths)


def defaults_path(entry_file: str) -> str:
    """toolchains.conf next to the running program: toolchains/toolchains.conf in
    CST, ~/.local/lib/cst/toolchains.conf for the timer's copy."""
    return os.path.join(program_folder(entry_file), DEFAULTS_NAME)


def load_settings(home: str, entry_file: str) -> Settings:
    """The checked settings of every target: the defaults, then the user's file.
    Raises ConfigError on any bad line or value (nothing runs then)."""
    path = defaults_path(entry_file)
    try:
        with open(path, encoding="utf-8") as f:
            text: str | None = f.read()
    except FileNotFoundError:
        text = None
    defaults = parse_defaults(path, text)
    try:
        with open(os.path.join(home, CONF_REL), encoding="utf-8") as f:
            user_text = f.read()
    except FileNotFoundError:
        user_text = ""
    return checked_settings(home, defaults, user_text)


def state_path(home: str, tid: str) -> str:
    return os.path.join(home, STATE_DIR_REL, f"toolchains-{tid.replace(':', '-')}.json")


def write_state(home: str, tid: str, data: RunRecord, out: RunOutputPort) -> None:
    path = state_path(home, tid)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 0600: what the temporary file it used to rename into place had.
        write_atomic(path, json.dumps(data), mode=0o600, durable=False, relabel=False)
    except OSError as e:
        out.say(f"{tid}: could not save the run summary: {e}", 4)


def read_state(home: str, tid: str) -> dict[str, Json] | None:
    try:
        with open(state_path(home, tid)) as f:
            data: Json = json.load(f)
            return data if isinstance(data, dict) else None
    except OSError, ValueError:
        return None


class RunSummaries:
    """RunStatePort: ~/.local/state/cst/toolchains-<target>.json, one summary per target
    (a JSON object; the installed copy of an older CST reads and writes the same files)."""

    def __init__(self, home: str, out: RunOutputPort) -> None:
        self.home = home
        self.out = out

    def read(self, tid: str) -> dict[str, Json] | None:
        return read_state(self.home, tid)

    def write(self, tid: str, record: RunRecord) -> None:
        write_state(self.home, tid, record, self.out)


def run_locked(home: str, fn: Callable[[], int], out: RunOutputPort, *, busy_refused: bool = False) -> int:
    """One run at a time (the timer and a manual apply, auto on/off): an exclusive
    flock on ~/.local/state/cst/toolchains.lock (0600). When another run holds the
    lock: nothing done, exit 0 (a prune that finds another prune running), or
    Refused with busy_refused (auto on/off: what was asked did not happen)."""
    path = os.path.join(home, STATE_DIR_REL, "toolchains.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if busy_refused:
                raise Refused(
                    "another run is in progress (the hourly prune, or another auto on/off): nothing done; "
                    f"run this again once it has finished (journalctl --user -u {SERVICE} shows the prune)"
                ) from None
            out.say("another run is in progress: nothing done", 5)
            return 0
        return fn()
    finally:
        os.close(fd)


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


def old_conf_note(home: str, out: RunOutputPort) -> None:
    if os.path.lexists(os.path.join(home, OLD_CONF_REL)):
        out.say(
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
