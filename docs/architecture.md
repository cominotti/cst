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

- The bash entry point `<module>-setup.sh` handles plan/apply/install/uninstall/status. Four are shims over Python:
  `swap-setup.sh` runs `swap/cli.py`; `cpu-setup.sh` runs `cpu/cpu-setup` (the `cpu.setup` package: its
  help from the script's header, cpu-watch for watch/report/quiet, the toolbox refusal and the sudo re-run of
  the script, then the root commands, which run the installer, systemctl and the checkout's programs as the bash
  script did); `vm-setup.sh` and `toolchains-setup.sh` run `cst_core/route.py`, which
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
- **State files shared between copies are registered** in `cst_core/domain/contracts.py`: one row of its table (a `StateFormat`, looked up with `state_format(key)`) per file that one CST program writes and another reads (the pending alerts, the stall records and queue, the bulk journal, docker-gc's state, the VM backup index, the install record, the game profile's files), with its path, the version this checkout writes, the repository files that write and read it, and where the version is kept. The writer and the reader are often different copies (a unit runs the installed one, `cst` the checkout), so a reader handles every older version (no version field is version 0). Python writers and readers import the path and version from there; shell programs keep their literal, checked by tests that run them. Paths of installed programs (btrfs-notify, the health check, the toolchains helper) come from the manifest's destinations, never a copied literal.

## Package layout

Python packages follow Hexagonal Architecture with a pure core, in one of
two profiles (next section): `domain.py` / `domain/` for pure rules and
frozen `slots=True` dataclasses, `adapters.py` / `adapters/` for host I/O
and subprocesses, `cli.py` / `cli/` for a program's commands, and, in the full
profile only, `ports.py` / `ports/` for `Protocol` boundaries,
`application.py` / `application/` for use cases, `wiring.py` and `presenter.py`. Only swap,
doctor, `cst_core` and btrfs's bulk, docker-gc, docker-volumes-nocow and
snapshot have the full profile (vm is on its way there: it has its `wiring.py`,
`ports.py` and `application.py`, and the local and NAS backups still run in
`vm/cli/`); in the other packages `cli.py` orchestrates the commands and some
still do I/O there. `tests/test_architecture.py`
enforces the import and dataclass invariants, and `CLI_CEILINGS` there caps
each `cli.py`: new logic goes into `domain` (pure),
`application` (with ports) or `adapters` (I/O). The I/O left outside the
adapters has a budget per package and layer that it may not go above
(`IO_BUDGETS`, see "Static gates beyond lint and types" below).

## Package profile

"Hexagonal + DDD" in CST means a pure functional core (functions and frozen
dataclasses) with the I/O at the edges, not a domain-model framework. Each
package has the light profile unless a layer of the full one pays for itself.

**Light profile, the default.** Reference package: `btrfs/chrome_cache`.

- `domain.py`: pure rules and value types (frozen dataclasses, `TypedDict`
  for JSON shapes, `NewType` where a value crosses a boundary). No I/O.
- `adapters.py`: the I/O, through the cst_core primitives (runner, files,
  locks, journal, systemd, virsh; see
  [reuse-primitives](../.agents/rules/reuse-primitives.md)).
- `cli.py`: parse argv, call one function, print, return the exit status.
  Its `main` is the composition root: it builds the adapters and may read
  the environment; it does no other I/O of its own.
- Functions first; a class only when it carries state across steps.
- Tests fake cst_core's `CommandRunnerPort` or `FileSystemPort`, or the
  adapter functions, instead of a port of the package's own.

`bluetooth` follows it too: `cli.py` holds the command lines of `bt-helper`
and `bt-ldac-watch`, with no I/O of its own; the watcher's loop (`pactl
subscribe`, the profile bounce, the stop signal) is in `bluetooth/adapters.py`;
the rules in `bluetooth/domain/`, split by program (`__init__.py` for
`bt-helper`, `watch.py` for the watcher, which imports from `__init__.py`
the JSON of `pw-dump`, `pactl` and `busctl` as `TypedDict`s and the parts of
it both read). Its `ports.py` predates the profile: `cli.main`
and the watcher are typed against `BluetoothHostPort` and `LiveAudioPort`;
the tests pass a fake host to `cli.main` and put a fake in `Watcher.live`.

`cpu` follows it too: its `cli.py` does no I/O of its own. The
recording loop of `cpu-watch watch`, vm-pin's virsh calls and saved
definition are in `cpu/adapters.py`; the rules and records in
`cpu/domain.py`; the `quiet` loop stays in `cli.py` with its clock, pause
and sampler passed in, and the apply prompt is cst_core's `confirm`. The
game profile (`cpu-game-profile`) is a light package of its own,
`cpu/game`: the records of `game.state` and `game.intended` (written as
bash's `printf %q` writes them, since an installed bash copy of an older CST
reads them) and the drift rules in `domain.py`; `GameProfile` in
`adapters.py` carries the paths, the settings and the runner across the
steps of `on`, `off`, `tick` and `state`. `cpu/setup` (`cst cpu`'s own
commands) has the full profile's `wiring.py` only: it reads what the shim
hands over and the environment and re-runs the script through sudo;
`CpuSetup` in its `adapters.py` carries the paths, the runner, the
checkout and the target user across the steps of install, status and
uninstall.

**Full profile: add a layer only when it pays.** Reference package:
`btrfs/docker_volumes_nocow` (a migration with rollback).

- `application.py`: only for a use case with two or more steps that change
  the machine and must roll back or resume (journals, twins, renames,
  multi-step installs).
- `ports.py`: the `Protocol`s `application.py` is typed against
  (application never imports adapters). Keep them few and shaped as
  boundaries (how the machine is reached: docker, the volume tree, the state
  store), never one method per shell command. Where no test needs a fake of
  its own, type the use case with cst_core's `CommandRunnerPort` /
  `FileSystemPort` and fake those.
- `wiring.py`: only when the program reads its environment and also re-runs
  itself (sudo, runuser); otherwise the construction stays in `cli.py`'s
  `main`.
- `presenter.py`: only when there are two or more output formats (text and
  `--json`).

`toolchains` is on its way to the full profile: `prune` renames folders away
and an interrupted run leaves leftovers the next one deletes, and the
program re-runs itself on the host from a toolbox, so it has
`application/` (the prune use case), `ports.py` and `wiring.py`. Its `auto`,
`config` and `status` commands still do their I/O in `cli/`; it joins
`HEXAGONAL_PACKAGES` once they are use cases too.

**What the gates check in each profile.**

- Every package: no I/O in a `domain` module (also in `application`),
  frozen slotted dataclasses in `domain`, no outer-layer imports from
  `domain`, `ports` or `application`, a role for every module, the
  `cli.py` size (`CLI_CEILINGS`), the I/O outside adapters
  (`IO_BUDGETS`), the module size cap and the duplication measure.
- Light: `cli.py` may import adapters and read the environment
  (`LIGHT_CLI_ALLOWED`: `os.environ`, `os.getenv`; a write to the
  environment, a re-exec or `sys.executable` still counts). Nothing asks
  for ports, application, wiring or presenter.
- Full (`HEXAGONAL_PACKAGES`): `cli.py` and `presenter.py` do no I/O at
  all, only `wiring.py` and the adapters import an adapter, and only
  `wiring.py` reads the environment or re-runs the program (see "The
  composition root" below).

**Size and cohesion.** No package module is over 800 lines
(`tests/test_module_size.py`; today's larger modules are in `OVER_CAP`, each
never above its entry). Split a big module by command group or check area,
not by layer. A big `cli.py` becomes a `cli/` folder, the same way
`domain.py` becomes `domain/`: `cli/__init__.py` (the `main`) and one
`cli/<group>.py` per command group. Every file in it has the cli role and
its rules (in a light package each may read the environment, in a full one
none does I/O), and `CLI_CEILINGS` caps the folder as a whole
(`<package>/cli/`): the split moves the `<package>/cli.py` entry to that
key by hand, at most its old value, and then moving code between the files
changes nothing. Import the group modules by their full name
(`from vm.cli import net`): the import graph of the gates (`tests/code_graph.py`)
follows only absolute imports, like the rest of CST. A big `application.py`
becomes an `application/` folder: one module per use case or check area,
each with the application role and its rules. There is no folder ceiling
as for `cli/`: each module stays under the 800-line cap, and the split
deletes the `OVER_CAP` entry (`doctor/application/`: one
module per check area, and `registry.py` with the areas in their output
order, and `shared.py` with what several areas use). Its `__init__.py`
imports none of the folder's modules: the re-export gate of
`tests/test_entrypoints.py` forbids that, so callers import each module by
its full name.

**Gates and metrics.** Budgets, ceilings and allow-lists fail only when the
code is above them (the sets that must stay empty, such as `NOT_YET_CLEAN`,
and `LICENSE_EXEMPT`, each entry of which must match a file, still fail on
an entry nothing needs); `invowk cmd fix budgets` lowers them to the measured
values and never raises one. Never reshape code that moves or deletes data
only to satisfy a metric (Sonar's cognitive complexity, the duplication
measure).

## The composition root: `wiring.py`

In a package of the full profile (see "Package profile" above), one module
puts the program together: `<pkg>/wiring.py`. It is the only place that

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
(`route.py`). A package that gets an application layer follows the same
split and joins `HEXAGONAL_PACKAGES`.

A package of the light profile (not in `HEXAGONAL_PACKAGES`) has no
`wiring.py`: its `cli.py` `main` is the composition root. It builds the
adapters and may read the environment (`LIGHT_CLI_ALLOWED`: reads of
`os.environ` and `os.getenv`) without them counting as I/O. A write to
`os.environ`, the `sudo`/`runuser` re-exec and `sys.executable` still
count in its `IO_BUDGETS`; a program that re-runs itself gets a
`wiring.py` and the full profile's rule.

## Most helpers are Python scripts with no `.py` extension

Examples: `btrfs/btrfs-bulk`, `vm/cst-vm`, `doctor/cst-doctor`, `toolchains/cst-toolchains`.

- Tests load them with `tests/script_loader.py` (`load_script(path, name)`), which registers the module in `sys.modules` before running it (`@dataclass` needs that on Python 3.14) and returns a fresh module each call.
- Tests of a converted program import its package. A test written against the old one-file namespace can use `tests/package_view.py` (`PackageView(cli, adapters, domain)`): a name set on it reaches every module that uses it, and `restore()` puts the real ones back.
- Tests call the functions directly, with fakes for `virsh`, `systemctl`, sudo, restic and similar tools.

## Entry points only wire argv; the logic lives once, in the package

- An extension-less Python program holds a docstring, the bootstrap, imports, one `main()` and the `__main__` guard (`tests/test_entrypoints.py`; `NOT_YET_THIN` is empty: every program is converted).
- **The bootstrap** is one stanza, byte for byte the same in every entry point that imports CST code (`BOOTSTRAP` in `tests/test_entrypoints.py`): from a checkout it puts the checkout root on `sys.path` (under `python3 -I` a script sees only its own folder); inside an installed zipapp it does nothing. Nothing else changes `sys.path`.
- Paths are never computed from `__file__` by hand: `cst_core.adapters.runtime` gives the checkout root, the program's own path and argv (to run itself again), a sibling program (checkout path, or its installed dest), and the installed bytes; `undo_python_start` undoes what Python changes at start (SIGPIPE ignored, LC_CTYPE=C.UTF-8 under a C locale) in `cst`, `cst swap`, `cst cpu` and the installer, whose shell wrappers hand the caller's LC_CTYPE over as `CST_LC_CTYPE` (an installed program a unit starts, such as `cpu-game-profile`, has no wrapper: under a C locale its children see C.UTF-8).
- `main()` is a docstring and one statement that hands argv to the package.
- No re-export facades (`from m import a as a`, `__all__`, a package `__init__.py` importing its own submodules): tests import a name from the module that defines it and inject fakes through ports.

## A concept more than one module needs lives once, in `cst_core`

A module imports it instead of keeping a copy. What `cst_core` provides, need by need, and what to do
when it lacks an option: the rule [reuse-primitives](../.agents/rules/reuse-primitives.md).

## Static gates beyond lint and types

All of them run in the unit suite:

- `tests/test_architecture.py`: layer imports; frozen slotted dataclasses in `domain`; no direct I/O in `domain` or `application`, as an allow-list of what they may import and use (`PURE_MODULES`, `PURE_NAMES`, resolved through aliases and `from m import x`; the I/O builtins fail); every package module reached from an entry point (`ORPHAN_ALLOWED` and `ORPHAN_ALLOWED_FILES` are empty: no shadow modules). Also:
  - every package module has one role (`module_role`: domain, ports, application, adapters, presenter, wiring, cli or entry); the few that have none yet are in `UNLAYERED_ALLOWED` with their reason, a table nothing is added to;
  - the I/O a module does outside its adapters (the lines `direct_io_problems` reports, with print, `sys.stdout`/`stderr`/`argv`/`exit` and argparse allowed) has a budget per package and role, `IO_BUDGETS`: moving code to an adapter lowers it, moving it from `cli.py` into another module of the same package does not hide it;
  - a package in `HEXAGONAL_PACKAGES` has a `cli.py` and a `presenter.py` with no I/O at all, and only its composition point (`wiring.py`) and its adapters import an adapter; a package that meets this is added (the test says so); `WIRING_ALLOWED` is what a `wiring.py` may use besides, and `LIGHT_CLI_ALLOWED` what the `cli.py` of a light package may (see "The composition root" above);
  - no module imports a private name (`_x`) from another module of the repository;
  - each shared primitive has an owner in cst_core (`PRIMITIVE_OWNERS`: `subprocess`, `os.replace`, `os.fsync`, temporary files, `fcntl.flock`, the NOCOW ioctl), and so do the literals that betray a copy of one (`PRIMITIVE_LITERALS`); a call or a literal outside its owner counts against its package's budget (`PRIMITIVE_BUDGETS`, `LITERAL_BUDGETS`), docstrings aside;
  - every public top-level name of cst_core's domain, ports, adapters and application has a production user outside its file (`CORE_NAMES_WITHOUT_USER`, with reasons; nothing is added to it).
- Budgets (`tests/budgets.py`) are counts per package and layer, never above their entry: a count above its entry fails, a count at or below it passes. A key missing from a table is 0. The same holds for every ceiling and allow-list of the gates (`CLI_CEILINGS`, the duplication `CEILINGS`, `UNLAYERED_ALLOWED`, `CORE_NAMES_WITHOUT_USER`, `ALLOWED_SAME_NAMES`, `ALLOWED_SHELL_SAME_NAMES`): only what is over them, or missing from them, fails. So a change that removes something does not have to edit a table, and parallel changes do not conflict on the numbers. `invowk cmd fix budgets` (`tests/fix_budgets.py`; a dry run, `--apply` writes) measures every table with the function its gate uses and lowers it to the measured values: numbers go down, a budget entry at 0 and an allow-list entry nothing needs any more are deleted; it never raises a number or adds an entry. A failing gate prints the measured table. (User decision 2026-11-17; before it, a count below its entry failed too.)
- `tests/test_distribution.py`: the manifest and the repository agree; every python-entry zipapp holds what it imports (AST import closure) and loads no code by path; modes fit the runner; entries with a known help mode run it from `/` with `/usr/bin/python3 -I`; sourced shell files exist; every shell file of the checkout (`tests/shell_files.py`, the list `invowk cmd test lint` runs bash -n and shellcheck on) is an installed shell program, a setup entry point or sourced library, a checkout-only executable or an E2E script (`SHELL_TEST_SCRIPTS`), and parses. Its tier 1 twin (`test_11_...` in `tests/e2e/suites/suite_tier1_systemd.py`) installs the whole manifest for real and runs every entry as root and as a user.
- `tests/test_unit_contract.py`: every unit CST installs or generates (`tests/unit_files.py`) names, in its `Exec*=` lines, only programs the manifest installs (kind python-entry or shell, with a mode its runner can run), and in its `Condition*=`/`Assert*=` lines only files the manifest installs, for every path under `/usr/local/` or `%h/.local/`. Tier 1's `test_16` runs `systemd-analyze verify` and `security` on the same units.
- `tests/test_bytecode.py`: every `python3 -I` that starts a CST program from the checkout (setup scripts, Python argv lists) passes `-B`, so no `__pycache__` lands in the checkout (as root it would be root's and block `git clean`); `-I -c` and `-I -` (inline code) are exempt.
- `tests/test_docs.py`: every relative link and anchor in the Markdown files resolves; every document is in the AGENTS.md index; every skill in `.agents/skills` has a SKILL.md with its name and a description, every rule a description; `.claude/rules` and `.claude/skills` link to `.agents`.
- `tests/test_license_headers.py`: every file of the checkout carries `SPDX-License-Identifier: MPL-2.0` once, in its comment syntax, on line 1 or right after a line that must stay first (shebang, `# shellcheck shell=`, XML declaration, `# syntax=`, Markdown frontmatter); the rules and `LICENSE_EXEMPT` (each entry matches a file and gives its reason) live in `tests/license_headers.py`, which also inserts the line (`invowk cmd fix license`). It also checks that the help printed from a script's header comment shows no license line and that every `.in` template renders with the line in its destination's syntax.
- `tests/test_module_size.py`: no package module is over `MODULE_LINE_CAP` (800) physical lines; `OVER_CAP` names today's larger modules with their size, never above (`invowk cmd fix budgets` lowers an entry and deletes it once the module is at the cap or under it). A module that grows past the cap is split by command group or check area, not by layer ("Package profile" above).
- `tests/test_duplication.py`: a ratchet on copied code (Python type 1 and type 2 clones, shell clones, the same top-level name defined in several files, the same shell function defined in several shell files: `ALLOWED_SHELL_SAME_NAMES` holds each allowed one with its reason). Its `CEILINGS` fail only when a measurement is above them; `invowk cmd fix budgets` lowers them. The entry bootstrap and the bodies of `Protocol` classes are the same by design and are not counted; type 2 also leaves out declarations (the fields of a `@dataclass`, `TypedDict` or `NamedTuple`, and an `__init__` that only stores its parameters) and lines of literals only (type 1 still counts both). A left-out block is a barrier: no window runs across it. A name in `ALLOWED_SAME_NAMES` carries its reason; an entry that is no longer defined twice passes, and `invowk cmd fix budgets` deletes it.

## Toolbox vs host

The scripts detect a container through `/run/.containerenv` or `/.dockerenv`. In Python
one class does it, `HostExecutionContext` (`cst_core.adapters.runner`); the `cst` dispatcher,
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

- Knobs: `/etc/cst/vm.conf` and `/etc/cst/swap.conf` (root; swap's partition, no default) and `~/.config/cst/toolchains.conf` (user). The defaults are in the scripts and in `toolchains/toolchains.conf`.
- The VM backup index is `/var/lib/cst/vm-backups.json`.
- User units go in `~/.config/systemd/user`.
- "Adapting CST to another PC" in README.md lists what is specific to this machine.
