---
description: A change is either a refactor (no behaviour change, proved) or a behaviour change (with a test that fails on the old code), never both; never use git stash in a worktree
---
<!-- SPDX-License-Identifier: MPL-2.0 -->
<!-- global rule: loaded for every request -->

# Refactor or behaviour change: one kind per change

Every commit and every pull request is one of two kinds, a refactor or a
behaviour change, unless it changes no behaviour at all (docs, below).
Say which one in the message, with its proof.

## A refactor changes no behaviour

Same output, same exit codes, same files written and the same commands
called, for every input. Prove it in one of two ways:

- the tests that existed before pass, unchanged, on the old code and on the
  new code (the test diff is empty, or it only moves tests without changing
  what they assert); or
- a parity harness runs the old and the new code on the same inputs and
  compares the results.

A refactor made for a checker rule (a ty or ruff rule it clears) also shows
the checker on both sides: the rule's hits on the commit before it and none
after, for example `tests/uv-dev ty check --error RULE FILE...` or
`tests/uv-dev ruff check --select RULE FILE...` on each. Put the counts in
the message.

The skill [cst-refactor-parity](../skills/cst-refactor-parity/SKILL.md) shows
both. A refactor that finds a bug does not fix it on the way: write the bug
down and fix it in a behaviour change of its own.

## A behaviour change comes with a test that fails on the old code

Which tests a change needs is in [AGENTS.md, "Rules"](../../AGENTS.md#rules).
Check that the new test fails without the change, and write in the pull
request how you checked it. Two ways:

- Run the new test against the old file: `git show main:path/to/file >
  "$tmp/file"` puts the old version in a temporary folder; point the test at
  that copy (an extensionless Python program loads with `load_script` from
  `tests/script_loader.py`).
- Commit the change first. Then `git checkout main -- path/to/file` puts the
  old file in the worktree; run the new test (it must fail); `git checkout
  HEAD -- path/to/file` puts the change back. Never do this with uncommitted
  work: the first command overwrites it.

## Gates are code too

A gate (a test in `tests/` that checks the repository, such as
`tests/test_architecture.py` or `tests/test_magic_values.py`, or a ruff or
ty setting in `pyproject.toml`) follows the same rule:

- A change in what a gate reports is a behaviour change of the gate. Its test
  plants a source the gate must report (or must not) and fails on the old
  gate.
- A checker rule is turned on in two steps: first the refactors that clear
  its hits, each with its proof; then the settings commit, whose proof is the
  checker passing on the whole tree (`invowk cmd test pylint` or
  `invowk cmd test types`).

## No behaviour at all: docs

A commit that changes only documents, comments, agent rules or skills says
so in its message ("docs, no behaviour
change") and needs no new test ([AGENTS.md, "Rules"](../../AGENTS.md#rules));
the docs gate (`invowk cmd test docs`) still runs. It changes no code CST
installs or runs: a docs fix that finds a bug writes it down for a behaviour
change of its own.

## Never mix the two

A commit that moves code and also changes what it does cannot be reviewed:
nobody can tell the moved lines from the changed ones. Split it: first the
refactor, with its parity proof; then the behaviour change, with its failing
test.

## Never use git stash in a worktree

The stash belongs to the repository, not to one worktree: every worktree of a
clone sees the same stash list, so a `git stash pop` in one worktree can
apply the changes of another agent working in a second one. To set work
aside, commit it on your branch (a work-in-progress commit you amend later)
or copy the files to a temporary folder.
