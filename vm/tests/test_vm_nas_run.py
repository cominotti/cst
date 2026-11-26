# SPDX-License-Identifier: MPL-2.0
"""A NAS backup's run (vm.application.nas.nas_run) over the fakes of test_vm_nas.py: the numbers of
vm.conf it reads once, the index after a good read, and the refusal of a disk not merged back."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
import fcntl
import io
import json
from dataclasses import dataclass, field, replace
import os
import signal
import stat
import tempfile
import threading
from typing import override
import unittest
from unittest import mock

from cst_core.domain.errors import Refused
from vm import domain as vm_domain, wiring as vm_wiring
from vm.adapters import conf_number
from vm.adapters import restic as vm_adapters_restic
from vm.adapters.backup import StopSignals, describe_entry, read_index, update_index
from vm.application.backup import index_local_backup
from vm.application import nas as vm_app_nas
from vm.domain import backup as vm_domain_backup
from vm.tests.test_vm_nas import (
    NOW,
    SNAP_ID,
    STAMP,
    FakeNasCommands,
    FakeNasOut,
    FakeNasRestic,
    FakeNasSettings,
    FakeNasSignals,
    FakeNasStore,
    FakeNasClock,
    default_virsh,
)

DISK = "/img/a.qcow2"
REPO: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
NUMBERS = ("VM_NAS_KEEP_WEEKLY", "VM_NAS_KEEP_MONTHLY", "VM_NAS_TIMEOUT_HOURS")
_STOPPED = "stopped by signal SIGTERM"

type VirshHook = Callable[[Sequence[str]], tuple[int, str, str] | None]


def _cfg() -> vm_domain.VmSettings:
    return vm_wiring.vm_settings({"CST_VM_NOW": str(NOW), "CST_PROG": "cst vm", "CST_VM_NONROOT_OK": "1"})


@dataclass(kw_only=True)
class _Run:
    """One NAS run of the running VM win (one disk) over the fakes; go() runs it."""

    settings: FakeNasSettings = field(default_factory=FakeNasSettings)
    store: FakeNasStore = field(default_factory=lambda: FakeNasStore([DISK]))
    restic: FakeNasRestic = field(default_factory=FakeNasRestic)
    signals: FakeNasSignals = field(default_factory=FakeNasSignals)
    out: FakeNasOut = field(default_factory=FakeNasOut)
    hook: VirshHook = lambda _args: None
    alerts: list[tuple[str, str]] = field(default_factory=list)
    cfg: vm_domain.VmSettings = field(default_factory=_cfg)

    def __post_init__(self) -> None:
        live = {"vda": DISK}

        def handle(args: Sequence[str]) -> tuple[int, str, str]:
            custom = self.hook(args)
            return default_virsh(args, [("vda", DISK)], live, self.store) if custom is None else custom

        self.commands = FakeNasCommands(handle, self.signals)
        self.port = vm_app_nas.NasBackup(
            commands=self.commands,
            store=self.store,
            settings=self.settings,
            restic=self.restic,
            signals=self.signals,
            lock=lambda _vm: contextlib.nullcontext(),
            alert=lambda title, body: self.alerts.append((title, body)),
            clock=FakeNasClock(),
            out=self.out,
            root=True,
        )

    def go(self) -> None:
        vm_app_nas.nas_run(self.cfg, self.port, "win", None, REPO, quiesce=False, live=True)

    def refusal(self) -> str:
        """The words of the Refused the run ends with (it must end so)."""
        try:
            self.go()
        except Refused as e:
            return str(e)
        raise AssertionError("the run was not refused")


class _IndexFails(FakeNasStore):
    """The index write fails with ENOSPC: before the index is replaced, or after (replaced: the rename
    done, the folder's fsync failed)."""

    def __init__(self, *, replaced: bool) -> None:
        super().__init__([DISK])
        self.replaced = replaced

    @override
    def write_index(self, entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
        if self.replaced:
            super().write_index(entries)
        raise OSError(28, "No space left on device")


class _Defaults(FakeNasSettings):
    """vm.conf with no NAS number: each one's default (a retention that runs restic forget)."""

    @override
    def number(self, key: str) -> int | None:
        return vm_domain.NAS_DEFAULTS[key]


class _StopInRetention(FakeNasRestic):
    """A stop comes while the retention's restic forget runs."""

    @override
    def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        answer = super().run(env, args, timeout)
        if "--keep-weekly" in args:
            raise vm_domain_backup.Stop(_STOPPED)
        return answer


class _StopAtReadLine(FakeNasOut):
    """A stop comes while the line of what restic read is printed."""

    @override
    def say(self, text: str, *, flush: bool = False) -> None:
        super().say(text, flush=flush)
        if text.startswith("  restic snapshot ") and " read, " in text:
            raise vm_domain_backup.Stop(_STOPPED)


def _write_failed() -> str:
    """The words of a good NAS backup of win whose index write failed with ENOSPC."""
    return (
        f"the restic snapshot {SNAP_ID[:8]} of win is a backup, but writing the index failed ([Errno 28] No space "
        "left on device): the index may not name it; the snapshot stays in the repository: the next NAS backups "
        "may name it as a snapshot no record names"
    )


class _ConfNumbers(FakeNasSettings):
    """The numbers of a vm.conf file, read as cst vm reads them."""

    def __init__(self, paths: vm_domain.VmPaths) -> None:
        self.paths = paths

    @override
    def number(self, key: str) -> int | None:
        return conf_number(self.paths, key)


class NasNumbersTest(unittest.TestCase):
    """A number of vm.conf that is set and does not read: the time limit refuses the run before anything
    changes; a keep number lets the backup run and be indexed, skips its retention, and ends the run
    with exit 1 (Refused) naming it."""

    def _run(self, key: str, value: str) -> _Run:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conf = os.path.join(tmp.name, "vm.conf")
        with open(conf, "w", encoding="utf-8") as f:
            f.write(f"{key}='{value}'\n")
        cfg = _cfg()
        cfg = replace(cfg, paths=replace(cfg.paths, vm_conf=conf))
        return _Run(settings=_ConfNumbers(cfg.paths), cfg=cfg)

    def test_a_number_that_does_not_read_is_named(self) -> None:
        for key in NUMBERS:
            for value in ("4w", "\N{SUPERSCRIPT TWO}", "1 x"):
                with self.subTest(key=key, value=value):
                    run = self._run(key, value)
                    msg = run.refusal()
                    unread = f"{key} in {run.cfg.paths.vm_conf} is not a whole number in ASCII digits"
                    if key == "VM_NAS_TIMEOUT_HOURS":
                        self.assertEqual(msg, f"{unread}: nothing backed up; fix it")
                        self.assertEqual((run.commands.virsh_log, run.restic.calls, run.store.written), ([], [], []))
                        continue
                    self.assertEqual(msg, f"{unread}: the backup is done; its retention is skipped until it is fixed")
                    self.assertEqual([e["snapshot"] for e in run.store.written[-1]], [SNAP_ID], "indexed")
                    self.assertFalse([c for c in run.restic.calls if c[0] == "forget"], run.restic.calls)
                    self.assertIn(f"  retention skipped: {unread}: nothing forgotten until it is fixed", run.out.lines)
                    self.assertIn(f"Done: restic snapshot {SNAP_ID[:8]} of win (crash-consistent).", run.out.lines)

    def test_a_number_that_reads_keeps_the_run_as_it_was(self) -> None:
        for key in NUMBERS:
            with self.subTest(key=key):
                run = self._run(key, "7")
                run.go()
                self.assertTrue([c for c in run.restic.calls if c[0] == "forget"], run.restic.calls)


class NasIndexWriteFailsTest(unittest.TestCase):
    """An OSError writing the index after a good NAS backup (a full /var; the folder's fsync after the
    rename) is the backup's refusal, after its done lines, with words true whether the index was replaced
    or not; after a stop, the stop says so."""

    def test_a_good_backup_whose_index_write_fails_is_refused_after_its_done_lines(self) -> None:
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                run = _Run(store=_IndexFails(replaced=replaced))
                msg = run.refusal()
                self.assertTrue(msg.startswith(_write_failed()), msg)
                self.assertIn(f"Done: restic snapshot {SNAP_ID[:8]} of win (crash-consistent).", run.out.lines)

    def test_a_stop_whose_index_write_fails_says_so(self) -> None:
        run = _Run(store=_IndexFails(replaced=False), restic=_StopInRetention(), settings=_Defaults())
        with self.assertRaises(vm_domain_backup.Stop) as e:
            run.go()
        self.assertTrue(str(e.exception).startswith(f"stopped by signal SIGTERM; {_write_failed()}"), str(e.exception))


class NasStopAtTheReadLineTest(unittest.TestCase):
    """A stop that comes while a good read's "restic snapshot ... read" line is printed (after the merge
    back) ends the run only once the snapshot is indexed, as a stop in the retention does."""

    def test_the_snapshot_is_indexed_before_the_stop_ends_the_run(self) -> None:
        run = _Run(out=_StopAtReadLine())
        with self.assertRaises(vm_domain_backup.Stop) as e:
            run.go()
        self.assertEqual(
            str(e.exception),
            f"stopped by signal SIGTERM; restic snapshot {SNAP_ID[:8]} of win is a backup: indexed (the retention "
            "runs at the next backup)",
        )
        self.assertEqual([[x["snapshot"] for x in w] for w in run.store.written], [[SNAP_ID]])


def _kept_entry(snap: str) -> vm_domain_backup.IndexEntry:
    return {
        "vm": "win",
        "mode": "nas",
        "snapshot": snap,
        "repository": "/repo",
        "host": "sophia",
        "epoch": 1,
        "keep": "X",
    }


class NasKeepFollowsTagsTest(unittest.TestCase):
    """An index entry's keep follows restic's tags: after restic tag --remove cst-keep, restic lists the
    snapshot under a new id without the tag, and the next NAS backup's index no longer calls it kept."""

    def test_nas_listing_names_the_ids_whose_tags_have_no_keep_tag(self) -> None:
        listing = vm_domain_backup.nas_listing(
            [
                {"id": "new", "original": "old", "tags": ["cst-vm", "vm:win", "X"]},
                {"id": "kept", "tags": ["cst-vm", "vm:win", "cst-keep", "X"]},
                {"id": "none"},
                {"id": "odd", "tags": "cst-vm"},
                {"id": "mixed", "tags": ["cst-vm", 5]},
            ]
        )
        self.assertEqual(listing.unkept, frozenset({"new"}))
        self.assertEqual(listing.alive["old"], "new")

    def test_the_entry_kept_no_longer_is_not_called_kept(self) -> None:
        old, new, own = "old11111", "new22222", _kept_entry(SNAP_ID) | {"keep": None}
        for tags, keep in (
            (["cst-vm", "vm:win", "X"], None),
            (["cst-vm", "vm:win", "cst-keep", "X"], "X"),
            (None, "X"),  # tags unknown: the entry's own keep
            ("cst-vm", "X"),
        ):
            with self.subTest(tags=tags):
                listed: dict[str, object] = {"id": new, "original": old, **({} if tags is None else {"tags": tags})}
                assert vm_domain_backup.is_restic_snapshot(listed)
                alive, unkept = vm_domain_backup.nas_listing([listed, {"id": SNAP_ID}])
                got = vm_domain_backup.nas_index([_kept_entry(old)], own, alive, unkept=unkept)
                moved = next(e for e in got if e["snapshot"] == new)
                self.assertEqual((moved.get("keep"), moved.get("original")), (keep, old))
                shown = describe_entry(moved)
                self.assertEqual("kept: X" in shown, keep is not None, shown)

    def test_the_backup_s_index_follows_the_listing(self) -> None:
        class _Listing(FakeNasRestic):
            @override
            def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
                if args[0] == "snapshots":
                    self.calls.append(list(args))
                    listed = [{"id": "new22222", "original": "old11111", "tags": ["cst-vm", "vm:win", "X"]}]
                    return 0, json.dumps([*listed, {"id": SNAP_ID, "tags": ["cst-vm", "vm:win"]}]), ""
                return super().run(env, args, timeout)

        class _Store(FakeNasStore):
            @override
            def read_index(self) -> list[vm_domain_backup.IndexEntry]:
                return [_kept_entry("old11111")]

        run = _Run(restic=_Listing(), store=_Store([DISK]))
        run.go()
        self.assertEqual(
            [(e["snapshot"], e.get("keep")) for e in run.store.written[-1]], [("new22222", None), (SNAP_ID, None)]
        )


class _SharedIndex(FakeNasStore):
    """The index every VM's backups share, as a list; another VM's backup may write it meanwhile."""

    def __init__(self) -> None:
        super().__init__([DISK])
        self.index: list[vm_domain_backup.IndexEntry] = []

    @override
    def read_index(self) -> list[vm_domain_backup.IndexEntry]:
        return list(self.index)

    @override
    def write_index(self, entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
        super().write_index(entries)
        self.index = list(entries)


class IndexLockTest(unittest.TestCase):
    """One lock for the index every VM's backups share, around each read-modify-write (update_index):
    two VMs' timers ending at once never lose an entry."""

    def test_the_index_is_read_after_the_listing(self) -> None:
        # VM b's backup writes its entry while restic lists VM a's snapshots (outside the index's lock).
        store = _SharedIndex()
        theirs = _kept_entry("b0000000") | {"vm": "b", "keep": None}

        class _Meanwhile(FakeNasRestic):
            @override
            def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
                if args[0] == "snapshots":
                    store.index = [*store.index, theirs]
                return super().run(env, args, timeout)

        _Run(store=store, restic=_Meanwhile()).go()
        self.assertEqual(sorted(e["snapshot"] for e in store.index), sorted(["b0000000", SNAP_ID]))

    def _cfg(self, tmp: str) -> vm_domain.VmSettings:
        return vm_wiring.vm_settings(
            {"CST_VM_INDEX": os.path.join(tmp, "lib", "vm-backups.json"), "CST_VM_RUN_DIR": os.path.join(tmp, "run")}
        )

    def test_a_second_writer_waits_for_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            inside, go_on, errors = threading.Event(), threading.Event(), list[Exception]()

            def first(entries: list[vm_domain_backup.IndexEntry]) -> list[vm_domain_backup.IndexEntry]:
                inside.set()
                go_on.wait(10)
                return [*entries, _kept_entry("a0000000")]

            def writer(
                change: Callable[[list[vm_domain_backup.IndexEntry]], list[vm_domain_backup.IndexEntry]],
            ) -> None:
                try:
                    update_index(cfg, change, lock_timeout=10)
                except (OSError, vm_domain_backup.IndexRefused) as e:  # handed to the test's thread
                    errors.append(e)

            a = threading.Thread(target=writer, args=(first,))
            b = threading.Thread(target=writer, args=(lambda entries: [*entries, _kept_entry("b0000000")],))
            a.start()
            self.assertTrue(inside.wait(10))
            b.start()
            b.join(0.5)
            b_waited = b.is_alive()
            go_on.set()
            a.join(10)
            b.join(10)
            self.assertTrue(b_waited, "the second writer did not wait for the lock")
            self.assertEqual(errors, [])
            got = sorted(e["snapshot"] for e in read_index(cfg))
            self.assertEqual(got, ["a0000000", "b0000000"])

    def test_a_lock_that_stays_taken_leaves_the_backup_out_and_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            old = os.umask(0o077)
            self.addCleanup(os.umask, old)
            update_index(cfg, lambda entries: entries)
            lock = os.path.join(tmp, "run", "backup-index.lock")
            self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o644)
            fd = os.open(lock, os.O_WRONLY)
            self.addCleanup(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(vm_domain_backup.IndexRefused) as e:
                update_index(cfg, lambda entries: [*entries, _kept_entry("a0000000")], lock_timeout=0.2)
            self.assertEqual(e.exception.problem, f"{lock} stayed taken for 0.2 s (another backup writing the index)")
            self.assertNotIn("move it aside", str(e.exception))
            self.assertEqual(read_index(cfg), [])

    def test_the_local_backup_writes_the_index_under_its_lock(self) -> None:
        class _Recorded(FakeNasStore):
            updates = 0

            @override
            def write_index(self, entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
                if not self.updates:
                    raise AssertionError("the index written without its lock")
                super().write_index(entries)

            @override
            def update_index(
                self, change: Callable[[list[vm_domain_backup.IndexEntry]], list[vm_domain_backup.IndexEntry]]
            ) -> None:
                self.updates += 1
                super().update_index(change)

        store = _Recorded([DISK])
        manifest: vm_domain_backup.Manifest = {
            "vm": "win",
            "uuid": "u",
            "timestamp": STAMP,
            "epoch": NOW,
            "mode": "reflink",
            "files": [],
            "versions": {},
            "deployment": None,
            "notes": [],
        }
        self.assertIsNone(index_local_backup(store, manifest, "/b/win-1"))
        self.assertEqual((store.updates, [[e.get("path") for e in w] for w in store.written]), (1, [["/b/win-1"]]))


class _Out:
    def say(self, text: str, *, flush: bool = False) -> None:
        del text, flush

    def warn(self, text: str) -> None:
        del text


class ResticStartStopTest(unittest.TestCase):
    """A stop that comes while restic starts (between fork and exec, inside Popen) waits until Popen has
    returned, then ends restic: before, it was raised inside Popen, p was never bound, and restic ran on
    unreaped while the overlay was merged back."""

    def _stops_caught(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, signal.pthread_sigmask(signal.SIG_BLOCK, []))
        StopSignals().catch_stops()

    def test_a_stop_while_restic_starts_ends_restic(self) -> None:
        self._stops_caught()

        class _Child:
            stdout = io.BytesIO(b"")
            terminated = waited = False

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.waited = True
                return -signal.SIGTERM

            def kill(self) -> None:
                pass

        child = _Child()

        def popen(*_args: object, **_kwargs: object) -> _Child:
            # Directed at this (the main) thread: a stop that comes between fork and exec.
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            return child

        with (
            mock.patch.object(vm_adapters_restic.subprocess, "Popen", side_effect=popen),
            self.assertRaises(vm_domain_backup.Stop),
        ):
            vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup({}, ["backup"], 60, _Out())
        self.assertEqual((child.terminated, child.waited), (True, True), "restic was not ended")
        self.assertNotIn(signal.SIGTERM, signal.pthread_sigmask(signal.SIG_BLOCK, []), "the main thread's mask is back")

    def test_restic_gets_the_stops_unblocked(self) -> None:
        # The hold around Popen blocks them in this thread; the child gets the mask from before it back.
        with tempfile.TemporaryDirectory() as tmp:
            stub, seen = os.path.join(tmp, "restic"), os.path.join(tmp, "sigblk")
            with open(stub, "w", encoding="utf-8") as f:
                f.write(
                    "#!/bin/sh\n"
                    f"grep '^SigBlk:' /proc/self/status >'{seen}'\n"
                    'echo \'{"message_type": "summary", "snapshot_id": "abc"}\'\n'
                )
            os.chmod(stub, 0o755)
            path = f"{tmp}:{os.environ.get('PATH', '/usr/bin:/bin')}"
            with mock.patch.dict(os.environ, {"PATH": path}):
                rc, summary = vm_adapters_restic.Restic(lambda *_: (0, "", "")).backup({}, ["backup"], 60, _Out())
            with open(seen, encoding="utf-8") as f:
                blocked = int(f.read().split()[1], 16)
        self.assertEqual((rc, summary.get("snapshot_id")), (0, "abc"))
        stops = sum(1 << (int(s) - 1) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT))
        self.assertEqual(blocked & stops, 0, f"restic started with the stops blocked: {blocked:#x}")


class NasUnmergedIndexedTest(unittest.TestCase):
    """A checked NAS read whose merge back failed is alerted on, then indexed with no listing before the
    refusal is raised (r25.3); an index failure keeps its words true, and unchecked reads are not indexed."""

    @staticmethod
    def _fail_commit(args: Sequence[str]) -> tuple[int, str, str] | None:
        return (1, "", "error: block commit timed out") if args[0] == "blockcommit" else None

    def _run_checked_case(self, *, with_stop: bool) -> None:
        class _StoppedAfter(FakeNasRestic):
            @override
            def backup(
                self,
                env: Mapping[str, str],
                args: Sequence[str],
                timeout: int,
                out: object,
                *,
                answered: Callable[[], None] | None = None,
            ) -> tuple[int, vm_domain_backup.ResticSummary]:
                del env, args, timeout, out, answered
                raise vm_domain_backup.StopAfterSnapshot(_STOPPED, 0, {"snapshot_id": SNAP_ID})

        events: list[str] = []
        run = _Run(
            hook=self._fail_commit,
            restic=_StoppedAfter() if with_stop else FakeNasRestic(),
        )
        orig_write = run.store.write_index

        def tracked_write(entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
            events.append("write_index")
            orig_write(entries)

        def record_alert(t: str, b: str) -> None:
            events.append("alert")
            run.alerts.append((t, b))

        run.port = replace(run.port, alert=record_alert)
        with mock.patch.object(run.store, "write_index", side_effect=tracked_write):
            msg = run.refusal()
        self.assertEqual(events, ["alert", "write_index"])
        self.assertEqual([[e["snapshot"] for e in w] for w in run.store.written], [[SNAP_ID]])
        prefix = "stopped by signal SIGTERM; " if with_stop else ""
        self.assertIn(
            f"(the backup itself: {prefix}restic snapshot {SNAP_ID[:8]} of win is a backup: indexed (the "
            "retention runs at the next backup))",
            msg,
        )

    def test_a_checked_read_whose_merge_back_fails_is_alerted_then_indexed(self) -> None:
        for with_stop in (False, True):
            with self.subTest(with_stop=with_stop):
                self._run_checked_case(with_stop=with_stop)

    def test_an_unmerged_checked_read_whose_index_write_fails_keeps_true_words(self) -> None:
        class _IndexRefused(FakeNasStore):
            @override
            def update_index(
                self, change: Callable[[list[vm_domain_backup.IndexEntry]], list[vm_domain_backup.IndexEntry]]
            ) -> None:
                del change
                raise vm_domain_backup.IndexRefused("index format 99", "move it aside")

        cases: tuple[tuple[str, FakeNasStore, str], ...] = (
            ("refused", _IndexRefused([DISK]), "index format 99"),
            ("oserror-before", _IndexFails(replaced=False), _write_failed()),
            ("oserror-after", _IndexFails(replaced=True), _write_failed()),
        )
        for name, store, expected in cases:
            with self.subTest(case=name):
                run = _Run(hook=self._fail_commit, store=store)
                msg = run.refusal()
                self.assertEqual(len(run.alerts), 1)
                self.assertIn("DISK NOT MERGED BACK", msg)
                self.assertIn(expected, msg)

    def test_an_unchecked_read_whose_merge_back_fails_is_never_indexed(self) -> None:
        class _FailedBackup(FakeNasRestic):
            @override
            def backup(
                self,
                env: Mapping[str, str],
                args: Sequence[str],
                timeout: int,
                out: object,
                *,
                answered: Callable[[], None] | None = None,
            ) -> tuple[int, vm_domain_backup.ResticSummary]:
                del env, args, timeout, out, answered
                return 1, {}

        run = _Run(hook=self._fail_commit, restic=_FailedBackup())
        msg = run.refusal()
        self.assertEqual(run.store.written, [])
        self.assertIn("(the backup itself: restic backup failed: restic exit 1; no backup recorded)", msg)


if __name__ == "__main__":
    unittest.main()
