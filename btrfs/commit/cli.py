# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-commit-interval` / `cst btrfs commit-interval`."""

from __future__ import annotations

from collections.abc import Sequence

from btrfs.commit import adapters as commit_adapters, domain as commit_domain
from btrfs.compress.adapters import make_btrfs_option_cli_spec
from cst_core.adapters.runtime import ManagedCliEnv, run_managed_cli, run_managed_entry

CommitCliDeps = ManagedCliEnv

_COMMIT_SPEC = make_btrfs_option_cli_spec(
    commit_domain.COMMIT_PROG,
    parse=commit_domain.parse_commit_args,
    format_usage=commit_domain.format_commit_usage,
    elevation_reason="modifies /etc/fstab, rpm-ostree kargs, live mount commit interval, and top-mount unit",
    execute=commit_adapters.dispatch_commit_cli_action,
)


def run_commit_cli(argv: Sequence[str] | None = None, *, deps: CommitCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root on writes, and invoke `btrfs-commit-interval` adapters."""
    args: Sequence[str] = () if argv is None else argv
    return run_managed_cli(args, _COMMIT_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-commit-interval`."""
    return run_managed_entry(_COMMIT_SPEC, argv, entry_file)
