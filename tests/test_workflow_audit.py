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
- the nightly runs `invowk cmd ci uv-audit`, and that task runs exactly
  UV_AUDIT_COMMAND: `uv audit --locked` on every dependency group;
- `invowk cmd check` runs `invowk cmd test workflows`, and that task runs
  exactly ZIZMOR_COMMAND: zizmor with --offline, the config file and
  --strict-collection (without it, a file zizmor cannot parse is skipped
  with a warning and the audit passes), on all of .github.

The two commands are compared word for word, not searched for options to
refuse: any other word (an option that filters findings or packages out or
keeps the tool from failing, such as --min-severity, --no-exit-codes,
--fix, --ignore-until-fixed or --service-url; a variable; a narrower input;
`|| true`) fails, including options a later release adds.

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
NIGHTLY = ".github/workflows/nightly.yml"
# The exact commands of the two audit tasks, word for word. Any other word
# (an option such as --no-exit-codes, --fix, --min-severity, --ignore or
# --service-url, a variable, a narrower input, `|| true`) could leave
# findings or packages out, or keep the audit from failing. Change the task
# and this test together, with the reason in the pull request.
AUDIT_WHY = 'docs/dev-tools.md, "The workflow audit"'
UV_AUDIT_WHY = 'docs/dev-tools.md, "Known vulnerabilities in the pins"'
ZIZMOR_COMMAND = ("tests/uv-dev", "zizmor", "--offline", "--strict-collection", "--config", CONFIG, ".github")
UV_AUDIT_COMMAND = ("uv", "audit", "--locked", "--preview-features", "audit-command")
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
    return out + command_problems(cue, "test workflows", ZIZMOR_COMMAND, AUDIT_WHY)


def command_problems(cue: str, task: str, want: tuple[str, ...], why: str) -> list[str]:
    """Whether the invowk task `task` runs exactly the command `want`, as text lines (empty: it does)."""
    script = task_script(cue, task)
    if script is None:
        return [f"{INVOWKFILE}: there is no task `{task}`"]
    got = script.split()
    if tuple(got) != want:
        return [
            f"{INVOWKFILE}: the task `{task}` must run exactly `{' '.join(want)}`, not `{' '.join(got)}` (see {why})"
        ]
    return []


def uv_audit_problems(cue: str, nightly: str) -> list[str]:
    """Whether the nightly audits every locked package with uv audit, as text lines (empty: yes)."""
    out: list[str] = []
    if not any(line.strip().endswith("invowk cmd ci uv-audit") for line in nightly.splitlines()):
        out.append(f"{NIGHTLY}: no step runs `invowk cmd ci uv-audit`")
    return out + command_problems(cue, "ci uv-audit", UV_AUDIT_COMMAND, UV_AUDIT_WHY)


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

    def test_check_runs_exactly_the_audit_command(self) -> None:
        self.assertEqual(task_problems(GOOD_CUE), [])
        self.assertEqual(
            task_problems(GOOD_CUE.replace("\t\t\t\tinvowk cmd test workflows\n", "")),
            [f"{INVOWKFILE}: the task check does not run `invowk cmd test workflows`"],
        )
        good = " ".join(ZIZMOR_COMMAND)
        for bad in (
            good.replace("--offline ", ""),
            good.replace("--config .github/zizmor.yml ", ""),
            good.replace("--strict-collection ", ""),
            good.replace("tests/uv-dev zizmor", "true"),
            # Options that leave findings out, or keep zizmor from failing on them.
            good + " --min-severity=high",
            good.removesuffix(" .github") + " --min-confidence high .github",
            good.removesuffix(" .github") + " --persona regular .github",
            good.removesuffix(" .github") + " --collect=workflows .github",
            good.removesuffix(" .github") + " --no-exit-codes .github",
            good.removesuffix(" .github") + " --no-ignores .github",
            good.removesuffix(" .github") + " --fix .github",
            good.removesuffix(" .github") + " --no-config .github",
            good + " || true",
            good.removesuffix(" .github") + " .github/workflows",
            "ZIZMOR_OFFLINE=1 " + good,
        ):
            with self.subTest(command=bad):
                self.assertEqual(
                    task_problems(GOOD_CUE.replace(good, bad)),
                    [
                        f"{INVOWKFILE}: the task `test workflows` must run exactly `{good}`, "
                        f"not `{bad}` (see {AUDIT_WHY})"
                    ],
                )
        self.assertEqual(
            task_problems(GOOD_CUE.replace('name:        "test workflows"', 'name:        "test flows"')),
            [f"{INVOWKFILE}: there is no task `test workflows`"],
        )

    def test_the_nightly_audits_every_locked_package(self) -> None:
        self.assertEqual(uv_audit_problems(GOOD_CUE, GOOD_NIGHTLY), [])
        self.assertEqual(
            uv_audit_problems(GOOD_CUE, "      - run: invowk cmd check\n"),
            [f"{NIGHTLY}: no step runs `invowk cmd ci uv-audit`"],
        )
        good = " ".join(UV_AUDIT_COMMAND)
        for bad in (
            good.replace("--locked ", ""),
            good + " --no-group mutation",
            good + " --only-dev",
            good + " --ignore=GHSA-1",
            good + " --ignore-until-fixed GHSA-1",
            good + " --no-extra docs",
            good + " --frozen",
            good + " --service-url http://localhost:1",
            "UV_NO_DEV=1 " + good,
            good + " || true",
        ):
            with self.subTest(command=bad):
                self.assertEqual(
                    uv_audit_problems(GOOD_CUE.replace(good, bad), GOOD_NIGHTLY),
                    [
                        f"{INVOWKFILE}: the task `ci uv-audit` must run exactly `{good}`, "
                        f"not `{bad}` (see {UV_AUDIT_WHY})"
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
