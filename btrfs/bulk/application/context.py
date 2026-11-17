# SPDX-License-Identifier: MPL-2.0
"""What every btrfs-bulk use case works on: whose home (Ctx), the
program's paths and its ports, bundled as Bulk."""

from __future__ import annotations

from dataclasses import dataclass, field

from btrfs.bulk.domain import BulkPaths, Ctx
from btrfs.bulk.ports import BootCounterPort, BulkStatePort, HomeTreePort, HostPort, OutputPort, UserFilesPort


@dataclass(frozen=True, slots=True, kw_only=True)
class Bulk:
    """One run for one home. boots: the boot step's attempts. bulk_only: the ids whose problem in the last
    apply() lies inside ~/Bulk only (the in-place +C rewrite of a folder
    already linked into ~/Bulk failed or was refused before it wrote a
    journal: no free space, a copy check, foreign entries)."""

    ctx: Ctx
    paths: BulkPaths
    user: UserFilesPort
    tree: HomeTreePort
    bulk_only: set[str] = field(default_factory=set)
    state: BulkStatePort
    boots: BootCounterPort
    host: HostPort
    out: OutputPort
