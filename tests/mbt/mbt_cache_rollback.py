# SPDX-License-Identifier: MPL-2.0
"""A state machine over `cst btrfs cache` attach and detach, with failed mounts and reloads (Hypothesis).

It runs the real command line (btrfs/cache/cli.py run_cache_mount_cli) on a
folder of its own, with the unit tests' fake findmnt, mount, umount and
systemctl (btrfs/tests/fakes.py FakeMountRunner, CacheTestCtx), for two
users, through any sequence of attach and detach of the cache, the
scratchpad or both, where one bind mount or the daemon-reload fails. os.fsync
is stubbed (the TestCase's setUp), so the machine does not depend on TMPDIR.

After every step: /etc/fstab's managed lines name exactly the binds mounted
under /var/home (no line waits for the next boot without its mount, and no
bind of CST lives without its line).

Each step: a step whose fault fired (the command ran the mount or the reload
that was made to fail) exits non-zero with an ERROR line, never ROLLBACK
INCOMPLETE (nothing here can make the undo fail), and left /etc/fstab's text
and the mounts as they were (the folders and the CACHEDIR.TAG it made may
stay, as btrfs/README.md says). A step with no fault, or whose fault did not
fire (the bind was already mounted, /etc/fstab did not change so no reload
ran), exits 0: the same command converges from every state.

Not modelled: a failed umount, a failed write of /etc/fstab, a findmnt that
fails, a mount CST did not make, kills.
"""

from enum import StrEnum
import io
import os
import shutil
import tempfile
from typing import Final, override
import unittest

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from btrfs.cache.cli import run_cache_mount_cli
from btrfs.cache.domain import (
    BASE_FOR_KIND,
    CACHE_BASE,
    REL_FOR_KIND,
    SCRATCHPAD_BASE,
    CacheMountKind,
    parse_managed_fstab_entries,
)
from btrfs.tests.fakes import CacheTestCtx, FakeMountRunner
from cst_core.adapters.runtime import ManagedCliEnv
from tests.fault_injection import stub_fsync
from tests.mbt import profiles
from tests.repo_files import REPO

profiles.load()

# name -> uid; neutral names, the first one is the caller (USER).
USERS: Final = {"alice": 1000, "bob": 1001}
CALLER: Final = "alice"


class CacheSelection(StrEnum):
    """Which binds one command takes: the cache, the scratchpad or both."""

    CACHE = "cache"
    SCRATCHPAD = "scratchpad"
    BOTH = "both"


SELECTION_FLAGS: Final = {
    CacheSelection.CACHE: ["--no-scratchpad"],
    CacheSelection.SCRATCHPAD: ["--no-cache"],
    CacheSelection.BOTH: [],
}


class CacheStepFault(StrEnum):
    """What fails in one step: nothing, the cache's or the scratchpad's mount --bind, or daemon-reload."""

    NONE = "none"
    CACHE_MOUNT = "cache-mount"
    SCRATCHPAD_MOUNT = "scratchpad-mount"
    RELOAD = "daemon-reload"


# The bind whose mount --bind each mount fault fails.
MOUNT_FAULT: Final = {
    CacheStepFault.CACHE_MOUNT: CacheMountKind.CACHE,
    CacheStepFault.SCRATCHPAD_MOUNT: CacheMountKind.SCRATCHPAD,
}

type Mounts = list[tuple[str, str, str]]


class CacheMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-mbt-cache-"))
        passwd = ["root:x:0:0:root:/root:/bin/bash\n"]
        for name, uid in USERS.items():
            os.makedirs(os.path.join(self.tmp, "var", "home", name))
            passwd.append(f"{name}:x:{uid}:{uid}:{name}:/var/home/{name}:/bin/bash\n")
        for base in (CACHE_BASE, SCRATCHPAD_BASE):
            os.makedirs(self.tmp + base)
        self._write("etc/passwd", "".join(passwd))
        self._write("etc/fstab", "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n")
        self.runner = FakeMountRunner(self.tmp)
        self.runner.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache")
        self.runner.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE, fsroot="/scratchpad")
        self.entry = os.path.join(REPO, "btrfs", "btrfs-cache")

    @override
    def teardown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------------------------
    def _write(self, rel: str, text: str) -> None:
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _fstab(self) -> str | None:
        path = os.path.join(self.tmp, "etc", "fstab")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()

    def _mounts(self) -> Mounts:
        return sorted((str(m["target"]), str(m["source"]), str(m["fsroot"])) for m in self.runner.mounts)

    def _binds(self) -> list[str]:
        return sorted(t for t, _source, _fsroot in self._mounts() if t.startswith("/var/home/"))

    def _run(self, argv: list[str]) -> tuple[int, list[str]]:
        out, err = io.StringIO(), io.StringIO()
        rc = run_cache_mount_cli(
            argv,
            deps=ManagedCliEnv(
                entry_file=self.entry,
                env={"CST_BTRFS_CACHE_ROOT": self.tmp, "USER": CALLER},
                out=out,
                err=err,
                ctx=CacheTestCtx(),
                runner=self.runner,
            ),
        )
        return rc, (out.getvalue() + err.getvalue()).splitlines()

    def _arm(self, user: str, fault: CacheStepFault) -> list[str]:
        """Make fault fail in the next command; the call that answers it, as FakeMountRunner records it."""
        if fault == CacheStepFault.RELOAD:
            self.runner.reload_fails = True
            return ["systemctl", "daemon-reload"]
        kind = MOUNT_FAULT.get(fault)
        if kind is None:
            return []
        # The binding as build_user_bindings makes it: BASE/USER -> HOME/REL.
        target = f"/var/home/{user}/{REL_FOR_KIND[kind]}"
        self.runner.mount_fails.add(target)
        source = f"{BASE_FOR_KIND[kind]}/{user}"
        return ["mount", "--bind", self.tmp + source, self.tmp + target]

    def _step(self, action: str, user: str, selection: CacheSelection, fault: CacheStepFault) -> None:
        argv = [action, "--user", user, *SELECTION_FLAGS[selection]]
        before = (self._fstab(), self._mounts())
        calls_before = len(self.runner.calls)
        failing = self._arm(user, fault)
        try:
            rc, lines = self._run(argv)
        finally:
            self.runner.mount_fails.clear()
            self.runner.reload_fails = False
        made = self.runner.calls[calls_before:]
        fired = bool(failing) and failing in made
        if not fired:
            assert rc == 0, f"{argv} ({fault} did not fire) exited {rc}: {lines}"
            return
        assert rc != 0, f"{argv}: {fault} fired and the command exited 0: {lines}"
        assert any(line.startswith("ERROR:") for line in lines), f"{argv}: exit {rc} with no ERROR line: {lines}"
        assert not any("ROLLBACK INCOMPLETE" in line for line in lines), f"{argv}: {lines}"
        after = (self._fstab(), self._mounts())
        assert after == before, f"{argv} failed ({fault}) and left a change: {before} -> {after}\n{lines}"

    # ---- rules -------------------------------------------------------------------------------
    @rule(
        user=st.sampled_from(sorted(USERS)),
        selection=st.sampled_from(CacheSelection),
        fault=st.sampled_from(CacheStepFault),
    )
    def attach(self, user: str, selection: CacheSelection, fault: CacheStepFault) -> None:
        self._step("attach", user, selection, fault)

    @rule(
        user=st.sampled_from(sorted(USERS)),
        selection=st.sampled_from(CacheSelection),
        fault=st.sampled_from([CacheStepFault.NONE, CacheStepFault.RELOAD]),
    )
    def detach(self, user: str, selection: CacheSelection, fault: CacheStepFault) -> None:
        self._step("detach", user, selection, fault)

    # ---- invariants --------------------------------------------------------------------------
    @invariant()
    def fstab_names_exactly_the_mounted_binds(self) -> None:
        lines = sorted(e.target for e in parse_managed_fstab_entries(self._fstab() or ""))
        assert lines == self._binds(), f"/etc/fstab names {lines}, mounted are {self._binds()}"


class CacheMachineTest(profiles.MbtTestCase):
    # Not `CacheMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where run_unit's
    # workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    @override
    def setUp(self) -> None:
        stub_fsync(self)

    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(CacheMachine)


if __name__ == "__main__":
    unittest.main()
