# SPDX-License-Identifier: MPL-2.0
"""A state machine over `cst btrfs cache` attach and detach, with failed mounts and reloads (Hypothesis).

It runs the use cases (btrfs/cache/application.py CacheMounts.run, argv
parsed as the command line parses it) over in-memory port fakes
(btrfs/tests/fakes.py CacheWorld: /etc/fstab, the mount table, what systemd
has loaded of /etc/fstab, the folders and the accounts; test_cache_ports.py
keeps them equal to the real adapters), for two users, through any sequence
of attach and detach of the cache, the scratchpad or both, where one bind
mount, one umount (of a detach) or the daemon-reload fails.

After every step: /etc/fstab's managed lines name exactly the binds mounted
under /var/home (no line waits for the next boot without its mount, and no
bind of CST lives without its line), except the binds a detach killed at its
daemon-reload left mounted after it took their lines out (until a later step
unmounts them or writes their lines back). A step that exits 0 leaves systemd
with /etc/fstab loaded as it is on disk (CacheWorld's view of it): a run
after one killed between its /etc/fstab write and its daemon-reload reloads.

Each step: a step killed at its daemon-reload (the fault KILLED_AT_RELOAD:
CacheWorld's before_reload raises Killed, after the /etc/fstab write)
has no exit to check. A step whose fault fired (the command ran the mount, the umount or the
reload that was made to fail) exits non-zero with an ERROR line, never ROLLBACK
INCOMPLETE (nothing here can make the undo fail), and left /etc/fstab's text
and the mounts as they were (the folders and the CACHEDIR.TAG it made may
stay, as btrfs/README.md says). A step with no fault, or whose fault did not
fire (the bind was already mounted, /etc/fstab did not change so no reload
ran), exits 0: the same command converges from every state.

Not modelled: a failed write of /etc/fstab, a findmnt that
fails, a mount CST did not make, kills elsewhere than at the daemon-reload
(btrfs/tests/test_cache_faults.py kills at every point of one run), and a fault that fails only some of
the calls it matches: a reload fault fails the step's one daemon-reload
(its undo runs none), a mount fault fails an attach's bind, never the
undo's mount again of a detach, and an umount fault fails a detach's
umount, never an attach's undo. So the undo never fails here (a detach's
failed umount mounts again what it unmounted, writes the lines back and
reloads); the ROLLBACK INCOMPLETE paths are covered by
btrfs/tests/test_cache_faults.py (an /etc/fstab that cannot be put back,
a bind not mounted again).
"""

from enum import StrEnum
import errno
from typing import Final
import unittest

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from btrfs.cache.domain import (
    BASE_FOR_KIND,
    CACHE_BASE,
    REL_FOR_KIND,
    SCRATCHPAD_BASE,
    CacheMountKind,
    parse_cache_mount_args,
    parse_managed_fstab_entries,
)
from btrfs.tests.fakes import CacheWorld
from tests.fault_injection import Killed
from tests.mbt import profiles

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
    """What fails in one step: nothing, the cache's or the scratchpad's mount --bind or umount, or
    daemon-reload; or the run is killed at its daemon-reload."""

    NONE = "none"
    CACHE_MOUNT = "cache-mount"
    SCRATCHPAD_MOUNT = "scratchpad-mount"
    CACHE_UMOUNT = "cache-umount"
    SCRATCHPAD_UMOUNT = "scratchpad-umount"
    RELOAD = "daemon-reload"
    KILLED_AT_RELOAD = "killed-at-daemon-reload"


# The bind whose mount --bind each mount fault fails.
MOUNT_FAULT: Final = {
    CacheStepFault.CACHE_MOUNT: CacheMountKind.CACHE,
    CacheStepFault.SCRATCHPAD_MOUNT: CacheMountKind.SCRATCHPAD,
}
# The bind whose umount each umount fault fails ("target is busy").
UMOUNT_FAULT: Final = {
    CacheStepFault.CACHE_UMOUNT: CacheMountKind.CACHE,
    CacheStepFault.SCRATCHPAD_UMOUNT: CacheMountKind.SCRATCHPAD,
}
ATTACH_FAULTS: Final = (
    CacheStepFault.NONE,
    CacheStepFault.CACHE_MOUNT,
    CacheStepFault.SCRATCHPAD_MOUNT,
    CacheStepFault.RELOAD,
    CacheStepFault.KILLED_AT_RELOAD,
)
DETACH_FAULTS: Final = (
    CacheStepFault.NONE,
    CacheStepFault.RELOAD,
    CacheStepFault.KILLED_AT_RELOAD,
    CacheStepFault.CACHE_UMOUNT,
    CacheStepFault.SCRATCHPAD_UMOUNT,
)

type Mounts = list[tuple[str, str, str]]

# What a failed mount --bind or daemon-reload answers (the error's text is the reason the line gives).
MOUNT_FAILED: Final = OSError(errno.EPERM, "mount: permission denied")
UMOUNT_FAILED: Final = OSError(errno.EBUSY, "umount: target is busy")
RELOAD_FAILED: Final = OSError(errno.EIO, "Failed to reload daemon")


def _kill() -> None:
    raise Killed


class CacheMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.world = CacheWorld()
        for name, uid in USERS.items():
            self.world.add_user(name, uid, f"/var/home/{name}")
        self.world.caller = CALLER
        for base in (CACHE_BASE, SCRATCHPAD_BASE):
            self.world.mkdirs(base)
        self.world.put_fstab("/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n")
        self.world.table.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache")
        self.world.table.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE, fsroot="/scratchpad")
        # The binds a detach killed at its reload left mounted without their lines.
        self.left_by_kill: set[str] = set()

    # ---- helpers -----------------------------------------------------------------------------
    def _fstab(self) -> str | None:
        return self.world.fstab

    def _mounts(self) -> Mounts:
        return sorted((str(m["target"]), str(m["source"]), str(m["fsroot"])) for m in self.world.table.mounts)

    def _lines(self) -> list[str]:
        return sorted(e.target for e in parse_managed_fstab_entries(self._fstab() or ""))

    def _binds(self) -> list[str]:
        return sorted(t for t, _source, _fsroot in self._mounts() if t.startswith("/var/home/"))

    def _run(self, argv: list[str]) -> tuple[int, list[str]]:
        return self.world.service().run(parse_cache_mount_args(argv), "cst btrfs cache")

    def _arm(self, user: str, fault: CacheStepFault) -> str:
        """Make fault fail in the next command; the point that answers it ("" for none)."""
        if fault == CacheStepFault.RELOAD:
            self.world.fail("systemctl daemon-reload", RELOAD_FAILED)
            return "systemctl daemon-reload"
        if fault == CacheStepFault.KILLED_AT_RELOAD:
            self.world.before_reload = _kill
            return ""
        # The binding as build_user_bindings makes it: BASE/USER -> HOME/REL.
        kind = UMOUNT_FAULT.get(fault)
        if kind is not None:
            point = f"umount /var/home/{user}/{REL_FOR_KIND[kind]}"
            self.world.fail(point, UMOUNT_FAILED)
            return point
        kind = MOUNT_FAULT.get(fault)
        if kind is None:
            return ""
        point = f"mount --bind {BASE_FOR_KIND[kind]}/{user} /var/home/{user}/{REL_FOR_KIND[kind]}"
        self.world.fail(point, MOUNT_FAILED)
        return point

    def _step(self, action: str, user: str, selection: CacheSelection, fault: CacheStepFault) -> None:
        argv = [action, "--user", user, *SELECTION_FLAGS[selection]]
        before = (self._fstab(), self._mounts())
        answered_before = len(self.world.answered)
        failing = self._arm(user, fault)
        try:
            rc, lines = self._run(argv)
        except Killed:
            if action == "detach":
                self.left_by_kill |= set(self._binds()) - set(self._lines())
            return
        finally:
            self.world.failing.clear()
            self.world.before_reload = None
        fired = bool(failing) and failing in self.world.answered[answered_before:]
        if rc == 0:
            assert self.world.view_current(), f"{argv} exited 0 and systemd has not loaded /etc/fstab: {lines}"
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
        fault=st.sampled_from(ATTACH_FAULTS),
    )
    def attach(self, user: str, selection: CacheSelection, fault: CacheStepFault) -> None:
        self._step("attach", user, selection, fault)

    @rule(
        user=st.sampled_from(sorted(USERS)),
        selection=st.sampled_from(CacheSelection),
        fault=st.sampled_from(DETACH_FAULTS),
    )
    def detach(self, user: str, selection: CacheSelection, fault: CacheStepFault) -> None:
        self._step("detach", user, selection, fault)

    # ---- invariants --------------------------------------------------------------------------
    @invariant()
    def fstab_names_exactly_the_mounted_binds(self) -> None:
        lines, binds = self._lines(), self._binds()
        self.left_by_kill &= set(binds) - set(lines)  # a later step unmounted them or wrote their lines
        assert set(lines) <= set(binds), f"/etc/fstab names {lines}, mounted are {binds}"
        extra = set(binds) - set(lines)
        assert extra <= self.left_by_kill, f"/etc/fstab names {lines}, mounted are {binds}"


class CacheMachineTest(profiles.MbtTestCase):
    # Not `CacheMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where run_unit's
    # workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    def test_any_sequence_of_steps_keeps_the_invariants(self) -> None:
        profiles.run_machine(CacheMachine)


if __name__ == "__main__":
    unittest.main()
