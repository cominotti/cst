#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Coverage floors: the domain and application layers keep their unit test coverage.

Use: python3 -I -B tests/coverage_floors.py COVERAGE_JSON   (from anywhere)

COVERAGE_JSON is the report `coverage json` writes after the unit suite ran
under coverage.py (`invowk cmd test coverage` does both; the nightly
workflow runs it). This script adds up, per "<package> <layer>" (the
packages and layers of tests/test_architecture.py, as the budgets use them),
the statements and branches the tests ran, and compares the percentage with
COVERAGE_FLOORS:

- a percentage below its floor fails: the change added code no unit test
  runs, or took tests away;
- a percentage at or above its floor passes. A floor is raised by hand, from
  a nightly report; nothing lowers one (`invowk cmd fix budgets` does not
  read this table);
- a key with no floor passes (a new package or layer); add its floor once a
  nightly has measured it;
- a floor whose key is no longer measured fails: rename it with its package,
  or delete it.

Only domain and application: their tests run in the test process, with
fakes behind the ports. Adapters, cli and the programs are mostly tested by
running the real program as a child process (`/usr/bin/python3 -I ...`),
which coverage.py cannot follow, so their numbers are a floor of the truth
and are only reported. See docs/dev-tools.md, "Coverage".

Standard library only.
"""

from collections.abc import Mapping, Sequence
import json
import os
import sys
from typing import Final

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.budgets import is_test_file, package_of
from tests.test_architecture import module_role

# The layers with a floor.
FLOORED_LAYERS: Final = ("domain", "application")

# "<package> <layer>" -> the lowest percentage of statements and branches
# covered, as coverage.py counts them together (its "percent_covered").
# Set from a run in a Fedora 45 toolbox, rounded down to a whole percent,
# minus 2: CI skips a few tests a workstation runs (EXPECTED_SKIPS of
# tests/run_unit.py).
COVERAGE_FLOORS: Final[dict[str, float]] = {
    "bluetooth domain": 94,
    "bluetooth/setup application": 34,
    "bluetooth/setup domain": 71,
    "btrfs/bulk application": 87,
    "btrfs/bulk domain": 90,
    "btrfs/chrome_cache domain": 94,
    "btrfs/docker_gc application": 90,
    "btrfs/docker_gc domain": 96,
    "btrfs/docker_volumes_nocow application": 98,
    "btrfs/docker_volumes_nocow domain": 98,
    "btrfs/health_check domain": 76,
    "btrfs/snapshot application": 86,
    "btrfs/snapshot domain": 95,
    "btrfs/stall_monitord domain": 80,
    "btrfs/stall_report domain": 78,
    "cpu domain": 93,
    "cpu/game domain": 95,
    "cpu/setup domain": 95,
    "cst_core application": 89,
    "cst_core domain": 95,
    "doctor application": 63,
    "doctor domain": 91,
    "swap application": 89,
    "swap domain": 92,
    "toolchains application": 90,
    "toolchains domain": 91,
    "vm application": 30,
    "vm domain": 85,
}


def layer_counts(report: Mapping[str, object]) -> dict[str, tuple[int, int]]:
    """{"<package> <layer>": (covered, total)} of the production files of a coverage JSON report.

    covered and total count statements and branches together. Test files and
    files outside a package layer (a program, an __init__.py at the top of a
    package) are left out.
    """
    files = report.get("files")
    if not isinstance(files, dict):
        raise ValueError("not a coverage JSON report: no 'files' table")
    out: dict[str, tuple[int, int]] = {}
    for rel, data in files.items():
        if is_test_file(rel) or rel.startswith(".github/"):
            continue
        role = module_role(rel)
        summary = data.get("summary", {}) if isinstance(data, dict) else {}
        if role is None or not isinstance(summary, dict):
            continue
        key = f"{package_of(rel)} {role}"
        covered = int(summary.get("covered_lines", 0)) + int(summary.get("covered_branches", 0))
        total = int(summary.get("num_statements", 0)) + int(summary.get("num_branches", 0))
        old = out.get(key, (0, 0))
        out[key] = (old[0] + covered, old[1] + total)
    return out


def percentages(counts: Mapping[str, tuple[int, int]]) -> dict[str, float]:
    """The covered percentage of each key (100 for a key with nothing to cover)."""
    return {key: 100.0 * covered / total if total else 100.0 for key, (covered, total) in counts.items()}


def floor_problems(measured: Mapping[str, float], floors: Mapping[str, float] = COVERAGE_FLOORS) -> list[str]:
    """Every floored key below its floor, and every floor no key is measured for, as text lines."""
    out: list[str] = []
    for key in sorted(floors):
        if key not in measured:
            out.append(
                f"{key}: has a floor ({floors[key]:g} %) but no such package layer was measured; "
                "rename the entry with its package, or delete it"
            )
        elif measured[key] < floors[key]:
            out.append(
                f"{key}: {measured[key]:.1f} % covered, floor {floors[key]:g} %: "
                "add unit tests for the code the change added"
            )
    return out


def format_table(measured: Mapping[str, float], floors: Mapping[str, float] = COVERAGE_FLOORS) -> str:
    """Every measured key with its percentage, and its floor where it has one."""
    lines = []
    for key in sorted(measured):
        floor = (
            f"floor {floors[key]:g} %" if key in floors else ("no floor" if key.split()[-1] in FLOORED_LAYERS else "")
        )
        lines.append(f"  {key:<40} {measured[key]:5.1f} %  {floor}".rstrip())
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: coverage_floors.py COVERAGE_JSON", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as fh:
        report = json.load(fh)
    measured = percentages(layer_counts(report))
    print("Unit test coverage per package and layer (statements and branches):")
    print(format_table(measured))
    floored = {k: v for k, v in measured.items() if k.split()[-1] in FLOORED_LAYERS}
    problems = floor_problems(floored)
    for line in problems:
        print(f"error: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
