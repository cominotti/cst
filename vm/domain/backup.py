# SPDX-License-Identifier: MPL-2.0
"""The records of the backups: an index entry, the two manifests, restic's summary of a backup,
the fingerprint of a source, the two ways a backup stops (Stop, Uncertain) and the state of one NAS
backup run (NasRun); the NAS backup's snapshot arguments, index, torn records and last lines, and the
lines status and backup-timer status print about them. Pure."""

from collections.abc import Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass, field
import datetime
import json
from enum import StrEnum
from typing import Final, NamedTuple, NotRequired, Required, TypedDict, TypeIs

from cst_core.domain.errors import ExitStatus, Refused, ResticExit
from cst_core.domain.shapes import KeyTypes, has_shape, items_of, json_object
from cst_core.domain.values import MINUTE, Json
from vm.domain import (
    ADOPTED_TAG,
    KEEP_TAG,
    ON_CALENDAR,
    TAG,
    DomainParts,
    NasRepo,
    TimerStatus,
    VmOverlayKind,
    timer_file_words,
)

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


class BackupMode(StrEnum):
    """How a backup was made, the mode of its manifest and its index entry: a local copy by reflink
    or in full (--full), or a restic snapshot on the NAS."""

    REFLINK = "reflink"
    FULL = "full"
    NAS = "nas"


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
    where = e.get("snapshot") if e.get("mode") == BackupMode.NAS else e.get("path")
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


def torn_entry(nas: NasRepo, snap: str, vm: str, stamp: str, why: str) -> TornEntry:
    """The record of snap, a torn snapshot of vm's run at stamp (why: what tore it)."""
    return {"snapshot": snap, "vm": vm, "repository": nas["repo"], "host": nas["host"], "timestamp": stamp, "why": why}


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
    tags and original as restic gives them, checked where they are read). original: the id the
    snapshot had first, which restic sets when restic tag saves it again under a new id."""

    id: str
    time: NotRequired[object]
    tags: NotRequired[object]
    original: NotRequired[object]


def is_restic_snapshot(value: object) -> TypeIs[ResticSnapshot]:
    """A snapshot of restic snapshots --json with a non-empty string id."""
    return json_object(value) and isinstance(snap_id := value.get("id"), str) and snap_id != ""


def snapshot_names(snap: ResticSnapshot) -> list[str]:
    """The ids a listed snapshot answers to: its own, and the one it had first when its tags were
    changed (restic tag saves it again under a new id, with that first id as its original)."""
    original = snap.get("original")
    return [snap["id"], *([original] if isinstance(original, str) and original else [])]


def restic_instant(value: object) -> datetime.datetime | None:
    """A snapshot's time as restic snapshots --json gives it (RFC 3339: nanoseconds, the local offset
    or Z), as an instant; None for anything else, a time without an offset included (it cannot be
    compared with one that has it)."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _names_any(snap: ResticSnapshot, ids: Sequence[str]) -> bool:
    """snap (by its id or its original) is one of ids, a prefix either way, as restic matches a short id."""
    return any(n.startswith(i) or i.startswith(n) for n in snapshot_names(snap) for i in ids if i)


def stray_snapshots(
    listed: Sequence[ResticSnapshot], indexed: Sequence[str], torn: Sequence[str], made: str
) -> list[ResticSnapshot]:
    """The listed snapshots of a VM that no record names, each of which may be torn: neither the
    index (indexed: the ids of its NAS entries), the record of torn snapshots (torn), the run's own
    snapshot (made, "" none) nor the tag ADOPTED_TAG accounts for it. Only one not older than the
    oldest listed snapshot the index names (the baseline) is a stray: those before it are an older
    CST's or from before the index was started again, and stay. No listed snapshot the index names: no
    baseline, no stray. A time that cannot be read is no baseline, and its snapshot can be a stray
    (the retention keeps more, never less)."""
    times = [t for s in listed if _names_any(s, indexed) and (t := restic_instant(s.get("time"))) is not None]
    if not times:
        return []
    baseline = min(times)
    known = [*indexed, *torn, made]

    def stray(s: ResticSnapshot) -> bool:
        tags, t = s.get("tags"), restic_instant(s.get("time"))
        adopted = isinstance(tags, list) and ADOPTED_TAG in tags
        return not adopted and not _names_any(s, known) and (t is None or t >= baseline)

    return [s for s in listed if stray(s)]


# The alert of a NAS run whose repository has a snapshot of the VM that no record names.
STRAYS_TITLE: Final = "VM backup: a NAS snapshot no record names"


def _stray_names(strays: Sequence[ResticSnapshot]) -> str:
    def when(s: ResticSnapshot) -> str:
        t = restic_instant(s.get("time"))
        return "time unknown" if t is None else t.isoformat(timespec="seconds")

    return ", ".join(f"{s['id'][:8]} ({when(s)})" for s in strays)


def strays_text(vm: str, strays: Sequence[ResticSnapshot]) -> str:
    """Why the retention waits for the strays of vm (stray_snapshots), with the commands that forget or
    keep each: one stray's full id in them, several as ID with their full ids after."""
    n = len(strays)
    ident = strays[0]["id"] if n == 1 else "ID"
    each = "" if n == 1 else f" (ID: each of {' '.join(s['id'] for s in strays)})"
    return (
        f"restic lists {n} snapshot{'s' if n > 1 else ''} of {vm} that neither the index nor the record of "
        f"torn snapshots names (each may be torn): {_stray_names(strays)}; look at each (restic ls {ident}), "
        f"then forget it (restic forget {ident}) or keep it as a backup (restic tag --add {ADOPTED_TAG} "
        f'{ident}){each}, with the repository\'s variables of vm/README.md, section "Restore from the NAS"'
    )


def strays_alert(vm: str, strays: Sequence[ResticSnapshot]) -> str:
    """The alert's body for the strays of vm: short, the commands are in the backup's output."""
    return (
        f"{vm}: the NAS repository has {len(strays)} snapshot(s) of {vm} that no record names "
        f"({', '.join(s['id'][:8] for s in strays)}); its retention waits until each is forgotten "
        f"(restic forget ID) or kept (restic tag --add {ADOPTED_TAG} ID): see the backup's output"
    )


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


class StopAfterSnapshot(Stop):
    """A stop that ended restic after restic had written its snapshot (its summary named it): the stop's
    message, with restic's exit status and its summary, so the run records the snapshot as torn (or
    names it) before the stop ends the read."""

    def __init__(self, message: str, rc: int, summary: ResticSummary) -> None:
        super().__init__(message)
        self.rc = rc
        self.summary = summary


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


def snapshot_args(
    vm: str, stamp: str, specs: Mapping[str, tuple[str, str]], others: Sequence[str], *, quiesce: bool
) -> list[str]:
    """virsh's arguments of the NAS backup's external disk-only snapshot: an overlay for each disk of
    specs (target -> (image, overlay); a comma in its path doubled, as virsh reads it), none for the
    others; quiesce: the guest's file systems frozen through its agent."""
    args: list[str] = [
        "snapshot-create-as",
        vm,
        "--name",
        f"cst-nas-{stamp}",
        "--disk-only",
        "--atomic",
        "--no-metadata",
    ]
    if quiesce:
        args.append("--quiesce")
    for t, (_, overlay) in specs.items():
        args += ["--diskspec", f"{t},snapshot=external,file={overlay.replace(',', ',,')}"]
    for t in others:
        args += ["--diskspec", f"{t},snapshot=no"]
    return args


def restic_failure_why(rc: int) -> str:
    """Why restic backup gave no backup, from its exit status."""
    return {
        ResticExit.INCOMPLETE: "some files could not be read",
        ExitStatus.TIMED_OUT: "stopped at the time limit (VM_NAS_TIMEOUT_HOURS)",
        ExitStatus.NOT_FOUND: "restic not found",
        ResticExit.LOCKED: "the repository stayed locked",
    }.get(rc, f"restic exit {rc}")


def nas_index(entries: Sequence[IndexEntry], entry: IndexEntry, alive: AbstractSet[str] | None) -> list[IndexEntry]:
    """The index with entry, a new NAS backup (an entry of the same snapshot replaced); alive: the ids
    of the VM's snapshots restic lists (None: not readable), without which the VM's NAS entries of
    entry's repository leave it."""
    snap = entry["snapshot"]
    out = [e for e in entries if not (e.get("mode") == BackupMode.NAS and e.get("snapshot") == snap)]
    out.append(entry)
    if alive is not None:
        alive = alive | {snap}
        out = [
            e
            for e in out
            if not (
                e["vm"] == entry["vm"]
                and e.get("mode") == BackupMode.NAS
                and e.get("repository") == entry["repository"]
                and not any(a.startswith(e["snapshot"]) or e["snapshot"].startswith(a) for a in alive)
            )
        ]
    out.sort(key=lambda e: (e["vm"], e.get("epoch") or 0))
    return out


def nas_done_lines(notes: Sequence[str], snap: str, vm: str, consistency: str, keep: str | None) -> list[str]:
    """The last lines of a NAS backup: its notes, the snapshot it made, how to restore it."""
    kept = f"; kept until removed by hand: tags {KEEP_TAG},{keep}" if keep is not None and keep != "" else ""
    return [
        *(f"  note: {n}" for n in notes),
        f"Done: restic snapshot {snap[:8]} of {vm} ({consistency}{kept}).",
        (
            'Restore: vm/README.md in CST, section "Restore from the NAS" (restic restore '
            f"{snap[:8]} --sparse --target <a NOCOW folder>)."
        ),
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class NasRun:
    """One NAS backup: what its checks read before restic is asked anything (the definition, the
    sources, the stamp, the live snapshot's overlays: specs target -> (image, overlay), others the
    targets left out), and what it fills in as it goes, also when a step fails: notes, before (the
    fingerprints of what is checked after the read) and overlaid (the overlays the VM runs on), which
    the merge back reads."""

    vm: str
    live: bool
    quiesce: bool
    xml_text: str
    parts: DomainParts
    notes: list[str]
    sources: list[tuple[str, str]]
    epoch: float
    stamp: str
    specs: dict[str, tuple[str, str]]
    others: list[str]
    agent: str
    versions: dict[str, str]
    deployment: BootedDeployment | None
    before: dict[str, Fingerprint] = field(default_factory=dict)
    overlaid: dict[str, tuple[str, str]] = field(default_factory=dict)
    # The snapshot's unclear outcome (virsh did not answer, and some disk never showed its overlay): the
    # words of the alert (one at most), which nas_run sends however the read ended.
    unclear: list[str] = field(default_factory=list)


class NasMergeBack(NamedTuple):
    """What the merge back after a NAS backup could not do (problems), how each overlay stood (how),
    and the disks it put back on their image (back: both listings show the image)."""

    problems: list[str]
    how: dict[str, VmOverlayKind]
    back: frozenset[str]


def nas_tags(vm: str, keep: str | None) -> list[str]:
    """restic's tags of a NAS backup of vm: every VM backup's, the VM's, and cst-keep with keep (--keep-tag)."""
    return [TAG, f"vm:{vm}"] + ([KEEP_TAG, keep] if keep is not None and keep != "" else [])


def nas_manifest(run: NasRun, files: list[CopiedFile], consistency: str, keep: str | None) -> Manifest:
    """The manifest a NAS backup stages for restic (files: the sources with their sizes), with
    restic's tags (nas_tags)."""
    return {
        "vm": run.vm,
        "uuid": run.parts.uuid,
        "timestamp": run.stamp,
        "epoch": int(run.epoch),
        "mode": BackupMode.NAS,
        "consistency": consistency,
        "keep": keep,
        "tags": nas_tags(run.vm, keep),
        "files": files,
        "versions": run.versions,
        "deployment": run.deployment,
        "notes": run.notes,
    }


def nas_index_entry(run: NasRun, nas: NasRepo, snap: str, consistency: str, keep: str | None) -> IndexEntry:
    """The index entry of a NAS backup: restic's snapshot snap in nas's repository."""
    return {
        "vm": run.vm,
        "timestamp": run.stamp,
        "epoch": int(run.epoch),
        "mode": BackupMode.NAS,
        "snapshot": snap,
        "repository": nas["repo"],
        "host": nas["host"],
        "consistency": consistency,
        "keep": keep,
        "versions": run.versions,
        "deployment": run.deployment,
    }
