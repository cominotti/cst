# SPDX-License-Identifier: MPL-2.0
"""Static gate: nothing switches the workflow audit or the nightly's uv audit off.

`invowk cmd test workflows` (part of `invowk cmd check`) runs zizmor offline
on .github: the workflows, the composite actions and dependabot.yml. Its
exceptions live in one reviewed file, .github/zizmor.yml. This test checks
what zizmor itself cannot:

- every ignore list of .github/zizmor.yml starts with a comment that says
  why, and no audit is disabled (`disable: true` takes an audit out for
  every file, and zizmor then counts none of its findings as ignored);
- no file of .github carries an inline `zizmor: ignore` comment: an
  exception goes into .github/zizmor.yml, with its reason;
- the nightly runs `invowk cmd ci uv-audit`, and that task runs
  `uv audit --locked` on every dependency group (no --no-group,
  --only-group, --no-dev, --only-dev, --ignore, --frozen);
- `invowk cmd check` runs `invowk cmd test workflows`, and that task gives
  zizmor --offline, the config file and --strict-collection (without it, a
  file zizmor cannot parse is skipped with a warning and the audit passes),
  and no option that filters findings out (--min-severity,
  --min-confidence, --persona, --collect).

Standard library only (no YAML parser: the config is read line by line).
See docs/dev-tools.md, "The workflow audit".
"""

from __future__ import annotations

from collections.abc import Iterable
import os
import re
import tempfile
import unittest

from tests.repo_files import REPO, repo_files

CONFIG = ".github/zizmor.yml"
INVOWKFILE = "cst.invowkmod/invowkfile.cue"
AUDIT_COMMAND = "tests/uv-dev zizmor"
FILTER_OPTIONS = ("--min-severity", "--min-confidence", "--persona", "--collect")
NIGHTLY = ".github/workflows/nightly.yml"
UV_AUDIT_FILTERS = (
    "--no-group",
    "--only-group",
    "--no-dev",
    "--only-dev",
    "--no-default-groups",
    "--ignore",
    "--frozen",
)
INLINE_IGNORE = re.compile(r"zizmor\s*:\s*ignore\b")
_KEY = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$")


def config_problems(text: str) -> list[str]:
    """What is wrong with the text of .github/zizmor.yml, as text lines (empty: all fine)."""
    out: list[str] = []
    lines = text.splitlines()
    rule = "?"
    for number, line in enumerate(lines, 1):
        key = _KEY.match(line)
        if key is None or line.lstrip().startswith("#"):
            continue
        indent, name, value = len(key.group(1)), key.group(2), key.group(3)
        if indent == 2:
            rule = name
        if name == "disable":
            out.append(
                f"{CONFIG}:{number}: {rule}: `disable` switches the audit off for every file; "
                "ignore the findings one by one instead, each with its reason"
            )
        elif name == "ignore" and not value and not _starts_with_comment(lines[number:]):
            out.append(f"{CONFIG}:{number}: {rule}: an ignore list must start with a comment that says why")
    return out


def _starts_with_comment(rest: list[str]) -> bool:
    first = next((line.strip() for line in rest if line.strip()), "")
    return first.startswith("#")


def inline_ignores(repo: str, files: Iterable[str]) -> list[str]:
    """The inline zizmor ignore comments in the given files, as text lines (empty: none)."""
    out: list[str] = []
    for rel in sorted(files):
        with open(os.path.join(repo, rel), encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                if "#" in line and INLINE_IGNORE.search(line.split("#", 1)[1]):
                    out.append(
                        f"{rel}:{number}: an inline zizmor ignore; put the exception in {CONFIG}, with its reason"
                    )
    return out


def task_script(cue: str, name: str) -> str | None:
    """The script of the invowk task `name` in the text of invowkfile.cue (None: no such task).

    A task starts at a `name:` indented by two tabs, as invowkfile.cue writes them.
    """
    found = re.search(
        r'\n\t\tname:\s*"' + re.escape(name) + r'"(?:(?!\n\t\tname:).)*?script:\s*\{content:\s*'
        r'(?:"""(?P<long>.*?)"""|"(?P<short>(?:[^"\\]|\\.)*)")',
        cue,
        re.DOTALL,
    )
    if found is None:
        return None
    return found.group("long") if found.group("long") is not None else found.group("short")


def task_problems(cue: str) -> list[str]:
    """Whether `check` runs the workflow audit, and with nothing filtered out, as text lines (empty: yes)."""
    out: list[str] = []
    check = task_script(cue, "check") or ""
    if not any(line.strip() == "invowk cmd test workflows" for line in check.splitlines()):
        out.append(f"{INVOWKFILE}: the task check does not run `invowk cmd test workflows`")
    audit = task_script(cue, "test workflows")
    if audit is None or AUDIT_COMMAND not in audit:
        out.append(f"{INVOWKFILE}: the task `test workflows` does not run {AUDIT_COMMAND}")
        return out
    words = audit.split()
    if "--offline" not in words:
        out.append(f"{INVOWKFILE}: `test workflows` must run zizmor --offline (no token, no network)")
    if f"--config {CONFIG}" not in " ".join(words):
        out.append(f"{INVOWKFILE}: `test workflows` must give zizmor --config {CONFIG}")
    if "--strict-collection" not in words:
        out.append(
            f"{INVOWKFILE}: `test workflows` must run zizmor --strict-collection "
            "(without it a file zizmor cannot parse is skipped and the audit passes)"
        )
    for option in FILTER_OPTIONS:
        if any(w == option or w.startswith(option + "=") for w in words):
            out.append(f"{INVOWKFILE}: `test workflows` passes {option}, which leaves findings out of the audit")
    return out


def uv_audit_problems(cue: str, nightly: str) -> list[str]:
    """Whether the nightly audits every locked package with uv audit, as text lines (empty: yes)."""
    out: list[str] = []
    if not any(line.strip().endswith("invowk cmd ci uv-audit") for line in nightly.splitlines()):
        out.append(f"{NIGHTLY}: no step runs `invowk cmd ci uv-audit`")
    words = (task_script(cue, "ci uv-audit") or "").split()
    if words[:2] != ["uv", "audit"] or "--locked" not in words:
        out.append(f"{INVOWKFILE}: the task `ci uv-audit` must run uv audit --locked")
    for option in UV_AUDIT_FILTERS:
        if any(w == option or w.startswith(option + "=") for w in words):
            out.append(f"{INVOWKFILE}: `ci uv-audit` passes {option}, which leaves packages out of the audit")
    return out


GOOD_CONFIG = """# header
rules:
  github-env:
    ignore:
      # why
      - action.yml:201
  dependabot-cooldown:
    config:
      days: 7
"""

GOOD_CUE = '''cmds: [
	{
		name:        "check"
		script: {content: """
				invowk cmd test types
				invowk cmd test workflows
				"""}
	},
	{
		name:        "test workflows"
		script: {content: "tests/uv-dev zizmor --offline --strict-collection --config .github/zizmor.yml .github"}
	},
	{
		name:        "ci uv-audit"
		script: {content: "uv audit --locked --preview-features audit-command"}
	},
]
'''
GOOD_NIGHTLY = "      - name: x\n        run: runuser -u ci -- invowk cmd ci uv-audit\n"


class WorkflowAuditTest(unittest.TestCase):
    def test_a_reasoned_config_passes(self) -> None:
        self.assertEqual(config_problems(GOOD_CONFIG), [])

    def test_an_ignore_list_without_a_reason_fails(self) -> None:
        bad = GOOD_CONFIG.replace("      # why\n", "")
        self.assertEqual(
            config_problems(bad),
            [f"{CONFIG}:4: github-env: an ignore list must start with a comment that says why"],
        )

    def test_a_disabled_audit_fails(self) -> None:
        bad = GOOD_CONFIG + "  artipacked:\n    disable: true\n"
        self.assertEqual(
            config_problems(bad),
            [
                f"{CONFIG}:11: artipacked: `disable` switches the audit off for every file; "
                "ignore the findings one by one instead, each with its reason"
            ],
        )

    def test_inline_ignores_fail(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-zizmor-") as tmp:
            # Built from pieces so that this file holds no ignore comment of its own.
            comment = "# zizmor" + ": ignore[artipacked] fine"
            files = {
                "ok.yml": 'run: echo "zizmor is mentioned here"\n',
                "bad.yml": f"steps:\n  - uses: actions/checkout@abc  {comment}\n",
            }
            for name, text in files.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            self.assertEqual(
                inline_ignores(tmp, files),
                [f"bad.yml:2: an inline zizmor ignore; put the exception in {CONFIG}, with its reason"],
            )

    def test_check_runs_the_audit_with_nothing_filtered(self) -> None:
        self.assertEqual(task_problems(GOOD_CUE), [])
        self.assertEqual(
            task_problems(GOOD_CUE.replace("\t\t\t\tinvowk cmd test workflows\n", "")),
            [f"{INVOWKFILE}: the task check does not run `invowk cmd test workflows`"],
        )
        self.assertEqual(
            task_problems(GOOD_CUE.replace("--offline ", "")),
            [f"{INVOWKFILE}: `test workflows` must run zizmor --offline (no token, no network)"],
        )
        self.assertEqual(
            task_problems(GOOD_CUE.replace("--config .github/zizmor.yml ", "")),
            [f"{INVOWKFILE}: `test workflows` must give zizmor --config {CONFIG}"],
        )
        self.assertEqual(
            task_problems(GOOD_CUE.replace("--strict-collection ", "")),
            [
                f"{INVOWKFILE}: `test workflows` must run zizmor --strict-collection "
                "(without it a file zizmor cannot parse is skipped and the audit passes)"
            ],
        )
        for option in ("--min-severity=high", "--min-confidence high", "--persona regular", "--collect=workflows"):
            with self.subTest(option=option):
                self.assertEqual(
                    task_problems(GOOD_CUE.replace(' .github"', f' {option} .github"')),
                    [
                        f"{INVOWKFILE}: `test workflows` passes {option.split('=')[0].split()[0]}, "
                        "which leaves findings out of the audit"
                    ],
                )
        self.assertEqual(
            task_problems(GOOD_CUE.replace("tests/uv-dev zizmor", "true")),
            [f"{INVOWKFILE}: the task `test workflows` does not run {AUDIT_COMMAND}"],
        )

    def test_the_nightly_audits_every_locked_package(self) -> None:
        self.assertEqual(uv_audit_problems(GOOD_CUE, GOOD_NIGHTLY), [])
        self.assertEqual(
            uv_audit_problems(GOOD_CUE, "      - run: invowk cmd check\n"),
            [f"{NIGHTLY}: no step runs `invowk cmd ci uv-audit`"],
        )
        self.assertEqual(
            uv_audit_problems(GOOD_CUE.replace("uv audit --locked", "uv audit"), GOOD_NIGHTLY),
            [f"{INVOWKFILE}: the task `ci uv-audit` must run uv audit --locked"],
        )
        for option in ("--no-group mutation", "--only-dev", "--ignore=GHSA-1", "--frozen"):
            with self.subTest(option=option):
                self.assertEqual(
                    uv_audit_problems(GOOD_CUE.replace("--locked", f"--locked {option}"), GOOD_NIGHTLY),
                    [
                        f"{INVOWKFILE}: `ci uv-audit` passes {option.split('=')[0].split()[0]}, "
                        "which leaves packages out of the audit"
                    ],
                )

    def test_the_repository_keeps_the_audit_on(self) -> None:
        files = repo_files()
        with open(os.path.join(REPO, CONFIG), encoding="utf-8") as fh:
            config = fh.read()
        with open(os.path.join(REPO, INVOWKFILE), encoding="utf-8") as fh:
            cue = fh.read()
        with open(os.path.join(REPO, NIGHTLY), encoding="utf-8") as fh:
            nightly = fh.read()
        found = (
            config_problems(config)
            + inline_ignores(REPO, [f for f in files if f.startswith(".github/")])
            + task_problems(cue)
            + uv_audit_problems(cue, nightly)
        )
        self.assertEqual(found, [], "the workflow audit misses something (see docs/dev-tools.md):\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
