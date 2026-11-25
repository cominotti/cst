# SPDX-License-Identifier: MPL-2.0
"""The VM's restic snapshots in the NAS repository: a torn one (no backup) forgotten, through the record
vm-nas-torn.json when restic cannot forget it now, the torn ones of earlier runs forgotten before the
retention, and the retention. Part of `cst vm backup --nas` (vm.application.nas, whose NasBackup it
reaches the machine through)."""

from collections.abc import Callable, Sequence
import re
from typing import TYPE_CHECKING

from cst_core.domain.errors import Refused
from cst_core.domain.shapes import list_of, parse_json
from cst_core.domain.text import first_line
from vm.domain import (
    KEEP_TAG,
    RESTIC_CHECK_TIMEOUT,
    RESTIC_FORGET_TIMEOUT,
    RETRY_LOCK,
    TAG,
    NasRepo,
    VmSettings,
    forget_args,
)
from vm.domain.backup import ResticSnapshot, Stop, TornEntry, is_restic_snapshot
from vm.ports import ResticPort

if TYPE_CHECKING:
    from vm.application.nas import NasBackup


def nas_snapshot_ids(restic: ResticPort, vm: str, nas: NasRepo) -> set[str] | None:
    """The ids of the VM's restic snapshots, or None when not readable."""
    snaps = nas_snapshots(restic, vm, nas)
    return None if snaps is None else {s["id"] for s in snaps}


def nas_snapshots(restic: ResticPort, vm: str, nas: NasRepo, host: str | None = None) -> list[ResticSnapshot] | None:
    """The VM's restic snapshots (restic snapshots --json of its tags, made on host: nas's by default),
    or None when not readable."""
    rc, so, _ = restic.run(
        nas["env"],
        [
            "snapshots",
            "--json",
            "--no-lock",
            "--host",
            nas["host"] if host is None else host,
            "--tag",
            f"{TAG},vm:{vm}",
        ],
        RESTIC_CHECK_TIMEOUT,
    )
    if rc != 0:
        return None
    try:
        data = parse_json(so)
    except ValueError, RecursionError:
        return None
    # All or nothing: an answer of another shape is no list of the live snapshots, and reading it
    # as one would drop the index entries of every snapshot it does not name.
    return data if list_of(data, is_restic_snapshot) else None


def forget_torn(
    cfg: VmSettings, p: NasBackup, nas: NasRepo, entry: TornEntry, *, release: Callable[[], None] | None = None
) -> str | None:
    """Forget a restic snapshot that is no backup (incomplete, or not consistent). None when restic
    forgot it; else the words that name the snapshot left in the repository and how to forget it.

    The snapshot goes into the torn record first, and out again once restic forgot it: a stop waits
    until the record is written (its lock: TORN_LOCK_TIMEOUT at most), so a stop then or while forget
    waits for the repository's lock leaves it recorded, and the next NAS backup of the VM forgets it
    before its retention could count it as a backup. A stop after a record that failed says which
    snapshot is left, neither recorded nor forgotten. release runs right after the record, before the
    forget: the stop it raises (one that came before, and waited) leaves the forget to the next run."""
    snap, vm = entry["snapshot"], entry["vm"]
    unrecorded: str | None = None
    try:
        with p.signals.held():
            try:
                p.store.record_torn(entry)
            except (OSError, Refused) as e:
                unrecorded = str(e)
        if release is not None:
            try:
                release()
            except Stop as e:
                if unrecorded is not None:
                    raise
                raise Stop(
                    f"{e}; restic snapshot {snap[:8]} is no backup: recorded in {cfg.paths.torn}, the next NAS "
                    f"backup of {vm} forgets it before its retention"
                ) from None
        failed = forget_snapshot(p, nas, snap)
    except Stop as e:
        if unrecorded is None:
            raise
        raise Stop(
            f"{e}; restic snapshot {snap[:8]} is no backup and could NOT be recorded ({unrecorded}) or forgotten: "
            f"forget it by hand before the next backup of {vm}, whose retention would count it: restic forget "
            f'{snap}, with the repository\'s variables of vm/README.md, section "Restore from the NAS"'
        ) from None
    if failed is None:
        if unrecorded is None:
            try:
                p.store.drop_torn([entry])
            except (OSError, Refused) as e:
                p.out.say(
                    f"  note: restic snapshot {snap[:8]} is forgotten but still in {cfg.paths.torn} ({e}): "
                    f"the next NAS backup of {vm} drops it"
                )
        return None
    left = (
        f"restic snapshot {snap[:8]} could NOT be forgotten ({failed}): it is no "
        f"backup; forget it by hand, restic forget {snap}, with the repository's variables of vm/README.md, "
        'section "Restore from the NAS"'
    )
    if unrecorded is None:
        return f"{left}; recorded in {cfg.paths.torn}: the next NAS backup of {vm} forgets it before its retention"
    return (
        f"{left}; it could NOT be recorded ({unrecorded}): forget it by hand before the next backup of {vm}, "
        "whose retention would count it"
    )


def retry_torn(p: NasBackup, vm: str, nas: NasRepo) -> str | None:
    """Forget the torn snapshots of vm in this repository that an earlier run recorded and could not
    forget, before the retention. A recorded id alone is never enough: restic must still list exactly
    one snapshot of vm's tags that it names (prefix either way), made on the host the entry recorded
    (restic lists a snapshot only under its own host, and NAS_RESTIC_HOST can change), and that one is
    forgotten by its listed id. One that restic no longer lists that way is left alone and dropped from
    the record. Returns why the retention must wait (an entry kept, the listing or the record not
    usable), else None. With no such entry it runs nothing."""
    try:
        mine = [e for e in p.store.read_torn() if e["vm"] == vm and e["repository"] == nas["repo"]]
    except (OSError, Refused) as e:
        return f"the record of torn snapshots cannot be read ({e})"
    if not mine:
        return None
    kept: list[str] = []
    done: list[TornEntry] = []
    for host in sorted({e["host"] for e in mine}):
        listed = nas_snapshots(p.restic, vm, nas, host)
        if listed is None:
            on = "" if host == nas["host"] else f" made on host {host}"
            kept.append(f"restic snapshots did not list the snapshots of {vm}{on}, so the torn ones were not forgotten")
            continue
        for e in mine:
            if e["host"] != host:
                continue
            why = forget_listed(p, vm, nas, e, listed)
            if why is None:
                done.append(e)
            else:
                kept.append(why)
    if done:
        try:
            p.store.drop_torn(done)
        except (OSError, Refused) as e:
            kept.append(f"the record of torn snapshots was not updated ({e})")
    return "; ".join(kept) or None


def forget_listed(p: NasBackup, vm: str, nas: NasRepo, e: TornEntry, listed: Sequence[ResticSnapshot]) -> str | None:
    """Forget the listed snapshot the torn entry e names (retry_torn); None when the entry is done with
    (forgotten now, or restic no longer lists it: forgotten already), else why it stays. A listed one
    without both tags is never forgotten: it stays, and the retention waits."""
    short = e["snapshot"][:8]
    found = [s for s in listed if s["id"].startswith(e["snapshot"]) or e["snapshot"].startswith(s["id"])]
    if len(found) > 1:
        return f"the torn snapshot {short} names {len(found)} listed snapshots"
    if not found:
        p.out.say(
            f"  torn restic snapshot {short}: restic no longer lists it among the {TAG} snapshots of {vm} "
            "(forgotten already): dropped from the record"
        )
        return None
    tags = found[0].get("tags")
    if not isinstance(tags, list) or TAG not in tags or f"vm:{vm}" not in tags:
        return f"restic lists the torn snapshot {short} without both tags {TAG} and vm:{vm}, so it was not forgotten"
    snap = found[0]["id"]
    if (failed := forget_snapshot(p, nas, snap)) is not None:
        return f"restic forget {snap[:8]} (torn, of an earlier run): {failed}"
    p.out.say(f"  torn restic snapshot {snap[:8]} of an earlier run forgotten")
    return None


def forget_snapshot(p: NasBackup, nas: NasRepo, snap: str) -> str | None:
    """restic forget of the one snapshot snap (waiting up to RETRY_LOCK for the repository's lock):
    None when restic forgot it, else why not (restic's first error line, or its exit status)."""
    rc, _, err = p.restic.run(nas["env"], ["forget", "--retry-lock", RETRY_LOCK, snap], RESTIC_FORGET_TIMEOUT)
    return None if rc == 0 else first_line(err) or f"exit {rc}"


def retention_after_torn(cfg: VmSettings, p: NasBackup, vm: str, nas: NasRepo) -> None:
    """The retention, once the torn snapshots of earlier runs are forgotten (retry_torn); skipped when
    one is not: a torn snapshot would take a good backup's place among those kept."""
    why = retry_torn(p, vm, nas)
    if why is None:
        retention(cfg, p, vm, nas)
    else:
        p.out.say(f"  retention skipped: {why}; the repository keeps more until the next backup")


def retention(cfg: VmSettings, p: NasBackup, vm: str, nas: NasRepo) -> None:
    weekly, monthly = p.settings.number("VM_NAS_KEEP_WEEKLY"), p.settings.number("VM_NAS_KEEP_MONTHLY")
    if weekly + monthly == 0:
        p.out.say(
            f"  retention: VM_NAS_KEEP_WEEKLY and VM_NAS_KEEP_MONTHLY are 0 in {cfg.paths.vm_conf}: nothing forgotten"
        )
        return
    rc, so, err = p.restic.run(nas["env"], forget_args(vm, nas["host"], weekly, monthly), RESTIC_FORGET_TIMEOUT)
    if rc != 0:
        p.out.say(
            f"  note: restic forget failed ({first_line(err) or f'exit {rc}'}): the backup is fine, the "
            "retention runs again next time"
        )
        return
    gone = len(re.findall(r"^remove \d+ snapshots?:", so, re.MULTILINE))
    p.out.say(
        f"  retention: {weekly} weekly + {monthly} monthly kept, and those tagged {KEEP_TAG}"
        f"{'; older ones forgotten' if gone else ''} (space is freed by the /home NAS job's weekly prune)"
    )
