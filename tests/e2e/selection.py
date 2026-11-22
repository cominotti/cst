# SPDX-License-Identifier: MPL-2.0
"""Selection helpers and domain tags for Docker E2E suites.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
import fnmatch
from typing import Final
import unittest


class E2ETag(StrEnum):
    """Domain areas for filtering Tier 1 E2E tests."""

    SMOKE = "smoke"
    BTRFS = "btrfs"
    CPU = "cpu"
    BLUETOOTH = "bluetooth"
    SWAP = "swap"
    TOOLCHAINS = "toolchains"
    VM = "vm"
    DOCTOR = "doctor"
    INSTALL = "install"
    NAS = "nas"


TAGS_ATTR: Final = "_cst_e2e_tags"


def e2e_tags[F: Callable[..., object]](*tags: E2ETag) -> Callable[[F], F]:
    """Attach one or more E2ETag values to a Tier 1 E2E test method."""
    if not tags:
        raise ValueError("e2e_tags requires at least one E2ETag")
    resolved: list[E2ETag] = []
    for tag in tags:
        if not isinstance(tag, E2ETag):
            raise TypeError(f"expected E2ETag, got {tag!r}")
        resolved.append(tag)
    frozen = frozenset(resolved)

    def decorate(fn: F) -> F:
        setattr(fn, TAGS_ATTR, frozen)
        return fn

    return decorate


def test_method_name(case: unittest.TestCase) -> str:
    """Return the bare test method name for a unittest TestCase."""
    return case.id().rsplit(".", 1)[-1]


def test_tags(case: unittest.TestCase) -> frozenset[E2ETag]:
    """Return the E2ETag set attached to a TestCase method (empty if untagged)."""
    fn = getattr(type(case), test_method_name(case), None)
    raw = getattr(fn, TAGS_ATTR, None)
    if not isinstance(raw, frozenset):
        return frozenset()
    return frozenset(item for item in raw if isinstance(item, E2ETag))


def parse_tags(raw_tags: Sequence[str]) -> frozenset[E2ETag]:
    """Parse repeatable/comma-separated tag options into a frozenset of E2ETag."""
    valid = {tag.value: tag for tag in E2ETag}
    out: set[E2ETag] = set()
    for chunk in raw_tags:
        for token in chunk.split(","):
            name = token.strip().lower()
            if not name:
                raise ValueError(f"empty E2E tag in {chunk!r} (valid: {', '.join(sorted(valid))})")
            member = valid.get(name)
            if member is None:
                raise ValueError(f"unknown E2E tag {token.strip()!r} (valid: {', '.join(sorted(valid))})")
            out.add(member)
    return frozenset(out)


def parse_patterns(raw_patterns: Sequence[str]) -> tuple[str, ...]:
    """Parse repeatable/comma-separated test name patterns."""
    out: list[str] = []
    for chunk in raw_patterns:
        for token in chunk.split(","):
            pat = token.strip()
            if not pat:
                raise ValueError(f"empty test match pattern in {chunk!r}")
            out.append(pat)
    return tuple(out)


def matches_pattern(name: str, pattern: str) -> bool:
    """Case-insensitive glob (when wildcard present) or substring match."""
    low_name = name.lower()
    low_pat = pattern.lower()
    if any(ch in low_pat for ch in "*?["):
        return fnmatch.fnmatchcase(low_name, low_pat)
    return low_pat in low_name


def iter_cases(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    """Flatten a unittest TestSuite into ordered TestCase instances."""
    cases: list[unittest.TestCase] = []
    stack: list[unittest.TestSuite | unittest.TestCase] = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(reversed(list(item)))
        else:
            cases.append(item)
    return cases


def select_cases(
    cases: Iterable[unittest.TestCase],
    *,
    patterns: Sequence[str] = (),
    tags: frozenset[E2ETag] = frozenset(),
) -> list[unittest.TestCase]:
    """Filter TestCase instances by tag union and pattern union (AND when both set)."""
    selected: list[unittest.TestCase] = []
    for case in cases:
        if tags and not (test_tags(case) & tags):
            continue
        if patterns:
            name = test_method_name(case)
            if not any(matches_pattern(name, pat) for pat in patterns):
                continue
        selected.append(case)
    return selected


def build_suite(cases: Sequence[unittest.TestCase]) -> unittest.TestSuite:
    """Wrap ordered TestCase instances into a unittest TestSuite."""
    return unittest.TestSuite(cases)


def untagged_methods(suite: unittest.TestSuite) -> list[str]:
    """Return names of any TestCase methods in suite without E2ETag annotations."""
    return [test_method_name(case) for case in iter_cases(suite) if not test_tags(case)]


def format_case_line(case: unittest.TestCase) -> str:
    """Format a TestCase name with its sorted E2ETags (if any)."""
    name = test_method_name(case)
    tags = sorted(tag.value for tag in test_tags(case))
    if not tags:
        return name
    return f"{name} [{', '.join(tags)}]"


@dataclass(frozen=True, slots=True)
class SuiteSelection:
    """Filter criteria for selecting tests from an E2E suite."""

    patterns: tuple[str, ...] = ()
    tags: frozenset[E2ETag] = frozenset()

    @property
    def active(self) -> bool:
        """True when at least one pattern or tag filter is requested."""
        return bool(self.patterns or self.tags)

    def for_tier(self, tier: int) -> SuiteSelection:
        """Return selection scoped to tier (tags only apply to tier 1)."""
        return self if tier == 1 else SuiteSelection(patterns=self.patterns)
