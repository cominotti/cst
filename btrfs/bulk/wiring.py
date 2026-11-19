# SPDX-License-Identifier: MPL-2.0
"""The composition root of btrfs-bulk: who runs it (root or not), its
paths (the BTRFS_BULK_* overrides of tests and dry runs, never honoured as
root), whose home it handles (--user NAME, BULK_USER of bulk.conf, or the
caller), where btrfs-snapshot is, and the use cases' ports over the real
adapters.

btrfs/bulk/cli.py parses argv, asks this module for a Bulk and runs one command.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
import os

from btrfs.bulk.adapters.host import Host, Output, default_home, error_trace, is_root, lookup_user, real_path
from btrfs.bulk.adapters.machine import HomeTree, Primitives
from btrfs.bulk.adapters.state import StateFiles, bulk_user
from btrfs.bulk.adapters.user import Account, UserFiles
from btrfs.bulk.application.context import Bulk
from btrfs.bulk.domain import BadSettings, BulkPaths, Ctx, NoUser, split_user_args
from btrfs.bulk.ports import HostPort, OutputPort
from cst_core.adapters.files import CounterFile
from cst_core.adapters.notify import PENDING_ALERTS
from cst_core.adapters.runtime import sibling_program
from cst_core.domain.values import ROOT_ACCOUNT


def bulk_paths(environ: Mapping[str, str], *, root: bool) -> BulkPaths:
    """The paths, with the overrides for tests and dry runs; never honoured as root."""
    base = BulkPaths()
    if root:
        return base
    return BulkPaths.under(
        etc=environ.get("BTRFS_BULK_ETC", base.etc),
        state=environ.get("BTRFS_BULK_STATE", base.state),
        lock=environ.get("BTRFS_BULK_LOCK", base.lock),
    )


class Wiring:
    """btrfs-bulk put together for one run. entry_file: the running entry
    point's __file__ (where btrfs-snapshot is found); snapshot: btrfs-snapshot's
    path when given (BTRFS_BULK_SNAPSHOT, never as root); home: the home of a
    run that is not root (BTRFS_BULK_HOME, else the caller's); alerts: where an alert btrfs-notify
    could not take is kept (BTRFS_BULK_ALERTS, never as root)."""

    def __init__(
        self,
        entry_file: str,
        *,
        root: bool,
        paths: BulkPaths,
        snapshot: str | None = None,
        home: str | None = None,
        machine: Primitives | None = None,
        host: HostPort | None = None,
        out: OutputPort | None = None,
        alerts: str = PENDING_ALERTS,
    ) -> None:
        self.entry_file, self.root, self.paths = entry_file, root, paths
        self.snapshot, self.home = snapshot, home
        self.machine = Primitives() if machine is None else machine
        self.tree = HomeTree(self.machine)
        self.host: HostPort = (
            Host(paths, self.tree, self.snapshot_program, alerts=alerts, runner=self.machine.runner)
            if host is None
            else host
        )
        self.out: OutputPort = Output() if out is None else out

    @classmethod
    def from_environment(cls, entry_file: str) -> Wiring:
        root = is_root()
        env = os.environ
        return cls(
            entry_file,
            root=root,
            paths=bulk_paths(env, root=root),
            snapshot=None if root else env.get("BTRFS_BULK_SNAPSHOT"),
            home=None if root else env.get("BTRFS_BULK_HOME") or None,
            alerts=PENDING_ALERTS if root else env.get("BTRFS_BULK_ALERTS") or PENDING_ALERTS,
        )

    def snapshot_program(self) -> str:
        """btrfs-snapshot: the override when set; else the checkout's copy from a
        checkout, the installed one (its manifest dest) from the zipapp."""
        if self.snapshot is not None:
            return self.snapshot
        if not self.entry_file:
            raise RuntimeError("btrfs-bulk's entry file is not known: main() names it")
        return sibling_program("btrfs/btrfs-snapshot", self.entry_file)

    def trace(self, e: Exception) -> str:
        """e and its traceback, as Python prints an uncaught exception."""
        return error_trace(e)

    def state(self) -> StateFiles:
        return StateFiles(self.paths)

    def home_ctx(self, home: str) -> Ctx:
        """The caller's own home (no user switch)."""
        return Ctx(real_path(home))

    def user(self, args: Sequence[str]) -> tuple[Ctx, Account | None]:
        """Whose home, from --user NAME (root) or, not as root, the current
        user and the home override. As root without a name: BULK_USER of
        bulk.conf (BadSettings on a bad line, or a name no account has);
        NoUser without either, or for a --user NAME no account has."""
        _, user_args = split_user_args(args)
        name = user_args[1] if len(user_args) == 2 else None
        if self.root:
            from_conf = name is None or name == ""
            if from_conf:
                name = bulk_user(self.paths.conf)  # a name or None, never ""
            if name is None or name == ROOT_ACCOUNT:
                raise NoUser("btrfs-bulk: needs --user NAME (not root)")
            try:
                pw = lookup_user(name)
            except KeyError:
                if from_conf:  # bulk.conf names nobody: fixed there, like a bad line
                    raise BadSettings(f"{self.paths.conf}: BULK_USER={name}: no such account") from None
                raise NoUser(f"btrfs-bulk: --user {name}: no such account") from None
            return Ctx(real_path(pw.pw_dir), uid=pw.pw_uid), pw
        return self.home_ctx(self.home or default_home()), None

    def user_ctx(self, args: Sequence[str]) -> Ctx:
        return self.user(args)[0]

    def keep_ctx(self, user_args: Sequence[str]) -> Ctx | None:
        """The home keep/unkeep looks at, if known: as root only with --user."""
        try:
            if self.root:
                return self.user_ctx(user_args) if user_args else None
            return self.user_ctx([])
        except BadSettings, NoUser:
            return None

    def build(self, ctx: Ctx, pw: Account | None = None) -> Bulk:
        return Bulk(
            ctx=ctx,
            paths=self.paths,
            user=UserFiles(self.tree, pw, ctx.home),
            tree=self.tree,
            state=self.state(),
            boots=CounterFile(self.paths.attempts),
            host=self.host,
            out=self.out,
        )

    def bulk(self, args: Sequence[str]) -> Bulk:
        ctx, pw = self.user(args)
        return self.build(ctx, pw)
