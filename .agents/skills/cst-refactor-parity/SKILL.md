---
name: cst-refactor-parity
description: "How to prove that a CST refactor changes no behaviour: the old code from git (git show main:path, git archive), the unchanged tests run on old and new code, a throwaway parity harness that runs both on the same inputs and compares output, exit codes, files and commands, and a review of the test diff. Use when moving, splitting, renaming or converting code (including shell to Python), when a pull request claims \"no behaviour change\", or when reviewing such a pull request."
---
<!-- SPDX-License-Identifier: MPL-2.0 -->

# Proving a refactor changes nothing

The rule is [refactor-vs-behaviour](../../rules/refactor-vs-behaviour.md).
Use the base branch the pull request goes into (`main` below).

A layering refactor moves a package towards its profile
([docs/architecture.md, "Package profile"](../../../docs/architecture.md#package-profile)):
the light one (domain, adapters, cli) unless a full-profile layer pays for
itself. Do not add a port that mirrors one command per method, a
`presenter.py` with one output format or a `wiring.py` without a re-exec,
and do not reshape code that moves or deletes data only for a metric. When
the refactor brings a budget or ceiling down, `invowk cmd fix budgets
--apply` records it; a refactor never raises one.

## 1. Get the old code

- One file: `git show main:path/to/file > "$tmp/old/file"`.
- The whole tree: `mkdir "$tmp/old" && git archive main | tar -x -C "$tmp/old"`.
  Keep temporary folders out of the checkout. Never `git stash`.

## 2. Run the unchanged tests on both

- `git diff main -- tests '*/tests/*'` shows the test diff. For a pure
  refactor it is empty, or it only moves tests or changes an import.
- Run the current tests on the new code, and the same test files on the old
  tree (`python3 -I -B tests/run_unit.py` in `$tmp/old`, after copying the
  new test files there when they only moved). Both pass.

## 2b. A refactor made for a checker rule

When the refactor clears the hits of a ty or ruff rule (a rule about to be
turned on, or one the merge of two branches broke), show the checker on both
sides as well: the rule's hits on the commit before, none on the commit after
(`tests/uv-dev ty check --output-format concise FILE... | grep -c
'\[RULE\]'`, `tests/uv-dev ruff check --select RULE FILE...`). ty has no
`--select`: `--error RULE` only raises that rule, which `pyproject.toml`
already does for every rule (`all = "error"`), so it still prints every
other rule's hits. The concise output names the rule on each line
(`error[RULE]`), and grep counts them.
Put the counts in the commit message next to the unchanged tests. A form the
checker accepts is not proof by itself: a truthiness fix such as `x is not
None` in place of `x` is a behaviour change where `""` or 0 reached the
test, so say in the message why it cannot
([docs/dev-tools.md, "What each gate checks"](../../../docs/dev-tools.md#what-each-gate-checks)).

## 2c. A typing refactor whose tests must pass members: two commits, A and B

When production code starts to take a StrEnum member where it took a plain
string, ty then asks the tests to pass the member too: every test file is
ty-checked, and a raw string where a parameter takes the StrEnum is an
`invalid-argument-type`. Split the change in two commits, each with its own
proof:

- **A**, production only: the tests are unchanged and pass on the old code
  and on the new (step 2). A `StrEnum` member equals its plain string, so
  the old tests still pass at run time.
- **B**, tests only: each raw string passed where A types the parameter
  as the StrEnum becomes the member of the same value, plus the imports those members need;
  assertions keep the raw strings, which pin the values
  ([magic-values](../../rules/magic-values.md)). Its proof is that the diff
  is nothing else: compare each changed test file's syntax tree before
  B and after it, with every member reference put back as its string value
  and the imports dropped; they must be equal. No such checker is in the
  repository today: a throwaway script in a temporary folder does it
  (`ast.parse` both versions, `git show B~1:FILE` and `git show B:FILE`),
  and the commit message says it ran.

Which comes first: when the StrEnum already exists, B does, and every
commit stays green: a member is a `str`, so ty accepts it where the parameter still
takes a plain string, and the tests pass; then A narrows the parameter with
the tests unchanged, and `invowk cmd check` passes on both. When A creates
the StrEnum, B cannot come first (it has nothing to import): A then fails
`invowk cmd test types` at the test calls until B, so B follows A
directly, A's message says so, and the pair lands together.

## 3. Review the test diff

A refactor must not:

- delete a test or an assertion, or add a skip;
- loosen an assertion (`assertEqual` to `assertIn`, an exact string to a
  pattern, a checked exit code to none);
- change an expected output, exit code or file content.

Each of these is a behaviour change: move it to its own commit, with a test
that fails on the old code.

## 4. A parity harness, when the tests do not cover enough

A throwaway script in a temporary folder (not committed unless asked):

- loads the old and the new program, with `load_script` from
  `tests/script_loader.py` for an extensionless one, or runs both as
  subprocesses with `/usr/bin/python3 -I` (bash with `stdin=subprocess.DEVNULL`);
- gives both the same argv, environment, scratch `HOME` and root folder, and
  the same fakes on `PATH` for `systemctl`, `virsh`, sudo and similar
  tools, logging their calls;
- covers every subcommand, bad arguments, and the fixtures the existing tests
  already use;
- compares stdout, stderr, the exit code, the files written (paths, bytes,
  modes) and the logged commands, and prints every difference.

The installed zipapp bytes (`cst_core/installer.py payload SRC`) can change
in a refactor that moves imported code; what the program does must not.

## 5. Write it down

The pull request says which inputs the harness ran, what it compared, and
the result. Each difference found is fixed in a separate behaviour commit,
with a test that failed before.
