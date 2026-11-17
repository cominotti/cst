---
name: cst-new-command
description: "Checklist for adding or changing a cst command or operation that changes the machine (a subcommand, an option, an apply, install or uninstall step of a module): the dry run, apply, status and undo, the unit and E2E test matrix, a cst doctor check, the module README and the cli.py ceiling. Use when a change adds or alters a `cst MODULE ...` command or anything that writes system files, units, sysctls or settings."
---
<!-- SPDX-License-Identifier: MPL-2.0 -->

# Adding a cst command that changes the machine

Read first: the module's README (find it in the
[AGENTS.md index](../../../AGENTS.md#index-where-each-part-is-documented)) and
[AGENTS.md, "Rules"](../../../AGENTS.md#rules). This list points to them; it does not
replace them.

## The command

- [ ] It has a dry run that changes nothing: `plan`, `status` or a list mode.
- [ ] `apply` (or `install`) does what `status` then shows, and a second run
      changes nothing more (the installer prints only `unchanged:` lines).
- [ ] It has an undo (`uninstall`, `off`, a revert step) that restores the
      previous state, written in the module README and in
      [README.md, "How to undo"](../../../README.md#how-to-undo).
- [ ] A root command calls sudo by itself and refuses to run inside a toolbox,
      printing the host command instead; read-only commands keep working there.
      See [docs/architecture.md, "Toolbox vs host"](../../../docs/architecture.md#toolbox-vs-host).
- [ ] It goes through the module's entry point (`<module>-setup.sh` or the
      package's `cli.py`) and its help text uses `CST_PROG`. A new module also
      needs a row in `MODULES` in `cst_core/domain/dispatch.py`. For vm and
      toolchains, whose scripts are shims, a new command (or subcommand) needs
      its row in `ROUTING` (same file): read, user, root or as-user, which
      decides the toolbox refusal and the sudo or runuser re-run; and a line
      in the script's header comment, which is its help.
- [ ] Every file it installs is a row of `INSTALL_MANIFEST`
      (`cst_core/domain/install.py`), written with `cst_install`,
      `cst_install_as` or `cst_install_text`, removed with the list from
      `cst_installed_dests`. A new Python program: skill
      [cst-new-helper](../cst-new-helper/SKILL.md).
- [ ] New logic goes into `domain` (pure) or `adapters` (I/O), not into
      `cli.py`: `CLI_CEILINGS` in `tests/test_architecture.py` caps each
      `cli.py` and only goes down.
- [ ] A need another module already has (sizes, managed files, units, locks,
      systemd, virsh) uses the `cst_core` primitive
      ([reuse-primitives](../../rules/reuse-primitives.md)) instead of
      a copy; `tests/test_duplication.py` fails on copies.

## Tests, in the same change

- [ ] Unit tests with the fakes the module's `tests/` already use: the normal
      path, failures, edge cases, and the bug being fixed.
- [ ] E2E tests in `tests/e2e/suites/`: tier 1 when the systemd container can
      host the check, tier 2 for disks, btrfs and cgroup controllers (written,
      run by CI only). See [tests/e2e/README.md](../../../tests/e2e/README.md).
- [ ] The whole cycle: `plan` changes nothing; `apply` then `status`; a
      second run; undo restores; an upgrade from the previous version's files
      and state formats.
- [ ] A fix: its test fails without the fix
      ([refactor-vs-behaviour](../../rules/refactor-vs-behaviour.md)).
- [ ] A test that runs bash passes `stdin=`
      ([AGENTS.md, "Test gotchas"](../../../AGENTS.md#test-gotchas)).

## Doctor and docs

- [ ] What can go wrong after `apply` (drift, leftovers, a postponed step)
      gets a read-only check: [doctor/README.md](../../../doctor/README.md),
      section 9 "Adding a check and internal layout", and a row in its
      section 2 table.
- [ ] The module README changes in the same commit: the command, what it
      changes on the machine, its knobs, how to undo it.

## Before the pull request

Run the gates in [AGENTS.md, "Commands"](../../../AGENTS.md#commands), and
`invowk cmd test e2e tier1` when a tier 1 test was added or changed. The
pull request lists the tests and how "fails without the fix" was checked.
