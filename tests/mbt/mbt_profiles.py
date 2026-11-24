# SPDX-License-Identifier: MPL-2.0
"""Tests of tests/mbt/profiles.py: the environment it reads, the settings of each profile, the seeding.

They test the parsers and the settings objects, never load() with another
environment: load() changes the active profile and Hypothesis's folder for
every test after it in the same worker. The parsers take the environment as
a parameter, so these tests do not depend on the CST_MBT_* of the run.
"""

from collections.abc import Callable
import glob
import importlib
import os
import subprocess
from typing import Final
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.stateful import RuleBasedStateMachine, rule

from tests.mbt import profiles
from tests.mbt.profiles import MbtProfile, MbtTestCase
from tests.repo_files import REPO

profiles.load()

# The inner tests of the seeding checks carry their own settings: the run's profile may be the
# derandomized ci one, where two runs draw the same values with no seed at all.
RANDOM: Final = settings(derandomize=False, database=None, max_examples=20, deadline=None)


class ProfileFromTest(MbtTestCase):
    def test_unset_or_empty_is_dev_outside_ci_and_ci_under_it(self) -> None:
        for environ, want in (
            ({}, "dev"),
            ({"CST_MBT_PROFILE": ""}, "dev"),
            ({"CI": ""}, "dev"),
            ({"CI": "true"}, "ci"),
            ({"GITHUB_ACTIONS": "true"}, "ci"),
            ({"CST_MBT_PROFILE": "", "CI": "1"}, "ci"),
        ):
            with self.subTest(environ=environ):
                self.assertEqual(profiles.profile_from(environ), want)

    def test_each_word_names_its_profile_and_wins_over_ci(self) -> None:
        for word in ("dev", "ci", "nightly"):
            with self.subTest(word=word):
                self.assertEqual(profiles.profile_from({"CST_MBT_PROFILE": word}), word)
                self.assertEqual(profiles.profile_from({"CST_MBT_PROFILE": word, "CI": "true"}), word)

    def test_any_other_word_stops_the_run_naming_the_variable(self) -> None:
        for word in ("Dev", "fast", "ci ", "0"):
            with self.subTest(word=word), self.assertRaises(SystemExit) as caught:
                profiles.profile_from({"CST_MBT_PROFILE": word})
            self.assertEqual(str(caught.exception), f"CST_MBT_PROFILE={word!r}: expected one of dev, ci, nightly")


class ExamplesFromTest(MbtTestCase):
    def test_unset_empty_or_zero_is_the_profile_s_default(self) -> None:
        for environ in ({}, {"CST_MBT_EXAMPLES": ""}, {"CST_MBT_EXAMPLES": "0"}):
            with self.subTest(environ=environ):
                self.assertEqual(profiles.examples_from(environ, MbtProfile.DEV), 100)
                self.assertEqual(profiles.examples_from(environ, MbtProfile.CI), 100)
                self.assertEqual(profiles.examples_from(environ, MbtProfile.NIGHTLY), 500)

    def test_a_whole_number_is_that_many(self) -> None:
        for text, want in (("1", 1), ("100", 100), ("06", 6), ("2000", 2000)):
            with self.subTest(text=text):
                self.assertEqual(profiles.examples_from({"CST_MBT_EXAMPLES": text}, MbtProfile.CI), want)

    def test_anything_else_stops_the_run_naming_the_variable(self) -> None:
        # "\N{SUPERSCRIPT TWO}": str.isdigit() takes it, int() does not.
        # 5000 digits: isascii() and isdigit() take them, int() raised ValueError ("Exceeds the limit").
        for text in ("-1", "1.5", "\N{SUPERSCRIPT TWO}", "x", " 5", "1" * 5000):
            with self.subTest(text=text[:10]), self.assertRaises(SystemExit) as caught:
                profiles.examples_from({"CST_MBT_EXAMPLES": text}, MbtProfile.CI)
            self.assertEqual(str(caught.exception), f"CST_MBT_EXAMPLES={text!r}: expected a whole number")


class SeedFromTest(MbtTestCase):
    def test_unset_or_empty_is_no_seed_and_a_number_is_that_seed(self) -> None:
        self.assertIsNone(profiles.seed_from({}))
        self.assertIsNone(profiles.seed_from({"CST_MBT_SEED": ""}))
        self.assertEqual(profiles.seed_from({"CST_MBT_SEED": "12"}), 12)
        self.assertEqual(profiles.seed_from({"CST_MBT_SEED": "0"}), 0)

    def test_anything_else_stops_the_run(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            profiles.seed_from({"CST_MBT_SEED": "x"})
        self.assertEqual(str(caught.exception), "CST_MBT_SEED='x': expected a whole number")

    def test_a_seed_past_int_s_digit_limit_stops_the_run(self) -> None:
        text = "1" * 5000
        with self.assertRaises(SystemExit) as caught:
            profiles.seed_from({"CST_MBT_SEED": text})
        self.assertEqual(str(caught.exception), f"CST_MBT_SEED={text!r}: expected a whole number")


class SettingsForTest(MbtTestCase):
    ENV: Final = {"XDG_CACHE_HOME": "/cache", "HOME": "/home/u"}

    def test_dev_is_random_with_a_database_outside_the_checkout(self) -> None:
        s = profiles.settings_for(MbtProfile.DEV, 100, self.ENV)
        self.assertFalse(s.derandomize)
        database = s.database
        assert isinstance(database, DirectoryBasedExampleDatabase), database
        self.assertEqual(str(database.path), str(profiles.cache_dir(self.ENV) / "examples"))
        self.assertEqual(s.max_examples, 100)

    def test_ci_is_derandomized_without_a_database(self) -> None:
        s = profiles.settings_for(MbtProfile.CI, 100, self.ENV)
        self.assertTrue(s.derandomize)
        self.assertIsNone(s.database)
        self.assertEqual(s.max_examples, 100)

    def test_nightly_is_random_without_a_database(self) -> None:
        s = profiles.settings_for(MbtProfile.NIGHTLY, 500, self.ENV)
        self.assertFalse(s.derandomize)
        self.assertIsNone(s.database)
        self.assertEqual(s.max_examples, 500)

    def test_no_profile_has_a_deadline_and_each_takes_the_examples_given(self) -> None:
        for profile in MbtProfile:
            with self.subTest(profile=profile.value):
                s = profiles.settings_for(profile, 7, self.ENV)
                self.assertIsNone(s.deadline)
                self.assertEqual(s.max_examples, 7)
                self.assertTrue(s.print_blob)


class CacheDirTest(MbtTestCase):
    def test_it_is_keyed_as_tests_uv_dev_keys_the_virtual_environment(self) -> None:
        # tests/uv-dev: key="$(printf '%s' "${repo}" | sha1sum | cut -c1-12)", repo from pwd -P.
        done = subprocess.run(["sha1sum"], input=os.path.realpath(REPO), capture_output=True, text=True, check=True)
        key = done.stdout[:12]
        self.assertEqual(str(profiles.cache_dir({"XDG_CACHE_HOME": "/c"})), f"/c/cst-mbt/{key}")
        self.assertEqual(str(profiles.cache_dir({"XDG_CACHE_HOME": "", "HOME": "/h"})), f"/h/.cache/cst-mbt/{key}")
        self.assertEqual(str(profiles.cache_dir({"HOME": "/h"})), f"/h/.cache/cst-mbt/{key}")

    def test_the_run_s_folder_is_outside_the_checkout(self) -> None:
        folder = os.path.realpath(profiles.cache_dir())
        self.assertFalse((folder + "/").startswith(os.path.realpath(REPO) + "/"), folder)


def _drawing_test(sink: list[int]) -> Callable[..., None]:
    """A @given test method, with its own random settings, that writes down what it draws."""

    @RANDOM
    @given(st.integers())
    def draws(_self: object, x: int) -> None:
        sink.append(x)

    return draws


def _drawing_machine(sink: list[int]) -> type[RuleBasedStateMachine]:
    """A state machine whose one rule writes down what it draws (a new class each time)."""

    class Drawing(RuleBasedStateMachine):
        @rule(x=st.integers())
        def draw(self, x: int) -> None:
            sink.append(x)

    return Drawing


class SeedingTest(MbtTestCase):
    def _draws(self, seed_value: int | None) -> list[int]:
        sink: list[int] = []
        profiles.seeded(_drawing_test(sink), seed_value)(self)
        return sink

    def _machine_draws(self, seed_value: int) -> list[int]:
        sink: list[int] = []
        profiles.run_machine(_drawing_machine(sink), seed_value=seed_value)
        return sink

    def test_a_seed_draws_the_same_values_twice_and_another_seed_others(self) -> None:
        first = self._draws(7)
        self.assertEqual(self._draws(7), first)
        self.assertNotEqual(self._draws(8), first)

    def test_no_seed_leaves_the_test_and_the_class_as_they_were(self) -> None:
        test = _drawing_test([])
        self.assertIs(profiles.seeded(test, None), test)

        class Plain(unittest.TestCase):
            test_draws = _drawing_test([])

        before = dict(vars(Plain))
        profiles.seed_tests(Plain, None)
        self.assertEqual(dict(vars(Plain)), before)

    def test_seed_tests_seeds_the_given_tests_and_leaves_the_others(self) -> None:
        sink: list[int] = []

        def plain(_self: unittest.TestCase) -> None:
            pass

        class Some(unittest.TestCase):
            test_draws = _drawing_test(sink)
            test_plain = plain

        profiles.seed_tests(Some, 7)
        self.assertIs(vars(Some)["test_plain"], plain)
        Some("test_draws").test_draws()
        self.assertEqual(sink, self._draws(7))

    def test_a_subclass_of_mbt_test_case_gets_the_run_s_seed(self) -> None:
        saved = profiles.Active.seed
        self.addCleanup(setattr, profiles.Active, "seed", saved)
        profiles.Active.seed = 7
        sink: list[int] = []

        class Seeded(MbtTestCase):
            test_draws = _drawing_test(sink)

        Seeded("test_draws").test_draws()
        self.assertEqual(sink, self._draws(7))

    def test_run_machine_seeds_a_state_machine(self) -> None:
        first = self._machine_draws(3)
        self.assertEqual(self._machine_draws(3), first)
        self.assertNotEqual(self._machine_draws(4), first)


class EveryTestCaseIsAnMbtTestCaseTest(MbtTestCase):
    def test_every_test_case_of_tests_mbt_derives_from_mbt_test_case(self) -> None:
        # Otherwise its @given tests would run unseeded, and a nightly failure could not be replayed.
        paths = sorted(glob.glob(os.path.join(REPO, "tests", "mbt", "mbt_*.py")))
        self.assertIn(os.path.join(REPO, "tests", "mbt", "mbt_profiles.py"), paths)
        for path in paths:
            name = "tests.mbt." + os.path.basename(path).removesuffix(".py")
            module = importlib.import_module(name)
            for value in vars(module).values():
                if isinstance(value, type) and issubclass(value, unittest.TestCase) and value.__module__ == name:
                    with self.subTest(test_case=f"{name}.{value.__name__}"):
                        self.assertTrue(issubclass(value, MbtTestCase))


if __name__ == "__main__":
    unittest.main()
