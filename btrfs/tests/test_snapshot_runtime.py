"""Unit tests for the shell side of the cache-folder exclusion: core_cache_args
in btrfs/btrfs-snapshot (the core's options from SNAPSHOT_EXCLUDE_CACHES and
SNAPSHOT_CACHES_SKIP) and what snap_status in btrfs/setup-snapshots.bash
(`snapshots status`) shows for SNAPSHOT_EXCLUDE_CACHES.

btrfs-snapshot cannot be sourced in a test: it reads /etc/btrfs-setup and
creates /var/lib/btrfs-snapshot at the top. The functions are cut out of the
file and run in bash with stubs and variables of the test's own.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_snapshot_runtime -v
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BTRFS = os.path.dirname(HERE)
RUNTIME = os.path.join(BTRFS, "btrfs-snapshot")


def shell_function(path, name):
    """The definition of the shell function NAME in PATH ('name() {' to '}')."""
    with open(path) as f:
        text = f.read()
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    if m is None:
        raise AssertionError(f"{name}() not found in {path}")
    return m.group(0)


class CoreCacheArgsTest(unittest.TestCase):
    def args(self, exclude="yes", skips=".local/share/containers", workspace_subvol=False):
        script = "\n".join([
            "set -Eeuo pipefail",
            'log() { echo "<$1>btrfs-snapshot: $2"; }',
            'is_subvol() { [[ "${WS_SUBVOL}" == 1 && "$1" == /var/home/u/Workspace ]]; }',
            shell_function(RUNTIME, "core_sources"),
            shell_function(RUNTIME, "core_cache_args"),
            "HOME_DIR=/var/home/u HOME_MOUNT=/var/home HOME_SUBVOL=home TOP=/top",
            'SNAPSHOT_EXCLUDE_CACHES="$1" SNAPSHOT_CACHES_SKIP="$2"',
            'core_sources "72h" "48h"',
            "core_cache_args",
            'printf "arg %s\\n" "${CORE_CACHE_ARGS[@]}"',
            'printf "skip %s\\n" "${CORE_CACHE_SKIPS[@]}"',
        ])
        with tempfile.TemporaryDirectory() as d:
            # Files in the working folder: a '*' in the skip list must stay a '*'.
            open(os.path.join(d, "a-file"), "w").close()
            r = subprocess.run(["bash", "-c", script, "_", exclude, skips], cwd=d, capture_output=True, text=True,
                               env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                    "WS_SUBVOL": "1" if workspace_subvol else "0"},
                               stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.skips = [ln[5:] for ln in lines if ln.startswith("skip ")]
        return ([ln[4:] for ln in lines if ln.startswith("arg ")],
                [ln for ln in lines if not ln.startswith(("arg ", "skip "))])

    def test_default_is_on_with_podman_kept_whole(self):
        args, logs = self.args()
        self.assertEqual(args, ["--exclude-caches", "--caches-skip", "home=u/.local/share/containers"])
        self.assertEqual(logs, [])
        # What `caches` gets (it takes no --exclude-caches).
        self.assertEqual(self.skips, ["--caches-skip", "home=u/.local/share/containers"])

    def test_off_keeps_the_skips_for_the_listing(self):
        args, _ = self.args(exclude="no")
        self.assertEqual(args, ["--caches-skip", "home=u/.local/share/containers"])
        self.assertEqual(self.skips, args)

    def test_a_typo_warns_and_stays_on(self):
        args, logs = self.args(exclude="nope")
        self.assertEqual(args[0], "--exclude-caches")
        self.assertEqual(logs, ["<4>btrfs-snapshot: SNAPSHOT_EXCLUDE_CACHES='nope' is not yes or no; using yes"])

    def test_paths_go_to_the_source_that_holds_them(self):
        skips = "Workspace/big/target Workspace .local/share/containers/ ./notes"
        args, _ = self.args(skips=skips, workspace_subvol=True)
        self.assertEqual(args, ["--exclude-caches", "--caches-skip", "Workspace=big/target",
                                "--caches-skip", "Workspace=.", "--caches-skip", "home=u/.local/share/containers",
                                "--caches-skip", "home=u/notes"])
        # Until ~/Workspace is a subvolume it is part of the home snapshots.
        args, _ = self.args(skips="Workspace/big/target", workspace_subvol=False)
        self.assertEqual(args, ["--exclude-caches", "--caches-skip", "home=u/Workspace/big/target"])

    def test_paths_outside_the_home_are_ignored_with_a_warning(self):
        args, logs = self.args(skips="/etc ../other a/../../b x/./y ok *")
        self.assertEqual(args, ["--exclude-caches", "--caches-skip", "home=u/ok", "--caches-skip", "home=u/*"])
        self.assertEqual(len(logs), 4, logs)
        self.assertTrue(all("is not a path inside your home; ignored" in ln for ln in logs))

    def test_empty_skip_list(self):
        args, logs = self.args(skips="")
        self.assertEqual((args, logs), (["--exclude-caches"], []))


class ListCachesTest(unittest.TestCase):
    """`btrfs-snapshot caches --json` must print only the core's JSON on
    stdout, also when the settings make core_cache_args warn."""

    def test_warnings_go_to_stderr_not_into_the_json(self):
        script = "\n".join([
            "set -Eeuo pipefail",
            'log() { echo "<$1>btrfs-snapshot: $2"; }',
            "is_subvol() { return 1; }",
            shell_function(RUNTIME, "core_sources"),
            shell_function(RUNTIME, "core_cache_args"),
            shell_function(RUNTIME, "list_caches"),
            "HOME_DIR=/var/home/u HOME_MOUNT=/var/home HOME_SUBVOL=home TOP=/top SNAPDIR=/top/snapshots",
            "HOME_PRESERVE=72h WS_PRESERVE=48h CONF=/etc/btrfs-snapshot/snapshots.conf",
            "SNAPSHOT_EXCLUDE_CACHES=nope SNAPSHOT_CACHES_SKIP='/etc ok'",
            "CORE=core",
            'core() { printf \'{"args": "%s"}\\n\' "$*"; }',
            "list_caches --json",
        ])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 1, r.stdout)
        self.assertTrue(r.stdout.startswith('{"args": "caches --snapdir /top/snapshots'), r.stdout)
        self.assertIn("--caches-skip home=u/ok --json", r.stdout)
        self.assertIn("SNAPSHOT_EXCLUDE_CACHES='nope' is not yes or no", r.stderr)
        self.assertIn("'/etc' is not a path inside your home", r.stderr)


class SnapStatusCachesTest(unittest.TestCase):
    """The cache line of snap_status, with stubs for everything that reads the machine."""

    STUBS = "\n".join([
        'snap_home() { echo /nonexistent/home; }',
        'systemctl() { return 1; }',
        'snap_entry_state() { echo -; }',
        'snap_plan_stale() { :; }',
        'snap_chrome() { :; }',
    ])

    def status(self, text):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "snapshots.conf")
            if text is not None:
                with open(conf, "w") as f:
                    f.write(text)
            script = "\n".join([
                'SRC="$1"; . "$1/setup-snapshots.bash"',
                self.STUBS,
                'PROG="cst btrfs"; SNAP_CONF="$2"; SNAP_STATE="$3/state"; SNAP_TOP="$3/top"',
                'SNAP_BOOT_FLAG="$3/boot-flag"; BULK_FLAG="$3/bulk-flag"',
                'snap_status',
            ])
            r = subprocess.run(["bash", "-c", script, "_", BTRFS, conf, d], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def cache_line(self, text):
        lines = [ln for ln in self.status(text) if "cache folders (CACHEDIR.TAG)" in ln]
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def test_values(self):
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

    def test_the_nas_part_still_reads_the_same_file(self):
        self.assertIn("NAS backup: off", self.status("NAS_ENABLED=0\n"))
        on = [ln for ln in self.status("NAS_ENABLED=1\nNAS_HOST=nas\nNAS_PORT=22\nNAS_USER=u\nNAS_PATH=/b\n")
              if ln.startswith("NAS backup")]
        self.assertEqual(len(on), 1, on)
        self.assertIn("(u@nas:22)", on[0])


if __name__ == "__main__":
    unittest.main()
