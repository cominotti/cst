#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Ruff budgets: rule hits that may not go above their entry (the end of `invowk cmd test pylint`).

Use: python3 -I -B tests/ruff_budget.py   (from anywhere; needs uv, like tests/uv-dev)

Some ruff rules have hits today that are not fixed yet, so they cannot be in
the `select` list of pyproject.toml (`ruff check` would fail). This script
runs ruff with them added, reads its JSON output and compares the number of
hits per package with RUFF_BUDGETS (tests/budgets.py: a count above its
entry fails, a count below it passes and `invowk cmd fix budgets` lowers
the entry; a missing key is 0):

- BLE001: `except Exception` (blind except) where a narrower error was meant;
- SLF001: a private member (`obj._x`) used outside its class, production
  code only (tests may reach into what they test);
- FBT001, FBT002: a bool positional parameter (`f(x, True)` says nothing at
  the call site; make it keyword-only with `*,`);
- C901: a function whose cyclomatic complexity is above
  [tool.ruff.lint.mccabe] max-complexity. It stands in for SonarCloud's
  cognitive complexity rule (S3776), which only a Sonar run measures: the
  same functions, counted another way.

Hits are counted with `--ignore-noqa`, so a `# noqa` comment does not hide
one. A second run (without it) fails on any `# noqa` that suppresses
nothing (RUF100). Keys are "<package> <rule>" and "<package> tests <rule>".

The counting is standard library only; tests/test_ruff_budget.py tests it
without ruff.
"""

from collections.abc import Iterable, Mapping, Sequence
import json
import os
import subprocess
import sys
from typing import Final

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.budgets import budget_problems, format_table, is_test_file, package_of

RULES: Final = ("BLE001", "SLF001", "FBT001", "FBT002", "C901")
PRODUCTION_ONLY: Final = frozenset({"SLF001"})

RUFF_BUDGETS: Final[dict[str, int]] = {
    ".github/scripts FBT001": 1,
    "bluetooth C901": 1,
    "bluetooth FBT001": 3,
    "bluetooth FBT002": 2,
    "bluetooth tests FBT001": 5,
    "bluetooth tests FBT002": 5,
    "btrfs tests C901": 1,
    "btrfs tests FBT001": 16,
    "btrfs tests FBT002": 11,
    "btrfs/bulk BLE001": 1,
    "btrfs/bulk FBT001": 12,
    "btrfs/bulk FBT002": 6,
    "btrfs/chrome_cache FBT001": 2,
    "btrfs/chrome_cache FBT002": 1,
    "btrfs/docker_volumes_nocow BLE001": 1,
    "btrfs/snapshot BLE001": 1,
    "btrfs/stall_report C901": 4,
    "btrfs/stall_report FBT001": 2,
    "cpu FBT001": 1,
    "cpu FBT002": 1,
    "cpu tests FBT001": 1,
    "cpu tests FBT002": 1,
    "cst_core FBT001": 2,
    "cst_core FBT002": 1,
    "doctor BLE001": 2,
    "doctor C901": 3,
    "doctor FBT001": 4,
    "doctor FBT002": 1,
    "doctor tests FBT001": 5,
    "doctor tests FBT002": 5,
    "swap C901": 1,
    "swap tests FBT001": 2,
    "tests tests C901": 3,
    "tests tests FBT001": 2,
    "toolchains C901": 7,
    "toolchains FBT001": 12,
    "toolchains tests FBT001": 1,
    "toolchains tests FBT002": 1,
    "vm BLE001": 3,
    "vm C901": 5,
    "vm FBT001": 17,
    "vm FBT002": 4,
    "vm tests FBT001": 9,
    "vm tests FBT002": 7,
}


def relative(path: str, repo: str = REPO) -> str:
    """A path from ruff's output, relative to the checkout."""
    return os.path.relpath(path, repo)


def rule_counts(records: Iterable[Mapping[str, object]], repo: str = REPO) -> dict[str, int]:
    """Hits of RULES per "<package> [tests ]<rule>"."""
    out: dict[str, int] = {}
    for rec in records:
        code, path = rec.get("code"), rec.get("filename")
        if not isinstance(code, str) or code not in RULES or not isinstance(path, str):
            continue
        rel = relative(path, repo)
        if is_test_file(rel) and code in PRODUCTION_ONLY:
            continue
        key = f"{package_of(rel)} {'tests ' if is_test_file(rel) else ''}{code}"
        out[key] = out.get(key, 0) + 1
    return out


def unused_noqa(records: Iterable[Mapping[str, object]], repo: str = REPO) -> list[str]:
    """Every RUF100 hit (a `# noqa` that suppresses nothing) as "path:line: message"."""
    out: list[str] = []
    for rec in records:
        if rec.get("code") != "RUF100":
            continue
        loc = rec.get("location")
        row = loc.get("row") if isinstance(loc, dict) else "?"
        out.append(f"{relative(str(rec.get('filename')), repo)}:{row}: {rec.get('message')}")
    return sorted(out)


def run_ruff(args: Sequence[str]) -> list[Mapping[str, object]]:
    """ruff check over the checkout through tests/uv-dev, its JSON diagnostics."""
    r = subprocess.run(
        [os.path.join(REPO, "tests", "uv-dev"), "ruff", "check", "--exit-zero", "--output-format", "json", *args],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=REPO,
    )
    if r.returncode != 0:
        raise SystemExit(f"ruff_budget: ruff failed ({r.returncode}): {r.stderr.strip()}")
    data = json.loads(r.stdout)
    if not isinstance(data, list):
        raise SystemExit("ruff_budget: ruff's JSON output is not a list")
    return [rec for rec in data if isinstance(rec, dict)]


def measure() -> dict[str, int]:
    """Hits of RULES per key over the checkout (runs ruff): what RUFF_BUDGETS caps."""
    return rule_counts(run_ruff(["--ignore-noqa", "--extend-select", ",".join(RULES)]))


def main() -> int:
    rules = ",".join(RULES)
    counted = measure()
    problems = budget_problems(counted, RUFF_BUDGETS, "hits")
    problems += unused_noqa(run_ruff(["--extend-select", f"RUF100,{rules}"]))
    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print("measured table for RUFF_BUDGETS (tests/ruff_budget.py):\n" + format_table(counted), file=sys.stderr)
        return 1
    print(f"ruff budgets: {sum(counted.values())} hits of {rules}, within RUFF_BUDGETS; no unused noqa")
    return 0


if __name__ == "__main__":
    sys.exit(main())
