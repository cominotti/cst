#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Ruff budgets: rule hits that may not go above their entry (the end of `invowk cmd test pylint`).

Use: python3 -I -B tests/ruff_budget.py   (from anywhere; needs uv, like tests/uv-dev)

Some ruff rules have hits today that are not fixed yet, so they cannot be in
the `select` list of pyproject.toml (`ruff check` would fail; those of a
selected family are in its `ignore` list instead). This script
runs ruff with them added, reads its JSON output and compares the number of
hits per package with RUFF_BUDGETS (tests/budgets.py: a count above its
entry fails, a count below it passes and `invowk cmd fix budgets` lowers
the entry; a missing key is 0):

- BLE001: `except Exception` (blind except) where a narrower error was meant;
- SLF001: a private member (`obj._x`) used outside its class, production
  code only (tests may reach into what they test);
- FBT001, FBT002: a bool positional parameter (`f(x, True)` says nothing at
  the call site; make it keyword-only with `*,`);
- PLR2004: a number compared with a literal (magic-value-comparison;
  ruff's default allows 0, 1, -1 and every string), production code only
  (tests compare with the numbers they pin). Name it: a constant, or a
  unit of cst_core.domain.values (.agents/rules/magic-values.md);
- C901: a function whose cyclomatic complexity is above
  [tool.ruff.lint.mccabe] max-complexity. It stands in for SonarCloud's
  cognitive complexity rule (S3776), which only a Sonar run measures: the
  same functions, counted another way;
- PLR0911, PLR0912, PLR0913, PLR0915, PLR0917: a function with too many
  return statements (6), branches (12), arguments (5), statements (50) or
  positional arguments (5), ruff's defaults; split it, pass a record
  instead of a long list of arguments, or make arguments keyword-only;
- FBT003: a bare True or False passed as a positional argument (the call
  site says nothing; pass it by keyword);
- N: pep8-naming, a whole family: a name that is not snake_case
  (functions, variables, arguments), CapWords (classes) or ends in Error
  (exceptions), counted under "N".

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

RULES: Final = (
    "BLE001",
    "SLF001",
    "FBT001",
    "FBT002",
    "C901",
    "PLR2004",
    "PLR0911",
    "PLR0912",
    "PLR0913",
    "PLR0915",
    "PLR0917",
    "FBT003",
    "N",
)
PRODUCTION_ONLY: Final = frozenset({"SLF001", "PLR2004"})

RUFF_BUDGETS: Final[dict[str, int]] = {
    ".github/scripts FBT001": 1,
    ".github/scripts PLR2004": 3,
    "bluetooth C901": 1,
    "bluetooth FBT001": 3,
    "bluetooth FBT002": 2,
    "bluetooth FBT003": 1,
    "bluetooth PLR0911": 4,
    "bluetooth PLR0912": 1,
    "bluetooth PLR0913": 1,
    "bluetooth PLR0915": 1,
    "bluetooth PLR2004": 1,
    "bluetooth tests FBT001": 5,
    "bluetooth tests FBT002": 5,
    "bluetooth tests FBT003": 10,
    "bluetooth tests PLR0913": 3,
    "bluetooth tests PLR0917": 2,
    "bluetooth/setup N": 1,
    "bluetooth/setup PLR0911": 2,
    "bluetooth/setup PLR2004": 4,
    "btrfs tests C901": 1,
    "btrfs tests FBT001": 15,
    "btrfs tests FBT002": 10,
    "btrfs tests FBT003": 28,
    "btrfs tests N": 20,
    "btrfs tests PLR0911": 2,
    "btrfs tests PLR0912": 2,
    "btrfs tests PLR0913": 11,
    "btrfs tests PLR0917": 5,
    "btrfs/bulk BLE001": 1,
    "btrfs/bulk FBT001": 12,
    "btrfs/bulk FBT002": 6,
    "btrfs/bulk FBT003": 2,
    "btrfs/bulk N": 33,
    "btrfs/bulk PLR0911": 5,
    "btrfs/bulk PLR0913": 8,
    "btrfs/bulk PLR0917": 5,
    "btrfs/bulk PLR2004": 6,
    "btrfs/chrome_cache FBT001": 2,
    "btrfs/chrome_cache FBT002": 1,
    "btrfs/chrome_cache PLR0911": 2,
    "btrfs/chrome_cache PLR2004": 1,
    "btrfs/docker_gc N": 2,
    "btrfs/docker_gc PLR0911": 2,
    "btrfs/docker_gc PLR0913": 1,
    "btrfs/docker_gc PLR2004": 2,
    "btrfs/docker_volumes_nocow BLE001": 1,
    "btrfs/docker_volumes_nocow FBT003": 3,
    "btrfs/docker_volumes_nocow PLR0913": 1,
    "btrfs/docker_volumes_nocow PLR0917": 1,
    "btrfs/docker_volumes_nocow PLR2004": 1,
    "btrfs/pending_alerts PLR2004": 1,
    "btrfs/snapshot BLE001": 1,
    "btrfs/snapshot PLR0911": 1,
    "btrfs/snapshot PLR0913": 13,
    "btrfs/snapshot PLR0917": 11,
    "btrfs/snapshot PLR2004": 3,
    "btrfs/snapshot_runtime N": 2,
    "btrfs/snapshot_runtime PLR0911": 1,
    "btrfs/snapshot_runtime PLR0912": 1,
    "btrfs/snapshot_runtime PLR0913": 1,
    "btrfs/stall_monitord PLR2004": 4,
    "btrfs/stall_report FBT001": 2,
    "btrfs/stall_report PLR0911": 1,
    "btrfs/stall_report PLR2004": 21,
    "cpu FBT001": 1,
    "cpu FBT002": 1,
    "cpu FBT003": 3,
    "cpu PLR0913": 4,
    "cpu PLR0917": 1,
    "cpu PLR2004": 2,
    "cpu tests FBT001": 1,
    "cpu tests FBT002": 1,
    "cpu tests FBT003": 2,
    "cpu/game PLR0913": 1,
    "cpu/game PLR2004": 5,
    "cpu/setup N": 1,
    "cpu/setup PLR0911": 2,
    "cpu/setup PLR0913": 2,
    "cst_core FBT001": 2,
    "cst_core FBT002": 1,
    "cst_core FBT003": 2,
    "cst_core N": 4,
    "cst_core PLR0911": 2,
    "cst_core PLR0913": 5,
    "cst_core PLR2004": 7,
    "doctor BLE001": 2,
    "doctor FBT001": 4,
    "doctor FBT002": 1,
    "doctor FBT003": 1,
    "doctor PLR0913": 2,
    "doctor PLR0917": 2,
    "doctor PLR2004": 8,
    "doctor tests FBT001": 5,
    "doctor tests FBT002": 5,
    "doctor tests FBT003": 5,
    "doctor tests PLR0913": 2,
    "doctor tests PLR0917": 1,
    "swap PLR0911": 1,
    "swap PLR0913": 2,
    "swap PLR2004": 3,
    "swap tests FBT001": 2,
    "swap tests FBT003": 3,
    "swap tests N": 1,
    "swap tests PLR0913": 2,
    "tests tests C901": 1,
    "tests tests FBT001": 2,
    "tests tests FBT003": 11,
    "tests tests PLR0912": 5,
    "tests tests PLR0913": 4,
    "tests tests PLR0915": 2,
    "tests tests PLR0917": 1,
    "toolchains FBT001": 12,
    "toolchains N": 1,
    "toolchains PLR0911": 4,
    "toolchains PLR0912": 2,
    "toolchains PLR0913": 5,
    "toolchains PLR0917": 4,
    "toolchains PLR2004": 4,
    "toolchains tests FBT001": 1,
    "toolchains tests FBT002": 1,
    "toolchains tests FBT003": 17,
    "toolchains tests PLR0913": 2,
    "toolchains tests PLR0917": 1,
    "vm BLE001": 3,
    "vm FBT001": 13,
    "vm FBT002": 4,
    "vm FBT003": 3,
    "vm N": 2,
    "vm PLR0911": 1,
    "vm PLR0912": 1,
    "vm PLR0913": 3,
    "vm PLR2004": 2,
    "vm tests FBT001": 9,
    "vm tests FBT002": 7,
    "vm tests FBT003": 29,
    "vm tests PLR0913": 2,
    "vm tests PLR0915": 1,
}


def relative(path: str, repo: str = REPO) -> str:
    """A path from ruff's output, relative to the checkout."""
    return os.path.relpath(path, repo)


def budgeted_rule(code: str) -> str | None:
    """The entry of RULES a ruff code counts under: the code itself, or the family it belongs to
    (N for N802: an entry of letters only, then digits); None for a code no entry names."""
    for rule in RULES:
        if code == rule or (rule.isalpha() and code.startswith(rule) and code[len(rule) :].isdigit()):
            return rule
    return None


def rule_counts(records: Iterable[Mapping[str, object]], repo: str = REPO) -> dict[str, int]:
    """Hits of RULES per "<package> [tests ]<rule>"."""
    out: dict[str, int] = {}
    for rec in records:
        code, path = rec.get("code"), rec.get("filename")
        rule = budgeted_rule(code) if isinstance(code, str) else None
        if rule is None or not isinstance(path, str):
            continue
        rel = relative(path, repo)
        if is_test_file(rel) and rule in PRODUCTION_ONLY:
            continue
        key = f"{package_of(rel)} {'tests ' if is_test_file(rel) else ''}{rule}"
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
