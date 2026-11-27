# SPDX-License-Identifier: MPL-2.0
"""Port protocols for `cst sync`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from cst_core.domain.install import Owner
from cst_core.domain.record import InstallRecord, RecordedFile
from sync.domain import SyncArgs


class SyncInspectPort(Protocol):
    """Read-only inspection of install records, files on disk, and desired checkout artifacts."""

    def in_container(self) -> bool:
        """True when running inside a Podman toolbox or Docker container."""
        ...

    def euid(self) -> int:
        """Effective UID of the current process."""
        ...

    def target_user(self) -> tuple[str, int, str]:
        """(username, uid, home_dir) of the user account whose `~/.local/state/cst/installed.json` applies.
        Returns `("", 0, "")` if running directly as root with no invoking non-root user.
        """
        ...

    def read_record(self, owner: Owner, home: str) -> tuple[InstallRecord | None, str | None]:
        """Read root or user install record.

        Returns `(record_or_None_if_absent, fatal_error_message_or_None)`.
        """
        ...

    def stat_and_hash(self, dest: str) -> tuple[bool, str | None]:
        """Return `(exists_on_disk, sha256_or_None_if_missing_or_unreadable)` for DEST."""
        ...

    def desired_manifest_sha256(self, src: str, values: dict[str, str] | None) -> str | None:
        """Return sha256 of SRC from checkout (rendered with VALUES if template or rustc-wrapper), or None."""
        ...

    def desired_generated_sha256(self, dest: str, f: RecordedFile) -> str | None:
        """Return sha256 of generated file DEST using `desired_generated_bytes`, or None."""
        ...


class SyncExecPort(Protocol):
    """Execution port for invoking CST module refresh commands and single-sudo subpass."""

    def run_cst_subcommand(self, argv: Sequence[str], *, as_user: str | None = None) -> int:
        """Run one `cst <module> ...` subcommand, stepping down to AS_USER when provided; returns exit code."""
        ...

    def run_root_subpass(self, args: SyncArgs) -> int:
        """Re-invoke `cst sync apply --root-only ...` once via sudo for root-owned components."""
        ...
