# SPDX-License-Identifier: MPL-2.0
"""The rust:build target: cargo's build folders, the workspaces that still
claim them, and the guarded deletion (cargo's locks, the processes that use
a folder, the rule measured again).
"""

from __future__ import annotations

import os
import stat
import time

from collections.abc import Callable, Mapping, Sequence

from cst_core.domain.errors import Refused
from cst_core.domain.values import Json
from toolchains.domain.build import (
    CargoCache,
    ENUM_BUDGET,
    Folder,
    Incomplete,
    ROOT_REL,
    RustPolicy,
    TRASH_PREFIX,
    WORKSPACE_REL,
    Workspaces,
    after_total,
    decide,
    merge_users,
    prefixes,
    unknown_text,
    users_text,
)
from toolchains.domain.common import ago, gib, json_items, span, tilde, when
from toolchains.domain.prune import RM_MARGIN, RUN_BUDGET, RUST_BUILD, Settings, rust_last_run
from toolchains.adapters.build_tree import measure, release, remove_tree, resolve_root, scan, take_locks
from toolchains.adapters.cargo import (
    ask_cargo,
    cargo_query,
    find_manifests,
    find_worktrees,
    metadata_env,
    user_cargo,
    workspace_metadata,
)
from toolchains.adapters.home_files import write_state
from toolchains.adapters.processes import cargo_procs, proc_users
from toolchains.adapters.program import say, time_left
from toolchains.cli.targets import Target


def cargo_users(
    root: str,
    home: str,
    cache: CargoCache,
) -> tuple[dict[str, list[tuple[str, str]]], list[tuple[str, str, str | None, str]]]:
    """({folder key: [(pid, comm)]}, [(pid, comm, cwd, why)]): the folders
    running cargo processes build in, and the cargo processes whose folder
    could not be found out. CACHE (a dict) keeps the answers for one run."""
    pre = prefixes(root)
    found: dict[str, list[tuple[str, str]]] = {}
    unknown: list[tuple[str, str, str | None, str]] = []
    for p in cargo_procs():
        q = cargo_query(p, home)
        if isinstance(q, str):
            unknown.append((p.pid, p.comm, p.cwd, q))
            continue
        if q.key not in cache:
            cache[q.key] = ask_cargo(q.cmd, q.env, q.cwd)
        bd, why = cache[q.key]
        if why or bd is None:
            unknown.append((p.pid, p.comm, p.cwd, why or "unknown"))
            continue
        for pr in pre:
            if bd.startswith(pr + "/"):
                k = "/".join(bd[len(pr) + 1 :].split("/")[:2])
                lst = found.setdefault(k, [])
                if (p.pid, p.comm) not in lst:
                    lst.append((p.pid, p.comm))
                break
    return found, unknown


def claimed_folders(root: str | None, home: str, with_cargo: bool = True) -> Workspaces:
    """Workspaces: which folders of ROOT a workspace on disk still claims.
    WITH_CARGO=False only lists worktrees and rust-toolchain files (status)."""
    ws = Workspaces()
    start = time.monotonic()
    deadline = start + ENUM_BUDGET
    pre = prefixes(root) if root else ()
    try:
        ws.worktrees = find_worktrees(home, deadline, ws)
        cargo = user_cargo(home) if with_cargo else ""
        if with_cargo and not cargo:
            raise Incomplete("no cargo program found to ask")
        env = metadata_env(home)
        for wt in ws.worktrees:
            manifests, pins, tools = find_manifests(wt, deadline)
            ws.pins += pins
            ws.tool_files += tools
            ws.manifests += len(manifests)
            if not with_cargo:
                continue
            covered: set[str] = set()
            # The shallowest first: a workspace's root answers for its members.
            for m in sorted(manifests, key=lambda p: (p.count("/"), p)):
                if m in covered:
                    continue
                data, why = workspace_metadata(cargo, m, env, deadline)
                ws.asked += 1
                if why or data is None:
                    ws.failures.append((m, why or "unknown"))
                    continue
                covered.add(m)
                for p in json_items(data.get("packages")):
                    mp = p.get("manifest_path") if isinstance(p, dict) else None
                    if isinstance(mp, str):
                        covered.add(os.path.normpath(mp))
                wr = data.get("workspace_root")
                if isinstance(wr, str):
                    covered.add(os.path.join(os.path.normpath(wr), "Cargo.toml"))
                bd = data.get("build_directory")
                if not isinstance(bd, str) or not bd:
                    continue  # a cargo without build-dir support: not in this root
                bd = os.path.normpath(bd)
                for pr in pre:
                    if bd.startswith(pr + "/"):
                        key = "/".join(bd[len(pr) + 1 :].split("/")[:2])
                        lst = ws.claimed.setdefault(key, [])
                        shown = wr if isinstance(wr, str) else os.path.dirname(m)
                        if shown not in lst:
                            lst.append(shown)
                        break
        if ws.failures:
            m, why = ws.failures[0]
            more = f" (and {len(ws.failures) - 1} more)" if len(ws.failures) > 1 else ""
            raise Incomplete(f"{tilde(m, home)}: {why}{more}")
    except Incomplete as e:
        ws.incomplete = str(e)
    ws.seconds = time.monotonic() - start
    return ws


def guarded(
    root: str,
    folder: Folder,
    home: str,
    cache: CargoCache,
    step: Callable[[], tuple[str, str]],
    recheck: Callable[[], str] | None = None,
) -> tuple[str, str]:
    """Run STEP() while holding every cargo lock of FOLDER, after checking
    that no process uses it and, with RECHECK, that the rule that chose the
    action still holds (RECHECK() measures the folder again and returns ''
    or why not). STEP returns (result, detail). Returns that, or
    ('busy', why), ('changed', why) from RECHECK, or ('stop', why): a cargo
    process whose build folder is unknown runs; nothing more is deleted in
    this run."""
    fds, busy = take_locks(folder.path)
    if busy is not None:
        return "busy", f"cargo lock held: {os.path.relpath(busy, folder.path)}"
    # The users are checked while the locks are held: a cargo that starts
    # after this check must first compile, and that waits for the lock.
    try:
        users = proc_users(root)[0].get(folder.key)
        if users:
            return "busy", "in use: " + users_text(users)
        ties, unknown = cargo_users(root, home, cache)
        if ties.get(folder.key):
            return "busy", "cargo runs on it: " + users_text(ties[folder.key])
        if unknown:
            return "stop", unknown_text(unknown)
        # The plan was made from the activity measured when the run started;
        # a build may have written there since (and ended: no lock, no
        # process). Measured again here, while the locks are held: a cargo
        # that starts now waits for them.
        why = recheck() if recheck else ""
        if why:
            return "changed", why
        return step()
    finally:
        release(fds)


def delete_folder(
    root: str,
    folder: Folder,
    now: float,
    home: str,
    cache: CargoCache,
    recheck: Callable[[], str] | None = None,
    rm_deadline: float | None = None,
) -> tuple[str, str]:
    """Lock, check for users (and RECHECK, see guarded), rename away,
    unlock, delete. ('deleted'|'busy'|'changed'|'stop'|'failed', detail)."""
    try:
        st = os.lstat(folder.path)
    except FileNotFoundError:
        return "failed", "already gone"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return "failed", "no longer a folder (refused)"
    top = os.path.dirname(folder.path)
    trash = os.path.join(root, f"{TRASH_PREFIX}{folder.key.replace('/', '')}-{int(now)}")

    def step() -> tuple[str, str]:
        try:
            os.rename(folder.path, trash)
        except OSError as e:
            return "failed", f"rename: {e.strerror}"
        return "moved", ""

    result, detail = guarded(root, folder, home, cache, step, recheck)
    if result != "moved":
        return result, detail
    try:
        os.rmdir(top)  # the XX folder, when this was its last hash
    except OSError:
        pass
    ok, msg = remove_tree(trash, time_left(rm_deadline))
    if not ok:
        return "failed", f"rm: {msg}"
    # A cargo that was waiting for the lock during the rename builds again,
    # from scratch, into a new folder at the same path (it does not fail).
    if os.path.lexists(folder.path):
        return "deleted", "a build started meanwhile and is filling a new folder at the same path"
    return "deleted", ""


def trim_folder(
    root: str,
    folder: Folder,
    now: float,
    home: str,
    cache: CargoCache,
    recheck: Callable[[], str] | None = None,
    rm_deadline: float | None = None,
) -> tuple[str, str]:
    """Delete only the incremental folders of FOLDER, with the same locks and
    checks as delete_folder (RECHECK may update folder.inc). Each is renamed away while the locks are held;
    the modification time of its parent is put back afterwards, so that the
    folder's last activity (MAX_IDLE) does not move. ('trimmed'|'busy'|
    'stop'|'failed', detail)."""
    moved: list[str] = []
    base = folder.key.replace("/", "")

    def step() -> tuple[str, str]:
        for i, d in enumerate(folder.inc):
            path = d.path
            parent = os.path.dirname(path)
            try:
                pst = os.lstat(parent)
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(pst.st_mode):
                continue
            trash = os.path.join(root, f"{TRASH_PREFIX}{base}-inc{i}-{int(now)}")
            try:
                os.rename(path, trash)
            except OSError as e:
                return "failed", f"rename {os.path.relpath(path, folder.path)}: {e.strerror}"
            moved.append(trash)
            try:
                os.utime(parent, ns=(pst.st_atime_ns, pst.st_mtime_ns), follow_symlinks=False)
            except OSError:
                pass
        return "moved", ""

    result, detail = guarded(root, folder, home, cache, step, recheck)
    # What was renamed away is deleted also after a later failure (else the
    # next run deletes it as a leftover).
    errors: list[str] = []
    for t in moved:
        ok, msg = remove_tree(t, time_left(rm_deadline))
        if not ok:
            errors.append(msg)
    if result != "moved":
        return result, detail
    if errors:
        return "failed", f"rm: {errors[0]}"
    return "trimmed", ""


class RustBuild(Target[RustPolicy]):
    SPEC = RUST_BUILD

    @classmethod
    def make(cls, home: str, now: float, settings: Settings | None) -> RustBuild:
        return cls(home, now, settings.rust if settings else None)

    def location(self) -> str:
        return os.path.join(self.home, ROOT_REL)

    def check(self) -> None:
        resolve_root(self.home)

    def gather(self) -> tuple[str | None, list[Folder], list[Folder], list[tuple[str, str]]]:
        root = resolve_root(self.home)
        if root is None:
            return None, [], [], []
        folders, trash, others = scan(root, self.now)
        return root, folders, trash, others

    def workspaces(self, root: str) -> tuple[dict[str, list[str]] | None, Workspaces | None]:
        """(claimed or None, Workspaces or None): the folders claimed by a
        workspace; None when the orphan rule is off or the search is
        incomplete (then nothing is an orphan)."""
        if not self.required_policy().orphans:
            return None, None
        ws = claimed_folders(root, self.home)
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

    def plan(self) -> int:
        home, now, pol = self.home, self.now, self.required_policy()
        root, folders, trash, others = self.gather()
        if root is None:
            say(f"  ~/{ROOT_REL} does not exist yet: nothing to do")
            return 0
        say(f"  root: {tilde(root, home)}/XX/YYYYYYYYYYYYYY (cargo build-dir)")
        say(
            f"  total: {gib(sum(f.size for f in folders))} in {len(folders)} folder(s) "
            "(sizes as du shows them: before btrfs compression; on disk usually less, twice with RAID1)"
        )
        claimed, ws = self.workspaces(root)
        for line in self.workspace_lines(ws, claimed, folders):
            say(f"  {line}")
        users_map, unreadable = proc_users(root)
        ties, unknown = cargo_users(root, home, {})
        users = merge_users(users_map, ties)
        plan = decide(folders, pol, now, users, claimed)
        if plan:
            say(f"  {'ACTION':<7} {'SIZE':>10}  {'LAST ACTIVE':<16}  FOLDER            WHY")
        for f, a, why in plan:
            say(f"  {a:<7} {gib(f.size):>10}  {when(f.activity):<16}  {f.key:<17} {why}")
        print_others(others, trash)
        gone = [f for f, a, _ in plan if a == "delete"]
        trims = [f for f, a, _ in plan if a == "trim"]
        say(
            f"  apply would delete {len(gone)} folder(s), {gib(sum(f.size for f in gone))}"
            + (
                f", and the incremental folders of {len(trims)} more, {gib(sum(f.inc_size for f in trims))}"
                if trims
                else ""
            )
            + f"; left: {gib(after_total(plan))} in {len(plan) - len(gone)} folder(s)"
        )
        if pol.max_total and after_total(plan) > pol.max_total:
            say("  still above MAX_TOTAL afterwards: the rest is active within MIN_KEEP, in use or left alone")
        if unreadable:
            say(
                f"  note: {unreadable} Rust tool process(es) of yours could not be inspected (run on the host to "
                "see all); cargo's own lock is still checked before each deletion"
            )
        if unknown and (gone or trims):
            say(f"  note: {unknown_text(unknown)}; apply deletes nothing while it runs")
        return 0

    def apply(self) -> int:
        home, now, pol = self.home, self.now, self.required_policy()
        root, folders, trash, others = self.gather()
        if root is None:
            self.tsay(f"~/{ROOT_REL} does not exist yet: nothing to do")
            write_state(
                home,
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
        users_map, unreadable = proc_users(root)
        users: dict[str, list[tuple[int | str, str]]] = merge_users(users_map, cargo_users(root, home, cache)[0])
        if unreadable:
            self.tsay(
                f"{unreadable} Rust tool process(es) of yours could not be inspected; cargo's lock is still checked", 5
            )
        for t in trash:
            if users.get(t.key):
                self.tsay(f"leftover {t.key} in use ({users_text(users[t.key])}): kept for now", 5)
                continue
            ok, msg = remove_tree(t.path, self.time_left())
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
            if a in ("delete", "trim") and f.key in users:
                nbusy += 1
                self.tsay(f"skipped {f.key}: in use: {users_text(users[f.key])}", 5)
        dev = os.lstat(root).st_dev
        rechecked: dict[str, int] = {}
        stopped_by_budget = False
        root_dir = root

        def recheck(f: Folder, a: str) -> Callable[[], str]:
            """Measure F again (the locks are held) and plan again with it:
            '' when the rule still gives action A (or a delete when A is a
            trim), else why not. F takes the fresh measurement either way."""

            def check() -> str:
                cur = self.clock()
                top, leaf = f.key.split("/")
                fresh = measure(Folder(root_dir, top, leaf), dev, cur)
                got, gwhy = next(
                    (x, w)
                    for g, x, w in decide([fresh if g is f else g for g in left], pol, cur, users, claimed)
                    if g is fresh
                )
                f.__dict__.update(fresh.__dict__)
                if got == a or (a == "trim" and got == "delete" and f.inc):
                    return ""
                return f"measured again: {gwhy}" if got == "keep" else f"measured again: now {got}: {gwhy}"

            return check

        # One folder at a time, planned again after each: a folder found busy (or
        # failing) stays counted and kept, so the cap picks the next one instead.
        while True:
            cur = self.clock()
            plan = decide(left, pol, cur, users, claimed)
            nxt = next(((f, a, why) for f, a, why in plan if a == "delete"), None)
            nxt = nxt or next(((f, a, why) for f, a, why in plan if a == "trim"), None)
            if nxt is None:
                break
            f, a, why = nxt
            left_s = self.time_left()
            if left_s is not None and left_s < RM_MARGIN:
                stopped_by_budget = True
                self.tsay(
                    f"stopped before {f.key}: the run's time budget ({span(RUN_BUDGET)}) is nearly used up; "
                    "the next run goes on",
                    5,
                )
                break
            if a == "delete":
                result, detail = delete_folder(root_dir, f, now, home, cache, recheck(f, a), self.deadline)
            else:
                result, detail = trim_folder(root_dir, f, now, home, cache, recheck(f, a), self.deadline)
            if result == "changed":
                # Planned again with the fresh measurement; a folder that keeps
                # changing is left for the next run.
                rechecked[f.key] = rechecked.get(f.key, 0) + 1
                self.tsay(f"not {'deleted' if a == 'delete' else 'trimmed'}: {f.key}: {detail}", 5)
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
                    f"could not delete {'the incremental folders of ' if a == 'trim' else ''}{f.key}: {detail}", 3
                )
            if a == "delete" and not os.path.lexists(f.path):
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
        write_state(
            home,
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

    @staticmethod
    def last_run_text(s: Mapping[str, Json]) -> str:
        return rust_last_run(s)

    def status_lines(self) -> list[str]:
        """Size and age of the folders, or why they are not looked at."""
        try:
            root = resolve_root(self.home)
        except Refused as e:
            return [f"not pruned: {e}"]
        if root is None:
            return [f"size:     ~/{ROOT_REL} does not exist yet"]
        folders, trash, others = scan(root, self.now)
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


def print_others(others: Sequence[tuple[str, str]], trash: Sequence[Folder]) -> None:
    for name, why in others:
        say(f"  left alone: {name} ({why})")
    for t in trash:
        say(f"  leftover of an interrupted run: {t.key} ({gib(t.size)}): deleted by apply")
