# SPDX-License-Identifier: MPL-2.0
"""Static `--affected` unit-test selection via repository structure, AST imports, and git diff.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import os
import subprocess
from typing import Final

from cst_core.domain.imports import imported_modules
from cst_core.domain.install import INSTALL_MANIFEST
from tests.code_graph import module_of
from tests.repo_files import REPO, repo_files
from tests.run_unit import GATE_MODULES

MODULE_ROOTS: Final = frozenset({"bluetooth", "btrfs", "cpu", "doctor", "perf", "swap", "toolchains", "vm"})
DOCS_GATES: Final = ("tests.test_docs", "tests.test_license_headers", "tests.test_repo_paths")
INSTALL_TESTS: Final = ("tests.test_install", "tests.test_install_record", "tests.test_module_routing")
E2E_UNIT_TESTS: Final = ("tests.e2e.test_run_guard", "tests.e2e.test_selection", "tests.e2e.test_image_digest")
INSTALL_SENSITIVE_SUFFIXES: Final = (
    "-setup.sh",
    ".bash",
    ".sh",
    ".service",
    ".timer",
    ".slice",
    ".mount",
    ".rules",
    ".in",
    ".conf",
    ".ini",
)
_DOCS_ONLY_FILES: Final = frozenset({"LICENSE"})
_DOCS_ONLY_PREFIXES: Final = (".agents/", ".claude/", "docs/")
_NON_UNIT_TEST_PREFIXES: Final = ("tests/e2e/suites/", "tests/fixtures/")
_E2E_PREFIX: Final = "tests/e2e/"
_GIT_REV_PARSE: Final = "rev-parse"
_GIT_VERIFY: Final = "--verify"
_MAIN_BRANCH: Final = "main"
_ORIGIN_MAIN_BRANCH: Final = "origin/main"
_BASE_CANDIDATES: Final = (_MAIN_BRANCH, _ORIGIN_MAIN_BRANCH)
_HEAD_REF: Final = "HEAD"
_FULL_SUITE_ROOT_REASONS: Final[dict[str, str]] = {
    "cst_core": "shared core (cst_core)",
    "cst.invowkmod": "repo automation/config",
    ".github": "repo automation/config",
}


@dataclass(frozen=True, slots=True)
class AffectedPlan:
    """Resolved unit-test selection plan for a set of changed files."""

    base: str
    changed: tuple[str, ...]
    full_suite: bool
    modules: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class _RepoIndex:
    test_mods_by_file: dict[str, str]
    valid_mods: set[str]
    rev: dict[str, set[str]]
    manifest_srcs: frozenset[str]


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _find_default_ref(repo: str) -> tuple[str, str] | None:
    for ref in _BASE_CANDIDATES:
        checked = _git(repo, _GIT_REV_PARSE, _GIT_VERIFY, f"{ref}^{{commit}}")
        if checked.returncode == 0 and checked.stdout.strip():
            return ref, checked.stdout.strip()
    return None


def resolve_base(repo: str = REPO, base: str | None = None) -> str:
    """Resolve explicit `--base` ref or default to merge-base with `main`/`origin/main` (else `HEAD`)."""
    if base is not None:
        checked = _git(repo, _GIT_REV_PARSE, _GIT_VERIFY, f"{base}^{{commit}}")
        if checked.returncode != 0:
            raise ValueError(f"unknown git revision {base!r}")
        return base
    head = _git(repo, _GIT_REV_PARSE, _GIT_VERIFY, _HEAD_REF)
    default_ref = _find_default_ref(repo) if head.returncode == 0 else None
    if default_ref is not None and default_ref[1] != head.stdout.strip():
        mb = _git(repo, "merge-base", default_ref[0], _HEAD_REF)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
    return _HEAD_REF


def git_changed_files(repo: str = REPO, base: str | None = None) -> tuple[str, tuple[str, ...]]:
    """Return `(resolved_base, sorted_changed_paths)` including untracked files."""
    resolved = resolve_base(repo, base)
    diff = _git(repo, "diff", "--name-only", "-z", resolved)
    if diff.returncode != 0:
        raise ValueError(f"git diff failed for {resolved!r}: {diff.stderr.strip()}")
    untracked = _git(repo, "ls-files", "-z", "--others", "--exclude-standard")
    if untracked.returncode != 0:
        raise ValueError(f"git ls-files failed: {untracked.stderr.strip()}")
    paths = {entry.replace("\\", "/") for entry in (*diff.stdout.split("\0"), *untracked.stdout.split("\0")) if entry}
    return resolved, tuple(sorted(paths))


def is_docs_only(path: str) -> bool:
    """True when `path` only touches documentation, license metadata, or agent instructions."""
    return path.endswith(".md") or path in _DOCS_ONLY_FILES or path.startswith(_DOCS_ONLY_PREFIXES)


def _is_standalone_test_file(path: str) -> bool:
    return (
        path.endswith(".py")
        and os.path.basename(path).startswith("test_")
        and not path.startswith(_NON_UNIT_TEST_PREFIXES)
    )


def _top_level_trigger(top: str, path: str) -> str | None:
    if top in _FULL_SUITE_ROOT_REASONS:
        return _FULL_SUITE_ROOT_REASONS[top]
    if top == "tests":
        is_isolated = path.startswith(_E2E_PREFIX) or _is_standalone_test_file(path)
        return None if is_isolated else "shared test infrastructure"
    return None if top in MODULE_ROOTS else "unclassified root"


def full_suite_trigger(path: str) -> str | None:
    """Return fallback reason when editing `path` requires the full unit suite, else `None`."""
    if is_docs_only(path):
        return None
    if "/" not in path:
        return "top-level file"
    return _top_level_trigger(path.split("/", 1)[0], path)


def unit_test_files(files: Iterable[str]) -> list[str]:
    """Sorted repository-relative unit-test files importable as dotted test modules."""
    return sorted(
        rel
        for rel in files
        if _is_standalone_test_file(rel) and not rel.startswith(_E2E_PREFIX) and module_of(rel) is not None
    )


def _file_for_dotted(dotted: str, existing: set[str]) -> str | None:
    base = dotted.replace(".", "/")
    for rel in (f"{base}.py", f"{base}/__init__.py"):
        if rel in existing:
            return rel
    return None


def _with_parent_inits(rel: str, existing: set[str]) -> set[str]:
    out = {rel}
    parts = rel.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        init = "/".join(parts[:i]) + "/__init__.py"
        if init in existing:
            out.add(init)
    return out


def _imported_files_for(rel: str, repo: str, existing: set[str]) -> set[str]:
    try:
        with open(os.path.join(repo, rel), encoding="utf-8") as fh:
            names = imported_modules(fh.read(), rel)
    except OSError:
        return set()
    out: set[str] = set()
    for name in names:
        target = _file_for_dotted(name, existing)
        if target is None and "." in name:
            target = _file_for_dotted(name.rsplit(".", 1)[0], existing)
        if target is not None:
            out.update(_with_parent_inits(target, existing))
    return out


def _reachable_files(entry: str, repo: str, existing: set[str], memo: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    todo = [entry]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        if rel not in memo:
            memo[rel] = _imported_files_for(rel, repo, existing)
        todo.extend(sorted(memo[rel] - seen))
    return seen


def reverse_import_map(
    test_files: Sequence[str],
    *,
    repo: str = REPO,
    existing: Iterable[str] | None = None,
) -> dict[str, set[str]]:
    """Map repository files to dotted test modules whose AST import closure reaches them."""
    known = set(repo_files(repo)) if existing is None else set(existing)
    memo: dict[str, set[str]] = {}
    rev: dict[str, set[str]] = {}
    for test_rel in test_files:
        mod = module_of(test_rel)
        if mod is None:
            continue
        for dep in _reachable_files(test_rel, repo, known, memo):
            rev.setdefault(dep, set()).add(mod)
    return rev


def _repo_inventory(changed: Sequence[str], all_files: Sequence[str] | None, repo: str) -> list[str]:
    if all_files is not None:
        return sorted(set(all_files))
    tracked = set(repo_files(repo))
    extras = {p for p in changed if os.path.isfile(os.path.join(repo, p))}
    return sorted(tracked | extras)


def _build_repo_index(inventory: Sequence[str], repo: str) -> _RepoIndex:
    existing = set(inventory)
    all_test_files = sorted(rel for rel in inventory if _is_standalone_test_file(rel) and module_of(rel) is not None)
    test_mods_by_file = {rel: mod for rel in all_test_files if (mod := module_of(rel)) is not None}
    valid_mods = set(test_mods_by_file.values())
    rev = reverse_import_map(all_test_files, repo=repo, existing=existing)
    manifest_srcs = frozenset(entry.src for entry in INSTALL_MANIFEST)
    return _RepoIndex(test_mods_by_file=test_mods_by_file, valid_mods=valid_mods, rev=rev, manifest_srcs=manifest_srcs)


def _apply_install_and_script_rules(path: str, index: _RepoIndex, selected: set[str]) -> None:
    top = path.split("/", 1)[0]
    is_extensionless_module_file = top in MODULE_ROOTS and "." not in os.path.basename(path)
    if path in index.manifest_srcs or path.endswith(INSTALL_SENSITIVE_SUFFIXES) or is_extensionless_module_file:
        selected.update(mod for mod in INSTALL_TESTS if mod in index.valid_mods)
    if path.endswith((".bash", ".conf")) and "tests.test_cst_core_shared" in index.valid_mods:
        selected.add("tests.test_cst_core_shared")
    if path.startswith(_E2E_PREFIX):
        selected.update(mod for mod in E2E_UNIT_TESTS if mod in index.valid_mods)


def _apply_path_rules(path: str, index: _RepoIndex, selected: set[str]) -> None:
    if is_docs_only(path):
        selected.update(DOCS_GATES)
        return
    if path in index.test_mods_by_file:
        selected.add(index.test_mods_by_file[path])
    selected.update(index.rev.get(path, ()))
    top = path.split("/", 1)[0]
    if top in MODULE_ROOTS:
        prefix = f"{top}/tests/"
        selected.update(mod for rel, mod in index.test_mods_by_file.items() if rel.startswith(prefix))
        if top != "doctor" and "doctor.tests.test_doctor" in index.valid_mods:
            selected.add("doctor.tests.test_doctor")
    _apply_install_and_script_rules(path, index, selected)


def select_affected(
    changed: Sequence[str],
    *,
    all_files: Sequence[str] | None = None,
    repo: str = REPO,
    base: str = _HEAD_REF,
) -> AffectedPlan:
    """Compute affected unit test modules for `changed` (pure logic when `all_files` is given)."""
    uniq = tuple(dict.fromkeys(p.strip().replace("\\", "/") for p in changed if p.strip()))
    if not uniq:
        return AffectedPlan(
            base=base,
            changed=(),
            full_suite=False,
            modules=GATE_MODULES,
            reason="no changed files; running static gates",
        )
    if all(is_docs_only(p) for p in uniq):
        return AffectedPlan(base=base, changed=uniq, full_suite=False, modules=DOCS_GATES, reason="docs-only change")
    triggers = [(path, trigger) for path in uniq if (trigger := full_suite_trigger(path)) is not None]
    if triggers:
        path, trigger = triggers[0]
        return AffectedPlan(
            base=base,
            changed=uniq,
            full_suite=True,
            modules=(),
            reason=f"fallback to full suite: {path} ({trigger})",
        )

    index = _build_repo_index(_repo_inventory(uniq, all_files, repo), repo)
    selected: set[str] = {m for m in GATE_MODULES if m in index.valid_mods}
    for path in uniq:
        _apply_path_rules(path, index, selected)
    final_mods = tuple(sorted(selected))
    return AffectedPlan(
        base=base,
        changed=uniq,
        full_suite=False,
        modules=final_mods,
        reason=f"{len(final_mods)} module(s) selected for {len(uniq)} changed file(s)",
    )


def format_plan(plan: AffectedPlan) -> str:
    """Format an `AffectedPlan` for `--list-affected`."""
    lines = [
        f"Affected unit-test plan (base={plan.base}):",
        f"  reason: {plan.reason}",
        f"  changed ({len(plan.changed)}):",
        *(f"    {p}" for p in plan.changed),
    ]
    if plan.full_suite:
        lines.append("  selected: FULL SUITE")
    else:
        lines.append(f"  selected ({len(plan.modules)}):")
        lines.extend(f"    {mod}" for mod in plan.modules)
    return "\n".join(lines)
