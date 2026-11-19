# SPDX-License-Identifier: MPL-2.0
"""What btrfs-snapshot-core prints on stdout: the answers of list and
caches (text or --json) and of latest. The journal lines go through the
log port instead.
"""

from __future__ import annotations

import json
import os
import sys
from typing import NotRequired, TypedDict

from btrfs.snapshot.domain import CacheWalk, Listing, SourceCaches, SourceListing
from cst_core.domain.values import format_bytes_human


class PathReasonJson(TypedDict):
    """An entry of ignored, unverifiable or problems in `list --json` and `caches --json`."""

    path: str
    reason: str


class ListedSnapshotJson(TypedDict):
    """One snapshot of a source in `list --json`, with its decision."""

    path: str
    entry: str
    time: str
    uuid: str
    gen_at_creation: int
    action: str
    reason: str


class ListedSnapshotsJson(TypedDict):
    """The part of a readable source in `list --json`."""

    uuid: str
    generation: int
    changed: bool
    snapshots: list[ListedSnapshotJson]
    ignored: list[PathReasonJson]
    unverifiable: list[PathReasonJson]
    deletions_blocked: str | None


class SourceListingJson(TypedDict):
    """One source of `list --json`: name, source and policy, then error or the listed part."""

    name: str
    source: str
    policy: str
    error: NotRequired[str]
    uuid: NotRequired[str]
    generation: NotRequired[int]
    changed: NotRequired[bool]
    snapshots: NotRequired[list[ListedSnapshotJson]]
    ignored: NotRequired[list[PathReasonJson]]
    unverifiable: NotRequired[list[PathReasonJson]]
    deletions_blocked: NotRequired[str | None]


def source_listing_json(s: SourceListing) -> SourceListingJson:
    """One source of `list --json` (an "error" entry when it could not be read)."""
    if s.error is not None:
        return {"name": s.name, "source": s.source, "policy": s.policy, "error": s.error}
    return {"name": s.name, "source": s.source, "policy": s.policy, **listed_snapshots_json(s)}


def listed_snapshots_json(s: SourceListing) -> ListedSnapshotsJson:
    """The part of a readable source in `list --json`."""
    return {
        "uuid": s.uuid,
        "generation": s.generation,
        "changed": s.changed,
        "snapshots": [
            {
                "path": d.snap.path,
                "entry": d.snap.entry,
                "time": d.snap.wall.strftime("%Y-%m-%dT%H:%M"),
                "uuid": d.snap.uuid,
                "gen_at_creation": d.snap.cgen,
                "action": d.action,
                "reason": d.reason,
            }
            for d in s.decisions
        ],
        "ignored": [{"path": p, "reason": r} for p, r in s.ignored],
        "unverifiable": [{"path": p, "reason": r} for p, r in s.unverifiable],
        "deletions_blocked": s.deletions_blocked,
    }


def render_listing(listing: Listing, *, as_json: bool) -> None:
    """`list`: every snapshot with its decision, per source."""
    if as_json:
        sources = [source_listing_json(s) for s in listing.sources]
        json.dump({"snapdir": listing.snapdir, "now": listing.now, "sources": sources}, sys.stdout, indent=2)
        print()
        return
    for s in listing.sources:
        if s.error is not None:
            print(f"{s.name}: {s.source}: ERROR {s.error}")
            continue
        print(
            f"{s.name}: {s.source} (generation {s.generation}, "
            f"{'changed' if s.changed else 'unchanged'} since the newest snapshot), policy '{s.policy}'"
        )
        for d in s.decisions:
            print(f"  {d.action.upper():7} {d.snap.entry:<32} {d.reason}")
        for p, r in s.ignored:
            print(f"  {'IGNORED':7} {os.path.basename(p):<32} {r}")
        for p, r in s.unverifiable:
            print(f"  {'UNKNOWN':7} {os.path.basename(p):<32} {r}")
        n_del = sum(1 for d in s.decisions if d.action == "delete")
        print(
            f"  {len(s.decisions)} snapshot(s), {n_del} due for deletion"
            + (f"; deletions would be blocked: {s.deletions_blocked}" if s.deletions_blocked else "")
        )


class CacheFolderJson(TypedDict):
    """One tagged folder in `caches --json`."""

    path: str
    files: int
    bytes: int


class CacheWalkJson(TypedDict):
    """The part of a readable source in `caches --json`."""

    folders: list[CacheFolderJson]
    files: int
    bytes: int
    kept: list[str]
    skipped: list[str]
    problems: list[PathReasonJson]
    seconds: float


class SourceCachesJson(TypedDict):
    """One source of `caches --json`: name, source and skips, then error or the walk's part."""

    name: str
    source: str
    skips: list[str]
    error: NotRequired[str | None]
    folders: NotRequired[list[CacheFolderJson]]
    files: NotRequired[int]
    bytes: NotRequired[int]
    kept: NotRequired[list[str]]
    skipped: NotRequired[list[str]]
    problems: NotRequired[list[PathReasonJson]]
    seconds: NotRequired[float]


def source_caches_json(s: SourceCaches) -> SourceCachesJson:
    """One source of `caches --json` (an "error" entry when it could not be read)."""
    if s.walk is None:
        return {"name": s.name, "source": s.source, "skips": list(s.skips), "error": s.error}
    return {"name": s.name, "source": s.source, "skips": list(s.skips), **walk_json(s.walk)}


def walk_json(walk: CacheWalk) -> CacheWalkJson:
    """The part of a readable source in `caches --json`."""
    return {
        "folders": [{"path": c.rel, "files": c.files, "bytes": c.size} for c in walk.folders],
        "files": walk.files,
        "bytes": walk.size,
        "kept": walk.kept,
        "skipped": walk.skipped,
        "problems": [{"path": p, "reason": r} for p, r in walk.problems],
        "seconds": round(walk.seconds, 2),
    }


def render_caches(sources: list[SourceCaches], *, as_json: bool) -> None:
    """`caches`: the cache folders --exclude-caches would drop, per source."""
    if as_json:
        json.dump({"sources": [source_caches_json(s) for s in sources]}, sys.stdout, indent=2)
        print()
        return
    for s in sources:
        if s.walk is None:
            print(f"{s.name}: {s.source}: ERROR {s.error}")
            continue
        walk = s.walk
        print(
            f"{s.name}: {s.source}: {len(walk.folders)} cache folder(s), {walk.files} files, "
            f"{format_bytes_human(walk.size)} (apparent size); walk {round(walk.seconds, 2):.1f} s"
        )
        for c in walk.folders:
            print(f"  DROP   {c.rel:<56} {c.files:>9} files {format_bytes_human(c.size):>10}")
        for rel in walk.kept:
            print(f"  KEEP   {rel}  (a cache folder that holds a skipped path)")
        for rel in walk.skipped:
            print(f"  SKIP   {rel}  (skip list: kept whole)")
        for p, r in walk.problems:
            print(f"  ERROR  {p}: {r}")


def render_latest(path: str | None) -> None:
    """`latest`: the newest snapshot's path (nothing when there is none)."""
    if path is not None:
        print(path)
