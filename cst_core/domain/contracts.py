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
import posixpath
from typing import Final, Literal, get_args

from cst_core.domain.install import manifest_by_src

type _FormatKey = Literal[
    "alert-spool",
    "stall-records",
    "stall-queue",
    "bulk-journal",
    "docker-gc-state",
    "docker-gc-last-run",
    "vm-index",
    "install-record",
    "game-state",
    "game-intended",
]


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
#   finishes or undoes it at the next boot.
# docker-gc-state: the last use of each image, written by the installed
#   recorder and read by the weekly run and `cst btrfs docker-gc status`.
# docker-gc-last-run: what the last weekly run removed (status shows it).
# vm-index: the VM backups cst vm made (local copies and NAS snapshots).
# install-record: what CST installed (cst_core/domain/record.py).
# game-state, game-intended: the game profile's state. Other projects check
#   that game.state exists (the profile is on): its name and place never
#   change. Neither has a version yet.
_TABLE: Final = r"""
alert-spool        | /var/lib/cst-alerts/pending/*.alert        | 1 | btrfs/btrfs-notify cst_core/adapters/notify.py | btrfs/cst-pending-alerts doctor/application/alerts.py | first line "cst-alert 1"
stall-records      | /var/log/btrfs-stall-monitor/stall-*.jsonl | 8 | btrfs/stall_monitord/adapters/monitor.py | btrfs/stall_report/domain/window.py                | "v" of each record
stall-queue        | /run/btrfs-stall-monitor/alert-*.txt       | 0 | btrfs/stall_monitord/adapters/monitor.py | btrfs/btrfs-notify                                 | none (plain text, one boot)
bulk-journal       | /var/lib/btrfs-bulk/journal-*.json         | 1 | btrfs/bulk/adapters/state.py     | btrfs/bulk/adapters/state.py                       | "v" (none: 0)
docker-gc-state    | /var/lib/docker-gc/last-used.json          | 1 | btrfs/docker_gc/adapters.py      | btrfs/docker_gc/adapters.py                        | "v" (none: 0)
docker-gc-last-run | /var/lib/docker-gc/last-run.json           | 1 | btrfs/docker_gc/adapters.py      | btrfs/docker_gc/adapters.py                        | "v" (none: 0)
vm-index           | /var/lib/cst/vm-backups.json               | 1 | vm/adapters/backup.py            | vm/adapters/backup.py doctor/application/vm.py     | "version"
install-record     | /var/lib/cst/installed.json                | 1 | cst_core/application/install.py  | cst_core/application/install.py doctor/application/install.py | "format"
game-state         | /run/cpu-setup/game.state                  | 0 | cpu/game-profile                 | cpu/game-profile cpu/cli.py doctor/application/cpu.py | none (its existence is the contract)
game-intended      | /run/cpu-setup/game.intended               | 0 | cpu/game-profile                 | cpu/game-profile doctor/application/cpu.py         | none yet
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
if sorted(_FORMATS) != sorted(get_args(_FormatKey.__value__)):
    raise ValueError("the state table and its keys differ")


# The program every CST alert goes through (its manifest destination); it also keeps the alert spool.
NOTIFY_PROGRAM: Final = manifest_by_src()["btrfs/btrfs-notify"].dest


def state_format(key: _FormatKey) -> StateFormat:
    """The registered format of key."""
    return _FORMATS[key]
