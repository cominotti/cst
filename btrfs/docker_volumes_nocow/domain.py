# SPDX-License-Identifier: MPL-2.0
"""docker-volumes-nocow's rules, no I/O: its paths, what a scan of a volume found, whether a copy
matches its original, how a dockerd names its data root, and the record of postponed boots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import time
from typing import Final, NotRequired, TypedDict, TypeIs

from cst_core.domain.contracts import NOTIFY_PROGRAM, StateFormatKey, state_format
from cst_core.domain.shapes import KeyTypes, has_shape, parse_json
from cst_core.domain.text import enum_member
from cst_core.domain.values import GIB
from cst_core.domain.units import ActiveState

MARGIN: Final = 20 * GIB  # free space kept besides the copy itself
GIVE_UP_AFTER = 2  # boots that tried the migration
GAVE_UP = "Gave up after two boots; see journalctl -u docker-volumes-nocow."
DAEMON_JSON = "/etc/docker/daemon.json"
# How the reason a boot records ends when the dockerd config file cannot be read (config_reason).
CONFIG_UNKNOWN: Final = ", so the volumes folder Docker uses is unknown"


def config_reason(problem: str) -> str:
    """The postponed reason of a boot whose dockerd config file cannot be read (NocowPaths.config_problem)."""
    return f"{problem}{CONFIG_UNKNOWN}"


def is_config_reason(reason: object) -> bool:
    """reason (a postponed record's) is config_reason's: the dockerd config file could not be read."""
    return isinstance(reason, str) and reason.endswith(CONFIG_UNKNOWN)


# A volume's data folder, in the volume's folder (Docker's name for it).
DATA_DIR: Final = "_data"
# Beside a volume's _data during its migration: the NOCOW copy being made, and the
# original between the two renames, followed by a number (old_copy_name: the time it
# was moved aside, or the newest old copy's number plus one when that is higher).
NEW_SUFFIX = ".nocow-new"
OLD_MARK = ".cow-old-"


@dataclass(frozen=True, slots=True)
class VolumeLeftovers:
    """What an interrupted migration may have left in one volume's folder: whether _data is there
    (a dangling link counts), its old copies (the _data.cow-old-* names, oldest first: oldest_first)
    and whether its twin (_data.nocow-new) is."""

    has_data: bool
    olds: tuple[str, ...]
    twin: bool


def old_copy_stamp(name: str) -> int | None:
    """The number in an old copy's name (_data.cow-old-N, N in ASCII digits); None for any other name."""
    stamp = name.removeprefix(DATA_DIR + OLD_MARK)
    if stamp == name or not (stamp.isascii() and stamp.isdigit()):
        return None
    return int(stamp)


def _age(name: str) -> tuple[bool, int, str]:
    stamp = old_copy_stamp(name)
    return stamp is not None, stamp or 0, name


def oldest_first(olds: Iterable[str]) -> tuple[str, ...]:
    """Old copies, oldest first: by the number in their names (old_copy_name), not by their text
    (_data.cow-old-999999999 is older than _data.cow-old-1000000000); a name without a number
    (not one this helper made) comes before them."""
    return tuple(sorted(olds, key=_age))


def old_copy_name(olds: Iterable[str], now: int) -> str:
    """The name a migration moves _data aside to: _data.cow-old-NOW, or a number above the newest old
    copy still there (one a removal could not delete), so the original is always the newest, even
    after the clock went back (a reset real-time clock before NTP)."""
    stamps = [s + 1 for s in map(old_copy_stamp, olds) if s is not None]
    return f"{DATA_DIR}{OLD_MARK}{max([now, *stamps])}"


@dataclass(frozen=True, slots=True)
class Recovery:
    """What recover does in one volume: the old copy that goes back as _data (None: none), then the
    entries removed when _data is a folder (the other old copies, oldest first, then the twin)."""

    restore: str | None
    remove: tuple[str, ...]


def recovery(v: VolumeLeftovers) -> Recovery:
    """A volume that died between the two renames (no _data, an old copy) gets its newest old copy
    back. The other old copies and the twin go only once _data is a folder: the caller checks it
    after the restore (the copy put back may be a file or a link)."""
    twin = (DATA_DIR + NEW_SUFFIX,) if v.twin else ()
    if not v.has_data and v.olds:
        return Recovery(v.olds[-1], v.olds[:-1] + twin)
    return Recovery(None, v.olds + twin)


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
    # Why no migration is scheduled, when the helper knows (JSON, world-readable): status shows it.
    outcome: str = state_format(StateFormatKey.DOCKER_NOCOW_OUTCOME).path
    notify: str = NOTIFY_PROGRAM
    # Where an alert btrfs-notify could not take is kept (for cst doctor and the next login).
    alerts: str = state_format(StateFormatKey.ALERT_SPOOL).folder
    proc: str = "/proc"  # where running dockerd processes are looked for
    # Why /etc/docker/daemon.json cannot be read ("" when it can or there is none): then which
    # volumes folder Docker uses is unknown, and the migration waits (wiring reads it).
    config_problem: str = ""


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


# docker.service's ActiveStates in which the migration may run (it is stopped), and those in which it
# waits because Docker is running; any other word is "state unknown", and it waits too.
DOCKER_STOPPED_STATES: Final = frozenset({ActiveState.INACTIVE, ActiveState.FAILED})
DOCKER_BUSY_STATES: Final = frozenset(
    {ActiveState.ACTIVE, ActiveState.ACTIVATING, ActiveState.RELOADING, ActiveState.DEACTIVATING}
)


def service_wait_reason(returncode: int, state: str) -> str | None:
    """None when docker.service is known to be stopped, else why the migration waits.
    Fails closed: only a clear "inactive" or "failed" lets it run."""
    if returncode == 0 and state in DOCKER_STOPPED_STATES:
        return None
    if returncode == 0 and state in DOCKER_BUSY_STATES:
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


# ---- the outcome record: why no migration is scheduled -----------------------------------------
OUTCOME_FORMAT: Final = state_format(StateFormatKey.DOCKER_NOCOW_OUTCOME)
OUTCOME_VERSION: Final = "v"  # the record's key that holds its format version


class NocowOutcome(StrEnum):
    """Why no migration is scheduled, as the helper recorded it (the outcome record's "outcome")."""

    GAVE_UP = "gave-up"  # the migration did not finish after GIVE_UP_AFTER boots
    # An install's mark found +C and copy-on-write data (or made +C) and could not write the request:
    # no boot asks again (a boot asks only for a folder without +C).
    NOT_SCHEDULED = "not-scheduled"


class OutcomeRecord(TypedDict):
    """The outcome file (JSON): its format version, the outcome's word (NocowOutcome), when (epoch
    seconds), the volumes folder and why."""

    v: NotRequired[int]
    outcome: str
    at: int
    volumes: str
    reason: str


_OUTCOME_REQUIRED: Final[KeyTypes] = {"outcome": (str,), "at": (int,), "volumes": (str,), "reason": (str,)}
_OUTCOME_OPTIONAL: Final[KeyTypes] = {OUTCOME_VERSION: (int,)}


def is_outcome_record(value: object) -> TypeIs[OutcomeRecord]:
    return has_shape(value, _OUTCOME_REQUIRED, _OUTCOME_OPTIONAL)


def outcome_record(outcome: NocowOutcome, *, at: int, volumes: str, reason: str) -> OutcomeRecord:
    """The record the helper writes."""
    return {"v": OUTCOME_FORMAT.version, "outcome": outcome, "at": at, "volumes": volumes, "reason": reason}


@dataclass(frozen=True, slots=True)
class OutcomeRead:
    """The outcome file as read back: no file (record None, problem ""), a record and its outcome, or
    why it cannot be used (problem: a line for status and cst doctor)."""

    record: OutcomeRecord | None = None
    outcome: NocowOutcome | None = None
    problem: str = ""


def outcome_unreadable(path: str) -> OutcomeRead:
    return OutcomeRead(problem=f"cannot read {path}")


def parse_outcome(text: str | None, path: str) -> OutcomeRead:
    """The outcome file's text (None: there is none), read as this checkout reads its format: one of a
    newer format is named so, never taken for a broken one; an outcome word this checkout does not know
    is unreadable."""
    if text is None:
        return OutcomeRead()
    try:
        data = parse_json(text)
    except ValueError, RecursionError:
        return outcome_unreadable(path)
    newer = OUTCOME_FORMAT.newer(data, OUTCOME_VERSION)
    if newer is not None:
        return OutcomeRead(problem=OUTCOME_FORMAT.newer_text(path, newer))
    if not OUTCOME_FORMAT.readable(data, OUTCOME_VERSION) or not is_outcome_record(data):
        return outcome_unreadable(path)
    outcome = enum_member(NocowOutcome, data["outcome"])
    if outcome is None:
        return outcome_unreadable(path)
    return OutcomeRead(data, outcome)


def outcome_line(read: OutcomeRead) -> str:
    """The status line of the outcome record, or "" (no record)."""
    if read.problem:
        return f"  {read.problem}"
    if read.record is None or read.outcome is None:
        return ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(read.record["at"]))
    match read.outcome:
        case NocowOutcome.GAVE_UP:
            return f"  migration gave up {when}: {read.record['reason']}; run cst btrfs install to try again"
        case NocowOutcome.NOT_SCHEDULED:
            return (
                f"  migration not scheduled by the install at {when} ({read.record['reason']}); no boot asks for it: "
                "fix that, then run cst btrfs install"
            )


def postponed_line(rec: StoredPostponed | None) -> str:
    """The status line of a postponed migration, or ""."""
    if rec is None or len(rec) == 0:
        return ""
    last = rec.get("last")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if type(last) is int else "?"
    return f"  migration postponed at {rec.get('boots', '?')} boot(s) in a row, last {when}: {rec.get('reason', '?')}"
