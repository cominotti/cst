# SPDX-License-Identifier: MPL-2.0
"""A NAS backup's run (vm.application.nas.nas_run) over the fakes of test_vm_nas.py: the numbers of
vm.conf it reads once, the index after a good read, and the refusal of a disk not merged back."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import contextlib
from dataclasses import dataclass, field, replace
import os
import tempfile
from typing import override
import unittest

from cst_core.domain.errors import Refused
from vm import domain as vm_domain, wiring as vm_wiring
from vm.adapters import conf_number
from vm.application import nas as vm_app_nas
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
OVERLAY = f"{DISK}{vm_domain.OVERLAY_MARK}{STAMP}"
REPO: vm_domain.NasRepo = {"host": "sophia", "repo": "/repo", "env": {}}
NUMBERS = ("VM_NAS_KEEP_WEEKLY", "VM_NAS_KEEP_MONTHLY", "VM_NAS_TIMEOUT_HOURS")

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


if __name__ == "__main__":
    unittest.main()
