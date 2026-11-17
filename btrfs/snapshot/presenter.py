# SPDX-License-Identifier: MPL-2.0
"""What btrfs-snapshot-core prints on stdout: the answers of list and
caches (text or --json) and of latest. The journal lines go through the
log port instead.
"""

from __future__ import annotations

import json
import os
import sys
from btrfs.snapshot.domain import Listing, SourceCaches, SourceListing
from cst_core.domain.values import format_bytes_human


def source_listing_json(s: SourceListing) -> dict[str, object]:
    """One source of `list --json` (an "error" entry when it could not be read)."""
    entry: dict[str, object] = {"name": s.name, "source": s.source, "policy": s.policy}
    if s.error is not None:
        entry["error"] = s.error
        return entry
    entry.update(
        {
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
    )
    return entry


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


def source_caches_json(s: SourceCaches) -> dict[str, object]:
    """One source of `caches --json` (an "error" entry when it could not be read)."""
    entry: dict[str, object] = {"name": s.name, "source": s.source, "skips": list(s.skips)}
    if s.walk is None:
        entry["error"] = s.error
        return entry
    walk = s.walk
    entry.update(
        {
            "folders": [{"path": c.rel, "files": c.files, "bytes": c.size} for c in walk.folders],
            "files": walk.files,
            "bytes": walk.size,
            "kept": walk.kept,
            "skipped": walk.skipped,
            "problems": [{"path": p, "reason": r} for p, r in walk.problems],
            "seconds": round(walk.seconds, 2),
        }
    )
    return entry


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
