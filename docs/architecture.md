<!-- SPDX-License-Identifier: MPL-2.0 -->
# How the code fits together

How CST's parts connect: the `cst` dispatcher, the modules, the install manifest, the entry
points, the package layout and the static gates. The rules for changing the code are in
[AGENTS.md](../AGENTS.md); each module's own design is in its README.

## `cst` only dispatches

`cst` delegates to `cst_core/cli.py`, which hands argv to the use case
`CstDispatcher` (`cst_core/application/dispatch.py`), over `cst_core/domain/dispatch.py`
and `CstDispatchPort`, implemented by `cst_core/adapters/dispatch.py` (which also
answers `which` and `realpath`); `cst_core/wiring.py` builds it (see "The
composition root" below).

- `cst MODULE ...` runs `<module>-setup.sh` with `CST_PROG="cst MODULE"` so that help text reads `cst MODULE`.
- The module metadata (`MODULES`, `MODULE_BY_NAME`) lives only in `cst_core/domain/dispatch.py`; `cst` itself is a short bash wrapper that runs `cst_core/cli.py`.
- `cst doctor` runs `doctor/cst-doctor`. `cst status` loops over every module.

## Each module has two parts

- The bash entry point `<module>-setup.sh` handles plan/apply/install/uninstall/status. Three are shims over Python:
  `swap-setup.sh` runs `swap/cli.py`; `vm-setup.sh` and `toolchains-setup.sh` run `cst_core/route.py`, which
  routes each command by the table `ROUTING` next to `MODULES` in `cst_core/domain/dispatch.py` (read, user,
  root or as-user: flatpak-spawn from a toolbox, the toolbox refusal, the sudo or runuser re-run of the script)
  and execs the module's program; the script's header comment stays its help.
- The parts it installs live in the `<module>/` folder: helper programs, systemd `units/`, `sysctl/`, `tmpfiles/` and udev rules.
- **`INSTALL_MANIFEST` (`cst_core/domain/install.py`) is the one list of installed files**: one row per file in a text table (source, destination, mode, owner, kind, who runs it and its component), read by `parse_manifest`. A component (`btrfs:core`, `btrfs:docker-gc`, `cpu:docker-slice`, ...) is the part of a module that one command installs as a whole; `COMPONENTS` maps each to that command (`cst btrfs docker-gc enable`, the refresh command `cst doctor` names), and a component has one owner, so one installer call covers it. A setup script never copies a file itself: it sources `cst-install.bash` and calls `cst_install --component C` or `cst_install SRC...` (root files), `cst_install_as USER SRC...` (files in a home, written by a process running as that user) or `cst_install_text DEST MODE [USER]` (generated text; `btrfs/btrfs-degraded-boot-setup.sh` writes its generated files this way too, and its plan mode asks `cst_install_text_plan` with the same arguments). They run `cst_core/installer.py` on `/usr/bin/python3`, which writes a file only when its content, mode or (as root) root ownership differs, atomically, and prints `unchanged: DEST` or `written:   DEST` as it goes (a failed write: `cannot write DEST: REASON`, exit 1). A template's `@KEY@` without a `--set`, or a `--set` no template uses, is a usage error (exit 2, nothing written). A link at a destination follows one rule (`cst_core.domain.install.link_read_through`, also used by `owned_file_state_at`): replaced at a root destination, judged by its target in a home; a write replaces the link, never the file it points to. `cst vm backup-timer` and `cst toolchains auto on` follow the same rule (`owned_file_state_at`), with one exception for their unit files: a link to `/dev/null` there is a `systemctl mask`, which they never undo (`unit_file_state_at`: `on` refuses, `status` says `masked`, `off` leaves it). `installer.py plan` (`text-plan` for generated text) shows the same without writing. Destinations come from the manifest only (there is no override); every destination and the staging `--root` must be plain absolute paths (`cst_core.domain.files.checked_path`). The few paths a setup script still keeps in a variable for `status` and `uninstall` (`SYSCTL_FILE`, `PENDING_READER`, ...) must name the same files, and the lists of files and units to remove come from the manifest (`cst_installed_dests --component C`, `cst_component_units C`); no setup script keeps its own list of programs or units.
  - **Units are verified before they replace the installed ones.** Within one installer call, programs are written before units. As root on the real root (not under `--root`), the units the call changes are copied under their own names into a temporary folder and checked together with `systemd-analyze verify --man=no --recursive-errors=no` (the strict form tier 1's test_13 proved: without `--recursive-errors=no`, an unknown key is only a warning); the exit status decides, the lines about these units are shown. When it fails none of them is written (exit 1). Drop-ins (a `NAME.d/` file), template units (`NAME@.service`) and staged installs are not verified; without `systemd-analyze` the units are written with a note; outside a machine systemd runs (`/run/systemd/system` missing) nothing is verified. A unit that runs another component's program needs that component first (`btrfs:alerts` before `btrfs:core`).
  - **The install record** (`cst_core/domain/record.py`): every file the installer writes or finds current is listed in `/var/lib/cst/installed.json` (root's files) or `~/.local/state/cst/installed.json` (a user's, written by that user): its source, component, kind, sha256, mode, owner, a template's `--set` values, and the checkout's commit (read from `.git` by hand) and component "tree" (the sha256 of its payloads). A file found current that the record did not list yet (an install from before the record) is adopted. A component is `pending` from a write until its setup command finishes and calls `cst_mark C` (`installer.py mark --component C done`): a crash in between leaves it pending, which `cst doctor` reports. Undo paths remove files with `cst_remove --component C` / `cst_remove_text DEST` (`installer.py remove`, `remove-text`), which drop their record lines too, and work without a record. Generated text is recorded only with a component (`cst_install_text DEST MODE OWNER COMPONENT`): settings files a user may edit are not. The record is written atomically (0644, folders 0755) under a lock (`RECORD.lock`); one of a newer format is never rewritten (the install stops before any write). `cst vm backup-timer`, `cst toolchains auto on|off` and `config apply`, `cst swap apply` and the bluetooth LDAC watcher write their manifest files through the same `InstallService` (`put`, `mark`, `forget`), after their own refusals.
- A `python-entry` is installed as a **zipapp**: one file holding the entry as `__main__.py`, and every repository package it imports (the static import closure); nothing else. Same path, same mode, shebang `#!/usr/bin/python3 -I`; its bytes are reproducible. Anything that compares an installed copy with the repository compares with these built bytes (`installer.py payload SRC`, `cst_core.adapters.runtime.install_payload`), never with the source file. A `*.in` source is a template: `--set KEY=VALUE` fills its `@KEY@`.
- A new installable file is added to the manifest in the same change (`tests/test_distribution.py` fails otherwise); a file that only the checkout reads goes to `CHECKOUT_ONLY_FILES` with the reason.
- The copy that runs on the machine can therefore be older than the repository. A change to a helper reaches the machine only when the module's `install` runs again.

## Package layout

Python packages follow the Hexagonal Architecture + DDD layout
(`domain.py` / `domain/` for pure rules and frozen `slots=True` dataclasses,
`ports.py` / `ports/` for `Protocol` boundaries, `application.py` for use
cases, `adapters.py` / `adapters/` for host I/O and subprocesses, `cli.py`
for a program's commands). Only swap, doctor, `cst_core` and btrfs's
bulk, docker-gc, docker-volumes-nocow and snapshot have an application layer
with ports; in the other packages `cli.py` orchestrates the commands and
still does some I/O itself. `tests/test_architecture.py`
enforces the import and dataclass invariants, and `CLI_CEILINGS` there caps
each `cli.py` so it only shrinks: new logic goes into `domain` (pure),
`application` (with ports) or `adapters` (I/O). The I/O left outside the
adapters has a budget per package and layer that only goes down
(`IO_BUDGETS`, see "Static gates beyond lint and types" below).

## The composition root: `wiring.py`

In a package with an application layer, one module puts the program
together: `<pkg>/wiring.py`. It is the only place that

- reads the process environment (`os.environ`: `CST_PROG`, what a shell
  wrapper hands over, the test overrides), and turns it into values for the
  use cases (a frozen settings value, or plain arguments);
- looks up the program itself: `sys.argv[0]`, `sys.executable`, its own
  path (to run it again);
- re-runs the program as another user: the `sudo` or `runuser` re-exec
  (in cst_core, running a script again as root or as `SUDO_USER` is the use
  case itself: `CstDispatcher` and the module router's `ModuleRouter` build
  those command lines in the application layer, and the port execs them);
- imports the package's adapters (and cst_core's) and builds them.

`cli.py` only parses argv, calls `wiring` and one use case, and renders
the answer (with the presenter). A function in `cli.py` that tests call
with fakes (`execute_cli(..., runner=, ctx=, elevate=)`) takes ports; when
the caller passes none, it asks `wiring` for the real ones.

`tests/test_architecture.py` enforces it for the packages in
`HEXAGONAL_PACKAGES` (swap, doctor, cst_core, `btrfs/bulk`, `btrfs/docker_gc`,
`btrfs/docker_volumes_nocow` and `btrfs/snapshot` today): their `cli.py` and
`presenter.py` do no I/O and import no adapter, and only `wiring.py` and
the adapters import one. `WIRING_ALLOWED` lets a `wiring.py`, and no other
module, use `os.environ`, `sys.executable` and `os.execvp` without them
counting as I/O; anything else it does (files, commands) counts in its
package's `IO_BUDGETS` like everywhere else. cst_core's `wiring.py` serves
its programs: `cst` (`cli.py`), the installer and the module router
(`route.py`). Every package converted from now on follows the same split
and joins `HEXAGONAL_PACKAGES`.

## Most helpers are Python scripts with no `.py` extension

Examples: `btrfs/btrfs-bulk`, `vm/cst-vm`, `doctor/cst-doctor`, `toolchains/cst-toolchains`.

- Tests load them with `tests/script_loader.py` (`load_script(path, name)`), which registers the module in `sys.modules` before running it (`@dataclass` needs that on Python 3.14) and returns a fresh module each call.
- Tests of a converted program import its package. A test written against the old one-file namespace can use `tests/package_view.py` (`PackageView(cli, adapters, domain)`): a name set on it reaches every module that uses it, and `restore()` puts the real ones back.
- Tests call the functions directly, with fakes for `virsh`, `systemctl`, sudo, restic and similar tools.

## Entry points only wire argv; the logic lives once, in the package

- An extension-less Python program holds a docstring, the bootstrap, imports, one `main()` and the `__main__` guard (`tests/test_entrypoints.py`; `NOT_YET_THIN` is empty: every program is converted).
- **The bootstrap** is one stanza, byte for byte the same in every entry point that imports CST code (`BOOTSTRAP` in `tests/test_entrypoints.py`): from a checkout it puts the checkout root on `sys.path` (under `python3 -I` a script sees only its own folder); inside an installed zipapp it does nothing. Nothing else changes `sys.path`.
- Paths are never computed from `__file__` by hand: `cst_core.adapters.runtime` gives the checkout root, the program's own path and argv (to run itself again), a sibling program (checkout path, or its installed dest), and the installed bytes; `undo_python_start` undoes what Python changes at start (SIGPIPE ignored, LC_CTYPE=C.UTF-8 under a C locale) in `cst`, `cst swap` and the installer, whose shell wrappers hand the caller's LC_CTYPE over as `CST_LC_CTYPE`.
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
  - a package in `HEXAGONAL_PACKAGES` has a `cli.py` and a `presenter.py` with no I/O at all, and only its composition point (`wiring.py`) and its adapters import an adapter; a package that meets this is added (the test says so); `WIRING_ALLOWED` is what a `wiring.py` may use besides (see "The composition root" above);
  - no module imports a private name (`_x`) from another module of the repository;
  - each shared primitive has an owner in cst_core (`PRIMITIVE_OWNERS`: `subprocess`, `os.replace`, `os.fsync`, temporary files, `fcntl.flock`, the NOCOW ioctl), and so do the literals that betray a copy of one (`PRIMITIVE_LITERALS`); a call or a literal outside its owner counts against its package's budget (`PRIMITIVE_BUDGETS`, `LITERAL_BUDGETS`), docstrings aside;
  - every public top-level name of cst_core's domain, ports, adapters and application has a production user outside its file (`CORE_NAMES_WITHOUT_USER`, with reasons, only shrinks).
- Budgets (`tests/budgets.py`) are counts per package and layer that only go down: a count above its entry fails, and a count below it fails too until the lower number is written down, so that what was removed cannot come back. A key missing from a table is 0. A failing gate prints the measured table to paste in.
- `tests/test_distribution.py`: the manifest and the repository agree; every python-entry zipapp holds what it imports (AST import closure) and loads no code by path; modes fit the runner; entries with a known help mode run it from `/` with `/usr/bin/python3 -I`; sourced shell files exist; every shell file of the checkout (`tests/shell_files.py`, the list `invowk cmd test lint` runs bash -n and shellcheck on) is an installed shell program, a setup entry point or sourced library, a checkout-only executable or an E2E script (`SHELL_TEST_SCRIPTS`), and parses. Its tier 1 twin (`test_11_...` in `tests/e2e/suites/suite_tier1_systemd.py`) installs the whole manifest for real and runs every entry as root and as a user.
- `tests/test_unit_contract.py`: every unit CST installs or generates (`tests/unit_files.py`) names, in its `Exec*=` lines, only programs the manifest installs (kind python-entry or shell, with a mode its runner can run), and in its `Condition*=`/`Assert*=` lines only files the manifest installs, for every path under `/usr/local/` or `%h/.local/`. Tier 1's `test_16` runs `systemd-analyze verify` and `security` on the same units.
- `tests/test_bytecode.py`: every `python3 -I` that starts a CST program from the checkout (setup scripts, Python argv lists) passes `-B`, so no `__pycache__` lands in the checkout (as root it would be root's and block `git clean`); `-I -c` and `-I -` (inline code) are exempt.
- `tests/test_docs.py`: every relative link and anchor in the Markdown files resolves; every document is in the AGENTS.md index; every skill in `.agents/skills` has a SKILL.md with its name and a description, every rule a description; `.claude/rules` and `.claude/skills` link to `.agents`.
- `tests/test_license_headers.py`: every file of the checkout carries `SPDX-License-Identifier: MPL-2.0` once, in its comment syntax, on line 1 or right after a line that must stay first (shebang, `# shellcheck shell=`, XML declaration, `# syntax=`, Markdown frontmatter); the rules and `LICENSE_EXEMPT` (each entry matches a file and gives its reason) live in `tests/license_headers.py`, which also inserts the line (`invowk cmd fix license`). It also checks that the help printed from a script's header comment shows no license line and that every `.in` template renders with the line in its destination's syntax.
- `tests/test_duplication.py`: a ratchet on copied code (Python type 1 and type 2 clones, shell clones, the same top-level name defined in several files, the same shell function defined in several shell files: `ALLOWED_SHELL_SAME_NAMES` holds each allowed one with its reason). Its `CEILINGS` only go down: lower a number in the change that removes copies. The entry bootstrap and the bodies of `Protocol` classes are the same by design and are not counted; type 2 skips lines of literals only (type 1 still counts them). A name in `ALLOWED_SAME_NAMES` carries its reason, and an entry that is no longer defined twice fails.

## Toolbox vs host

The scripts detect a container through `/run/.containerenv` or `/.dockerenv`. In Python
one class does it, `HostExecutionContext` (`cst_core.adapters`); the `cst` dispatcher,
doctor's `Host` and toolchains ask it (`PRIMITIVE_LITERALS` keeps the markers out of
every other module).

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
