# SPDX-License-Identifier: MPL-2.0
"""tests/xdg_cache.py: the user's cache folder the dev tools use."""

import os
import pwd
import unittest
from unittest import mock

from tests.xdg_cache import cache_base

# The password database's entry of the user, as the tests give it.
_ENTRY = pwd.struct_passwd(("u", "x", 1000, 1000, "", "/pw/home", "/bin/sh"))


class CacheBaseTest(unittest.TestCase):
    def test_an_absolute_xdg_cache_home_wins(self) -> None:
        self.assertEqual(cache_base({"XDG_CACHE_HOME": "/c", "HOME": "/h"}), "/c")
        self.assertEqual(cache_base({"XDG_CACHE_HOME": "/c"}), "/c")

    def test_a_relative_or_empty_xdg_cache_home_is_ignored(self) -> None:
        # The XDG Base Directory spec ignores a relative path: the tools run from the top of the checkout.
        for xdg in ("cache", "./cache", "", "~/.cache"):
            with self.subTest(xdg=xdg):
                self.assertEqual(cache_base({"XDG_CACHE_HOME": xdg, "HOME": "/h"}), os.path.join("/h", ".cache"))

    def test_an_absolute_home_gives_its_cache(self) -> None:
        self.assertEqual(cache_base({"HOME": "/h"}), "/h/.cache")

    def test_an_empty_relative_or_unset_home_goes_to_the_password_database(self) -> None:
        for environ in ({"HOME": ""}, {"HOME": "h"}, {}, {"XDG_CACHE_HOME": "rel", "HOME": ""}):
            with self.subTest(environ=environ), mock.patch("tests.xdg_cache.pwd.getpwuid", return_value=_ENTRY) as get:
                self.assertEqual(cache_base(environ), "/pw/home/.cache")
                get.assert_called_once_with(os.getuid())

    def test_no_entry_in_the_password_database_gives_none(self) -> None:
        with mock.patch("tests.xdg_cache.pwd.getpwuid", side_effect=KeyError("no entry")):
            self.assertIsNone(cache_base({"HOME": ""}))
            self.assertEqual(cache_base({"XDG_CACHE_HOME": "/c", "HOME": ""}), "/c")

    def test_a_relative_or_empty_home_in_the_password_database_gives_none(self) -> None:
        # tests/uv-dev stops on such a getent home too: either would put the cache inside the checkout.
        for pw_dir in ("", "rel", "./rel"):
            entry = pwd.struct_passwd(("u", "x", 1000, 1000, "", pw_dir, "/bin/sh"))
            with self.subTest(pw_dir=pw_dir), mock.patch("tests.xdg_cache.pwd.getpwuid", return_value=entry):
                self.assertIsNone(cache_base({"HOME": ""}))
                self.assertIsNone(cache_base({"XDG_CACHE_HOME": "rel"}))
                self.assertEqual(cache_base({"XDG_CACHE_HOME": "/c", "HOME": ""}), "/c")


if __name__ == "__main__":
    unittest.main()
