# SPDX-License-Identifier: MPL-2.0
"""Concrete host adapters for the `bluetooth` bounded context."""

from collections.abc import Sequence
import contextlib
import ctypes
import json
import os
import sys
from typing import Any

from bluetooth.domain import (
    LE_SECTION,
    RESTART_UNITS,
    classify_other_fragments,
    parse_pending,
    render_pending,
    snapshot,
    still_pending,
)
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.files import ensure_dir, write_atomic
from cst_core.ports import CommandRunnerPort


def read(path: str) -> str:
    """Read a UTF-8 text file."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_or_none(path: str) -> str | None:
    """Read a UTF-8 text file, or return None if it does not exist."""
    try:
        return read(path)
    except FileNotFoundError:
        return None


def other_fragments(ours: str, dirs: Sequence[str]) -> list[tuple[str, str]]:
    """[(before|after, path)] of the WirePlumber fragments other than OURS
    (a file name) that define monitor.bluez.properties, in the order
    WirePlumber reads them: by file name, over DIRS (highest priority
    first: a name found in an earlier dir hides the same name in later ones)."""
    seen_names: set[str] = set()
    dir_files: list[tuple[str, list[tuple[str, str]]]] = []
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        entries: list[tuple[str, str]] = []
        for n in names:
            if not n.endswith(".conf") or n in seen_names:
                continue
            seen_names.add(n)
            if n == ours:
                continue
            try:
                text = read(os.path.join(d, n))
            except OSError, UnicodeDecodeError:
                continue
            entries.append((n, text))
        dir_files.append((d, entries))
    return classify_other_fragments(ours, dir_files)


def wp_effective_roles() -> tuple[bool, str | None]:
    """bluez5.roles as WirePlumber resolves its config right now (all its
    files, merged by libwireplumber itself, read-only: nothing is started or
    connected). Returns (True, value_or_None) on success, or (False, error)
    when libwireplumber cannot be loaded or the config cannot be opened."""
    try:
        lib = ctypes.CDLL(os.environ.get("CST_BT_LIBWP") or "libwireplumber-0.5.so.0")
        lib.wp_init.argtypes = [ctypes.c_int]
        lib.wp_init(0)
        lib.wp_conf_new_open.restype = ctypes.c_void_p
        lib.wp_conf_new_open.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.wp_properties_new_empty.restype = ctypes.c_void_p
        lib.wp_conf_section_update_props.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        lib.wp_properties_get.restype = ctypes.c_char_p
        lib.wp_properties_get.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        err = ctypes.c_void_p()
        conf = lib.wp_conf_new_open(b"wireplumber.conf", None, ctypes.byref(err))
        if not conf:
            return False, "WirePlumber could not open wireplumber.conf"
        props = lib.wp_properties_new_empty()
        lib.wp_conf_section_update_props(conf, LE_SECTION.encode(), props)
        v: bytes | None = lib.wp_properties_get(props, b"bluez5.roles")
        return True, None if v is None else v.decode("utf-8", "replace")
    except (OSError, AttributeError) as exc:
        return False, str(exc)


class LocalBluetoothHostAdapter:
    """Default adapter reading from the local filesystem and libwireplumber."""

    def read(self, path: str) -> str:
        """Read a UTF-8 file from disk."""
        return read(path)

    def read_or_none(self, path: str) -> str | None:
        """Read a UTF-8 file from disk, or None when missing."""
        return read_or_none(path)

    def other_fragments(self, ours: str, dirs: Sequence[str]) -> list[tuple[str, str]]:
        """List other WirePlumber fragments defining monitor.bluez.properties."""
        return other_fragments(ours, dirs)

    def wp_effective_roles(self) -> tuple[bool, str | None]:
        """Resolve effective WirePlumber bluez5.roles via libwireplumber."""
        return wp_effective_roles()

    def read_stdin(self) -> str:
        """Read all text from sys.stdin."""
        return sys.stdin.read()

    def restart_pending(self, path: str, ids: tuple[str, str], skipped: Sequence[str], *, save: bool) -> str:
        """The restarts still owed, one per line; with save, the file at path holds them (atomically), or is removed.
        A record that cannot be read (not UTF-8, not a file) counts as none, as parse_pending treats one that is not JSON."""
        try:
            text = read_or_none(path)
        except OSError, ValueError:
            text = None
        owed = still_pending(parse_pending(text), dict(zip(RESTART_UNITS, ids, strict=True)), skipped)
        if save and owed:
            ensure_dir(os.path.dirname(path), 0o755)
            write_atomic(path, render_pending(owed), mode=0o644, relabel=False)
        elif save:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        return "".join(f"{u}\n" for u in sorted(owed))


# ---- the LDAC rate watcher: pw-dump and pactl ------------------------------------------------
RUNNER: CommandRunnerPort = SubprocessCommandRunner()


def run(cmd: Sequence[str], timeout: float = 15) -> tuple[int, str]:
    """(exit status, stdout) of CMD; 127 when it does not exist, 124 on timeout.
    stdin is ours; bytes that are not UTF-8 become U+FFFD; a command that
    cannot start raises (as subprocess.run did)."""
    r = RUNNER.run(cmd, timeout=timeout, stdin="inherit")
    if r.fault in ("missing", "timeout"):
        return r.returncode, ""
    if r.cause is not None:
        raise r.cause
    return r.returncode, r.stdout


class Live:
    """The real system: pw-dump and pactl (a LiveAudioPort)."""

    def __init__(self, dev: str) -> None:
        self.dev = dev

    def snap(self) -> dict[str, Any] | None:
        """Query `pw-dump` and return this device's parsed snapshot, or None."""
        rc, out = run(["pw-dump"], timeout=10)
        if rc != 0:
            return None
        try:
            return snapshot(self.dev, json.loads(out))
        except ValueError:
            return None

    def cards(self) -> list[dict[str, Any]] | None:
        """Query `pactl -f json list cards`, or return None on failure."""
        rc, out = run(["pactl", "-f", "json", "list", "cards"])
        if rc != 0:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    def set_profile(self, prof: str) -> bool:
        """Switch `bluez_card.<dev>` to profile `prof` via `pactl`."""
        return run(["pactl", "set-card-profile", "bluez_card." + self.dev, prof])[0] == 0
