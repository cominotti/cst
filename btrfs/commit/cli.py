# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-commit-interval` / `cst btrfs commit-interval`."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.commit import adapters as commit_adapters, domain as commit_domain
from btrfs.commit.domain import CommitAction, CommitParsedArgs
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start

CommitCliDeps = ManagedCliEnv


def _commit_parse_error(parsed: CommitParsedArgs) -> tuple[int, str] | None:
    return None if parsed.error is None else (parsed.code, parsed.error)


def _commit_write_action(parsed: CommitParsedArgs) -> str | None:
    return parsed.action.value if commit_domain.is_commit_write(parsed.action) else None


def _resolve_commit_prog(raw: str) -> str:
    return "cst btrfs commit-interval" if raw == "cst btrfs" else (raw or "cst btrfs commit-interval")


_COMMIT_SPEC = ManagedCliSpec(
    default_prog="cst btrfs commit-interval",
    parse=commit_domain.parse_commit_args,
    format_usage=commit_domain.format_commit_usage,
    is_help=lambda p: p.action == CommitAction.HELP,
    parse_error=_commit_parse_error,
    write_action=_commit_write_action,
    elevation_reason="modifies /etc/fstab, rpm-ostree kargs, live mount commit interval, and top-mount unit",
    execute=commit_adapters.dispatch_commit_cli_action,
    resolve_prog=_resolve_commit_prog,
)


def run_commit_cli(argv: Sequence[str] | None = None, *, deps: CommitCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root on writes, and invoke `btrfs-commit-interval` adapters."""
    return run_managed_cli(sys.argv[1:] if argv is None else argv, _COMMIT_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-commit-interval`."""
    undo_python_start()
    return run_commit_cli(argv, deps=CommitCliDeps(entry_file=entry_file))
