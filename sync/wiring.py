# SPDX-License-Identifier: MPL-2.0
"""Composition root for `cst sync`."""

from __future__ import annotations

import os

from cst_core.adapters.runtime import restore_default_signals, undo_python_start
from sync.adapters import HostSyncExecAdapter, HostSyncInspectAdapter
from sync.domain import SYNC_PROG, SyncAction, SyncArgs
from sync.ports import SyncExecPort, SyncInspectPort


def sync_prog() -> str:
    """Resolve invoking command name from environment."""
    return os.environ.get("CST_PROG") or SYNC_PROG


def prepare_process(args: SyncArgs) -> None:
    """Undo Python start defaults; keep SIGPIPE default only on read-only plan/help."""
    if args.action in (SyncAction.PLAN, SyncAction.HELP):
        restore_default_signals()
    else:
        undo_python_start()


def build_adapters(entry_file: str, prog: str) -> tuple[SyncInspectPort, SyncExecPort]:
    """Build real host inspection and execution adapters."""
    env = dict(os.environ)
    return HostSyncInspectAdapter(entry_file, env=env), HostSyncExecAdapter(entry_file, prog=prog, env=env)
