# SPDX-License-Identifier: MPL-2.0
"""Static gate: nothing switches the workflow audit or the nightly's uv audit off.

`invowk cmd test workflows` (part of `invowk cmd check`) runs zizmor offline
on .github: the workflows, the composite actions and dependabot.yml. Its
exceptions live in one reviewed file, .github/zizmor.yml. This test checks
what zizmor itself cannot:

- every ignore list and every per-audit `config` block of
  .github/zizmor.yml starts with a comment that says why (a config can
  weaken an audit as much as an ignore list: a cooldown of 0 days, actions
  pinned by tag), and no audit is disabled (`disable: true` takes an audit
  out for every file, and zizmor then counts none of its findings as
  ignored). The file must be plain block YAML, which this reader follows:
  no flow style (`ignore: [a, b]`), anchor, alias, tag or quoted key,
  except below a `config` block, where zizmor's settings take quoted keys
  and keys with / * . @ (unpinned-uses' `"actions/*": hash-pin`); a
  quoted key is compared without its quotes (`"disable"` is `disable`);
- every ignore entry names exactly one file of .github: zizmor matches an
  entry by the file's base name only, so `action.yml:201` would also hide
  a finding at line 201 of a second composite action;
- no file of .github carries an inline `zizmor: ignore` comment: an
  exception goes into .github/zizmor.yml, with its reason;
- the nightly runs `invowk cmd ci uv-audit` as the last step of a job with
  no `if:`, `continue-on-error:` or `needs:` (a failed audit then skips no
  other gate and cannot be let through or skipped), with no uv variable
  (`UV_...`) in the workflow, and that task runs exactly UV_AUDIT_COMMAND:
  `uv audit --locked` on every dependency group;
- `invowk cmd check` runs `invowk cmd test workflows`, and that task runs
  exactly ZIZMOR_COMMAND: zizmor with --offline, the config file and
  --strict-collection (without it, a file zizmor cannot parse is skipped
  with a warning and the audit passes), on all of .github;
- neither task sets a variable: no `env` block (vars or files) and no uv or
  zizmor variable (`UV_...`, `ZIZMOR_...`) in its text.

The two commands are compared word for word, not searched for options to
refuse: any other word (an option that filters findings or packages out or
keeps the tool from failing, such as --min-severity, --no-exit-codes,
--fix, --ignore-until-fixed or --service-url; a variable; a narrower input;
`|| true`) fails, including options a later release adds.

Standard library only (no YAML parser: the config is read line by line).
See docs/dev-tools.md, "The workflow audit".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import os
import re
import tempfile
import unittest

from cst_core.domain.text import group_or_none
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
# Below a per-audit config block, zizmor's settings take other keys too, quoted or with / * . @ in
# them (unpinned-uses' policies: "actions/*": hash-pin). Never an unquoted key that starts with *, &,
# ! or @ (an alias, an anchor, a tag, a reserved character).
_CONFIG_KEY = re.compile(r"""^(\s*)("[^"\\]*"|'[^']*'|[A-Za-z0-9_./-][A-Za-z0-9_./*@-]*)\s*:\s*(.*?)\s*$""")


# What the line reader cannot follow: a flow collection, an anchor, an alias or a tag.
_NOT_BLOCK = ("{", "[", "&", "*", "!")
# The keys whose contents are an exception, each with its reason first.
_REASONED = {"ignore": "an ignore list", "config": "a config block"}


def config_problems(text: str) -> list[str]:
    """What is wrong with the text of .github/zizmor.yml, as text lines (empty: all fine).

    The file is read line by line, so it must be plain block YAML: every
    line a comment, an unquoted `key:` or a `- item`, and no flow style,
    anchor, alias or tag, which could hide a `disable` or an ignore list
    from this reader.
    """
    out: list[str] = []
    lines = text.splitlines()
    rule = "?"
    config_indent: int | None = None  # the indent of the config: key whose block this line is in
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        where = f"{CONFIG}:{number}:"
        if config_indent is not None and len(line) - len(line.lstrip()) <= config_indent:
            config_indent = None
        if stripped.startswith("- ") or stripped == "-":
            if stripped[1:].strip().startswith(_NOT_BLOCK):
                out.append(f"{where} {rule}: {_FLOW}")
            continue
        key = (_KEY if config_indent is None else _CONFIG_KEY).match(line)
        if key is None:
            out.append(
                f"{where} write plain block YAML here (an unquoted `key:`, a `- item` or a comment): "
                "the gate cannot read this line"
            )
            continue
        indent, raw, value = len(key.group(1)), key.group(2), key.group(3)
        name = raw[1:-1] if raw[0] in "\"'" else raw  # a quoted key compared without its quotes
        if indent == 2:
            rule = name
        if name == "config" and not value and config_indent is None:
            config_indent = indent
        if name in _REASONED and value:
            out.append(
                f"{where} {rule}: nothing after `{name}:` on its line; the reason comment and the "
                "entries go on the lines below"
            )
        elif value.startswith(_NOT_BLOCK):
            out.append(f"{where} {rule}: {_FLOW}")
        if name == "disable":
            out.append(
                f"{where} {rule}: `disable` switches the audit off for every file; "
                "ignore the findings one by one instead, each with its reason"
            )
        elif name in _REASONED and not value and not _starts_with_comment(lines[number:]):
            out.append(f"{where} {rule}: {_REASONED[name]} must start with a comment that says why")
    return out


_FLOW = "no flow style, anchor, alias or tag here (`{`, `[`, `&`, `*`, `!`): the gate reads block YAML only"


_ENTRY = re.compile(r"^(\s*)-\s+([^\s:#\"']+)(?::(\d+))?(?::\d+)?\s*$")


def entry_problems(text: str, line_counts: Mapping[str, int]) -> list[str]:
    """Every ignore entry of .github/zizmor.yml that does not name exactly one file, as text lines.

    zizmor matches an entry by the base name of the file only (a folder in
    the entry matches nothing), so `action.yml:201` also hides that audit's
    finding at line 201 of every other `action.yml`. line_counts holds the
    files of .github, each with its number of lines: an entry fails when
    no file has its base name, or when several do (for `FILE:LINE`: several
    long enough to have that line).
    """
    out: list[str] = []
    rule = "?"
    ignore_indent: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key = _KEY.match(line)
        if key is not None:
            if indent == 2:
                rule = key.group(2)
            ignore_indent = indent if key.group(2) == "ignore" else None
            continue
        if ignore_indent is None or indent <= ignore_indent:
            continue
        entry = _ENTRY.match(line)
        if entry is None:
            out.append(
                f"{CONFIG}:{number}: {rule}: write the entry as FILE or FILE:LINE, unquoted and with no "
                "comment after it, so that this test can check it"
            )
            continue
        name, at = entry.group(2), entry.group(3)
        files = sorted(p for p in line_counts if os.path.basename(p) == name)
        hit = [p for p in files if at is None or line_counts[p] >= int(at)]
        if not files:
            out.append(f"{CONFIG}:{number}: {rule}: `{name}` is no file of .github; delete the entry")
        elif len(hit) > 1:
            out.append(
                f"{CONFIG}:{number}: {rule}: `{stripped[1:].strip()}` matches {', '.join(hit)}: zizmor "
                "matches an entry by the file's base name, so it hides the finding in each; rename one "
                "of the files (an action may be action.yaml) or fix the finding"
            )
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
    long = group_or_none(found, "long")
    return long if long is not None else group_or_none(found, "short")


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
    return [
        f"{INVOWKFILE}: the task `{task}` sets variables (`{word}`): they can change what the command checks "
        f"(see {why})"
        for word in _task_variables(task_block(cue, task) or "")
    ]


# What sets a variable for a task: an env block (vars or files) or a uv or zizmor variable named.
# The env key may be quoted ("env": {...}) or inside a struct on one line ({name: "native", env:
# {...}}); a longer word that holds it (environment:, my_env:) is not one, nor an env: in a string
# or a comment (_code_only).
_TASK_ENV = re.compile(r'(?<![\w"-])"?env"?\s*:')
_TOOL_VARIABLE = re.compile(r"\b(?:UV|ZIZMOR)_[A-Z0-9_]*")
# What is not CUE code in a task's text, left to right: a raw multi-line string (three double
# quotes between # signs, as many on both sides), a multi-line string or bytes (three double or
# three single quotes), a raw string (a string between # signs, which a lone quote or backslash
# does not end), any other string, bytes (in single quotes), a // comment.
_NOT_CODE = re.compile(
    r'(?P<rawmulti>#+)"""[\s\S]*?"""(?P=rawmulti)'
    r'|"""[\s\S]*?"""'
    r"|'''[\s\S]*?'''"
    r'|(?P<raw>#+)"(?:(?!"(?P=raw))[^\n])*"(?P=raw)'
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r"|//[^\n]*"
)
# The env key in any quoting ("env", #"env"#), and the ':' after a key.
_ENV_KEY = re.compile(r'(?P<hashes>#*)"env"(?P=hashes)')
_KEY_COLON = re.compile(r"[ \t]*:")


# Every character but those str.splitlines() breaks a line at.
_NOT_A_LINE_BREAK = re.compile(r"[^\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


def _code_only(block: str) -> str:
    """block with its strings and comments blanked, newlines kept (its lines stay in step), so an
    env: in a string value ("echo $env: x") or after a // is not an env key. A quoted env key
    ("env": or #"env"#:) becomes "env" padded with spaces to its length."""

    def blank(found: re.Match[str]) -> str:
        text = found.group(0)
        if _ENV_KEY.fullmatch(text) and _KEY_COLON.match(block, found.end()):
            return '"env"'.ljust(len(text))
        return _NOT_A_LINE_BREAK.sub(" ", text)

    return _NOT_CODE.sub(blank, block)


def task_block(cue: str, name: str) -> str | None:
    """The text of the invowk task `name`, from its `name:` to the next task's (None: no such task)."""
    found = re.search(r'\n\t\tname:\s*"' + re.escape(name) + r'"(?:(?!\n\t\tname:).)*', cue, re.DOTALL)
    return None if found is None else found.group(0)


def _task_variables(block: str) -> list[str]:
    """The env keys and uv or zizmor variables of a task's text, comment lines left out."""
    out: list[str] = []
    for line, code in zip(block.splitlines(), _code_only(block).splitlines(), strict=True):
        if line.lstrip().startswith("//"):
            continue
        if _TASK_ENV.search(code):
            out.append("env")
        out += _TOOL_VARIABLE.findall(line)
    return out


UV_AUDIT_STEP = re.compile(r"^\s*(?:-\s+)?run:\s*invowk cmd ci uv-audit\s*$")
_JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_LENIENT = re.compile(r"^\s*(?:-\s+)?(if|continue-on-error|needs)\s*:")
_STEP = re.compile(r"^(\s*)-\s")


def uv_audit_problems(cue: str, nightly: str) -> list[str]:
    """Whether the nightly audits every locked package with uv audit, as text lines (empty: yes).

    The nightly must run the task in a step `run: invowk cmd ci uv-audit`
    (on one line, nothing added), as the last step of its job (a failed
    audit then skips no other gate), in a job with no `if:`,
    `continue-on-error:` or `needs:` at any depth (a job that needs another
    is skipped when that one is skipped, and the run still passes), and no uv variable (`UV_...`) may
    appear anywhere in the workflow. Comment lines are left out.
    """
    out: list[str] = []
    lines = [(n, line) for n, line in enumerate(nightly.splitlines(), 1) if not line.lstrip().startswith("#")]
    out += [
        f"{NIGHTLY}:{n}: a uv variable (`UV_...`) in the nightly; it can leave packages or vulnerabilities out "
        "of the audit"
        for n, line in lines
        if "UV_" in line
    ]
    jobs: dict[str, list[tuple[int, str]]] = {}
    job: list[tuple[int, str]] | None = None
    for n, line in lines:
        found = _JOB.match(line)
        if found is not None:
            job = jobs.setdefault(found.group(1), [])
        elif line and not line.startswith(" "):
            job = None
        elif job is not None:
            job.append((n, line))
    runs = [(name, n) for name, body in jobs.items() for n, line in body if UV_AUDIT_STEP.match(line)]
    if not runs:
        out.append(f"{NIGHTLY}: no step runs `run: invowk cmd ci uv-audit` (on one line, nothing added)")
    for name, at in runs:
        body = jobs[name]
        for n, line in body:
            lenient = _LENIENT.match(line)
            if lenient is not None:
                out.append(
                    f"{NIGHTLY}:{n}: the job {name} has `{lenient.group(1)}:`; the job that runs uv audit may "
                    "not, since it lets the nightly pass without the audit"
                )
        steps = [(n, len(m.group(1))) for n, line in body if (m := _STEP.match(line)) is not None]
        depth = min((d for _, d in steps), default=0)
        if any(n > at and d == depth for n, d in steps):
            out.append(
                f"{NIGHTLY}:{at}: the uv audit step must be the last step of the job {name}: when it fails, "
                "the steps after it do not run (give the audit a job of its own)"
            )
    return out + command_problems(cue, "ci uv-audit", UV_AUDIT_COMMAND, UV_AUDIT_WHY)


_STEPS_HDR = re.compile(r"^(\s*)steps\s*:\s*(?:#.*)?$")
_DASH_ITEM = re.compile(r"^(\s*-\s+)(.*)$")
_UPLOAD_USES = re.compile(r"^uses\s*:\s*actions/upload-artifact(?:[@\s]|$)")
_WITH_HDR = re.compile(r"^with\s*:\s*(?:#.*)?$")
_WITH_KV = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$")


def _strip_yaml_comment(value: str) -> str:
    """Trailing `# comment` outside quotes removed from a YAML scalar."""
    quote = ""
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
    return value.rstrip()


def _has_hidden_segment(entry: str) -> bool:
    """True when entry names a file or folder whose name starts with `.` (negation lines left out)."""
    raw = _strip_yaml_comment(entry).strip().strip("\"'")
    if not raw or raw.startswith("!"):
        return False
    return any(part.startswith(".") and part not in (".", "..") for part in raw.replace("\\", "/").split("/"))


def _step_blocks(text: str) -> list[list[tuple[int, str]]]:
    """The step line blocks under every `steps:` section in a workflow file."""
    blocks: list[list[tuple[int, str]]] = []
    steps_indent: int | None = None
    step_indent: int | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if _STEPS_HDR.match(line):
            steps_indent, step_indent = indent, None
            continue
        if steps_indent is None:
            continue
        item = _DASH_ITEM.match(line)
        if indent < steps_indent or (indent == steps_indent and item is None):
            steps_indent, step_indent = None, None
        elif item is not None and (step_indent is None or indent == step_indent):
            step_indent = indent
            blocks.append([(lineno, line)])
        elif blocks and step_indent is not None:
            blocks[-1].append((lineno, line))
    return blocks


def _upload_step_with(step: Sequence[tuple[int, str]]) -> tuple[int | None, list[str], str]:
    """(uses line number or None, path entries, include-hidden-files value) of one step."""
    first = _DASH_ITEM.match(step[0][1])
    if first is None:
        return None, [], ""
    body_indent = len(first.group(1))
    use_line: int | None = None
    with_lines: list[str] = []
    in_with = False
    for idx, (lineno, line) in enumerate(step):
        content = first.group(2) if idx == 0 else line.lstrip()
        indent = body_indent if idx == 0 else len(line) - len(content)
        if indent <= body_indent:
            in_with = indent == body_indent and _WITH_HDR.match(content) is not None
            if indent == body_indent and _UPLOAD_USES.match(content):
                use_line = lineno
        elif in_with:
            with_lines.append(line)
    paths, include_hidden = _parse_with_paths(with_lines)
    return use_line, paths, include_hidden


def _parse_with_paths(lines: Sequence[str]) -> tuple[list[str], str]:
    """(`path` entries, lower-cased `include-hidden-files` value) inside a step's `with:` lines."""
    if not lines:
        return [], ""
    key_indent = len(lines[0]) - len(lines[0].lstrip())
    paths: list[str] = []
    include_hidden = ""
    in_path = False
    for line in lines:
        content = line.lstrip()
        indent = len(line) - len(content)
        if indent == key_indent and (kv := _WITH_KV.match(content)) is not None:
            key, val = kv.group(1), _strip_yaml_comment(kv.group(2))
            in_path = key == "path" and val in ("|", "|-", ">", ">-")
            if key == "include-hidden-files":
                include_hidden = val.strip("\"'").lower()
            elif key == "path" and not in_path and val:
                paths.append(val)
        elif in_path and indent > key_indent:
            paths.append(content)
    return paths, include_hidden


def artifact_upload_problems(rel: str, text: str) -> list[str]:
    """Every actions/upload-artifact step in `rel` that names a hidden path without `include-hidden-files: true`."""
    out: list[str] = []
    for step in _step_blocks(text):
        use_line, paths, include_hidden = _upload_step_with(step)
        if use_line is None or include_hidden == "true":
            continue
        hidden = [p for p in paths if _has_hidden_segment(p)]
        if hidden:
            out.append(
                f"{rel}:{use_line}: actions/upload-artifact uploads a hidden path ({hidden[0]}) "
                "without `include-hidden-files: true` (v4+ skips hidden files by default)"
            )
    return out


GOOD_CONFIG = """# header
rules:
  github-env:
    ignore:
      # why
      - action.yml:201
  dependabot-cooldown:
    config:
      # why
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
GOOD_NIGHTLY = """name: Nightly
permissions:
  contents: read
jobs:
  static-gates:
    runs-on: ubuntu-24.04
    steps:
      - name: Fast gates
        if: always()
        run: invowk cmd check
  # The audit, in a job of its own.
  uv-audit:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@abc # v7
        with:
          persist-credentials: false
      - name: Audit
        run: invowk cmd ci uv-audit
"""


class WorkflowAuditTest(unittest.TestCase):
    def test_a_reasoned_config_passes(self) -> None:
        self.assertEqual(config_problems(GOOD_CONFIG), [])

    def test_a_config_block_may_hold_zizmor_s_quoted_policy_keys(self) -> None:
        policies = (
            "  unpinned-uses:\n    config:\n      # why\n      policies:\n"
            '        "actions/*": hash-pin\n        "*": ref-pin\n        github/codeql-action/*: ref-pin\n'
            "  artipacked:\n    ignore:\n      # why\n      - ci.yml\n"
        )
        self.assertEqual(config_problems(GOOD_CONFIG + policies), [])

    def test_an_ignore_list_without_a_reason_fails(self) -> None:
        bad = GOOD_CONFIG.replace("      # why\n", "", 1)
        self.assertEqual(
            config_problems(bad),
            [f"{CONFIG}:4: github-env: an ignore list must start with a comment that says why"],
        )

    def test_a_disabled_audit_fails(self) -> None:
        bad = GOOD_CONFIG + "  artipacked:\n    disable: true\n"
        self.assertEqual(
            config_problems(bad),
            [
                (
                    f"{CONFIG}:12: artipacked: `disable` switches the audit off for every file; "
                    "ignore the findings one by one instead, each with its reason"
                )
            ],
        )

    def test_a_config_block_without_a_reason_fails(self) -> None:
        bad = GOOD_CONFIG.replace("      # why\n      days", "      days")
        self.assertEqual(
            config_problems(bad),
            [f"{CONFIG}:8: dependabot-cooldown: a config block must start with a comment that says why"],
        )

    def test_only_plain_block_yaml_is_read(self) -> None:
        after = "{}: nothing after `{}:` on its line; the reason comment and the entries go on the lines below"
        flow = "{}: no flow style, anchor, alias or tag here (`{{`, `[`, `&`, `*`, `!`): the gate reads block YAML only"
        plain = (
            "write plain block YAML here (an unquoted `key:`, a `- item` or a comment): the gate cannot read this line"
        )
        for extra, want in (
            (
                "  artipacked:\n    ignore: [ci.yml, nightly.yml]\n",
                [f"{CONFIG}:12: " + after.format("artipacked", "ignore")],
            ),
            (
                "  artipacked:\n    ignore: # why\n      - ci.yml\n",
                [f"{CONFIG}:12: " + after.format("artipacked", "ignore")],
            ),
            ("  artipacked:\n    config: {days: 0}\n", [f"{CONFIG}:12: " + after.format("artipacked", "config")]),
            ("  artipacked: {disable: true}\n", [f"{CONFIG}:11: " + flow.format("artipacked")]),
            (
                "  artipacked: &off\n    disable: true\n",
                [
                    f"{CONFIG}:11: " + flow.format("artipacked"),
                    (
                        f"{CONFIG}:12: artipacked: `disable` switches the audit off for every file; "
                        "ignore the findings one by one instead, each with its reason"
                    ),
                ],
            ),
            ("  artipacked: *off\n", [f"{CONFIG}:11: " + flow.format("artipacked")]),
            (
                "  artipacked:\n    ignore:\n      # why\n      - [ci.yml]\n",
                [f"{CONFIG}:14: " + flow.format("artipacked")],
            ),
            ('  artipacked:\n    "disable": true\n', [f"{CONFIG}:12: {plain}"]),
            # Below a config block a quoted key is read, and compared without its quotes.
            (
                '  artipacked:\n    config:\n      # why\n      "disable": true\n',
                [
                    (
                        f"{CONFIG}:14: artipacked: `disable` switches the audit off for every file; "
                        "ignore the findings one by one instead, each with its reason"
                    )
                ],
            ),
            ("  artipacked:\n    config:\n      # why\n      *off: x\n", [f"{CONFIG}:14: {plain}"]),
            ('  artipacked:\n    config:\n      # why\n    "x": y\n', [f"{CONFIG}:14: {plain}"]),
            ("  artipacked:\n    <<: *off\n", [f"{CONFIG}:12: {plain}"]),
            ("---\n", [f"{CONFIG}:11: {plain}"]),
        ):
            with self.subTest(extra=extra):
                self.assertEqual(config_problems(GOOD_CONFIG + extra), want)

    def test_an_entry_names_exactly_one_file(self) -> None:
        files = {
            ".github/actions/one/action.yml": 231,
            ".github/actions/two/action.yml": 118,
            ".github/workflows/ci.yml": 40,
        }
        self.assertEqual(entry_problems(GOOD_CONFIG, files), [])
        # The second action.yml grows to line 201: the entry would hide its finding there too.
        self.assertEqual(
            entry_problems(GOOD_CONFIG, {**files, ".github/actions/two/action.yml": 201}),
            [
                (
                    f"{CONFIG}:6: github-env: `action.yml:201` matches .github/actions/one/action.yml, "
                    ".github/actions/two/action.yml: zizmor matches an entry by the file's base name, so it "
                    "hides the finding in each; rename one of the files (an action may be action.yaml) or "
                    "fix the finding"
                )
            ],
        )
        whole = GOOD_CONFIG + "  artipacked:\n    ignore:\n      # why\n      - action.yml\n      - ci.yml\n"
        self.assertEqual(
            entry_problems(whole, files),
            [
                (
                    f"{CONFIG}:14: artipacked: `action.yml` matches .github/actions/one/action.yml, "
                    ".github/actions/two/action.yml: zizmor matches an entry by the file's base name, so it "
                    "hides the finding in each; rename one of the files (an action may be action.yaml) or "
                    "fix the finding"
                )
            ],
        )
        for odd in ('"action.yml:201"', "action.yml:201  # why", "'ci.yml'"):
            with self.subTest(entry=odd):
                self.assertEqual(
                    entry_problems(GOOD_CONFIG.replace("action.yml:201", odd), files),
                    [
                        (
                            f"{CONFIG}:6: github-env: write the entry as FILE or FILE:LINE, unquoted and with no "
                            "comment after it, so that this test can check it"
                        )
                    ],
                )
        self.assertEqual(
            entry_problems(GOOD_CONFIG.replace("action.yml:201", "gone.yml:3"), files),
            [f"{CONFIG}:6: github-env: `gone.yml` is no file of .github; delete the entry"],
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
                        (
                            f"{INVOWKFILE}: the task `test workflows` must run exactly `{good}`, "
                            f"not `{bad}` (see {AUDIT_WHY})"
                        )
                    ],
                )
        self.assertEqual(
            task_problems(GOOD_CUE.replace('name:        "test workflows"', 'name:        "test flows"')),
            [f"{INVOWKFILE}: there is no task `test workflows`"],
        )
        # A variable set for the task, by an env block or by name, changes what zizmor checks.
        script = '\t\tscript: {content: "' + good + '"}'
        for env, words in (
            ('\t\tenv: {vars: {ZIZMOR_OFFLINE: "0"}}\n', ["env", "ZIZMOR_OFFLINE"]),
            ('\t\tenv: {files: [".env"]}\n', ["env"]),
            ('\t\t"env": {vars: {A: "1"}}\n', ["env"]),
            ('\t\truntimes: [{name: "native", env: {vars: {A: "1"}}}]\n', ["env"]),
            ('\t\tenvironment: "x"\n\t\tmy_env: "x"\n', []),
            # env: in a string value, a multi-line string, a quoted key of another name or after a //.
            ('\t\tdescription: "echo $env: x"\n', []),
            ('\t\tdescription: "a \\"quoted\\" env: x"\n', []),
            ('\t\tdescription: """\n\t\t\tenv: x\n\t\t\t"""\n', []),
            ('\t\t"my env": "x"\n', []),
            ('\t\tdescription: "x" // env: none\n', []),
            # env: inside CUE's other literals: raw strings, multi-line bytes, bytes.
            ('\t\tdescription: #"a "env: x"#\n', []),
            ('\t\tdescription: #"""\n\t\t\t"""env: x\n\t\t\t"""#\n', []),
            ("\t\tdescription: '''\n\t\t\tenv: x\n\t\t\t'''\n", []),
            ("\t\tdescription: 'env: x'\n", []),
            # Still an env key after a raw string or bytes that hold a lone ", and as a raw key.
            ('\t\tdescription: #"a "b"#, env: {vars: {A: "1"}}\n', ["env"]),
            ('\t\tdescription: \'a "b\', env: {vars: {A: "1"}}\n', ["env"]),
            ('\t\t#"env"#: {vars: {A: "1"}}\n', ["env"]),
            # Still an env key: after a string with a // in it, in CUE's short form, after a string.
            ('\t\tlinks: ["https://x"], env: {vars: {A: "1"}}\n', ["env"]),
            ('\t\truntimes: env: {vars: {A: "1"}}\n', ["env"]),
            ('\t\tdescription: "x", "env": {vars: {A: "1"}}\n', ["env"]),
            # A tool variable in a string still counts.
            ('\t\tdescription: "ZIZMOR_X=1"\n', ["ZIZMOR_X"]),
            ("\t\t// ZIZMOR_X is only a comment\n", []),
        ):
            with self.subTest(env=env):
                self.assertEqual(
                    task_problems(GOOD_CUE.replace(script, env + script)),
                    [
                        f"{INVOWKFILE}: the task `test workflows` sets variables (`{word}`): they can change "
                        f"what the command checks (see {AUDIT_WHY})"
                        for word in words
                    ],
                )

    def test_the_nightly_audits_every_locked_package(self) -> None:
        self.assertEqual(uv_audit_problems(GOOD_CUE, GOOD_NIGHTLY), [])
        self.assertEqual(
            uv_audit_problems(GOOD_CUE, "      - run: invowk cmd check\n"),
            [f"{NIGHTLY}: no step runs `run: invowk cmd ci uv-audit` (on one line, nothing added)"],
        )
        good = " ".join(UV_AUDIT_COMMAND)
        script = '\t\tscript: {content: "' + good + '"}'
        self.assertEqual(
            uv_audit_problems(GOOD_CUE.replace(script, '\t\tenv: {vars: {UV_NO_DEV: "1"}}\n' + script), GOOD_NIGHTLY),
            [
                f"{INVOWKFILE}: the task `ci uv-audit` sets variables (`{word}`): they can change what the "
                f"command checks (see {UV_AUDIT_WHY})"
                for word in ("env", "UV_NO_DEV")
            ],
        )
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
                        (
                            f"{INVOWKFILE}: the task `ci uv-audit` must run exactly `{good}`, "
                            f"not `{bad}` (see {UV_AUDIT_WHY})"
                        )
                    ],
                )

    def test_the_nightly_job_cannot_skip_or_narrow_the_audit(self) -> None:
        none = [f"{NIGHTLY}: no step runs `run: invowk cmd ci uv-audit` (on one line, nothing added)"]
        last = (
            f"{NIGHTLY}:19: the uv audit step must be the last step of the job uv-audit: when it fails, "
            "the steps after it do not run (give the audit a job of its own)"
        )
        lenient = (
            "{}: the job uv-audit has `{}:`; the job that runs uv audit may not, since it lets the "
            "nightly pass without the audit"
        )
        uv_var = (
            "{}: a uv variable (`UV_...`) in the nightly; it can leave packages or vulnerabilities out of the audit"
        )
        step = "        run: invowk cmd ci uv-audit\n"
        for nightly, want in (
            (GOOD_NIGHTLY.replace(step, "        # run: invowk cmd ci uv-audit\n"), none),
            (GOOD_NIGHTLY.replace(step, "        run: invowk cmd ci uv-audit || true\n"), none),
            (
                GOOD_NIGHTLY.replace(step, "        run: UV_NO_DEV=1 invowk cmd ci uv-audit\n"),
                [uv_var.format(f"{NIGHTLY}:19"), *none],
            ),
            (GOOD_NIGHTLY + "      - name: After\n        run: invowk cmd check\n", [last]),
            (
                GOOD_NIGHTLY.replace(step, step.replace("run:", "continue-on-error: true\n        run:")),
                [lenient.format(f"{NIGHTLY}:19", "continue-on-error")],
            ),
            (
                GOOD_NIGHTLY.replace("  uv-audit:\n", "  uv-audit:\n    if: false\n"),
                [lenient.format(f"{NIGHTLY}:13", "if")],
            ),
            (
                GOOD_NIGHTLY.replace("  uv-audit:\n", "  uv-audit:\n    needs: static-gates\n"),
                [lenient.format(f"{NIGHTLY}:13", "needs")],
            ),
            (
                GOOD_NIGHTLY.replace("permissions:", "env:\n  UV_NO_DEV: '1'\npermissions:"),
                [uv_var.format(f"{NIGHTLY}:3")],
            ),
        ):
            with self.subTest(nightly=nightly):
                self.assertEqual(uv_audit_problems(GOOD_CUE, nightly), want)

    def test_the_repository_keeps_the_audit_on(self) -> None:
        files = repo_files()
        with open(os.path.join(REPO, CONFIG), encoding="utf-8") as fh:
            config = fh.read()
        with open(os.path.join(REPO, INVOWKFILE), encoding="utf-8") as fh:
            cue = fh.read()
        with open(os.path.join(REPO, NIGHTLY), encoding="utf-8") as fh:
            nightly = fh.read()
        github = [f for f in files if f.startswith(".github/")]
        line_counts: dict[str, int] = {}
        for rel in github:
            with open(os.path.join(REPO, rel), encoding="utf-8", errors="replace") as fh:
                line_counts[rel] = sum(1 for _ in fh)
        found = (
            config_problems(config)
            + entry_problems(config, line_counts)
            + inline_ignores(REPO, github)
            + task_problems(cue)
            + uv_audit_problems(cue, nightly)
        )
        for rel in sorted(github):
            if rel.startswith(".github/workflows/") and rel.endswith((".yml", ".yaml")):
                with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                    found += artifact_upload_problems(rel, fh.read())
        self.assertEqual(found, [], "the workflow audit misses something (see docs/dev-tools.md):\n" + "\n".join(found))

    def test_upload_artifact_hidden_paths_require_include_hidden_files(self) -> None:
        base = (
            "jobs:\n  e2e:\n    steps:\n      - name: Upload\n        if: always()\n"
            "        uses: actions/upload-artifact@abc # v7\n        with:\n          name: logs\n"
        )
        msg = (
            "ci.yml:6: actions/upload-artifact uploads a hidden path (tests/e2e/.artifacts/) "
            "without `include-hidden-files: true` (v4+ skips hidden files by default)"
        )
        want = [msg]
        self.assertEqual(artifact_upload_problems("ci.yml", base + "          path: tests/e2e/.artifacts/\n"), want)
        self.assertEqual(
            artifact_upload_problems(
                "ci.yml", base + "          path: tests/e2e/.artifacts/\n          include-hidden-files: false\n"
            ),
            want,
        )
        self.assertEqual(
            artifact_upload_problems(
                "ci.yml",
                base + "          path: |\n            /tmp/out/\n            tests/e2e/.artifacts/\n",
            ),
            want,
        )
        for ok in (
            base + "          path: tests/e2e/.artifacts/\n          include-hidden-files: true\n",
            base + "          path: tests/e2e/.artifacts/\n          include-hidden-files: 'true'\n",
            base + "          path: audit-report.json\n",
            base + "          path: |\n            /tmp/cst-coverage/\n            !/tmp/cst-coverage/coverage.data\n",
        ):
            with self.subTest(ok=ok):
                self.assertEqual(artifact_upload_problems("ci.yml", ok), [])


if __name__ == "__main__":
    unittest.main()
