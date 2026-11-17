---
name: cst-new-helper
description: "Checklist for adding a Python helper that CST installs on the machine (a python-entry such as btrfs/docker-gc or vm/cst-vm, shipped as a zipapp): the package layout, the INSTALL_MANIFEST row, the pyproject.toml lists, the BOOTSTRAP stanza and one-statement main(), the help mode and the tier 1 install test. Use when adding a new installed Python program or a new installed file, or when tests/test_distribution.py, tests/test_entrypoints.py, tests/test_python_files.py or tests/test_architecture.py fail for a new file."
---

# Adding an installed Python helper

Background: [docs/architecture.md](../../../docs/architecture.md)
(the manifest, zipapps, entry points) and
[docs/dev-tools.md, "What each gate checks"](../../../docs/dev-tools.md#what-each-gate-checks).

## The package

- [ ] The code lives in a package: `domain.py` (pure rules, frozen
      `slots=True` dataclasses), `ports.py` (`Protocol` boundaries),
      `adapters.py` (host I/O, subprocesses), `cli.py` (the commands), and
      `application.py` only with ports. `tests/test_architecture.py` checks
      the layers (`PURE_MODULES`, `PURE_NAMES`), and every module must be
      reached from an entry point.
- [ ] A new `cli.py` gets its line in `CLI_CEILINGS` (same file).
- [ ] Shared needs come from `cst_core`
      ([reuse-primitives](../../rules/reuse-primitives.md)), not a copy:
      `tests/test_duplication.py` counts copies, and a name defined in two
      files needs a reason in `ALLOWED_SAME_NAMES`.

## The entry point (a file without `.py`)

- [ ] Shebang `#!/usr/bin/python3 -I`, executable.
- [ ] A docstring (often the help), the `BOOTSTRAP` stanza byte for byte as in
      `tests/test_entrypoints.py`, the imports, a `main()` that is a docstring
      and one statement handing argv to the package, and the `__main__`
      guard. `NOT_YET_THIN` there stays empty.
- [ ] Paths come from `cst_core.adapters.runtime`, never from `__file__`.

## The lists

- [ ] A row in `INSTALL_MANIFEST` (`cst_core/domain/install.py`): source,
      destination, mode, owner, kind `python-entry`, runner. A file only the
      checkout reads goes to `CHECKOUT_ONLY_FILES` (with the reason) or
      `CHECKOUT_ONLY_EXECUTABLES` instead; `tests/test_distribution.py`
      fails on a file in neither.
- [ ] `pyproject.toml`: the entry in `[tool.ruff] extend-include`; every new
      Python file (tests too) in `[tool.ty.src] include` and in the
      `"!{...}" = ["ANN"]` typed entry. `tests/test_python_files.py` checks
      it; `NOT_YET_CLEAN` stays empty; no ignore comments.
- [ ] `HELP_MODES` in `tests/test_distribution.py`: an argv that returns
      before the program touches the machine, and its exit status.
- [ ] The same entry in the `entries` table of
      `test_11_manifest_installs_and_every_entry_runs_from_root` in
      `tests/e2e/suites/suite_tier1_systemd.py` (installed path, help argv,
      exit status).

## Install, tests, docs

- [ ] The module's setup script installs it with `cst_install` or
      `cst_install_as` and removes it with `cst_installed_dests`. The copy on
      the machine changes only when that install runs again.
- [ ] Unit tests in the module's `tests/`, importing the package and injecting
      fakes through its ports. A command that changes the machine: skill
      [cst-new-command](../cst-new-command/SKILL.md).
- [ ] The module README's file table names the helper and where it is
      installed.
- [ ] The gates in [AGENTS.md, "Commands"](../../../AGENTS.md#commands).
