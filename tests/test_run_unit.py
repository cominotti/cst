"""tests/run_unit.py: what it loads (all, --gates, names)."""

import os
import unittest

from tests.run_unit import EXPECTED_SKIPS, GATE_MODULES, expected_skip, load, skip_problems
from tests.repo_files import REPO


def ids(suite: unittest.TestSuite) -> set[str]:
    out: set[str] = set()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            out |= ids(test)
        else:
            out.add(test.id())
    return out


class LoadTest(unittest.TestCase):
    def test_gate_modules_exist_and_load(self) -> None:
        for name in GATE_MODULES:
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(REPO, *name.split(".")) + ".py"))
        got = {i.rsplit(".", 2)[0] for i in ids(load(REPO, ["--gates"]))}
        self.assertEqual(got, set(GATE_MODULES))

    def test_names_load_only_those(self) -> None:
        got = ids(load(REPO, ["-v", "tests.test_run_unit.LoadTest.test_names_load_only_those"]))
        self.assertEqual(got, {"tests.test_run_unit.LoadTest.test_names_load_only_those"})

    def test_every_expected_skip_names_a_test(self) -> None:
        every = ids(load(REPO, []))
        stale = sorted(key for key in EXPECTED_SKIPS if not any(i == key or i.startswith(key + ".") for i in every))
        self.assertEqual(stale, [], "no such test (renamed or gone): update EXPECTED_SKIPS")

    def test_unexpected_skips_fail(self) -> None:
        self.assertTrue(expected_skip("vm.tests.test_vm.BtrfsReflinkTest.test_reflink_of_a_nocow_image"))
        self.assertFalse(expected_skip("vm.tests.test_vm.BtrfsReflinkTestOther.test_x"))
        self.assertEqual(
            skip_problems(
                [("vm.tests.test_vm.BtrfsReflinkTest.setUp", "x"), ("vm.tests.test_vm.PartsTest.test_a", "y")]
            ),
            ["vm.tests.test_vm.PartsTest.test_a: skipped (y), and not in EXPECTED_SKIPS of tests/run_unit.py"],
        )

    def test_unknown_option_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            load(REPO, ["--gate"])


if __name__ == "__main__":
    unittest.main()
