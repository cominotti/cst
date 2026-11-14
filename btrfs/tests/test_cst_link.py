"""Unit tests for cst-link.bash (the ~/.local/bin/cst link made by
`cst btrfs install` and `cst cpu install`), on a scratch home. getent and
runuser are replaced by shell functions, so no root is needed.

Run from the repository root:
    python3 -I -B -m unittest discover -s btrfs/tests -t . -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(REPO, "cst-link.bash")
CST = os.path.join(REPO, "cst")


class CstLinkTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="cst-link-test-")
        self.link = os.path.join(self.home, ".local", "bin", "cst")

    def tearDown(self):
        shutil.rmtree(self.home)

    def run_link(self, user="alice"):
        script = (
            'set -euo pipefail; source "$0"; H="$1"\n'
            'getent() { echo "alice:x:1000:1000::$H:/bin/bash"; }\n'
            # runuser -u USER -- CMD...: run CMD as is.
            'runuser() { shift 3; "$@"; }\n'
            f'link_cst_command {user}\n')
        return subprocess.run(["bash", "-c", script, LIB, self.home],
                              capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)

    def test_creates_link_once(self):
        r = self.run_link()
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(CST))
        self.assertIn("linked:", r.stdout)
        r = self.run_link()                       # idempotent, quiet
        self.assertEqual(r.stdout + r.stderr, "")

    def test_root_or_empty_user_does_nothing(self):
        self.run_link(user="root")
        self.run_link(user="''")
        self.assertFalse(os.path.lexists(self.link))

    def test_own_file_is_left_alone(self):
        os.makedirs(os.path.dirname(self.link))
        with open(self.link, "w") as f:
            f.write("#!/bin/sh\n")
        r = self.run_link()
        self.assertIn("left alone", r.stderr)
        self.assertFalse(os.path.islink(self.link))

    def test_dangling_link_is_replaced(self):
        os.makedirs(os.path.dirname(self.link))
        os.symlink(os.path.join(self.home, "moved-away", "cst"), self.link)
        self.run_link()
        self.assertEqual(os.path.realpath(self.link), os.path.realpath(CST))


if __name__ == "__main__":
    unittest.main()
