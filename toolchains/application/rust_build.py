# SPDX-License-Identifier: MPL-2.0
"""rust:build: cargo's build folders under the root build-dir names (one per workspace
path; cargo never deletes them). plan says what apply would delete and why; apply deletes
one folder at a time, planned again after each, through BuildTreePort.delete/trim
(cargo's locks held, the users and the rule checked again, renamed away, removed).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from cst_core.domain.errors import Refused
from toolchains.application.prune import BaseTarget
from toolchains.domain.build import (
    ROOT_REL,
    WORKSPACE_REL,
    CargoCache,
    Folder,
    FolderAction,
    RustPolicy,
    Workspaces,
    after_total,
    decide,
    merge_users,
    unknown_text,
    users_text,
)
from toolchains.domain.common import ago, gib, span, tilde, when
from toolchains.domain.prune import RM_MARGIN, RUST_BUILD

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from toolchains.ports import RunOutputPort


class RustBuild(BaseTarget[RustPolicy]):
    """rust:build: cargo's build folders under the root build-dir names."""

    SPEC = RUST_BUILD

    def location(self) -> str:
        return os.path.join(self.home, ROOT_REL)

    def check(self) -> None:
        self.machine.tree.root()

    def gather(self) -> tuple[str | None, list[Folder], list[Folder], list[tuple[str, str]]]:
        root = self.machine.tree.root()
        if root is None:
            return None, [], [], []
        folders, trash, others = self.machine.tree.scan(root, self.now)
        return root, folders, trash, others

    def workspaces(self, root: str) -> tuple[dict[str, list[str]] | None, Workspaces | None]:
        """(claimed or None, Workspaces or None): the folders claimed by a
        workspace; None when the orphan rule is off or the search is
        incomplete (then nothing is an orphan)."""
        if not self.required_policy().orphans:
            return None, None
        ws = self.machine.tree.workspaces(root)
        if ws.incomplete:
            return None, ws
        if not ws.claimed:
            ws.incomplete = "no workspace claims any folder (a search that finds nothing is not trusted)"
            return None, ws
        return ws.claimed, ws

    def workspace_lines(
        self, ws: Workspaces | None, claimed: Mapping[str, Sequence[str]] | None, folders: Sequence[Folder]
    ) -> list[str]:
        if ws is None:
            return ["orphans: the rule is off (RUST_BUILD_ORPHANS=no)"]
        out = [
            f"workspaces: {ws.repos} repositories under ~/{WORKSPACE_REL}, {len(ws.worktrees)} worktree(s), "
            f"{ws.manifests} Cargo.toml, {ws.asked} asked (cargo metadata --offline --no-deps), "
            f"{ws.seconds:.0f}s"
        ]
        if claimed is None:
            out.append(f"orphans: NOT looked for in this run: {ws.incomplete}")
        else:
            unclaimed = [f for f in folders if f.key not in claimed]
            out.append(
                f"orphans: {len(claimed)} folder(s) claimed by a workspace, {len(unclaimed)} not claimed "
                f"({gib(sum(f.size for f in unclaimed))})"
            )
        return out

    def plan(self) -> None:
        home, now, pol = self.home, self.now, self.required_policy()
        root, folders, trash, others = self.gather()
        if root is None:
            self.machine.out.say(f"  ~/{ROOT_REL} does not exist yet: nothing to do")
            return
        self.machine.out.say(f"  root: {tilde(root, home)}/XX/YYYYYYYYYYYYYY (cargo build-dir)")
        self.machine.out.say(
            f"  total: {gib(sum(f.size for f in folders))} in {len(folders)} folder(s) "
            "(sizes as du shows them: before btrfs compression; on disk usually less, twice with RAID1)"
        )
        claimed, ws = self.workspaces(root)
        for line in self.workspace_lines(ws, claimed, folders):
            self.machine.out.say(f"  {line}")
        users_map, unreadable = self.machine.tree.open_by(root)
        ties, unknown = self.machine.tree.cargo_runs(root, {})
        users = merge_users(users_map, ties)
        plan = decide(folders, pol, now, users, claimed)
        if plan:
            self.machine.out.say(f"  {'ACTION':<7} {'SIZE':>10}  {'LAST ACTIVE':<16}  FOLDER            WHY")
        for f, a, why in plan:
            self.machine.out.say(f"  {a:<7} {gib(f.size):>10}  {when(f.activity):<16}  {f.key:<17} {why}")
        print_others(self.machine.out, others, trash)
        gone = [f for f, a, _ in plan if a == FolderAction.DELETE]
        trims = [f for f, a, _ in plan if a == FolderAction.TRIM]
        self.machine.out.say(
            f"  apply would delete {len(gone)} folder(s), {gib(sum(f.size for f in gone))}"
            + (
                f", and the incremental folders of {len(trims)} more, {gib(sum(f.inc_size for f in trims))}"
                if trims
                else ""
            )
            + f"; left: {gib(after_total(plan))} in {len(plan) - len(gone)} folder(s)"
        )
        if pol.max_total and after_total(plan) > pol.max_total:
            self.machine.out.say(
                "  still above MAX_TOTAL afterwards: the rest is active within MIN_KEEP, in use or left alone"
            )
        if unreadable:
            self.machine.out.say(
                f"  note: {unreadable} Rust tool process(es) of yours could not be inspected (run on the host to "
                "see all); cargo's own lock is still checked before each deletion"
            )
        if unknown and (gone or trims):
            self.machine.out.say(f"  note: {unknown_text(unknown)}; apply deletes nothing while it runs")

    def apply(self) -> int:
        now, pol = self.now, self.required_policy()
        root, folders, trash, others = self.gather()
        if root is None:
            self.tsay(f"~/{ROOT_REL} does not exist yet: nothing to do")
            self.machine.state.write(
                self.SPEC.id,
                {
                    "time": now,
                    "deleted": 0,
                    "freed": 0,
                    "total": 0,
                    "folders": 0,
                    "busy": 0,
                    "failed": 0,
                    "trimmed": 0,
                    "trim_freed": 0,
                },
            )
            return 0
        before = sum(f.size for f in folders)
        self.tsay(f"{len(folders)} folder(s), {gib(before)}; policy {pol.text()}")
        for name, why in others:
            self.tsay(f"left alone: {name} ({why})", 5)
        claimed, ws = self.workspaces(root)
        for line in self.workspace_lines(ws, claimed, folders):
            self.tsay(line, 5 if claimed is None and ws is not None else 6)
        rc = 0
        cache: CargoCache = {}
        users_map, unreadable = self.machine.tree.open_by(root)
        users: dict[str, list[tuple[int | str, str]]] = merge_users(
            users_map, self.machine.tree.cargo_runs(root, cache)[0]
        )
        if unreadable:
            self.tsay(
                f"{unreadable} Rust tool process(es) of yours could not be inspected; cargo's lock is still checked", 5
            )
        for t in trash:
            if users.get(t.key):
                self.tsay(f"leftover {t.key} in use ({users_text(users[t.key])}): kept for now", 5)
                continue
            ok, msg = self.machine.tree.remove(t.path, self.time_left())
            self.tsay(
                f"deleted leftover {t.key} ({gib(t.size)})" if ok else f"could not delete leftover {t.key}: {msg}",
                6 if ok else 3,
            )
        deleted: dict[str, Folder] = {}
        trimmed: dict[str, int] = {}
        nbusy = failed = 0
        left = list(folders)
        # Said once: what the policy alone would delete but a process uses now.
        for f, a, _ in decide(left, pol, now, {}, claimed):
            if a in (FolderAction.DELETE, FolderAction.TRIM) and f.key in users:
                nbusy += 1
                self.tsay(f"skipped {f.key}: in use: {users_text(users[f.key])}", 5)
        dev = self.machine.tree.device(root)
        rechecked: dict[str, int] = {}
        stopped_by_budget = False
        root_dir = root

        def recheck(f: Folder, a: FolderAction) -> Callable[[], str]:
            """Measure F again (the locks are held) and plan again with it:
            '' when the rule still gives action A (or a delete when A is a
            trim), else why not. F takes the fresh measurement either way."""

            def check() -> str:
                cur = self.clock()
                top, leaf = f.key.split("/")
                fresh = self.machine.tree.measure(Folder(root_dir, top, leaf), dev, cur)
                got, gwhy = next(
                    (x, w)
                    for g, x, w in decide([fresh if g is f else g for g in left], pol, cur, users, claimed)
                    if g is fresh
                )
                f.__dict__.update(fresh.__dict__)
                if got == a or (a == FolderAction.TRIM and got == FolderAction.DELETE and f.inc):
                    return ""
                return f"measured again: {gwhy}" if got == FolderAction.KEEP else f"measured again: now {got}: {gwhy}"

            return check

        # One folder at a time, planned again after each: a folder found busy (or
        # failing) stays counted and kept, so the cap picks the next one instead.
        while True:
            cur = self.clock()
            plan = decide(left, pol, cur, users, claimed)
            nxt = next(((f, a, why) for f, a, why in plan if a == FolderAction.DELETE), None)
            nxt = nxt or next(((f, a, why) for f, a, why in plan if a == FolderAction.TRIM), None)
            if nxt is None:
                break
            f, a, why = nxt
            left_s = self.time_left()
            if left_s is not None and left_s < RM_MARGIN:
                stopped_by_budget = True
                self.tsay(
                    f"stopped before {f.key}: the run's time budget ({span(self.limits.run_budget)}) is nearly used up; "
                    "the next run goes on",
                    5,
                )
                break
            if a == FolderAction.DELETE:
                result, detail = self.machine.tree.delete(root_dir, f, now, cache, recheck(f, a), self.deadline)
            else:
                result, detail = self.machine.tree.trim(root_dir, f, now, cache, recheck(f, a), self.deadline)
            if result == "changed":
                # Planned again with the fresh measurement; a folder that keeps
                # changing is left for the next run.
                rechecked[f.key] = rechecked.get(f.key, 0) + 1
                self.tsay(f"not {'deleted' if a == FolderAction.DELETE else 'trimmed'}: {f.key}: {detail}", 5)
                if rechecked[f.key] < 3:
                    continue
                result = "busy"
            if result == "deleted":
                left.remove(f)
                deleted[f.key] = f
                self.tsay(f"deleted {f.key} ({gib(f.size)}, last active {when(f.activity)}): {why}")
                if detail:
                    self.tsay(f"note: {f.key}: {detail}", 5)
                continue
            if result == "trimmed":
                trimmed[f.key] = f.inc_size
                self.tsay(
                    f"deleted the incremental folders of {f.key} ({gib(f.inc_size)}, last active "
                    f"{when(f.activity)}): {why}"
                )
                f.size -= f.inc_size  # planned again without them (the cap counts what is left)
                f.inc = []
                continue
            if result == "stop":
                nbusy += 1
                self.tsay(f"stopped before {f.key}: {detail}; nothing more is deleted in this run", 5)
                break
            if result == "busy":
                nbusy += 1
                self.tsay(f"skipped {f.key}: {detail}", 5)
            else:
                failed += 1
                rc = 1
                self.tsay(
                    f"could not delete {'the incremental folders of ' if a == FolderAction.TRIM else ''}{f.key}: {detail}",
                    3,
                )
            if a == FolderAction.DELETE and not self.machine.tree.exists(f.path):
                left.remove(f)  # renamed away; the next run deletes the leftover
            users = dict(users)
            users[f.key] = [("-", detail)]
        freed = sum(f.size for f in deleted.values())
        trim_freed = sum(trimmed.values())
        total = before - freed - trim_freed
        self.tsay(
            f"done: deleted {len(deleted)} folder(s), freed {gib(freed)}"
            + (f"; incremental of {len(trimmed)} folder(s), freed {gib(trim_freed)}" if trimmed else "")
            + f"; left {gib(total)} in {len(folders) - len(deleted)} folder(s)"
            + (f"; {nbusy} busy" if nbusy else "")
            + ("; stopped at the time budget" if stopped_by_budget else "")
        )
        if pol.max_total and total > pol.max_total and not stopped_by_budget:
            self.tsay(
                f"still above MAX_TOTAL ({gib(total)} > {gib(pol.max_total)}): "
                "the rest is active within MIN_KEEP, in use or left alone",
                4,
            )
        self.machine.state.write(
            self.SPEC.id,
            {
                "time": now,
                "deleted": len(deleted),
                "freed": freed,
                "total": total,
                "folders": len(folders) - len(deleted),
                "busy": nbusy,
                "failed": failed,
                "trimmed": len(trimmed),
                "trim_freed": trim_freed,
            },
        )
        return rc

    def status_lines(self) -> list[str]:
        """Size and age of the folders, or why they are not looked at."""
        try:
            root = self.machine.tree.root()
        except Refused as e:
            return [f"not pruned: {e}"]
        if root is None:
            return [f"size:     ~/{ROOT_REL} does not exist yet"]
        folders, trash, others = self.machine.tree.scan(root, self.now)
        out = [
            f"size:     {gib(sum(f.size for f in folders))} in {len(folders)} folder(s) "
            "(as du shows it: before btrfs compression)"
        ]
        if folders:
            old = min(folders, key=lambda f: f.activity)
            new = max(folders, key=lambda f: f.activity)
            out.append(
                f"oldest:   {when(old.activity)} ({ago(self.now - old.activity)} ago, {old.key}, {gib(old.size)})"
            )
            out.append(
                f"newest:   {when(new.activity)} ({ago(self.now - new.activity)} ago, {new.key}, {gib(new.size)})"
            )
        out += [f"left alone: {name} ({why})" for name, why in others]
        out += [f"leftover of an interrupted run: {t.key} ({gib(t.size)}): deleted by apply" for t in trash]
        return out


def print_others(out: RunOutputPort, others: Sequence[tuple[str, str]], trash: Sequence[Folder]) -> None:
    for name, why in others:
        out.say(f"  left alone: {name} ({why})")
    for t in trash:
        out.say(f"  leftover of an interrupted run: {t.key} ({gib(t.size)}): deleted by apply")
