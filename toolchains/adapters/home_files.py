# SPDX-License-Identifier: MPL-2.0
"""The files CST writes in the home folder: auto on's copies and units and the
sccache wrapper (through the installer and its record), the settings files
it appends to or rewrites, the run summaries of the targets and the run lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from toolchains.ports import RunOutputPort

from cst_core.adapters.files import owned_file_state_at, read_bytes, read_text, unit_file_state_at, write_atomic
from cst_core.adapters.install import program_installer
from cst_core.adapters.runtime import install_payload
from cst_core.application.install import InstallService, Placement, record_file
from cst_core.domain.errors import Refused
from cst_core.domain.files import OwnedState, UnitFileState, WholeFileState
from cst_core.domain.install import INSTALL_MANIFEST, Owner, expand_dest, manifest_by_src
from toolchains.adapters.program import program_folder
from toolchains.domain.auto import AUTO_UNITS, OFF_TEXT, render_timer
from toolchains.domain.common import (
    CONF_REL,
    ConfigError,
    DEFAULTS_NAME,
    DEFAULTS_REL,
    MARKER,
    OFF_REL,
    OLD_CONF_REL,
    OLD_DATA,
    OLD_FILES,
    OLD_MARKER,
    OLD_UNITS,
    PROG_REL,
    SERVICE,
    STATE_DIR_REL,
    TIMER,
    UNIT_DIR_REL,
)
from toolchains.domain.build import expected_template
from toolchains.domain.config import (
    CARGO_CFG_REL,
    SCCACHE_SIZE_LINE,
    SCCACHE_WORKSPACE_LINE,
    WRAPPER_NAME,
    WrapperValues,
    wrapper_path,
)
from toolchains.domain.prune import RunRecord, SavedRun, Settings, checked_settings, is_saved_run, parse_defaults


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
        record=record_file(Owner.USER, home),
        component=component,
        src=src,
        kind=kind,
        owner=Owner.USER,
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
    home_installer(entry_file).mark(record_file(Owner.USER, home), component)


def forget_home_files(home: str, paths: Sequence[str], entry_file: str) -> None:
    """These files of home were removed: its install record no longer lists them."""
    home_installer(entry_file).forget(record_file(Owner.USER, home), paths)


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


def read_state(home: str, tid: str) -> SavedRun | None:
    try:
        with open(state_path(home, tid)) as f:
            data: object = json.load(f)
            return data if is_saved_run(data) else None
    except OSError, ValueError:
        return None


class RunSummaries:
    """RunStatePort: ~/.local/state/cst/toolchains-<target>.json, one summary per target
    (a JSON object; the installed copy of an older CST reads and writes the same files)."""

    def __init__(self, home: str, out: RunOutputPort) -> None:
        self.home = home
        self.out = out

    def read(self, tid: str) -> SavedRun | None:
        return read_state(self.home, tid)

    def write(self, tid: str, record: RunRecord) -> None:
        write_state(self.home, tid, record, self.out)

    def forget(self, tid: str) -> None:
        try:
            os.unlink(state_path(self.home, tid))
        except OSError:
            pass


class ToolchainsLock:
    """One run at a time (the timer and a manual apply, auto on/off): an
    exclusive flock on ~/.local/state/cst/toolchains.lock (0600) in HOME; OUT says when
    another run holds it."""

    def __init__(self, home: str, out: RunOutputPort) -> None:
        self.home = home
        self.out = out

    def run(self, fn: Callable[[], int], *, busy_refused: bool = False) -> int:
        """FN() under the lock. When another run holds the lock: nothing done, exit 0 (a
        prune that finds another prune running), or Refused with busy_refused (auto
        on/off: what was asked did not happen)."""
        path = os.path.join(self.home, STATE_DIR_REL, "toolchains.lock")
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
                self.out.say("another run is in progress: nothing done", 5)
                return 0
            return fn()
        finally:
            os.close(fd)


def file_state(path: str, wanted: str | bytes | None, marker: str = MARKER) -> WholeFileState:
    """ok | change (ours: it holds MARKER, and differs) | conflict (not ours) | missing.

    Every file here is in the user's home: a symbolic link is judged by what
    it points to (cst_core.domain.install.link_read_through), and a write
    replaces the link itself."""
    want = wanted.encode() if isinstance(wanted, str) else wanted
    return owned_file_state_at(path, want, lambda data: marker.encode() in data, owner=Owner.USER)


def timer_unit_state(path: str, wanted: str) -> UnitFileState:
    """file_state of a unit file `auto on` writes: also "masked" when
    `systemctl --user mask` left a link to /dev/null there (see
    cst_core.adapters.files.unit_file_state_at)."""
    return unit_file_state_at(path, wanted.encode(), lambda data: MARKER.encode() in data, owner=Owner.USER)


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


def copies(entry_file: str) -> dict[str, bytes]:
    """{installed path (relative to home): bytes}: this program as `install`
    writes it (a zipapp) and its defaults, as the timer runs them."""
    defaults = read_bytes(defaults_path(entry_file))
    if defaults is None:
        raise ConfigError(f"the defaults file {defaults_path(entry_file)} is missing")
    return {PROG_REL: install_payload("toolchains/cst-toolchains", entry_file), DEFAULTS_REL: defaults}


def unit_states(home: str, entry_file: str) -> dict[str, UnitFileState]:
    out: dict[str, UnitFileState] = {
        rel: file_state(os.path.join(home, rel), text) for rel, text in copies(entry_file).items()
    }
    for name, render in AUTO_UNITS.items():
        out[name] = timer_unit_state(os.path.join(home, UNIT_DIR_REL, name), render())
    return out


def stale_files(home: str) -> list[tuple[str, bool]]:
    """[(path, ours)] of what `cst btrfs cargo-gc enable|disable` left
    (the first version of this cleanup)."""
    out: list[tuple[str, bool]] = []
    for rel in [f"{UNIT_DIR_REL}/{u}" for u in OLD_UNITS] + list(OLD_FILES):
        path = os.path.join(home, rel)
        st = file_state(path, None, OLD_MARKER)
        if st != OwnedState.MISSING:
            out.append((path, st == OwnedState.CHANGE))
    for rel in OLD_DATA:
        path = os.path.join(home, rel)
        if os.path.lexists(path):
            out.append((path, not os.path.islink(path)))
    return out


def cargo_build_dir_is_csts(home: str) -> bool:
    try:
        with open(os.path.join(home, CARGO_CFG_REL), "rb") as f:
            build = tomllib.load(f).get("build")
    except OSError, tomllib.TOMLDecodeError:
        return False
    return isinstance(build, dict) and build.get("build-dir") == expected_template(home)


def wrapper_text(values: WrapperValues, entry_file: str) -> str | None:
    """The wrapper as installed (the settings written in), or None when its
    source is not next to this program (the timer's copy has none)."""
    text = read_text(os.path.join(program_folder(entry_file), WRAPPER_NAME))
    if text is None:
        return None
    text, n = SCCACHE_SIZE_LINE.subn(f"cache_size={values.size}", text, count=1)
    text, m = SCCACHE_WORKSPACE_LINE.subn(f"workspace={'yes' if values.workspace else 'no'}", text, count=1)
    return text if n == m == 1 else None


def wrapper_state(home: str, text: str) -> WholeFileState:
    """ok | change | missing | conflict (not CST's; a symlink counts by what it points to)."""
    path = wrapper_path(home)
    st = file_state(path, text)
    if st == OwnedState.OK:
        try:
            if stat.S_IMODE(os.stat(path).st_mode) != 0o755:
                st = OwnedState.CHANGE
        except OSError:
            st = OwnedState.CHANGE
    return st


class HomeFiles:
    """HomeFilesPort: the files CST keeps in HOME for the running program (ENTRY_FILE: its
    entry point; its copies and defaults are read next to it)."""

    def __init__(self, home: str, entry_file: str) -> None:
        self.home = home
        self.entry_file = entry_file

    def auto_copies(self) -> dict[str, bytes]:
        return copies(self.entry_file)

    def auto_states(self) -> dict[str, UnitFileState]:
        return unit_states(self.home, self.entry_file)

    def timer_missing(self) -> bool:
        return file_state(os.path.join(self.home, UNIT_DIR_REL, TIMER), render_timer()) == OwnedState.MISSING

    def stale_files(self) -> list[tuple[str, bool]]:
        return stale_files(self.home)

    def put_auto_copy(self, rel: str, data: bytes) -> bool:
        return put_auto_copy(self.home, rel, data, self.entry_file)

    def put_auto_unit(self, name: str, text: str) -> bool:
        return put_auto_unit(self.home, name, text, self.entry_file)

    def auto_done(self) -> None:
        record_done(self.home, AUTO_COMPONENT, self.entry_file)

    def forget_auto_files(self) -> None:
        forget_auto_files(self.home, self.entry_file)

    def turned_off(self) -> bool:
        return turned_off(self.home)

    def mark_off(self) -> None:
        write_atomic(os.path.join(self.home, OFF_REL), OFF_TEXT, mode=0o644)

    def remove(self, path: str) -> None:
        os.unlink(path)

    def dangling_link(self, path: str) -> bool:
        return os.path.islink(path) and not os.path.exists(path)

    def remove_if_empty(self, path: str) -> None:
        try:
            os.rmdir(path)
        except OSError:
            pass

    def old_conf_present(self) -> bool:
        return os.path.lexists(os.path.join(self.home, OLD_CONF_REL))

    def read_settings(self, path: str) -> str | None:
        return read_cfg(path)

    def append(self, path: str, text: str) -> str:
        return append_text(path, text)

    def rewrite(self, path: str, text: str) -> str:
        return write_cfg(path, text)

    def build_dir_is_csts(self) -> bool:
        return cargo_build_dir_is_csts(self.home)

    def wrapper_text(self, values: WrapperValues) -> str | None:
        return wrapper_text(values, self.entry_file)

    def wrapper_state(self, text: str) -> WholeFileState:
        return wrapper_state(self.home, text)

    def put_wrapper(self, text: str, values: Mapping[str, str]) -> bool:
        return put_wrapper(self.home, wrapper_path(self.home), text, values, self.entry_file)
