# SPDX-License-Identifier: MPL-2.0
"""The Hypothesis settings of tests/mbt: one profile each for a developer, CI and the nightly run.

dev: random examples, failures kept in an example database outside the
checkout, so the next run tries them first. ci: derandomized (the same
examples on every run of the same code), no database. nightly: random, five
times the examples, no database; `invowk cmd test mbt` picks a seed and
prints it, so a failure can be replayed with `--seed N`. None of them has a
deadline: on a shared machine one slow example is load, not a bug.

Hypothesis's own folder (its example database and the constants/ and
unicode_data/ caches) is ${XDG_CACHE_HOME:-~/.cache}/cst-mbt/<key>, the key as
tests/uv-dev makes it for the checkout's virtual environment. Its default
would be ./.hypothesis, in the checkout.

The environment, read once by load():
- CST_MBT_PROFILE: dev, ci or nightly; unset or empty: ci under CI (CI or
  GITHUB_ACTIONS set), else dev.
- CST_MBT_EXAMPLES: examples per test; unset, empty or 0: the profile's.
- CST_MBT_SEED: the seed of every test; unset or empty: none.

A @given test takes the profile that is loaded when it is decorated, so every
tests/mbt module calls load() at import, before its tests. Hypothesis has no
setting for a seed: a TestCase of tests/mbt derives from MbtTestCase, which
seeds its @given tests, and runs a state machine with run_machine().
"""

from collections.abc import Callable, Mapping
from enum import StrEnum
import hashlib
import os
from pathlib import Path
from typing import Final, override
import unittest

from hypothesis import HealthCheck, seed, settings
from hypothesis.configuration import set_hypothesis_home_dir
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test

from cst_core.domain.text import enum_member
from cst_core.domain.values import parse_whole_number
from tests.repo_files import REPO

PROFILE_VAR: Final = "CST_MBT_PROFILE"
EXAMPLES_VAR: Final = "CST_MBT_EXAMPLES"
SEED_VAR: Final = "CST_MBT_SEED"
# The variables that say a run is in CI (GitHub Actions sets both).
CI_VARS: Final = ("CI", "GITHUB_ACTIONS")


class MbtProfile(StrEnum):
    DEV = "dev"
    CI = "ci"
    NIGHTLY = "nightly"


# Examples per test. 100 on a pull request: 5 examples missed a known bug (fv-poc VERIFY).
DEFAULT_EXAMPLES: Final[Mapping[MbtProfile, int]] = {
    MbtProfile.DEV: 100,
    MbtProfile.CI: 100,
    MbtProfile.NIGHTLY: 500,
}


class Active:
    """What load() chose for this process: read by MbtTestCase and run_machine."""

    seed: int | None = None


def cache_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """Hypothesis's folder for this checkout, outside it (as tests/uv-dev keys its virtual environment)."""
    key = hashlib.sha1(os.path.realpath(REPO).encode()).hexdigest()[:12]
    home = environ.get("HOME") or os.path.expanduser("~")
    base = environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")
    return Path(base) / "cst-mbt" / key


def profile_from(environ: Mapping[str, str]) -> MbtProfile:
    """The profile CST_MBT_PROFILE names; unset or empty: ci under CI, else dev."""
    word = environ.get(PROFILE_VAR, "")
    if not word:
        in_ci = any(environ.get(name, "") != "" for name in CI_VARS)
        return MbtProfile.CI if in_ci else MbtProfile.DEV
    profile = enum_member(MbtProfile, word)
    if profile is None:
        names = ", ".join(p.value for p in MbtProfile)
        raise SystemExit(f"{PROFILE_VAR}={word!r}: expected one of {names}")
    return profile


def _count(name: str, text: str) -> int:
    # ASCII digits within int()'s digit limit: isdigit() alone takes "²", which int() refuses.
    number = parse_whole_number(text)
    if number is None:
        raise SystemExit(f"{name}={text!r}: expected a whole number")
    return number


def examples_from(environ: Mapping[str, str], profile: MbtProfile) -> int:
    """The examples per test CST_MBT_EXAMPLES names; unset, empty or 0: the profile's default."""
    text = environ.get(EXAMPLES_VAR, "")
    count = _count(EXAMPLES_VAR, text) if text else 0
    return count or DEFAULT_EXAMPLES[profile]


def seed_from(environ: Mapping[str, str]) -> int | None:
    """The seed CST_MBT_SEED names; unset or empty: None (Hypothesis draws one)."""
    text = environ.get(SEED_VAR, "")
    return _count(SEED_VAR, text) if text else None


def settings_for(profile: MbtProfile, examples: int, environ: Mapping[str, str] = os.environ) -> settings:
    """The settings of one profile, not registered: load() registers them."""
    common = settings(
        max_examples=examples,
        deadline=None,
        print_blob=True,
        suppress_health_check=[HealthCheck.too_slow],
    )
    match profile:
        case MbtProfile.DEV:
            database = DirectoryBasedExampleDatabase(cache_dir(environ) / "examples")
            return settings(common, derandomize=False, database=database)
        case MbtProfile.CI:
            return settings(common, derandomize=True, database=None)
        case MbtProfile.NIGHTLY:
            return settings(common, derandomize=False, database=None)


def load(environ: Mapping[str, str] = os.environ) -> MbtProfile:
    """Register the profiles and load the one the environment chooses (process-wide); return it."""
    chosen = profile_from(environ)
    examples = examples_from(environ, chosen)
    Active.seed = seed_from(environ)
    # Before anything else: Hypothesis's own files would go to ./.hypothesis, the checkout.
    set_hypothesis_home_dir(cache_dir(environ))
    for profile in MbtProfile:
        count = examples if profile == chosen else DEFAULT_EXAMPLES[profile]
        settings.register_profile(profile.value, settings_for(profile, count, environ))
    settings.load_profile(chosen.value)
    return chosen


def seeded[T: Callable[..., object]](test: T, seed_value: int | None) -> T:
    """The test (a @given function or a state machine class) with seed_value applied; None: the test as it was."""
    return test if seed_value is None else seed(seed_value)(test)


def seed_tests(cls: type, seed_value: int | None) -> None:
    """Apply seed_value to every @given test_* method of cls (a @given test has a .hypothesis attribute)."""
    if seed_value is None:
        return
    for name, value in list(vars(cls).items()):
        if name.startswith("test") and callable(value) and hasattr(value, "hypothesis"):
            setattr(cls, name, seeded(value, seed_value))


class MbtTestCase(unittest.TestCase):
    """The base of every TestCase of tests/mbt: its @given tests get the run's seed (CST_MBT_SEED)."""

    @override
    def __init_subclass__(cls, *args: object, **kwargs: object) -> None:
        if args or kwargs:
            raise TypeError(f"{cls.__name__}: MbtTestCase takes no class arguments")
        super().__init_subclass__()
        seed_tests(cls, Active.seed)


def run_machine(machine: type[RuleBasedStateMachine], *, seed_value: int | None = None) -> None:
    """Run a state machine as a test, with the run's seed (or seed_value when given)."""
    chosen = Active.seed if seed_value is None else seed_value
    run_state_machine_as_test(seeded(machine, chosen))
