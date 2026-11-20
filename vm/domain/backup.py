# SPDX-License-Identifier: MPL-2.0
"""The records of the backups: an index entry, the two manifests, restic's summary of a backup,
the fingerprint of a source, and the two ways a backup stops (Stop, Uncertain); the lines status
and backup-timer status print about them. Pure."""

from collections.abc import Sequence
import datetime
import json
from enum import StrEnum
from typing import Final, NotRequired, Required, TypedDict, TypeIs

from cst_core.domain.errors import Refused
from cst_core.domain.shapes import KeyTypes, has_shape, items_of, json_object
from cst_core.domain.values import MINUTE, Json
from vm.domain import ON_CALENDAR, TimerStatus, timer_file_words

# The files a backup writes next to the copies (a local one) or stages for restic (the NAS one).
DOMAIN_XML = "domain.xml"
MANIFEST_JSON = "manifest.json"

# What changes when something writes to a file or a folder tree (vm.adapters.backup.fingerprint):
# only compared, before and after a copy.
type Fingerprint = tuple[object, ...]


class BootedDeployment(TypedDict):
    """The booted rpm-ostree deployment a backup records (vm.adapters.booted_deployment): each
    value as rpm-ostree status --json gives it, null when it names none."""

    version: Json
    checksum: Json
    timestamp: Json


class IndexEntry(TypedDict, total=False):
    """One backup in the index (vm-backups.json). A local copy has path, a NAS backup snapshot,
    repository and host. Keys this cst vm does not know are kept as they are."""

    vm: Required[str]
    timestamp: str
    epoch: float
    path: str
    mode: str
    snapshot: str
    repository: str
    host: str
    consistency: str
    keep: str | None
    versions: dict[str, str]
    deployment: BootedDeployment | None


# The optional keys of an index entry, of their types (is_index_entry).
_ENTRY_KEYS: KeyTypes = {
    "timestamp": (str,),
    "epoch": (int, float),
    "path": (str,),
    "mode": (str,),
    "snapshot": (str,),
    "repository": (str,),
    "host": (str,),
    "consistency": (str,),
    "keep": (str, type(None)),
    "versions": (dict,),
    "deployment": (dict, type(None)),
}
_DEPLOYMENT_KEYS = ("version", "checksum", "timestamp")


def is_index_entry(e: object) -> TypeIs[IndexEntry]:
    """An entry of the index this cst vm understands: vm a non-empty string; a NAS backup (mode "nas")
    with its restic snapshot, any other with its folder (path); its other known keys of their types.
    An entry that is not one is never listed, pruned or dropped: write_index keeps it as it is."""
    if not json_object(e) or not has_shape(e, {"vm": (str,)}, _ENTRY_KEYS) or not e["vm"]:
        return False
    versions, deployment = e.get("versions", {}), e.get("deployment")
    if not json_object(versions) or not all(isinstance(v, str) for v in versions.values()):
        return False
    if deployment is not None and not (json_object(deployment) and all(k in deployment for k in _DEPLOYMENT_KEYS)):
        return False
    where = e.get("snapshot") if e.get("mode") == "nas" else e.get("path")
    return isinstance(where, str) and where != ""


# Why an index that parses is no index (stored_list found no "backups" list): cst vm and cst doctor.
NO_BACKUPS_LIST = 'no "backups" list'


class IndexRefused(Refused):
    """The index cannot be used: it cannot be read as one, or a newer cst vm wrote it. problem says
    what is wrong with the file, advice what to do about it; the message is the two around the
    words that nothing is backed up or listed with it."""

    def __init__(self, problem: str, advice: str) -> None:
        super().__init__(f"{problem}: left as it is, and nothing is backed up or listed with it; {advice}")
        self.problem = problem
        self.advice = advice


def not_indexed_text(backup: str, kept: str, e: IndexRefused) -> str:
    """The message of a backup that is made but cannot be recorded: the index changed while it ran
    (a newer cst vm rewrote it, or it was damaged). backup names it, kept says where it stays and
    how it is restored."""
    return (
        f"{backup} is complete but not in the index: {e.problem}, so the index was left as it is and "
        f"cst vm does not list this backup; {kept}. Then: {e.advice}"
    )


def stored_list(data: object) -> list[object] | None:
    """The "backups" list of a parsed index, each entry as stored; None when data is not an object
    with one (NO_BACKUPS_LIST). cst vm's stored_backups and cst doctor read the index with it."""
    backups = data.get("backups") if json_object(data) else None
    return items_of(backups) if isinstance(backups, list) else None


class TornEntry(TypedDict):
    """A restic snapshot a NAS backup made and failed to forget (vm-nas-torn.json): torn, so no backup
    (restic could not read every file, or the VM changed while restic read it). snapshot: restic's id;
    vm, repository and host: whose and where; timestamp: the UTC stamp of that run; why: what tore it.
    The next NAS backup of vm forgets it before its retention would count it as a backup."""

    snapshot: str
    vm: str
    repository: str
    host: str
    timestamp: str
    why: str


_TORN_KEYS: KeyTypes = {
    "snapshot": (str,),
    "vm": (str,),
    "repository": (str,),
    "host": (str,),
    "timestamp": (str,),
    "why": (str,),
}


def is_torn_entry(e: object) -> TypeIs[TornEntry]:
    """An entry of the torn record this cst vm understands: every key a string, and snapshot, vm,
    repository and host not empty. An entry that is not one is never retried or dropped: it is
    written back as it is."""
    return (
        json_object(e) and has_shape(e, _TORN_KEYS, {}) and all(e[k] for k in ("snapshot", "vm", "repository", "host"))
    )


# Why a torn record that parses is no record (stored_torn found no "torn" list): cst vm and cst doctor.
NO_TORN_LIST = 'no "torn" list'

# How long a change of the torn record waits for another one (a NAS backup of another VM) to end.
TORN_LOCK_TIMEOUT: Final = MINUTE


def stored_torn(data: object) -> list[object] | None:
    """The "torn" list of a parsed torn record, each entry as stored; None when data is not an object
    with one (NO_TORN_LIST). cst vm and cst doctor read the record with it."""
    torn = data.get("torn") if json_object(data) else None
    return items_of(torn) if isinstance(torn, list) else None


class TornRefused(Refused):
    """The torn record cannot be used: it cannot be read as one, a newer cst vm wrote it, or its lock
    stayed taken. A NAS backup refuses to start with it (its retention could count a torn snapshot)."""


class CopiedFile(TypedDict):
    """A file or folder in a manifest: where it came from, what it is and its sizes; stored: where
    a local copy keeps it, relative to the backup folder (a NAS backup has none)."""

    source: str
    stored: NotRequired[str]
    kind: str
    allocated: int
    apparent: int


class Manifest(TypedDict):
    """manifest.json of a backup (local, or staged for restic). The NAS backup adds consistency,
    keep and tags."""

    vm: str
    uuid: str
    timestamp: str
    epoch: int
    mode: str
    consistency: NotRequired[str]
    keep: NotRequired[str | None]
    tags: NotRequired[list[str]]
    files: list[CopiedFile]
    versions: dict[str, str]
    deployment: BootedDeployment | None
    notes: list[str]


class Stop(Exception):
    """SIGTERM/SIGHUP/SIGINT (systemd stopping the unit, a closed terminal,
    Ctrl-C): raised so the overlays are always committed back before the
    program ends."""


class Uncertain(Refused):
    """virsh snapshot-create-as did not answer in time and libvirt's result
    stayed unclear: the overlay files are kept, an alert is sent."""


class ResticMessage(StrEnum):
    """The message_type of a line of `restic backup --json` that a backup reads (others are skipped)."""

    SUMMARY = "summary"
    STATUS = "status"
    ERROR = "error"


class ResticSnapshot(TypedDict):
    """One snapshot of `restic snapshots --json` (the fields the NAS prune and the torn retry read;
    tags as restic gives them, checked where they are read)."""

    id: str
    tags: NotRequired[object]


def is_restic_snapshot(value: object) -> TypeIs[ResticSnapshot]:
    """A snapshot of restic snapshots --json with a non-empty string id."""
    return json_object(value) and isinstance(snap_id := value.get("id"), str) and snap_id != ""


class ResticSummary(TypedDict, total=False):
    """The summary line of `restic backup --json` (the fields a backup reads)."""

    snapshot_id: str
    total_bytes_processed: int
    data_added: int


_SUMMARY_KEYS: KeyTypes = {"snapshot_id": (str,), "total_bytes_processed": (int,), "data_added": (int,)}


def is_restic_summary(value: object) -> TypeIs[ResticSummary]:
    """The summary line of restic backup --json as a backup reads it: snapshot_id, when there, a
    non-empty string; total_bytes_processed and data_added, when there, whole numbers."""
    return has_shape(value, {}, _SUMMARY_KEYS) and json_object(value) and value.get("snapshot_id") != ""


def copied_kind(src: str, tpm_dir: str, *, big: bool) -> str:
    """What a copied source is in the manifest: a disk (big), the TPM folder or the UEFI variables."""
    if big:
        return "disk"
    return "tpm" if src == tpm_dir else "nvram"


def index_entry(manifest: Manifest, path: str) -> IndexEntry:
    """The index entry of a local copy, from its manifest."""
    return {
        "vm": manifest["vm"],
        "timestamp": manifest["timestamp"],
        "epoch": manifest["epoch"],
        "path": path,
        "mode": manifest["mode"],
        "versions": manifest["versions"],
        "deployment": manifest["deployment"],
    }


def utc_stamp(seconds: float) -> str:
    """The UTC stamp of a backup's name (20261108T010640Z)."""
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def manifest_text(manifest: Manifest) -> str:
    """manifest.json as a backup writes it."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def package_change_lines(changes: Sequence[str]) -> list[str]:
    """status's lines about the virtualization packages since the last backup (changes: version_changes)."""
    if not changes:
        return ["  virtualization packages: unchanged since the last backup"]
    return [
        "  virtualization packages changed since then (back up again once the VM works on them):",
        *(f"    {c}" for c in changes),
    ]


def timer_status_lines(st: TimerStatus, vm: str, prog: str) -> list[str]:
    """backup-timer status's lines from the timer's state (st): on or off, its files, the next run
    and the last result."""
    words = timer_file_words(prog, vm)
    out = [f"weekly NAS backup of {vm}: {'on' if st.on else 'off'} ({st.unit}; {ON_CALENDAR}, up to 30 min later)"]
    out += [f"  {p}: {words[state]}" for p, state in st.files.items()]
    if st.next_run:
        out.append(f"  next run: {st.next_run}")
    if st.last_result:
        out.append(f"  last run of the service: {st.last_result}")
    return out
