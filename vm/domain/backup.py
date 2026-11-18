# SPDX-License-Identifier: MPL-2.0
"""The records of the backups: an index entry, the two manifests, restic's summary of a backup,
and the fingerprint of a source. Pure."""

import datetime
import json
from typing import NotRequired, TypedDict

from cst_core.domain.values import Json

# What changes when something writes to a file or a folder tree (vm.adapters.backup.fingerprint):
# only compared, before and after a copy.
type Fingerprint = tuple[object, ...]


class IndexEntry(TypedDict, total=False):
    """One backup in the index (vm-backups.json). A local copy has path, a NAS backup snapshot,
    repository and host. Keys this cst vm does not know are kept as they are."""

    vm: str
    timestamp: str
    epoch: int
    path: str
    mode: str
    snapshot: str
    repository: str
    host: str
    consistency: str
    keep: str | None
    versions: dict[str, str]
    deployment: dict[str, Json] | None


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
    deployment: dict[str, Json] | None
    notes: list[str]


class ResticSummary(TypedDict, total=False):
    """The summary line of `restic backup --json` (the fields a backup reads)."""

    snapshot_id: str
    total_bytes_processed: int
    data_added: int


def utc_stamp(seconds: float) -> str:
    """The UTC stamp of a backup's name (20261108T010640Z)."""
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def manifest_text(manifest: Manifest) -> str:
    """manifest.json as a backup writes it."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"
