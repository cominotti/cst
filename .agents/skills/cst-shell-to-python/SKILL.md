---
name: cst-shell-to-python
description: "Procedure for converting a CST bash script (a <module>-setup.sh, a helper, a sourced .bash library) to Python without changing its behaviour: behaviour tests against the bash first, the same command line, exit codes and output, the swap-setup.sh precedent, a thin shim, deleting the bash, lowering the ceilings and keeping the docs in step. Use when asked to port, rewrite or convert a shell script of this repository to Python."
---
<!-- SPDX-License-Identifier: MPL-2.0 -->

# Converting a bash script to Python

A conversion is a refactor: it changes no behaviour
([refactor-vs-behaviour](../../rules/refactor-vs-behaviour.md)). Read the
module's README and every caller of the script first: `cst_core/domain/dispatch.py`,
other scripts that source it, units that run it, E2E tests.

## The precedents: swap, cpu, bluetooth, the health check, btrfs-snapshot

Commit 2865a78 (pull request #27) turned `swap-setup.sh`, `swap/swap-lib.bash`
and `swap/swap-status` into the `swap/` package. Read it with
`git show 2865a78 -- swap-setup.sh swap/`. What it kept:

- `swap-setup.sh` is now a shim: it sets `CST_PROG` and runs
  `/usr/bin/python3 -I -B swap/cli.py "$@"`. `swap/cli.py` is in
  `CHECKOUT_ONLY_EXECUTABLES` (`cst_core/domain/install.py`).
- `swap/tests/test_swap.py`: `test_execute_cli_and_bash_wrapper` keeps the old
  usage layout and the exit status of bad arguments, and
  `test_sysctl_payload_matches_old_script` adopts the files the old script
  wrote (the upgrade path).

The cpu port (commit 086c2c3, #60: cpu-setup.sh over `cpu/cpu-setup` and the `cpu.setup`
package; `cpu/game-profile` as a python-entry zipapp of `cpu.game`) kept
the installed bash copy working next to the new one: see the checklist's
last items. Then commit b819032 (#66: `btrfs-health-check` and
`cst-pending-alerts`, installed programs that became zipapps of
`btrfs/health_check` and `btrfs/pending_alerts`) and commit c94600a (#67:
`bluetooth-setup.sh` over `bluetooth/bt-setup` and the `bluetooth.setup`
package, with tier 1's `test_95` running the old bash and the new Python
against each other).

The btrfs-snapshot port (the merge e31e8b3, "Merge the btrfs-snapshot
port"; read it with `git log e31e8b3^1..e31e8b3^2`) moved only part of a
program: the bash front `btrfs/btrfs-snapshot` keeps its conversion engine
and `busy` for good and execs `btrfs/btrfs-snapshot-runtime` (a zipapp of
`btrfs/snapshot_runtime`, installed next to it) for `hourly`, `nas`,
`list`, `caches`, `prune`, `purge` and `configure`, handing over
`CST_LC_CTYPE` and `CST_SIGPIPE_IGNORED`. `btrfs/tests/test_snapshot_mixed.py`
runs the last bash version (`tests/fixtures/legacy/btrfs-snapshot@47038aa`)
and the new one on the same state, both ways; tier 1's `test_109` and
`test_110` run the installed pair as root, and `test_108` and `test_111`
check that `snapshots.conf` is read as data. Its fix commits gave the last
items of the checklist below.

## The port checklist (from PR-A's regressions)

Each item is something a port got wrong once. Check it for every ported
command, against the bash, with a test:

- [ ] **exec, not a child.** Where the bash `exec`s (the shim, a sudo
      re-run, a program it hands over to), the Python execs too
      (`LocalCstDispatchAdapter.exec_replace`): same process id, the
      program's exit status and signals are the caller's. Test it: a fake
      program prints `$$`, which must be the pid the test started.
- [ ] **Signals and Ctrl-C.** Ctrl-C ends the program as it ended bash
      (`default_ctrl_c`: killed by SIGINT, no KeyboardInterrupt traceback);
      a child bash waited for in the foreground (an interactive one) runs
      through `run_passthrough`.
- [ ] **A closed stdout pipe.** Only a command that only reads (help,
      status, state) restores SIGPIPE (`restore_default_signals`, or
      `undo_python_start` for a setup script whose bash had it); what the
      calling shell sees is 141 and no traceback. Never in a command with
      cleanup.
- [ ] **Exit codes.** A child killed by signal N gives 128+N
      (`shell_exit_status`); a step bash ran under `set -e` ends the
      command with that step's status; a missing command gives 127.
- [ ] **No environment leaks.** The program and what it runs get only what
      the bash passed: the shim's own variables (`CST_SELF`, `CST_ARG0`,
      `CST_LC_CTYPE`) are popped, nothing new is exported, sudo gets
      `CST_PROG` only when the caller set it.
- [ ] **LC_CTYPE.** The caller's (`CST_LC_CTYPE` from the shim,
      `undo_python_start`), not Python's C.UTF-8 under a C locale; programs
      the bash started with `CST_LC_CTYPE=...` (the installer) get it too.
      An installed program that a unit or another program starts directly
      (a zipapp such as `cpu-game-profile`) has no wrapper to hand it over:
      under a C locale its children see C.UTF-8. Check that what they print
      and what the program reads from them is the same (the harness under
      LANG unset), and say so in the pull request.
- [ ] **No bytecode in the checkout.** Every Python the shim and the
      program start from the checkout runs with `-B`.
- [ ] **Empty-string arguments.** `""` is an argument (`${1:-help}` makes
      an empty first word help); a sudo re-run passes it on.
- [ ] **The help text, byte for byte.** `script_help` of the shim's header
      (lines 2 to the first empty one, the license line left out), with
      `CST_PROG` or `$0`; the shim keeps the header.
- [ ] **State files another copy reads.** Written in exactly the bytes the
      bash wrote (`printf %q`: `cst_core.domain.text.printf_q`, checked
      against bash), under the same lock (flock(2) on the same file), and
      proven both ways: the installed bash copy reads what the Python wrote
      and the reverse (a parity harness, and a tier 1 test that runs the
      bash version next to the checkout: vendored as
      `tests/fixtures/legacy/<file>@<short-sha>`, never read from the git
      history: the short SHAs in the names of the older fixtures, such as
      d4d8c91 and cfb496a, no longer resolve in it;
      tests/e2e/README.md, "Mixed-version tests").
- [ ] **What the commands get, as bash gave it.** A program with cleanup
      leaves SIGPIPE ignored, but its children get the disposition bash's
      children got (`keep_signals` of the runner when the front says
      SIGPIPE was ignored, as under a systemd unit); output bytes that are
      not UTF-8 reach the next command and the files unchanged
      (`decode_errors="surrogateescape"`); lines are split at newlines only,
      as `read` splits them (`on_line`); the run goes on when its stderr is
      gone.
- [ ] **Settings and state as bash read and wrote them.** A settings file
      bash sourced is read as data (`parse_snapshots_conf`,
      `parse_shell_assignments`), never run; a number bash evaluated as
      arithmetic keeps bash's rules (64-bit wrap, `BASE#DIGITS`, powers); a
      state record is replaced whole (`write_atomic`), never rewritten in
      place.
- [ ] **What a caller can tell apart.** Every difference the harness finds
      that a caller or a script could notice (an exit code, a file, stdout,
      the order of lines) is either made the same as the bash's or moved
      out of the port into a behaviour commit with its test; only
      differences no caller sees (stderr wording, the order of read-only
      calls) stay, named in the commit message.
- [ ] **A parity harness** (outside the repository) runs the bash and the
      Python on the same stubs and scratch paths and compares stdout,
      stderr, exit codes, every command line and every file; list what
      differs, and why, in the pull request.

## Steps

1. **Behaviour tests first, against the bash.** Run the script in tests with
   stubs on `PATH` and scratch folders, as
   `btrfs/tests/test_health_check.py` does, and pin what a user or caller sees:
   every subcommand and option, the usage text, the exit codes (usage errors
   included), stdout and stderr, the files written (content and mode), the
   commands called. A subprocess that runs bash passes `stdin=`
   ([AGENTS.md, "Test gotchas"](../../../AGENTS.md#test-gotchas)). Commit these tests while
   the bash still passes them.
2. **Write the Python package**, in the light profile (domain, adapters,
   cli) unless a layer of the full one pays for itself
   ([docs/architecture.md, "Package profile"](../../../docs/architecture.md#package-profile)).
   Its words, numbers and records follow
   [magic-values](../../rules/magic-values.md) from the start: the port is
   the border where bash's strings become enums and checked shapes. Skill
   [cst-new-helper](../cst-new-helper/SKILL.md) has the lists a new Python
   file must join.
3. **Replace the entry point with a shim** like `swap-setup.sh`, so
   `cst MODULE ...` and the help text stay the same.
4. **Run the step 1 tests, unchanged, against the Python.** The test diff
   must not loosen or delete an assertion (skill
   [cst-refactor-parity](../cst-refactor-parity/SKILL.md)).
5. **Delete the bash** (or, as btrfs-snapshot did, keep a bash front for
   the part that stays bash and say so in the module README): the old
   files, and their `INSTALL_MANIFEST` rows
   (kind `shell`) or `SETUP_ENTRYPOINTS` entries (`cst_core/domain/install.py`)
   when they go away. `test lint` finds the shell files by itself
   (`tests/shell_files.py`); only a test-only script is listed by hand there
   (`SHELL_TEST_SCRIPTS`).
6. **Lower the ceilings**: run `invowk cmd fix budgets` (a dry run) and
   then `invowk cmd fix budgets --apply`: it sets `shell W=5 duplicated %`
   in `CEILINGS` of `tests/test_duplication.py` (and every other table the
   change brought down) to the new measured value. It never raises one;
   neither do you. `CLI_CEILINGS` applies to the new `cli.py`.
7. **Docs in the same change**: the module README's file table and test
   command, README.md "Other files in this folder", and docs/architecture.md where it
   names which modules have an application layer.
8. Run the gates in [AGENTS.md, "Commands"](../../../AGENTS.md#commands), and tier 1 when
   the module has a tier 1 test.
