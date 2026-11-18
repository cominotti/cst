# SPDX-License-Identifier: MPL-2.0
"""The read-only commands of btrfs-snapshot: list, caches and configure."""

from collections.abc import Callable, Mapping, Sequence
import sys

from btrfs.snapshot_runtime.adapters.hourly import hourly, manual_prune, purge
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun
from btrfs.snapshot_runtime.adapters.nas import nas
from btrfs.snapshot_runtime.domain import CORE_HINTS, NOT_SET_UP, source_args


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
