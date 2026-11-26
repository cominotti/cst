# SPDX-License-Identifier: MPL-2.0
"""The read-only commands of btrfs-snapshot (list, caches and configure), and COMMANDS: every
command the bash front hands over, hourly, nas, prune and purge too; INTERNAL_COMMANDS: the
internal ones, which read only the file named on their command line (nas-numbers)."""

from collections.abc import Callable, Mapping, Sequence
import sys

from btrfs.snapshot_runtime.adapters.hourly import hourly, manual_prune, purge
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun
from btrfs.snapshot_runtime.adapters.nas import nas
from btrfs.snapshot_runtime.domain import (
    CORE_HINTS,
    DEFAULTS,
    EXIT_USAGE,
    NOT_SET_UP,
    USAGE,
    nas_unreadable,
    nas_unreadable_lines,
    source_args,
)
from cst_core.domain.text import parse_snapshots_conf


def list_snapshots(m: SnapshotRun, args: Sequence[str]) -> int:
    """Every snapshot with btrfs-snapshot-core's keep/delete decision (no lock: read only)."""
    s = m.settings
    if not s["HOME_DIR"]:
        m.say(NOT_SET_UP, sys.stderr)
        return 1
    src = m.sources()
    argv = [m.core, "list", "--snapdir", m.snapdir, "--pin-file", m.pin_file, "--state-dir", m.state, *CORE_HINTS]
    return m.rc(m.run([*argv, *source_args(src, s["HOME_PRESERVE"], s["WS_PRESERVE"]), *args]))


def list_caches(m: SnapshotRun, args: Sequence[str]) -> int:
    """The cache folders (CACHEDIR.TAG) the next snapshots drop, per source, with their size; read
    only (no lock), whatever SNAPSHOT_EXCLUDE_CACHES says. Warnings go to stderr, not into --json."""
    s = m.settings
    if not s["HOME_DIR"]:
        m.say(NOT_SET_UP, sys.stderr)
        return 1
    src = m.sources()
    caches = m.cache_args(src, sys.stderr)
    if s["SNAPSHOT_EXCLUDE_CACHES"] == "no":
        m.say(
            f"SNAPSHOT_EXCLUDE_CACHES=no in {m.conf}: new snapshots keep their cache folders; with yes they would drop:",
            sys.stderr,
        )
    sources = source_args(src, s["HOME_PRESERVE"], s["WS_PRESERVE"])
    return m.rc(m.run([m.core, "caches", "--snapdir", m.snapdir, *sources, *caches.skips, *args]))


def configure(m: SnapshotRun, args: Sequence[str]) -> int:
    """No-op kept for compatibility: earlier versions (re)wrote btrbk configs here."""
    del m, args
    return 0


def nas_numbers(args: Sequence[str]) -> int:
    """`nas-numbers FILE` (internal, for cst btrfs snapshots status): a line on stdout for each NAS number
    of FILE (with DEFAULTS, as a run reads it) that a NAS run cannot compute, in the run's words; 0 (2: a usage error).
    It reads FILE only (never a path below CST_SNAPSHOT_ROOT) and creates nothing; FILE's problem lines are
    settings-lines' to name."""
    if len(args) != 1:
        print("usage: btrfs-snapshot nas-numbers FILE", file=sys.stderr)
        return EXIT_USAGE
    settings = dict(DEFAULTS)
    settings.update(
        (a.key, a.value) for a in parse_snapshots_conf(SnapshotRun.read_raw(args[0])).entries if a.key in DEFAULTS
    )
    for line in nas_unreadable_lines(
        settings, args[0], nas_unreadable(settings), ends="each NAS run ends with an error"
    ):
        print(f"  {line}")
    return 0


def unknown_command(args: Sequence[str]) -> int:
    """No command, or one btrfs-snapshot does not have: the usage on stderr; 2."""
    del args
    print(USAGE, file=sys.stderr)
    return EXIT_USAGE


type Command = Callable[[SnapshotRun, Sequence[str]], int]

# The commands the bash front hands over, by name.
COMMANDS: Mapping[str, Command] = {
    "hourly": hourly,
    "nas": nas,
    "list": list_snapshots,
    "caches": list_caches,
    "prune": manual_prune,
    "purge": purge,
    "configure": configure,
}

# The internal commands the bash front hands over, by name: they run before the start (no settings of the
# state folder read, nothing created).
INTERNAL_COMMANDS: Mapping[str, Callable[[Sequence[str]], int]] = {"nas-numbers": nas_numbers}
