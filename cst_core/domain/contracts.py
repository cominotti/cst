# SPDX-License-Identifier: MPL-2.0
"""The state files one CST program writes and another reads (pure data, no I/O).

The writer and the reader are often different copies of CST: a unit runs the
copy installed under /usr/local, while `cst` runs the checkout, which a git
pull moves ahead (or back). So each format names its version, and a reader
handles every older one: a file with no version is version 0. A reader that
finds a newer version refuses to act on it and says so, instead of reading
it as broken and writing over it.

One row per format in the table below: its key, the file (a glob for a folder of
them), the version this checkout writes (0: the format has no version field
yet), the repository files that write and read it, and where the version is
kept. Python writers and readers take the path and the version from
state_format(key); shell programs keep a literal of their own, checked by
tests that run them.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import posixpath
import re
from typing import Final

from cst_core.domain.install import manifest_by_src


class StateFormatKey(StrEnum):
    """The key of each registered state format (the first column of the table below)."""

    ALERT_SPOOL = "alert-spool"
    STALL_RECORDS = "stall-records"
    STALL_QUEUE = "stall-queue"
    BULK_JOURNAL = "bulk-journal"
    DOCKER_GC_STATE = "docker-gc-state"
    DOCKER_GC_LAST_RUN = "docker-gc-last-run"
    VM_INDEX = "vm-index"
    VM_NAS_TORN = "vm-nas-torn"
    INSTALL_RECORD = "install-record"
    GAME_STATE = "game-state"
    GAME_INTENDED = "game-intended"
    GAME_LEFTOVERS = "game-leftovers"
    DOCKER_NOCOW_OUTCOME = "docker-nocow-outcome"


@dataclass(frozen=True, slots=True)
class StateFormat:
    """One state file format shared between CST programs (a row of the table)."""

    key: str
    path: str
    version: int
    writers: tuple[str, ...]
    readers: tuple[str, ...]
    versioned_by: str

    @property
    def folder(self) -> str:
        """The folder the file (or the files of the glob) live in."""
        return posixpath.dirname(self.path)

    @property
    def file_name(self) -> str:
        """The file's name (a glob for a folder of them)."""
        return posixpath.basename(self.path)

    def newer(self, data: object, field: str) -> int | None:
        """The version data (a JSON object of this format, read back) carries in field when it is newer
        than this checkout reads, else None (also when the version is not a number: version_of)."""
        found = version_of(data, field)
        return found if found is not None and found > self.version else None

    def readable(self, data: object, field: str) -> bool:
        """data is an object of this format or an older one, with a version that is a number."""
        found = version_of(data, field)
        return found is not None and found <= self.version

    def newer_text(self, path: str, found: int) -> str:
        """What a reader says of a file of a newer version than it knows."""
        return (
            f"{path} is format {found} of the {self.key} (this CST reads format {self.version} and older): "
            "written by a newer CST"
        )


def first_line_version(text: str, magic: str) -> int | None:
    """The format version a text file names on its first line, "MAGIC N" (cst-alert 1, cst-game-intended
    1): 0 when the first line is no such line (a file written before the format had a version); None when
    it names MAGIC with something that is not a whole number."""
    words = text.split("\n", 1)[0].split()
    if not words or words[0] != magic:
        return 0
    if len(words) != 2 or not re.fullmatch(r"[0-9]+", words[1]):
        return None
    try:
        return int(words[1])
    except ValueError:  # more digits than int() converts (sys.get_int_max_str_digits): no version either
        return None


def version_of(data: object, field: str) -> int | None:
    """The format version a JSON object carries in field: 0 when it has no such field; None when data is not
    an object or the field is not a whole number >= 0 (true is not 1)."""
    if not isinstance(data, dict):
        return None
    found = data.get(field, 0)
    return found if type(found) is int and found >= 0 else None


# key | path | version | writers | readers | where the version is kept.
# Writers and readers: repository files, separated by blanks.
#
# alert-spool: alerts no desktop session could show, kept for the next login.
# stall-records: one JSON record per line; the report reads every older "v".
# stall-queue: alerts the stall monitor hands to btrfs-notify --queue
#   (through btrfs-stall-alert.service): line 1 the urgency, line 2 the title,
#   the rest the body. In /run: written and read within one boot, by copies
#   installed together (btrfs:core).
# bulk-journal: a move of ~/Bulk not finished yet: `cst btrfs bulk apply`
#   (the checkout) writes it, btrfs-bulk-boot.service (the installed copy)
#   finishes or undoes it at the next boot. btrfs-snapshot's hourly run waits
#   while one is there (whatever its version) that is not "bulk_only".
# docker-gc-state: the last use of each image, written by the installed
#   recorder and read by the weekly run and `cst btrfs docker-gc status`.
# docker-gc-last-run: what the last weekly run removed (status shows it).
# vm-index: the VM backups cst vm made (local copies and NAS snapshots).
# vm-nas-torn: the restic snapshots cst vm's NAS backup failed to forget (torn:
#   incomplete, or not consistent), retried before that VM's next retention.
# install-record: what CST installed (cst_core/domain/record.py).
# game-state, game-intended: the game profile's state. Other projects check
#   that game.state exists (the profile is on): its name and place never
#   change, and it has no version. game.intended (what 'on' set, for the
#   drift repair) names its format on its first line; an installed bash copy
#   of an older CST skips that line (it is no record it knows).
# game-leftovers: the records 'off' could not undo (a failed set-property, a
#   sysfs file that refused the write), kept for the next 'off' and the
#   watchdog's tick while the profile is off. game.state alone keeps meaning
#   "on": 'off' removes it whatever it could undo. An older CST never reads
#   or writes this file. Format 2 adds the reloads due ("reload MGR") and a
#   user line before every entry ("user -" for none); this version still
#   reads format 1, and a CST that reads only format 1 leaves a format 2 file
#   alone.
# docker-nocow-outcome: why docker-volumes-nocow left no migration scheduled
#   (it gave up, or an install could not schedule it), for its status and cst
#   doctor; written and removed best effort, read by no decision of the helper.
_TABLE: Final = r"""
alert-spool        | /var/lib/cst-alerts/pending/*.alert        | 1 | btrfs/btrfs-notify cst_core/adapters/notify.py | btrfs/pending_alerts/domain.py doctor/application/alerts.py | first line "cst-alert 1"
stall-records      | /var/log/btrfs-stall-monitor/stall-*.jsonl | 8 | btrfs/stall_monitord/adapters/monitor.py | btrfs/stall_report/domain/window.py                | "v" of each record
stall-queue        | /run/btrfs-stall-monitor/alert-*.txt       | 0 | btrfs/stall_monitord/adapters/monitor.py | btrfs/btrfs-notify                                 | none (plain text, one boot)
bulk-journal       | /var/lib/btrfs-bulk/journal-*.json         | 1 | btrfs/bulk/adapters/state.py     | btrfs/bulk/adapters/state.py btrfs/snapshot_runtime/adapters/machine.py | "v" (none: 0)
docker-gc-state    | /var/lib/docker-gc/last-used.json          | 1 | btrfs/docker_gc/adapters.py      | btrfs/docker_gc/adapters.py                        | "v" (none: 0)
docker-gc-last-run | /var/lib/docker-gc/last-run.json           | 1 | btrfs/docker_gc/adapters.py      | btrfs/docker_gc/adapters.py                        | "v" (none: 0)
vm-index           | /var/lib/cst/vm-backups.json               | 1 | vm/adapters/backup.py            | vm/adapters/backup.py doctor/application/vm.py     | "version"
vm-nas-torn        | /var/lib/cst/vm-nas-torn.json              | 1 | vm/adapters/backup.py            | vm/adapters/backup.py doctor/application/vm.py     | "version"
install-record     | /var/lib/cst/installed.json                | 1 | cst_core/application/install.py  | cst_core/application/install.py doctor/application/install.py | "format"
game-state         | /run/cpu-setup/game.state                  | 0 | cpu/game/application/profile.py  | cpu/game/application/profile.py cpu/game/application/report.py cpu/adapters.py cpu/power/adapters.py doctor/application/cpu.py | none (its existence is the contract)
game-intended      | /run/cpu-setup/game.intended               | 1 | cpu/game/application/profile.py  | cpu/game/application/profile.py cpu/game/application/report.py cpu/game/application/drift.py doctor/application/cpu.py | first line "cst-game-intended 1" (none: 0)
game-leftovers     | /run/cpu-setup/game.leftovers              | 2 | cpu/game/application/leftovers.py | cpu/game/application/leftovers.py cpu/game/application/report.py doctor/application/cpu.py | first line "cst-game-leftovers 2" (none: 0)
docker-nocow-outcome | /var/lib/docker-volumes-nocow/outcome    | 1 | btrfs/docker_volumes_nocow/adapters.py | btrfs/docker_volumes_nocow/adapters.py doctor/application/docker_nocow.py | "v" (none: 0)
"""


def _parse(table: str) -> tuple[StateFormat, ...]:
    """The rows of a state table, in order; a malformed row raises ValueError."""
    rows: list[StateFormat] = []
    for number, raw in enumerate(table.splitlines(), 1):
        if not raw.strip():
            continue
        fields = [f.strip() for f in raw.split("|")]
        try:
            key, path, version, writers, readers, versioned_by = fields
            rows.append(
                StateFormat(key, path, int(version), tuple(writers.split()), tuple(readers.split()), versioned_by)
            )
        except ValueError as exc:
            raise ValueError(f"state table row {number}: {raw.strip()!r}: {exc}") from None
    return tuple(rows)


_FORMATS: Final[Mapping[str, StateFormat]] = {f.key: f for f in _parse(_TABLE)}
if sorted(_FORMATS) != sorted(StateFormatKey):
    raise ValueError("the state table and its keys differ")


# The program every CST alert goes through (its manifest destination); it also keeps the alert spool.
NOTIFY_PROGRAM: Final = manifest_by_src()["btrfs/btrfs-notify"].dest
# The cpu settings file: cst cpu, its game profile and vm-pin read it, and so do cst doctor and cst vm.
CPU_SETTINGS: Final = "/etc/cpu-setup/cpu-setup.conf"

# The /home NAS backup's files, which btrfs-snapshot's runtime owns and cst vm's NAS backup reuses
# (cst doctor reads the settings file too); setup-snapshots.bash keeps literals of its own.
NAS_SSH_ALIAS: Final = "btrfs-setup-nas"  # the managed Host block in /root/.ssh/config
SNAPSHOTS_CONF: Final = "/etc/btrfs-setup/snapshots.conf"
RESTIC_PW_FILE: Final = "/etc/btrfs-setup/restic-password"  # the repository's password (root only)
RESTIC_CACHE_DIR: Final = "/var/cache/btrfs-snapshot-restic"


class Urgency(StrEnum):
    """An alert's urgency, as btrfs-notify -u takes it (and the alert spool's files keep it)."""

    LOW = "low"
    NORMAL = "normal"
    CRITICAL = "critical"


# As btrfs-notify bounds an alert's app name, title and body before it shows or keeps it (the alert
# spool's files hold them so bounded; cst-pending-alerts reads them with the same bounds).
ALERT_APP_MAX: Final = 40
ALERT_TITLE_MAX: Final = 200
ALERT_BODY_MAX: Final = 4000


def state_format(key: StateFormatKey) -> StateFormat:
    """The registered format of key."""
    return _FORMATS[key]
