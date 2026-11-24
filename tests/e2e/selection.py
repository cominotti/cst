# SPDX-License-Identifier: MPL-2.0
"""Selection helpers and domain tags for Docker E2E suites.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import fnmatch
import os
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


class HostScope(StrEnum):
    """Whether host-exclusive Tier 1 tests run with or apart from parallel shards."""

    ALL = "all"
    PARALLEL = "parallel"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True, slots=True)
class ShardSpec:
    """1-based shard selector (`index/total`) for splitting a suite."""

    index: int
    total: int

    def label(self) -> str:
        """Compact `I/N` representation."""
        return f"{self.index}/{self.total}"

    def dir_suffix(self) -> str:
        """Directory suffix for shard artifact folders."""
        return f"s{self.index}-of-{self.total}"


DEFAULT_TEST_WEIGHT: Final = 4
# Measured CI seconds for the slower Tier 1 tests so LPT packing balances shards.
TIER1_WEIGHTS: Final[Mapping[str, int]] = {
    "test_80_bluetooth_second_apply_and_le_audio_cycle": 82,
    "test_26_btrfs_watch_saves_the_report_of_a_run": 66,
    "test_78_docker_gc_recorder_stays_failed_on_a_state_of_a_newer_docker_gc": 60,
    "test_95_bluetooth_python_program_with_the_bash_one_runuser_and_le_audio": 50,
    "test_19_every_component_full_cycle_on_the_container_root": 33,
    "test_101_bluetooth_keeps_the_headset_of_an_older_setup_and_works_without_one": 27,
    "test_11_manifest_installs_and_every_entry_runs_from_root": 26,
    "test_106_stall_monitor_upgrade_installs_the_program_before_the_unit": 20,
    "test_104_stall_monitor_unit_is_active_only_after_ready": 17,
    "test_105_stall_monitor_is_restarted_when_its_pings_stop": 15,
    "test_13_degraded_boot_install_cycle": 14,
    "test_151_cst_vm_forgets_torn_nas_snapshots_with_the_real_restic": 13,
    "test_85_cpu_game_profile_bash_and_python_copies_share_their_state_and_lock": 12,
    "test_08_cpu_game_profile_idles_escaped_user_boxes_and_pins_memory_low": 10,
    "test_10_cpu_quiet_reads_the_real_proc_files": 10,
    "test_84_cpu_install_cycle_over_the_bash_version": 10,
    "test_103_docker_gc_installer_keeps_the_registry_of_mains_install": 10,
    "test_90_swap_upgrade_from_an_install_with_the_partition_in_the_code": 9,
    "test_05_bluetooth_config_wp_roles_and_ldac_unit": 8,
    "test_102_docker_gc_enable_keeps_the_registry_of_mains_install": 7,
}
# Tests that load all host CPUs and must run solo when local containers run in parallel.
HOST_EXCLUSIVE_TESTS: Final[frozenset[str]] = frozenset({"test_10_cpu_quiet_reads_the_real_proc_files"})


def parse_shard(raw: str) -> ShardSpec:
    """Parse an `I/N` shard specification (1 <= I <= N)."""
    parts = raw.strip().split("/")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"invalid --shard {raw!r} (expected I/N, e.g. 1/3)")
    index, total = int(parts[0]), int(parts[1])
    if total < 1 or index < 1 or index > total:
        raise ValueError(f"invalid --shard {raw!r} (expected 1 <= I <= N)")
    return ShardSpec(index=index, total=total)


def case_weight(case: unittest.TestCase, weights: Mapping[str, int] = TIER1_WEIGHTS) -> int:
    """Estimated execution weight of one TestCase for shard balancing."""
    return weights.get(test_method_name(case), DEFAULT_TEST_WEIGHT)


def assign_shards(
    cases: Sequence[unittest.TestCase],
    total: int,
    weights: Mapping[str, int] = TIER1_WEIGHTS,
) -> list[list[unittest.TestCase]]:
    """Partition cases across `total` bins via deterministic LPT packing."""
    if total < 1:
        raise ValueError(f"shard count must be >= 1, got {total}")
    indexed = list(enumerate(cases))
    indexed.sort(key=lambda pair: (-case_weight(pair[1], weights), pair[0]))
    bins: list[list[tuple[int, unittest.TestCase]]] = [[] for _ in range(total)]
    bin_weights = [0] * total
    for orig_idx, case in indexed:
        target = min(range(total), key=lambda idx: (bin_weights[idx], idx))
        bins[target].append((orig_idx, case))
        bin_weights[target] += case_weight(case, weights)
    return [[case for _, case in sorted(bucket, key=lambda pair: pair[0])] for bucket in bins]


def select_shard(
    cases: Sequence[unittest.TestCase],
    shard: ShardSpec,
    weights: Mapping[str, int] = TIER1_WEIGHTS,
) -> list[unittest.TestCase]:
    """Return the ordered subset of cases belonging to `shard`."""
    return assign_shards(cases, shard.total, weights)[shard.index - 1]


def split_host_exclusive(cases: Sequence[unittest.TestCase]) -> tuple[list[unittest.TestCase], list[unittest.TestCase]]:
    """Split cases into `(parallel_cases, host_exclusive_cases)`."""
    parallel: list[unittest.TestCase] = []
    exclusive: list[unittest.TestCase] = []
    for case in cases:
        if test_method_name(case) in HOST_EXCLUSIVE_TESTS:
            exclusive.append(case)
        else:
            parallel.append(case)
    return parallel, exclusive


def filter_host_scope(cases: Sequence[unittest.TestCase], scope: HostScope) -> list[unittest.TestCase]:
    """Keep all cases, only parallel cases, or only host-exclusive cases."""
    if scope == HostScope.ALL:
        return list(cases)
    parallel, exclusive = split_host_exclusive(cases)
    return parallel if scope == HostScope.PARALLEL else exclusive


def _usable_cpus() -> int:
    """The CPUs this process may run on: its CPU affinity (a pinned toolbox, a cgroup's cpuset), or every
    CPU of the machine when the affinity cannot be read.

    os.sched_getaffinity (Linux, where the E2E tests run), not os.process_cpu_count (Python 3.13 and
    later): CI's tier 1 job runs this file on the python3 of its Ubuntu 24.04 host, 3.12."""
    try:
        return len(os.sched_getaffinity(0))
    except OSError:
        return os.cpu_count() or 1


def default_tier1_jobs(selected_count: int, cpu_count: int | None = None) -> int:
    """Default parallel container count for local Tier 1 runs (`1` for small selections): half the CPUs
    this process may run on (`cpu_count` when given), at most 4."""
    if selected_count <= 4:
        return 1
    cpus = max(1, _usable_cpus()) if cpu_count is None else max(1, cpu_count)
    return min(4, max(1, cpus // 2), selected_count)


@dataclass(frozen=True, slots=True)
class SuiteSelection:
    """Filter and sharding criteria for selecting tests from an E2E suite."""

    patterns: tuple[str, ...] = ()
    tags: frozenset[E2ETag] = frozenset()
    shard: ShardSpec | None = None
    host_scope: HostScope = HostScope.ALL

    @property
    def active(self) -> bool:
        """True when at least one pattern or tag filter is requested."""
        return bool(self.patterns or self.tags)

    def for_tier(self, tier: int) -> SuiteSelection:
        """Return selection scoped to tier (tags, shards and host scope apply to tier 1)."""
        return self if tier == 1 else SuiteSelection(patterns=self.patterns)
