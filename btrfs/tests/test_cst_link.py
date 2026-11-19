# SPDX-License-Identifier: MPL-2.0
"""Unit tests for cst-link.bash (the ~/.local/bin/cst link made by
`cst btrfs install` and `cst cpu install`), on a scratch home. getent and
runuser are replaced by shell functions, so no root is needed.

Run from the repository root:
    python3 -I -B -m unittest discover -s btrfs/tests -t . -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import override
import unittest

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from btrfs.chrome_cache.domain import FLAG_DIR, FLAG_SIZE
from btrfs.snapshot.domain import parse_policy, policy_max_kept

LIB = os.path.join(REPO, "cst-link.bash")
CST = os.path.join(REPO, "cst")


class CstLinkTest(unittest.TestCase):
    home: str
    link: str

    @override
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="cst-link-test-")
        self.link = os.path.join(self.home, ".local", "bin", "cst")

    @override
    def tearDown(self) -> None:
        shutil.rmtree(self.home)

    def run_link(self, user: str = "alice") -> subprocess.CompletedProcess[str]:
        script = (
            'set -euo pipefail; source "$0"; H="$1"\n'
            'getent() { echo "alice:x:1000:1000::$H:/bin/bash"; }\n'
            # runuser -u USER -- CMD...: run CMD as is.
            'runuser() { shift 3; "$@"; }\n'
            f"link_cst_command {user}\n"
        )
        return subprocess.run(
            ["bash", "-c", script, LIB, self.home], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL
        )

    def test_creates_link_once(self) -> None:
        r = self.run_link()
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(CST))
        self.assertIn("linked:", r.stdout)
        r = self.run_link()  # idempotent, quiet
        self.assertEqual(r.stdout + r.stderr, "")

    def test_root_or_empty_user_does_nothing(self) -> None:
        self.run_link(user="root")
        self.run_link(user="''")
        self.assertFalse(os.path.lexists(self.link))

    def test_own_file_is_left_alone(self) -> None:
        os.makedirs(os.path.dirname(self.link))
        with open(self.link, "w") as f:
            f.write("#!/bin/sh\n")
        r = self.run_link()
        self.assertIn("left alone", r.stderr)
        self.assertFalse(os.path.islink(self.link))

    def test_dangling_link_is_replaced(self) -> None:
        os.makedirs(os.path.dirname(self.link))
        os.symlink(os.path.join(self.home, "moved-away", "cst"), self.link)
        self.run_link()
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(CST))


class DomainRulesTest(unittest.TestCase):
    """Rules of the btrfs packages, called straight from their domain modules.
    The stall thresholds are tested on the daemon (test_stall_monitord,
    EpisodeThresholdsTest) and the protected registries on docker-gc
    (test_docker_gc)."""

    def test_domain_policies_and_parsers(self) -> None:
        # Retention policies: btrfs-snapshot-core's parser (the real one).
        self.assertEqual(parse_policy("48h 14d 8w 12m 2y"), {"h": 48, "d": 14, "w": 8, "m": 12, "y": 2})
        self.assertEqual(parse_policy("*d"), {"d": "all"})
        self.assertIsNone(policy_max_kept(parse_policy("*d")))
        for bad in ("", "*"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_policy(bad)

        # chrome-cache's flag lines: its domain (the real one).
        self.assertEqual(
            (FLAG_DIR, FLAG_SIZE),
            ("--disk-cache-dir=/tmp/chrome-cache", "--disk-cache-size=536870912"),
        )


if __name__ == "__main__":
    unittest.main()
