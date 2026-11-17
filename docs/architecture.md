# How the code fits together

How CST's parts connect: the `cst` dispatcher, the modules, the install manifest, the entry
points, the package layout and the static gates. The rules for changing the code are in
[AGENTS.md](../AGENTS.md); each module's own design is in its README.

## `cst` only dispatches

`cst` delegates to `cst_core/cli.py`
(`CstDispatcher`, backed by `cst_core/domain/dispatch.py` and `CstDispatchPort`).

- `cst MODULE ...` runs `<module>-setup.sh` with `CST_PROG="cst MODULE"` so that help text reads `cst MODULE`.
- The module metadata (`MODULES`, `MODULE_BY_NAME`) lives only in `cst_core/domain/dispatch.py`; `cst` itself is a short bash wrapper that runs `cst_core/cli.py`.
- `cst doctor` runs `doctor/cst-doctor`. `cst status` loops over every module.

## Each module has two parts

- The bash entry point `<module>-setup.sh` handles plan/apply/install/uninstall/status.
- The parts it installs live in the `<module>/` folder: helper programs, systemd `units/`, `sysctl/`, `tmpfiles/` and udev rules.
- **`INSTALL_MANIFEST` (`cst_core/domain/install.py`) is the one list of installed files**: one row per file in a text table (source, destination, mode, owner, kind and who runs it), read by `parse_manifest`. A setup script never copies a file itself: it sources `cst-install.bash` and calls `cst_install SRC...` (root files), `cst_install_as USER SRC...` (files in a home, written by a process running as that user) or `cst_install_text DEST MODE [USER]` (generated text; `btrfs/btrfs-degraded-boot-setup.sh` writes its generated files this way too). They run `cst_core/installer.py` on `/usr/bin/python3`, which writes a file only when its content, mode or (as root) root ownership differs, atomically, and prints `unchanged: DEST` or `written:   DEST` as it goes (a failed write: `cannot write DEST: REASON`, exit 1). A template's `@KEY@` without a `--set`, or a `--set` no template uses, is a usage error (exit 2, nothing written). A link at a destination follows one rule (`cst_core.domain.install.link_read_through`, also used by `owned_file_state_at`): replaced at a root destination, judged by its target in a home; a write replaces the link, never the file it points to. `cst vm backup-timer` and `cst toolchains auto on` follow the same rule (`owned_file_state_at`). `installer.py plan` shows the same without writing. Destinations come from the manifest only (there is no override); every destination and the staging `--root` must be plain absolute paths (`cst_core.domain.files.checked_path`). The few paths a setup script still keeps in a variable for `status` and `uninstall` (`SYSCTL_FILE`, `PENDING_READER`, ...) must name the same files, and the lists of programs to remove come from `cst_installed_dests`.
- A `python-entry` is installed as a **zipapp**: one file holding the entry as `__main__.py`, and every repository package it imports (the static import closure); nothing else. Same path, same mode, shebang `#!/usr/bin/python3 -I`; its bytes are reproducible. Anything that compares an installed copy with the repository compares with these built bytes (`installer.py payload SRC`, `cst_core.adapters.runtime.install_payload`), never with the source file. A `*.in` source is a template: `--set KEY=VALUE` fills its `@KEY@`.
- A new installable file is added to the manifest in the same change (`tests/test_distribution.py` fails otherwise); a file that only the checkout reads goes to `CHECKOUT_ONLY_FILES` with the reason.
- The copy that runs on the machine can therefore be older than the repository. A change to a helper reaches the machine only when the module's `install` runs again.

## Package layout

Python packages follow the Hexagonal Architecture + DDD layout
(`domain.py` / `domain/` for pure rules and frozen `slots=True` dataclasses,
`ports.py` / `ports/` for `Protocol` boundaries, `application.py` for use
cases, `adapters.py` / `adapters/` for host I/O and subprocesses, `cli.py`
for a program's commands). Only swap, doctor and `cst_core` have an
application layer with ports; in the other packages `cli.py` orchestrates
the commands and still does some I/O itself. `tests/test_architecture.py`
enforces the import and dataclass invariants, and `CLI_CEILINGS` there caps
each `cli.py` so it only shrinks: new logic goes into `domain` (pure),
`application` (with ports) or `adapters` (I/O). The I/O left outside the
adapters has a budget per package and layer that only goes down
(`IO_BUDGETS`, see "Static gates beyond lint and types" below).

## Most helpers are Python scripts with no `.py` extension

Examples: `btrfs/btrfs-bulk`, `vm/cst-vm`, `doctor/cst-doctor`, `toolchains/cst-toolchains`.

- Tests load them with `tests/script_loader.py` (`load_script(path, name)`), which registers the module in `sys.modules` before running it (`@dataclass` needs that on Python 3.14) and returns a fresh module each call.
- Tests of a converted program import its package. A test written against the old one-file namespace can use `tests/package_view.py` (`PackageView(cli, adapters, domain)`): a name set on it reaches every module that uses it, and `restore()` puts the real ones back.
- Tests call the functions directly, with fakes for `virsh`, `systemctl`, sudo, restic and similar tools.

## Entry points only wire argv; the logic lives once, in the package

- An extension-less Python program holds a docstring, the bootstrap, imports, one `main()` and the `__main__` guard (`tests/test_entrypoints.py`; `NOT_YET_THIN` is empty: every program is converted).
- **The bootstrap** is one stanza, byte for byte the same in every entry point that imports CST code (`BOOTSTRAP` in `tests/test_entrypoints.py`): from a checkout it puts the checkout root on `sys.path` (under `python3 -I` a script sees only its own folder); inside an installed zipapp it does nothing. Nothing else changes `sys.path`.
- Paths are never computed from `__file__` by hand: `cst_core.adapters.runtime` gives the checkout root, the program's own path and argv (to run itself again), a sibling program (checkout path, or its installed dest), and the installed bytes.
- `main()` is a docstring and one statement that hands argv to the package.
- No re-export facades (`from m import a as a`, `__all__`, a package `__init__.py` importing its own submodules): tests import a name from the module that defines it and inject fakes through ports.

## A concept more than one module needs lives once, in `cst_core`

A module imports it instead of keeping a copy. What `cst_core` provides, need by need, and what to do
when it lacks an option: the rule [reuse-primitives](../.agents/rules/reuse-primitives.md).

## Static gates beyond lint and types

All of them run in the unit suite:

- `tests/test_architecture.py`: layer imports; frozen slotted dataclasses in `domain`; no direct I/O in `domain` or `application`, as an allow-list of what they may import and use (`PURE_MODULES`, `PURE_NAMES`, resolved through aliases and `from m import x`; the I/O builtins fail); every package module reached from an entry point (`ORPHAN_ALLOWED` and `ORPHAN_ALLOWED_FILES` are empty: no shadow modules). Also:
  - every package module has one role (`module_role`: domain, ports, application, adapters, presenter, wiring, cli or entry); the few that have none yet are in `UNLAYERED_ALLOWED` with their reason, a table that only shrinks;
  - the I/O a module does outside its adapters (the lines `direct_io_problems` reports, with print, `sys.stdout`/`stderr`/`argv`/`exit` and argparse allowed) has a budget per package and role, `IO_BUDGETS`: moving code to an adapter lowers it, moving it from `cli.py` into another module of the same package does not hide it;
  - a package in `HEXAGONAL_PACKAGES` has a `cli.py` and a `presenter.py` with no I/O at all, and only its composition point (`wiring.py`) and its adapters import an adapter; a package that meets this is added (the test says so);
  - no module imports a private name (`_x`) from another module of the repository;
  - each shared primitive has an owner in cst_core (`PRIMITIVE_OWNERS`: `subprocess`, `os.replace`, `os.fsync`, temporary files, `fcntl.flock`, the NOCOW ioctl), and so do the literals that betray a copy of one (`PRIMITIVE_LITERALS`); a call or a literal outside its owner counts against its package's budget (`PRIMITIVE_BUDGETS`, `LITERAL_BUDGETS`), docstrings aside;
  - every public top-level name of cst_core's domain, ports, adapters and application has a production user outside its file (`CORE_NAMES_WITHOUT_USER`, with reasons, only shrinks).
- Budgets (`tests/budgets.py`) are counts per package and layer that only go down: a count above its entry fails, and a count below it fails too until the lower number is written down, so that what was removed cannot come back. A key missing from a table is 0. A failing gate prints the measured table to paste in.
- `tests/test_distribution.py`: the manifest and the repository agree; every python-entry zipapp holds what it imports (AST import closure) and loads no code by path; modes fit the runner; entries with a known help mode run it from `/` with `/usr/bin/python3 -I`; sourced shell files exist; every shell file of the checkout (`tests/shell_files.py`, the list `invowk cmd test lint` runs bash -n and shellcheck on) is an installed shell program, a setup entry point or sourced library, a checkout-only executable or an E2E script (`SHELL_TEST_SCRIPTS`), and parses. Its tier 1 twin (`test_11_...` in `tests/e2e/suites/suite_tier1_systemd.py`) installs the whole manifest for real and runs every entry as root and as a user.
- `tests/test_bytecode.py`: every `python3 -I` that starts a CST program from the checkout (setup scripts, Python argv lists) passes `-B`, so no `__pycache__` lands in the checkout (as root it would be root's and block `git clean`); `-I -c` and `-I -` (inline code) are exempt.
- `tests/test_docs.py`: every relative link and anchor in the Markdown files resolves; every document is in the AGENTS.md index; every skill in `.agents/skills` has a SKILL.md with its name and a description, every rule a description; `.claude/rules` and `.claude/skills` link to `.agents`.
- `tests/test_duplication.py`: a ratchet on copied code (Python type 1 and type 2 clones, shell clones, the same top-level name defined in several files, the same shell function defined in several shell files: `ALLOWED_SHELL_SAME_NAMES` holds each allowed one with its reason). Its `CEILINGS` only go down: lower a number in the change that removes copies. The entry bootstrap and the bodies of `Protocol` classes are the same by design and are not counted; type 2 skips lines of literals only (type 1 still counts them). A name in `ALLOWED_SAME_NAMES` carries its reason, and an entry that is no longer defined twice fails.

## Toolbox vs host

The scripts detect a container through `/run/.containerenv` or `/.dockerenv`.

- Inside one, read-only commands still work, sometimes by moving to the host with `flatpak-spawn --host`.
- Root commands refuse to run there. Keep that split when adding commands.

## `doctor/cst-doctor` is a read-only check across all modules

It prints ok/info/WARN/ERROR lines.

- New module behaviour that can go wrong usually also needs a doctor check, documented in `doctor/README.md`.

## CI

CI (`.github/workflows/ci.yml`) runs the same `invowk cmd ...` tasks: the fast-gates job runs `invowk cmd ci audit` (the JSON report and the job summary) and then `invowk cmd check`, the same task people and agents run; the nightly workflow runs the same two. A pull request that changes only Markdown still runs the fast gates (the docs gate is in the unit suite); only the E2E tiers are skipped.

- `invowk` is pinned by SHA256 and verified with cosign in `.github/actions/setup-invowk`.
- Change the version with `invowk cmd ci pin-invowk <tag>`, not by editing the hashes.

## Machine-specific state lives outside the repository

- Knobs: `/etc/cst/vm.conf` (root) and `~/.config/cst/toolchains.conf` (user). The defaults are in the scripts and in `toolchains/toolchains.conf`.
- The VM backup index is `/var/lib/cst/vm-backups.json`.
- User units go in `~/.config/systemd/user`.
- "Adapting CST to another PC" in README.md lists what is specific to this machine.
