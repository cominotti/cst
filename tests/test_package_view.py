"""Unit tests for tests/package_view.py: the tests' one namespace over a package's modules."""

import types
import unittest
from unittest import mock

from tests.package_view import PackageView


def modules() -> tuple[types.ModuleType, types.ModuleType]:
    """A defining module and one that imported its name (and has one of its own)."""
    domain = types.ModuleType("domain")
    vars(domain).update(LIMIT=5)
    cli = types.ModuleType("cli")
    vars(cli).update(LIMIT=domain.LIMIT, ONLY_CLI="cli")
    return cli, domain


class PackageViewTest(unittest.TestCase):
    def test_reads_from_the_first_module_with_the_name(self) -> None:
        cli, domain = modules()
        view = PackageView(cli, domain)
        self.addCleanup(view.restore)
        self.assertEqual(view.ONLY_CLI, "cli")
        self.assertEqual(view.LIMIT, 5)
        with self.assertRaises(AttributeError):
            _ = view.MISSING

    def test_a_set_reaches_every_module_and_restore_undoes_it(self) -> None:
        cli, domain = modules()
        view = PackageView(cli, domain)
        view.LIMIT = 0
        self.assertEqual((cli.LIMIT, domain.LIMIT), (0, 0))
        view.LIMIT = 1  # a second set keeps the first original
        view.restore()
        self.assertEqual((cli.LIMIT, domain.LIMIT), (5, 5))
        view.restore()  # twice is fine
        self.assertEqual(cli.LIMIT, 5)

    def test_setting_an_unknown_name_is_an_error(self) -> None:
        cli, domain = modules()
        view = PackageView(cli, domain)
        self.addCleanup(view.restore)
        with self.assertRaisesRegex(AttributeError, "nothing would see"):
            view.LIMMIT = 0

    def test_a_new_view_restores_the_previous_one(self) -> None:
        cli, domain = modules()
        first = PackageView(cli, domain)
        first.LIMIT = 0
        second = PackageView(cli, domain)
        self.addCleanup(second.restore)
        self.assertEqual((cli.LIMIT, domain.LIMIT), (5, 5))

    def test_mock_patch_object_sets_and_restores_every_module(self) -> None:
        cli, domain = modules()
        view = PackageView(cli, domain)
        self.addCleanup(view.restore)
        with mock.patch.object(view, "LIMIT", 0):
            self.assertEqual((cli.LIMIT, domain.LIMIT), (0, 0))
            with mock.patch.object(view, "LIMIT", 1):  # nested: the inner end goes back to 0
                self.assertEqual((cli.LIMIT, domain.LIMIT), (1, 1))
            self.assertEqual((cli.LIMIT, domain.LIMIT), (0, 0))
        self.assertEqual((cli.LIMIT, domain.LIMIT), (5, 5))
        view.restore()  # nothing left to restore
        self.assertEqual((cli.LIMIT, domain.LIMIT), (5, 5))

    def test_deleting_a_name_never_set_is_an_error(self) -> None:
        cli, domain = modules()
        view = PackageView(cli, domain)
        self.addCleanup(view.restore)
        with self.assertRaisesRegex(AttributeError, "not replaced"):
            del view.LIMIT
        self.assertEqual(cli.LIMIT, 5)


if __name__ == "__main__":
    unittest.main()
