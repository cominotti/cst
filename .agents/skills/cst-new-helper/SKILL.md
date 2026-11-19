---
name: cst-new-helper
description: "Checklist for adding a Python helper that CST installs on the machine (a python-entry such as btrfs/docker-gc or vm/cst-vm, shipped as a zipapp): the package layout, the INSTALL_MANIFEST row, the pyproject.toml lists, the BOOTSTRAP stanza and one-statement main(), the help mode and the tier 1 install test. Use when adding a new installed Python program or a new installed file, or when tests/test_distribution.py, tests/test_entrypoints.py, tests/test_python_files.py or tests/test_architecture.py fail for a new file."
---
<!-- SPDX-License-Identifier: MPL-2.0 -->

# Adding an installed Python helper

Background: [docs/architecture.md](../../../docs/architecture.md)
(the manifest, zipapps, entry points) and
[docs/dev-tools.md, "What each gate checks"](../../../docs/dev-tools.md#what-each-gate-checks).

## The package

- [ ] The code lives in a package of the light profile
      ([docs/architecture.md, "Package profile"](../../../docs/architecture.md#package-profile);
      reference `btrfs/chrome_cache`): `domain.py` (pure rules, frozen
      `slots=True` dataclasses), `adapters.py` (host I/O, subprocesses,
      through cst_core) and `cli.py` (parse argv, build the adapters, call
      one function, print). Add `application.py` with `ports.py`, `wiring.py`
      or `presenter.py` only when the profile's reason for each holds (the
      full profile, reference `btrfs/docker_volumes_nocow`; the package then
      joins `HEXAGONAL_PACKAGES`). `tests/test_architecture.py` checks the
      layers (`PURE_MODULES`, `PURE_NAMES`), and every module must be
      reached from an entry point.
- [ ] No module over 800 lines (`tests/test_module_size.py`): split by
      command group or check area, not by layer. A big `cli.py` becomes
      `cli/__init__.py` and `cli/<group>.py` (one `CLI_CEILINGS` entry,
      `<package>/cli/`, for the folder).
- [ ] A new `cli.py` gets its line in `CLI_CEILINGS` (same file).
- [ ] Shared needs come from `cst_core`
      ([reuse-primitives](../../rules/reuse-primitives.md)), not a copy:
      `tests/test_duplication.py` counts copies, and a name defined in two
      files needs a reason in `ALLOWED_SAME_NAMES`.

## The entry point (a file without `.py`)

- [ ] Shebang `#!/usr/bin/python3 -I`, executable, then the license line
      `# SPDX-License-Identifier: MPL-2.0` (`invowk cmd fix license --apply`
      adds it to every new file; `tests/test_license_headers.py` checks it).
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
- [ ] `pyproject.toml`: the helper's entry in `[tool.ruff] extend-include`
      and in `[tool.ty.src] include` (a `*.py` file is in the ty gate
      through its folder; a new top-level folder needs its entry); every
      new Python file (tests too) fully annotated
      (ruff's ANN rules apply to every file; no per-file-ignores entry may
      switch them off). `tests/test_python_files.py` checks it;
      `NOT_YET_CLEAN` stays empty; no ignore comments.
- [ ] ty passes with every rule an error (`[tool.ty.rules] all = "error"`,
      [docs/dev-tools.md, "What each gate checks"](../../../docs/dev-tools.md#what-each-gate-checks)):
      a method that overrides one (`setUp` included) carries `@override`
      from `typing`; a test on `X | None` says which case it means
      (`is not None`, `!= ""`); JSON and TOML are parsed only through
      `cst_core.domain.shapes` and their shape is checked before a typed
      value gets them; a regex group comes from `group` or `group_or_none`
      and the standard streams from `cst_core.adapters.terminal`. ruff's
      selected families (preview rules included) pass too, and no
      `# fmt: skip`, `# fmt: off` or `# fmt: on` (`tests/test_python_files.py`).
- [ ] Its words, numbers and records follow
      [magic-values](../../rules/magic-values.md): `tests/test_magic_values.py`
      has no ceiling left to spend (`MAGIC_CEILINGS` is empty).
- [ ] `HELP_MODES` in `tests/test_distribution.py`: an argv that returns
      before the program touches the machine, and its exit status.
- [ ] The same entry in the `entries` table of
      `test_11_manifest_installs_and_every_entry_runs_from_root` in
      `tests/e2e/suites/suite_tier1_systemd.py` (installed path, help argv,
      exit status).

## Install, tests, docs

- [ ] The module installs it through the installer: `cst_install` or
      `cst_install_as` in a bash setup script, `cst_core/installer.py install
      --component ...` (or the `InstallService` of `program_installer`, as swap, vm and toolchains do) in a Python
      one; it removes it with the component's destinations
      (`cst_installed_dests`, or `installer.py remove --component`). The copy on
      the machine changes only when that install runs again.
- [ ] Unit tests in the module's `tests/`, importing the package and injecting
      fakes (and closing what they open: `tests/run_unit.py` fails the run on
      a ResourceWarning,
      [AGENTS.md, "Test gotchas"](../../../AGENTS.md#test-gotchas)): through
      cst_core's `CommandRunnerPort` / `FileSystemPort` (or the adapter
      functions) in a light package, through its ports in a full
      one. A command that changes the machine: skill
      [cst-new-command](../cst-new-command/SKILL.md).
- [ ] The module README's file table names the helper and where it is
      installed.
- [ ] The gates in [AGENTS.md, "Commands"](../../../AGENTS.md#commands).
