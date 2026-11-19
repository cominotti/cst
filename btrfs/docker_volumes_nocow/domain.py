# SPDX-License-Identifier: MPL-2.0
"""docker-volumes-nocow's rules, no I/O: its paths, what a scan of a volume found, whether a copy
matches its original, how a dockerd names its data root, and the record of postponed boots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Final, TypedDict, TypeIs

from cst_core.domain.contracts import NOTIFY_PROGRAM, StateFormatKey, state_format
from cst_core.domain.values import GIB

MARGIN: Final = 20 * GIB  # free space kept besides the copy itself
GIVE_UP_AFTER = 2  # boots that tried the migration
GAVE_UP = "Gave up after two boots; see journalctl -u docker-volumes-nocow."
DAEMON_JSON = "/etc/docker/daemon.json"
# Beside a volume's _data during its migration: the NOCOW copy being made, and the
# original between the two renames (followed by the time it was moved aside).
NEW_SUFFIX = ".nocow-new"
OLD_MARK = ".cow-old-"


class NocowCommand(StrEnum):
    """docker-volumes-nocow's commands (its first argument), in its usage's order."""

    STATUS = "status"
    MARK = "mark"
    MIGRATE = "migrate"
    UNMARK = "unmark"


@dataclass(frozen=True, slots=True)
class NocowPaths:
    """Where the helper looks and keeps its state (tests point them into a temporary folder)."""

    volumes: str = "/var/lib/docker/volumes"  # data-root/volumes (wiring reads data-root)
    flag: str = "/etc/btrfs-setup/docker-nocow-pending"
    attempts: str = "/var/lib/docker-volumes-nocow/attempts"
    # The boots at which migrate had to wait (JSON, world-readable): shown by
    # status and by `cst doctor`, removed at the first boot that is not held back.
    postponed: str = "/var/lib/docker-volumes-nocow/postponed"
    notify: str = NOTIFY_PROGRAM
    # Where an alert btrfs-notify could not take is kept (for cst doctor and the next login).
    alerts: str = state_format(StateFormatKey.ALERT_SPOOL).folder
    proc: str = "/proc"  # where running dockerd processes are looked for


@dataclass(frozen=True, slots=True)
class TreeScan:
    """A walk of a folder: regular files with data and without NOCOW, their apparent
    bytes, all entries, and the apparent bytes of all regular files."""

    cow_files: int = 0
    cow_bytes: int = 0
    entries: int = 0
    total_bytes: int = 0


@dataclass(frozen=True, slots=True)
class PendingVolume:
    """A volume that still holds copy-on-write data: its name, its _data folder, the files and bytes."""

    name: str
    data: str
    files: int = 0
    size: int = 0


def copy_matches(old: TreeScan, new: TreeScan) -> bool:
    """The copy has the original's entries and bytes, and no copy-on-write file."""
    return new.entries == old.entries and new.total_bytes == old.total_bytes and not new.cow_files


def copy_check_failed(old: TreeScan, new: TreeScan) -> str:
    return (
        f"copy check failed (entries {old.entries} vs {new.entries}, bytes "
        f"{old.total_bytes} vs {new.total_bytes}, CoW files {new.cow_files})"
    )


def label_text(value: bytes | None) -> str:
    return "none" if value is None else value.decode(errors="replace")


def label_mismatch(old: Mapping[str, bytes | None], new: Mapping[str, bytes | None]) -> str:
    """The first entry whose SELinux label differs between original and copy, as the error ("" when none)."""
    differ = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    if not differ:
        return ""
    rel = differ[0]
    return f"copy check failed (SELinux label of {rel}: {label_text(old.get(rel))} vs {label_text(new.get(rel))})"


def service_wait_reason(returncode: int, state: str) -> str | None:
    """None when docker.service is known to be stopped, else why the migration waits.
    Fails closed: only a clear "inactive" or "failed" lets it run."""
    if returncode == 0 and state in ("inactive", "failed"):
        return None
    if returncode == 0 and state in ("active", "activating", "reloading", "deactivating"):
        return "Docker is running"
    return f"docker.service state unknown (systemctl exit {returncode}, ActiveState {state!r})"


@dataclass(frozen=True, slots=True)
class DaemonArgs:
    """What a dockerd's command line says of its data root: --data-root (or -g, --graph) and --config-file."""

    root: str | None = None
    config: str = DAEMON_JSON


def daemon_args(args: Sequence[str]) -> DaemonArgs:
    """Read dockerd's argv (args[0] is the program); the last of each option wins."""
    root, config = None, DAEMON_JSON
    rest = iter(args[1:])
    for raw in rest:
        # the shorthand with its value attached: -g/path
        arg = "-g=" + raw[2:] if raw.startswith("-g") and raw[2:3] not in ("", "=") else raw
        key, eq, value = arg.partition("=")
        if key not in ("--data-root", "-g", "--graph", "--config-file"):
            continue
        if not eq:
            value = next(rest, "")
        if key == "--config-file":
            config = value
        else:
            root = value
    return DaemonArgs(root, config)


class PostponedRecord(TypedDict):
    """The postponed file (JSON) as this helper writes it: the latest reason, the boots in a row
    (counted once per boot id), the first and the last of them (epoch seconds), the last boot's id."""

    reason: str
    boots: int
    first: int
    last: int
    boot_id: str


class StoredPostponed(TypedDict, total=False):
    """The postponed file as read back: PostponedRecord's keys, none of them checked (a file this
    helper did not write may hold anything); next_postponed and postponed_line check what they use."""

    reason: object
    boots: object
    first: object
    last: object
    boot_id: object


def is_stored_postponed(value: object) -> TypeIs[StoredPostponed]:
    """A JSON object is a StoredPostponed: none of its keys is required, any value is accepted."""
    return isinstance(value, dict)


def next_postponed(rec: StoredPostponed | None, *, why: str, boot: str, now: int) -> PostponedRecord:
    """The record after one more postponed run: boots counted once per boot (by the kernel's
    boot id), the latest reason kept. A record this helper did not write starts afresh."""
    old = rec or {}
    boots, first = old.get("boots"), old.get("first")
    if type(boots) is not int or type(first) is not int or boots < 1:
        boots, first = 0, now  # no record, or one this helper did not write
    if not boots or old.get("boot_id") != boot:
        boots += 1
    return {"reason": why, "boots": boots, "first": first, "last": now, "boot_id": boot}


def postponed_line(rec: StoredPostponed | None) -> str:
    """The status line of a postponed migration, or ""."""
    if not rec:
        return ""
    last = rec.get("last")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if isinstance(last, int) else "?"
    return f"  migration postponed at {rec.get('boots', '?')} boot(s) in a row, last {when}: {rec.get('reason', '?')}"
