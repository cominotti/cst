# SPDX-License-Identifier: MPL-2.0
"""The helpers of btrfs/tests/bulk_fakes.py that the fault and MBT tests of btrfs-bulk build on.

A fault in these helpers hides a fault of the code under test (an error swallowed, a call that is
no point, a +C the fake forgets) or leaves scratch folders behind, so each gets a test here.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from btrfs.tests import bulk_fakes
from btrfs.tests.bulk_fakes import BulkHome


def fail() -> None:
    raise RuntimeError("planted failure")


class BulkHomeCleanupTest(unittest.TestCase):
    """A BulkHome removes its temporary folder whatever fails while it is built or closed."""

    def scratch(self) -> str:
        """A folder of this test's own as tempfile's default folder: what BulkHome leaves is seen."""
        folder = tempfile.mkdtemp(prefix="bulk-fakes-test-")
        self.addCleanup(shutil.rmtree, folder)
        self.enterContext(mock.patch.object(tempfile, "tempdir", folder))
        return folder

    def test_a_home_that_fails_to_build_leaves_no_folder(self) -> None:
        folder = self.scratch()
        with (
            mock.patch.object(bulk_fakes, "Wiring", side_effect=RuntimeError("planted")),
            self.assertRaises(RuntimeError),
        ):
            BulkHome()
        self.assertEqual(os.listdir(folder), [])

    def test_close_removes_the_folder_when_undoing_a_patch_fails(self) -> None:
        folder = self.scratch()
        fx = BulkHome()
        fx.stack.callback(fail)
        with self.assertRaises(RuntimeError):
            fx.close()
        self.assertEqual(os.listdir(folder), [])


if __name__ == "__main__":
    unittest.main()
