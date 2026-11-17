---
name: cst-shell-to-python
description: "Procedure for converting a CST bash script (a <module>-setup.sh, a helper, a sourced .bash library) to Python without changing its behaviour: behaviour tests against the bash first, the same command line, exit codes and output, the swap-setup.sh precedent, a thin shim, deleting the bash, lowering the ceilings and keeping the docs in step. Use when asked to port, rewrite or convert a shell script of this repository to Python."
---

# Converting a bash script to Python

A conversion is a refactor: it changes no behaviour
([refactor-vs-behaviour](../../rules/refactor-vs-behaviour.md)). Read the
module's README and every caller of the script first: `cst_core/domain/dispatch.py`,
other scripts that source it, units that run it, E2E tests.

## The precedent: swap

Commit 8d8fc94 (pull request #27) turned `swap-setup.sh`, `swap/swap-lib.bash`
and `swap/swap-status` into the `swap/` package. Read it with
`git show 8d8fc94 -- swap-setup.sh swap/`. What it kept:

- `swap-setup.sh` is now a shim: it sets `CST_PROG` and runs
  `/usr/bin/python3 -I -B swap/cli.py "$@"`. `swap/cli.py` is in
  `CHECKOUT_ONLY_EXECUTABLES` (`cst_core/domain/install.py`).
- `swap/tests/test_swap.py`: `test_execute_cli_and_bash_wrapper` keeps the old
  usage layout and the exit status of bad arguments, and
  `test_sysctl_payload_matches_old_script` adopts the files the old script
  wrote (the upgrade path).

## Steps

1. **Behaviour tests first, against the bash.** Run the script in tests with
   stubs on `PATH` and scratch folders, as
   `btrfs/tests/test_health_check.py` does, and pin what a user or caller sees:
   every subcommand and option, the usage text, the exit codes (usage errors
   included), stdout and stderr, the files written (content and mode), the
   commands called. A subprocess that runs bash passes `stdin=`
   ([AGENTS.md, "Test gotchas"](../../../AGENTS.md#test-gotchas)). Commit these tests while
   the bash still passes them.
2. **Write the Python package** (domain, ports, adapters, application,
   presenter, cli). Skill [cst-new-helper](../cst-new-helper/SKILL.md) has the
   lists a new Python file must join.
3. **Replace the entry point with a shim** like `swap-setup.sh`, so
   `cst MODULE ...` and the help text stay the same.
4. **Run the step 1 tests, unchanged, against the Python.** The test diff
   must not loosen or delete an assertion (skill
   [cst-refactor-parity](../cst-refactor-parity/SKILL.md)).
5. **Delete the bash**: the old files, their names in the `test lint` script
   list in `cst.invowkmod/invowkfile.cue`, and their `INSTALL_MANIFEST` rows
   (kind `shell`) or `SETUP_ENTRYPOINTS` entries when they go away.
6. **Lower the ceilings**: in `CEILINGS` of `tests/test_duplication.py`,
   set `shell W=5 duplicated %` (and any other metric that went down) to the
   new measured value. Never raise one; `CLI_CEILINGS` applies to the new
   `cli.py`.
7. **Docs in the same change**: the module README's file table and test
   command, README.md "Other files in this folder", and docs/architecture.md where it
   names which modules have an application layer.
8. Run the gates in [AGENTS.md, "Commands"](../../../AGENTS.md#commands), and tier 1 when
   the module has a tier 1 test.
