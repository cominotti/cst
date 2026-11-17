# Working on CST (for agents and people)

CST changes the workstation it runs on: boot, btrfs, systemd units, cgroups,
Bluetooth, toolchain caches. These rules keep changes safe and the repository
consistent.

## Rules

- **Repository text is English and plain.** Code, comments, READMEs, commit
  messages. Write for someone who is not a kernel or btrfs expert.
- **Docs follow the code in the same change.** A behaviour change updates the
  module's README (see the index below) in the same commit.
- **plan before apply.** Every command that changes the machine has a dry run
  (`plan`, `status`, or a list mode) that changes nothing. Root commands call
  sudo by themselves and refuse to run inside a toolbox; they print the host
  command instead.
- **Every change comes with tests that would catch its regression.** A new
  command, option or operation, and every change in behaviour, adds or updates
  tests in the same change:
  - **Unit tests**, with the fakes and stubs the existing tests use. They cover
    the normal path, the failure and edge cases, and the bug being fixed. A
    test for a fix must fail without the fix. Assert on behaviour (output,
    files written, commands called), not on the source text.
  - **E2E tests** (`tests/e2e`) for every new command or operation and every
    behaviour change that touches the machine. Above all, they cover what a
    fake cannot prove: how the real systemd, cgroups, btrfs, udev, Polkit or
    another tool merges, orders or applies a setting, for example drop-in
    order across `daemon-reload`. Use tier 1 when the systemd container can
    host the check, and tier 2 for disks, btrfs and cgroup controllers. Only
    pure logic with no effect on the machine (parsing, report wording and
    numbers) may rely on unit tests alone.
  - For anything that changes the machine, cover the whole cycle:
    - `plan` changes nothing;
    - `apply` does what `status` then shows;
    - a second run changes nothing more;
    - undo or `uninstall` restores the previous state;
    - an upgrade from the previous version (old files, old state formats)
      works.
  - The pull request lists the tests for each change and says how "fails
    without the fix" was checked (for example: run against the old code).
  - A test that cannot run locally (tier 2 on a workstation, or tier 1 when its
    container does not start) is still written. Say so in the pull request; CI
    runs it before the merge. Never skip a test, mark it as an expected failure
    or loosen it to get a green run.
  - A change with no behaviour change (docs, comments) needs no new tests; the
    pull request says so.
- **Tests never touch the real host.** Unit tests use no sudo, no real systemd
  and no real libvirt, and write only in temporary folders. E2E tests run only
  inside their containers (tier 2 only in CI or on a disposable VM).
- **btrfs subvolumes only generic and reusable.** A new subvolume must serve
  many programs and projects, like `~/.cache` or `~/Bulk`. Never create one
  for a single app or project (for example a cargo `target/`, one IDE, one
  checkout). Prefer redirecting the data into an existing generic one, or
  marking it as a cache (`CACHEDIR.TAG`, which the snapshots drop). Each
  subvolume is one more thing to convert, list, back up and undo.
- **Never walk `/`** (`find /`, `fd . /`, ...). Inside a toolbox `/run/host`
  is the whole host, including network shares.
- **No fixed checkout path.** CST works from wherever it is cloned: the
  scripts find their own folder at run time, and installed programs are
  copies. Never write where the checkout lives (a path from a home folder
  into the CST folder). Name files relative to the CST folder (`./cst`, "at the
  top of the CST folder", `"$PWD/cst"`) or use the `cst` command, which the
  installs link into `~/.local/bin`. `tests/test_repo_paths.py` enforces it.
- **CI is the gate.** Work on a branch and open a pull request; `main` accepts
  squash merges with a green `CI Status` (fast gates, both E2E tiers) and a
  green SonarCloud check. Never weaken a check to make it pass.

## Index: where each part is documented

This file holds the rules for the whole repository. Each module's README
holds what is specific to it: what it changes on the machine, its knobs, how
to undo it, and the reasons behind its defaults. Read the module's README
before changing that module.

| Area | Document |
|---|---|
| The `cst` command, recommended order, undo, adapting to another PC | [README.md](README.md) |
| Storage: disk health, stalls, writeback, Docker volumes, degraded boot | [btrfs/README.md](btrfs/README.md) |
| Snapshots, `~/Bulk`, NAS backup of `/home` | [btrfs/SNAPSHOTS.md](btrfs/SNAPSHOTS.md) |
| CPU: game profile, VM/toolbox/Docker priority, E-cores | [cpu/README.md](cpu/README.md) |
| Swap and memory | [swap/README.md](swap/README.md) |
| Bluetooth audio | [bluetooth/README.md](bluetooth/README.md) |
| Toolchain caches, cargo/uv settings, sccache wrapper | [toolchains/README.md](toolchains/README.md) |
| The libvirt VM: backups, virtual hardware, NOCOW folders | [vm/README.md](vm/README.md) |
| `cst doctor` checks | [doctor/README.md](doctor/README.md) |
| Docker E2E tests and the tier 2 opt-in | [tests/e2e/README.md](tests/e2e/README.md) |
| Python dev tools: uv, ruff, ty, pinned versions, mutation testing | [docs/dev-tools.md](docs/dev-tools.md) |

## Commands

Run from the repository root.

```
python3 -I -B tests/run_unit.py -v            # all unit tests, socket as stdin (what CI runs)
python3 -B -m unittest vm.tests.test_vm -v    # one module; or vm.tests.test_vm.PartsTest.test_disks_nvram_tpm
invowk cmd test lint                          # bash -n + shellcheck -S warning (falls back to the devbox toolbox)
invowk cmd test pylint                        # ruff check + ruff format --check on every Python file (through uv)
invowk cmd test types                         # ty on the ty gate list + annotations on the typed list (through uv)
invowk validate cst.invowkmod && invowk audit cst.invowkmod
invowk cmd test e2e tier1                     # Docker, Fedora 45 + systemd; needs no opt-in
invowk cmd test mutation swap/domain.py swap.tests.test_swap --functions share  # optional (see below)
```

- Before a pull request, run the first line and the third to sixth, and
  `test e2e tier1` when the change adds or touches a tier 1 E2E test (if its
  container does not start, say so in the pull request).
- `test mutation` is optional and slow, and neither `test all` nor CI runs
  it. It changes a Python helper on purpose, one small change at a time, on
  a throwaway copy inside a bubblewrap sandbox, and reports the changes no
  test notices. It needs uv, `bwrap` and git, and refuses to run without
  `bwrap`. See [docs/dev-tools.md](docs/dev-tools.md), "Mutation testing".
- The unit tests need no uv: they run on `/usr/bin/python3 -I` with the
  standard library, like the helpers. Only `test pylint`, `test types` and
  `test mutation` use uv, which installs the pinned ruff and ty (and
  cosmic-ray, for `test mutation`) from `uv.lock` into a
  virtual environment under `~/.cache`, never inside the checkout. Run a
  tool by hand with `tests/uv-dev`, for example `tests/uv-dev ruff check --fix`,
  or `tests/uv-dev ruff format` when the format check fails.
  See [docs/dev-tools.md](docs/dev-tools.md).
- A new Python helper without a `.py` extension has to be added to
  `extend-include` in `pyproject.toml`. Until then ruff does not see it, and
  `tests/test_python_files.py` fails.
- Every Python file in the repository, tests included, must be ty-clean and
  strictly annotated, listed in both `[tool.ty.src] include` and the
  `!{...} = ["ANN"]` typed entry in `pyproject.toml`. `NOT_YET_CLEAN` in
  `tests/test_python_files.py` is now empty (`frozenset()`), and nothing may be
  added back to it. No ignore comments (`ty: ignore`, `type: ignore`) and no
  `[tool.ty]` settings that exclude files or switch rules off: fix what ty
  reports in the code. The test fails on each.
- Python packages follow the Hexagonal Architecture + DDD layout
  (`domain.py` / `domain/` for pure rules and frozen `slots=True` dataclasses,
  `ports.py` / `ports/` for `Protocol` boundaries, `application.py` for use
  cases, `adapters.py` / `adapters/` for host I/O and subprocesses, `cli.py`
  for a program's commands). Only swap, doctor and `cst_core` have an
  application layer with ports; in the other packages `cli.py` orchestrates
  the commands and still does some I/O itself. `tests/test_architecture.py`
  enforces the import and dataclass invariants, and `CLI_CEILINGS` there caps
  each `cli.py` so it only shrinks: new logic goes into `domain` (pure) or
  `adapters` (I/O).
- A single-module run doesn't go through `tests/run_unit.py`. It skips the socket-stdin gate (see "Test gotchas"), so the full suite still has to pass.
- A new shell entry point has to be added to the `test lint` script list in `cst.invowkmod/invowkfile.cue`. Until then it isn't linted.
- Don't run E2E tier 2 on a workstation. It starts a `--privileged` container that can see the machine's disks (`CST_E2E_ALLOW_PRIVILEGED=1`). CI runs it.
- CI runs the unit tests as a non-root user named `ci`. Tests must not depend on the developer's home or groups.

## How the code fits together

- **`cst` only dispatches.** `cst` delegates to `cst_core/cli.py`
  (`CstDispatcher`, backed by `cst_core/domain/dispatch.py` and `CstDispatchPort`).
  - `cst MODULE ...` runs `<module>-setup.sh` with `CST_PROG="cst MODULE"` so that help text reads `cst MODULE`.
  - The module metadata (`MODULES`, `MODULE_BY_NAME`) lives only in `cst_core/domain/dispatch.py`; `cst` itself is a short bash wrapper that runs `cst_core/cli.py`.
  - `cst doctor` runs `doctor/cst-doctor`. `cst status` loops over every module.
- **Each module has two parts.**
  - The bash entry point `<module>-setup.sh` handles plan/apply/install/uninstall/status.
  - The parts it installs live in the `<module>/` folder: helper programs, systemd `units/`, `sysctl/`, `tmpfiles/` and udev rules.
  - **`INSTALL_MANIFEST` (`cst_core/domain/install.py`) is the one list of installed files**: one row per file in a text table (source, destination, mode, owner, kind and who runs it), read by `parse_manifest`. A setup script never copies a file itself: it sources `cst-install.bash` and calls `cst_install SRC...` (root files), `cst_install_as USER SRC...` (files in a home, written by a process running as that user) or `cst_install_text DEST MODE [USER]` (generated text; `btrfs/btrfs-degraded-boot-setup.sh` writes its generated files this way too). They run `cst_core/installer.py` on `/usr/bin/python3`, which writes a file only when its content or mode changed, atomically, and prints `unchanged: DEST` or `written:   DEST`. `installer.py plan` shows the same without writing. Destinations come from the manifest only (there is no override); every destination and the staging `--root` must be plain absolute paths (`cst_core.domain.files.checked_path`). The few paths a setup script still keeps in a variable for `status` and `uninstall` (`SYSCTL_FILE`, `PENDING_READER`, ...) must name the same files, and the lists of programs to remove come from `cst_installed_dests`.
  - A `python-entry` is installed as a **zipapp**: one file holding the entry as `__main__.py`, and every repository package it imports (the static import closure); nothing else. Same path, same mode, shebang `#!/usr/bin/python3 -I`; its bytes are reproducible. Anything that compares an installed copy with the repository compares with these built bytes (`installer.py payload SRC`, `cst_core.adapters.runtime.install_payload`), never with the source file. A `*.in` source is a template: `--set KEY=VALUE` fills its `@KEY@`.
  - A new installable file is added to the manifest in the same change (`tests/test_distribution.py` fails otherwise); a file that only the checkout reads goes to `CHECKOUT_ONLY_FILES` with the reason.
  - The copy that runs on the machine can therefore be older than the repository. A change to a helper reaches the machine only when the module's `install` runs again.
- **Most helpers are Python scripts with no `.py` extension.** Examples: `btrfs/btrfs-bulk`, `vm/cst-vm`, `doctor/cst-doctor`, `toolchains/cst-toolchains`.
  - Tests load them with `tests/script_loader.py` (`load_script(path, name)`), which registers the module in `sys.modules` before running it (`@dataclass` needs that on Python 3.14) and returns a fresh module each call.
  - Tests of a converted program import its package. A test written against the old one-file namespace can use `tests/package_view.py` (`PackageView(cli, adapters, domain)`): a name set on it reaches every module that uses it, and `restore()` puts the real ones back.
  - Tests call the functions directly, with fakes for `virsh`, `systemctl`, sudo, restic and similar tools.
- **Entry points only wire argv; the logic lives once, in the package.**
  - An extension-less Python program holds a docstring, the bootstrap, imports, one `main()` and the `__main__` guard (`tests/test_entrypoints.py`; `NOT_YET_THIN` is empty: every program is converted).
  - **The bootstrap** is one stanza, byte for byte the same in every entry point that imports CST code (`BOOTSTRAP` in `tests/test_entrypoints.py`): from a checkout it puts the checkout root on `sys.path` (under `python3 -I` a script sees only its own folder); inside an installed zipapp it does nothing. Nothing else changes `sys.path`.
  - Paths are never computed from `__file__` by hand: `cst_core.adapters.runtime` gives the checkout root, the program's own path and argv (to run itself again), a sibling program (checkout path, or its installed dest), and the installed bytes.
  - `main()` is a docstring and one statement that hands argv to the package.
  - No re-export facades (`from m import a as a`, `__all__`, a package `__init__.py` importing its own submodules): tests import a name from the module that defines it and inject fakes through ports.
- **A concept more than one module needs lives once, in `cst_core`**; a module imports it instead of keeping a copy:
  - domain: `errors` (`Usage`, `Refused`, `shell_exit_status`: 128+N for a child killed by signal N), `text` (`first_line`, `is_yes`, `file_lines` for lines as bash's `read -r` sees them, `parse_assignments` for KEY=VALUE settings files), `libvirt_xml` (`keep_prefixes`), `values` (sizes: `GIB`, `MIB`, `ByteSize`, `format_bytes_*`; durations; CPU sets), `files` (managed lines and files, `owned_file_state`, `checked_path`), `units` (`render_unit_file`, `render_timer`), `install` (the manifest), `imports` (AST import analysis);
  - ports and adapters: commands (`CommandRunnerPort`), files (`FileSystemPort`), host vs toolbox (`HostExecutionContext`), systemd (`SystemdPort`), libvirt (`VirshPort`, `SubprocessVirshAdapter`), `adapters.files` (`read_text`, `read_int`, `read_bytes`), `adapters.journal.log`, `adapters.locks.take_lock`, `adapters.nocow` (`set_nocow`, `inode_flags`), `adapters.runtime` (also `restore_default_signals`: SIGPIPE back to its default in `cst`, the installer and `cst swap`; `restore_lc_ctype`: the caller's LC_CTYPE back for the modules `cst` runs), `adapters.dispatch` (how `cst` runs a module: `exec`, or in the foreground with bash's signal rules), `adapters.zipapp`;
  - application: `install` (the installer's use case).
- **Static gates beyond lint and types** (all in the unit suite):
  - `tests/test_architecture.py`: layer imports; frozen slotted dataclasses in `domain`; no direct I/O in `domain` or `application`, as an allow-list of what they may import and use (`PURE_MODULES`, `PURE_NAMES`, resolved through aliases and `from m import x`; the I/O builtins fail); every package module reached from an entry point (`ORPHAN_ALLOWED` and `ORPHAN_ALLOWED_FILES` are empty: no shadow modules).
  - `tests/test_distribution.py`: the manifest and the repository agree; every python-entry zipapp holds what it imports (AST import closure) and loads no code by path; modes fit the runner; entries with a known help mode run it from `/` with `/usr/bin/python3 -I`; sourced shell files exist. Its tier 1 twin (`test_11_...` in `tests/e2e/suites/suite_tier1_systemd.py`) installs the whole manifest for real and runs every entry as root and as a user.
  - `tests/test_bytecode.py`: every `python3 -I` that starts a CST program from the checkout (setup scripts, Python argv lists) passes `-B`, so no `__pycache__` lands in the checkout (as root it would be root's and block `git clean`); `-I -c` and `-I -` (inline code) are exempt.
  - `tests/test_duplication.py`: a ratchet on copied code (Python type 1 and type 2 clones, shell clones, the same top-level name defined in several files). Its `CEILINGS` only go down: lower a number in the change that removes copies. The entry bootstrap and the bodies of `Protocol` classes are the same by design and are not counted; type 2 skips lines of literals only (type 1 still counts them). A name in `ALLOWED_SAME_NAMES` carries its reason, and an entry that is no longer defined twice fails.
- **Toolbox vs host.** The scripts detect a container through `/run/.containerenv` or `/.dockerenv`.
  - Inside one, read-only commands still work, sometimes by moving to the host with `flatpak-spawn --host`.
  - Root commands refuse to run there. Keep that split when adding commands.
- **`doctor/cst-doctor` is a read-only check across all modules.** It prints ok/info/WARN/ERROR lines.
  - New module behaviour that can go wrong usually also needs a doctor check, documented in `doctor/README.md`.
- **CI** (`.github/workflows/ci.yml`) runs the same `invowk cmd ...` tasks.
  - `invowk` is pinned by SHA256 and verified with cosign in `.github/actions/setup-invowk`.
  - Change the version with `invowk cmd ci pin-invowk <tag>`, not by editing the hashes.
- **Machine-specific state lives outside the repository.**
  - Knobs: `/etc/cst/vm.conf` (root) and `~/.config/cst/toolchains.conf` (user). The defaults are in the scripts and in `toolchains/toolchains.conf`.
  - The VM backup index is `/var/lib/cst/vm-backups.json`.
  - User units go in `~/.config/systemd/user`.
  - "Adapting CST to another PC" in README.md lists what is specific to this machine.

## Test gotchas

### bash reads ~/.bashrc when stdin is a socket

bash treats "SHLVL unset and stdin is a socket" as a start by sshd/rshd and
reads `~/.bashrc`, even for `bash -c`. Tests that run bash with a clean
environment (`env={"PATH": ...}`) have no SHLVL, and they inherit stdin from
whoever started the run. Agents and task runners sometimes give that run a
socket. The developer's `~/.bashrc` then prints into the test's stderr, and
the test fails only in those environments.

**Rule:** a subprocess call in a test that runs bash or sh passes `stdin=`
or `input=`. Use `stdin=subprocess.DEVNULL` when the command reads nothing:

```python
subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
               stdin=subprocess.DEVNULL, check=False)
```

Two gates enforce it:

- `tests/test_subprocess_hygiene.py` (static): fails on a call to
  `subprocess.run/Popen/call/check_call/check_output` with an argument list
  that starts with bash/sh, or with `shell=True`, and no `stdin=`/`input=`.
- `tests/run_unit.py` (at run time): runs the whole suite with a socket as
  stdin, so the failure shows up on every run. `invowk cmd test unit` and CI
  use it.
