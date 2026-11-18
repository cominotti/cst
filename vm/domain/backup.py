# SPDX-License-Identifier: MPL-2.0
"""The records of the backups: an index entry, the two manifests, restic's summary of a backup,
the fingerprint of a source, and the two ways a backup stops (Stop, Uncertain). Pure."""

import datetime
import json
from enum import StrEnum
from typing import NotRequired, TypedDict

from cst_core.domain.errors import Refused
from cst_core.domain.values import Json

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
    deployment: BootedDeployment | None


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


class ResticSummary(TypedDict, total=False):
    """The summary line of `restic backup --json` (the fields a backup reads)."""

    snapshot_id: str
    total_bytes_processed: int
    data_added: int


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
