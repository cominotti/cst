# SPDX-License-Identifier: MPL-2.0
"""Unit tests for what snap_status in btrfs/setup-snapshots.bash (`snapshots
status`) shows for SNAPSHOT_EXCLUDE_CACHES, and for the units that run
btrfs/btrfs-snapshot. (btrfs-snapshot's own options from SNAPSHOT_EXCLUDE_CACHES
and SNAPSHOT_CACHES_SKIP: test_snapshot_hourly.py, on the whole program.)

shell_function cuts one function out of a script's text, for the
setup-snapshots.bash and btrfs-setup.sh tests that still need it.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_snapshot_runtime -v
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BTRFS = os.path.dirname(HERE)


def shell_function(path: str, name: str) -> str:
    """The definition of the shell function NAME in PATH ('name() {' to '}')."""
    with open(path) as f:
        text = f.read()
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    if m is None:
        raise AssertionError(f"{name}() not found in {path}")
    return m.group(0)


class SnapStatusCachesTest(unittest.TestCase):
    """The cache line of snap_status, with stubs for everything that reads the machine."""

    STUBS = "\n".join(
        [
            "snap_home() { echo /nonexistent/home; }",
            "systemctl() { return 1; }",
            "snap_entry_state() { echo -; }",
            "snap_plan_stale() { :; }",
            "snap_chrome() { :; }",
        ]
    )

    def status(self, text: str | None) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "snapshots.conf")
            if text is not None:
                with open(conf, "w") as f:
                    f.write(text)
            script = "\n".join(
                [
                    'SRC="$1"; . "$1/setup-snapshots.bash"',
                    self.STUBS,
                    'PROG="cst btrfs"; SNAP_CONF="$2"; SNAP_STATE="$3/state"; SNAP_TOP="$3/top"',
                    'SNAP_BOOT_FLAG="$3/boot-flag"; BULK_FLAG="$3/bulk-flag"',
                    "snap_status",
                ]
            )
            r = subprocess.run(
                ["bash", "-c", script, "_", BTRFS, conf, d],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def cache_line(self, text: str | None) -> str:
        lines = [ln for ln in self.status(text) if "cache folders (CACHEDIR.TAG)" in ln]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def test_values(self) -> None:
        dropped = "  cache folders (CACHEDIR.TAG): dropped from new snapshots; which ones: cst btrfs snapshots caches"
        kept = "  cache folders (CACHEDIR.TAG): kept in new snapshots (SNAPSHOT_EXCLUDE_CACHES=no)"
        self.assertEqual(self.cache_line(None), dropped)
        self.assertEqual(self.cache_line("NAS_ENABLED=0\n"), dropped)
        self.assertEqual(self.cache_line("SNAPSHOT_EXCLUDE_CACHES=no\n"), kept)
        self.assertEqual(self.cache_line("SNAPSHOT_EXCLUDE_CACHES='no'\n"), kept)
        self.assertEqual(self.cache_line('SNAPSHOT_EXCLUDE_CACHES="yes"\nSNAPSHOT_EXCLUDE_CACHES=no\n'), kept)
        self.assertEqual(self.cache_line("SNAPSHOT_EXCLUDE_CACHES=no\nSNAPSHOT_EXCLUDE_CACHES=yes\n"), dropped)
        self.assertEqual(self.cache_line("# SNAPSHOT_EXCLUDE_CACHES=no\n"), dropped)
        # As btrfs-snapshot sources it: a comment after the value is no part of it.
        self.assertEqual(self.cache_line("SNAPSHOT_EXCLUDE_CACHES=no   # off for now\n"), kept)

    def test_the_nas_part_still_reads_the_same_file(self) -> None:
        self.assertIn("NAS backup: off", self.status("NAS_ENABLED=0\n"))
        on = [
            ln
            for ln in self.status("NAS_ENABLED=1\nNAS_HOST=nas\nNAS_PORT=22\nNAS_USER=u\nNAS_PATH=/b\n")
            if ln.startswith("NAS backup")
        ]
        self.assertEqual(len(on), 1, on)
        self.assertIn("(u@nas:22)", on[0])


class SetupHandsNoSeamTest(unittest.TestCase):
    """Every btrfs-snapshot run of `cst btrfs snapshots ...` (setup-snapshots.bash, as root) is
    started without CST_SNAPSHOT_ROOT, btrfs-snapshot's test seam, even when the caller has it.
    (setup's live conversion: test_btrfs_setup.py, the setup cycle.)"""

    def run_setup(self, body: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "btrfs-snapshot")
            with open(fake, "w", encoding="utf-8") as f:
                f.write('#!/bin/bash\nprintf "%s|%s\\n" "$*" "${CST_SNAPSHOT_ROOT-unset}" >>"$T/seen"\n')
            os.chmod(fake, 0o755)
            script = "\n".join(
                [
                    'SRC="$1"; . "$1/setup-snapshots.bash"',
                    'SNAP_RUNTIME="$T/btrfs-snapshot"; PROG=cst-btrfs-test',
                    # What snap_disable does besides the purge (units, mounts, the btrbk configs of an
                    # older version in the host's /etc/btrbk) is not this test's, and stays off the host.
                    "disable_timers() { :; }; systemctl() { :; }; systemd-escape() { echo top; }; cst_remove() { :; }",
                    """grep() { [[ " $* " == *" /etc/btrbk/"* ]] && return 1; command grep "$@"; }""",
                    'SNAP_BOOT_FLAG="$T/convert-at-boot"; BULK_FLAG="$T/bulk-at-boot"',
                    "export CST_SNAPSHOT_ROOT=/elsewhere",
                    body,
                ]
            )
            r = subprocess.run(
                ["bash", "-c", script, "_", BTRFS],
                check=False,
                capture_output=True,
                text=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "T": d},
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual((r.returncode, r.stderr), (0, ""), r.stdout)
            with open(os.path.join(d, "seen"), encoding="utf-8") as f:
                return f.read().splitlines()

    def test_list_caches_prune_and_purge(self) -> None:
        self.assertEqual(
            self.run_setup(
                "snapshots_cmd list --json; snapshots_cmd caches; snapshots_cmd prune; "
                "snapshots_cmd prune --confirm; snap_disable --purge >/dev/null"
            ),
            [
                "list --json|unset",
                "caches|unset",
                "prune --dry-run --allow-mass-delete|unset",
                "prune --allow-mass-delete|unset",
                "purge|unset",
            ],
        )


class ShippedUnitsTest(unittest.TestCase):
    def test_no_unit_hands_the_test_seam_or_any_setting_to_btrfs_snapshot(self) -> None:
        # CST_SNAPSHOT_ROOT moves every path of a root program into a test folder: the units
        # that run btrfs-snapshot pass it no environment at all.
        for name in ("btrfs-snapshot.service", "btrfs-snapshot-nas.service", "btrfs-subvol-convert.service"):
            with open(os.path.join(BTRFS, "units", name), encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln and not ln.startswith(("#", "["))]
            with self.subTest(name):
                self.assertIn("ExecStart=/usr/local/sbin/btrfs-snapshot ", "\n".join(lines))
                found = {ln.partition("=")[0].strip() for ln in lines} & {
                    "Environment",
                    "EnvironmentFile",
                    "PassEnvironment",
                }
                self.assertEqual(found, set())


if __name__ == "__main__":
    unittest.main()


class InstallOrderTest(unittest.TestCase):
    """DESIGN 8.3: the bash front (btrfs/btrfs-snapshot) hands its commands to btrfs-snapshot-runtime,
    so the runtime is written before the front and removed after it. install writes in manifest
    order and remove deletes in the reverse order (tests/test_install_record.py), so its row comes
    first, in the same component."""

    def test_the_runtime_is_written_before_the_front_and_removed_after_it(self) -> None:
        from cst_core.application.install import InstallRequest, InstallService
        from cst_core.domain.install import INSTALL_MANIFEST, select_entries
        from tests.test_install import MemoryTarget

        entries = select_entries(INSTALL_MANIFEST, component="btrfs:snapshots")
        target = MemoryTarget()
        svc = InstallService(target, lambda e: f"{e.src}\n".encode())
        req = InstallRequest()
        list(svc.install(entries, req))
        runtime, front = "/usr/local/sbin/btrfs-snapshot-runtime", "/usr/local/sbin/btrfs-snapshot"
        self.assertLess(target.writes.index(runtime), target.writes.index(front))
        removed = [ln.removeprefix("removed:   ") for ln in svc.remove([svc.placement(e, req) for e in entries])]
        self.assertLess(removed.index(front), removed.index(runtime))
        by_src = {e.src: e for e in INSTALL_MANIFEST}
        runtime, front = by_src["btrfs/btrfs-snapshot-runtime"], by_src["btrfs/btrfs-snapshot"]
        # Side by side: the front runs the runtime from its own folder.
        self.assertEqual(os.path.dirname(runtime.dest), os.path.dirname(front.dest))
        self.assertEqual((runtime.kind, runtime.owner), ("python-entry", "root"))


class GuardNumbersTest(unittest.TestCase):
    """DESIGN 8.2: the space-guard defaults are the runtime's (DEFAULTS); btrfs-bulk reads them from
    the text of btrfs-snapshot, whose three lines must say the same."""

    def test_the_front_and_the_runtime_agree(self) -> None:
        from btrfs.bulk.adapters.host import Host
        from btrfs.bulk.adapters.machine import HomeTree, Primitives
        from btrfs.bulk.domain import BIG, BulkPaths
        from btrfs.snapshot_runtime.domain import DEFAULTS, GUARD_KEYS

        with tempfile.TemporaryDirectory() as tmp:
            paths = BulkPaths.under(etc=tmp, state=tmp, lock=os.path.join(tmp, "lock"))
            script = os.path.join(BTRFS, "btrfs-snapshot")
            guard = Host(paths, HomeTree(Primitives()), lambda: script).snapshot_guard()
        names = {
            "pause": "SKIP_AT_OR_BELOW_GIB",
            "resume": "RESUME_ABOVE_GIB",
            "emergency": "EMERGENCY_AT_OR_BELOW_GIB",
        }
        self.assertEqual(set(names.values()), set(GUARD_KEYS))
        self.assertEqual(guard, {k: int(DEFAULTS[n]) * BIG for k, n in names.items()})
        # Read from the file, not bulk's own fallback: each line is there once.
        with open(script, encoding="utf-8") as f:
            text = f.read()
        for name in GUARD_KEYS:
            self.assertEqual(re.findall(rf"^{name}=(\d+)$", text, re.MULTILINE), [DEFAULTS[name]])
