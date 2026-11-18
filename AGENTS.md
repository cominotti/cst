<!-- SPDX-License-Identifier: MPL-2.0 -->
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
- **Every file names its license.** One line,
  `SPDX-License-Identifier: MPL-2.0`, in the file's comment syntax: line 1,
  or right after a line that must stay first (a shebang, a
  `# shellcheck shell=` directive, an XML declaration, a Dockerfile
  `# syntax=` directive, a Markdown file's frontmatter). The full
  notice stays in LICENSE. `invowk cmd fix license --apply` adds the line to
  new files; `tests/test_license_headers.py` fails on a file without it.
  The few files that cannot carry it (no comment syntax, captured data) are
  in `LICENSE_EXEMPT` of `tests/license_headers.py`, each with its reason.
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
| How the code fits together: `cst` dispatch, modules, the install manifest, entry points, package layout and profile (light or full), static gates | [docs/architecture.md](docs/architecture.md) |
| What `cst_core` already provides: reuse it before you write | [.agents/rules/reuse-primitives.md](.agents/rules/reuse-primitives.md) |

## Agent rules and skills

Read the two rules in `.agents/rules/` before any change:
[reuse-primitives](.agents/rules/reuse-primitives.md) (use what `cst_core`
provides, never a copy) and
[refactor-vs-behaviour](.agents/rules/refactor-vs-behaviour.md) (one kind of
change per commit, with its proof). `.agents/skills/` holds checklists for
one kind of task (`<name>/SKILL.md`): `cst-new-command`, `cst-new-helper`,
`cst-shell-to-python` and `cst-refactor-parity`; open the one whose
description fits the task. `.claude/rules` and `.claude/skills` are links to
these two folders, for Claude Code.

## Commands

Run from the repository root.

```
invowk cmd check                              # every fast gate: validate, audit, test lint, pylint, types, workflows, unit (what CI runs)
invowk cmd test unit                          # all unit tests, socket as stdin (tests/run_unit.py)
invowk cmd test module vm.tests.test_vm       # one module, class or method, through tests/run_unit.py
invowk cmd test arch                          # the static gates only (seconds)
invowk cmd test docs                          # the docs gate only
invowk cmd test license                       # the license header gate only (tests/test_license_headers.py)
invowk cmd fix license [--apply]              # list the files without the SPDX line; --apply adds it
invowk cmd fix budgets [--apply]              # list the budget and ceiling entries the code is below; --apply lowers them
invowk cmd test lint                          # bash -n + shellcheck -S warning on every shell file (falls back to the devbox toolbox)
invowk cmd test pylint                        # ruff check + ruff format --check + ruff budgets on every Python file (through uv)
invowk cmd test types                         # ty on the ty gate list + ruff's annotation rules (ANN) on every Python file (through uv)
invowk cmd test workflows                     # zizmor (offline) on .github, exceptions in .github/zizmor.yml (through uv)
invowk cmd fmt                                # ruff's safe fixes, then ruff format
invowk cmd test e2e tier1                     # Docker, Fedora 45 + systemd; needs no opt-in
invowk cmd test mutation swap/domain.py swap.tests.test_swap --functions share  # optional (see below)
```

- Before a pull request, run `invowk cmd check`, and `test e2e tier1` when
  the change adds or touches a tier 1 E2E test (if its container does not
  start, say so in the pull request).
- Budgets and ceilings (`IO_BUDGETS`, `RUFF_BUDGETS`, `CEILINGS`, the
  allow-lists `invowk cmd fix budgets` lists, ...) fail only when the code is
  above an entry: never above; lowered with `invowk cmd fix budgets`. The
  sets that must stay empty (`NOT_YET_CLEAN`, `ORPHAN_ALLOWED`, ...) and
  `LICENSE_EXEMPT` still fail on a stale entry. A change that removes something
  need not edit them; run `invowk cmd fix budgets --apply` when you want the
  lower numbers recorded. It never raises an entry or adds one; raising one
  by hand needs its reason in the pull request.
- `tests/run_unit.py` lists every skipped test with its reason and fails on a
  skip that `EXPECTED_SKIPS` there does not name (only skips that depend on
  the machine: a tool, root, the file system). Never add a skip there to get a
  green run.
- `test mutation` is optional and slow; neither `test all` nor CI runs it
  ([docs/dev-tools.md, "Mutation testing"](docs/dev-tools.md#mutation-testing-optional)).
- The unit tests need no uv: they run on `/usr/bin/python3 -I` with the
  standard library. `test pylint`, `test types` and `test mutation` use uv,
  whose virtual environment never lives in the checkout; run a tool by hand
  with `tests/uv-dev` ([docs/dev-tools.md](docs/dev-tools.md)).
- Every Python file, tests included, is ty-clean and strictly annotated: it is
  listed in `[tool.ty.src] include` of `pyproject.toml`, and an
  extensionless helper also in `extend-include`. ruff's annotation rules
  (ANN) apply to every file: no per-file-ignores entry ignores them.
  No ignore comments, nothing added to `NOT_YET_CLEAN`, no `[tool.ty]`
  setting that switches a file or a rule off: fix the code.
  `tests/test_python_files.py` fails on each
  ([docs/dev-tools.md, "What each gate checks"](docs/dev-tools.md#what-each-gate-checks)).
- Run one module with `invowk cmd test module NAME`: a bare `python3 -m unittest NAME` skips the socket-stdin and no-sudo gates (see "Test gotchas"). Either way the full suite still has to pass.
- Don't run E2E tier 2 on a workstation. It starts a `--privileged` container that can see the machine's disks (`CST_E2E_ALLOW_PRIVILEGED=1`). CI runs it.
- CI runs the unit tests as a non-root user named `ci`. Tests must not depend on the developer's home or groups.

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

### A test that reaches sudo passes in a toolbox

In a toolbox sudo asks for no password. A test that runs a script which
calls sudo (even with a PATH of its own, so a stub cannot catch it) then
runs it as root on the real machine, and still passes.

**Rule:** no unit test reaches sudo or another setuid program.
`tests/run_unit.py` enforces it at run time: before the tests start it sets
the kernel's no_new_privs flag (`prctl(PR_SET_NO_NEW_PRIVS)`), which every
child inherits and none can clear. sudo then refuses to run, and such a test
fails on every run. `NoPrivilegeGainTest` in `tests/test_run_unit.py`
checks the flag. Never clear it or work around it. The flag reaches only
the runner's children: a command handed to a daemon (`flatpak-spawn --host`,
docker, `systemd-run`) does not inherit it, so a unit test never starts one
of those either.

### A test that reaches systemctl changes the host from a toolbox

From a toolbox, `systemctl`, `busctl`, `loginctl` and `systemd-run` talk
D-Bus to the host's systemd, `flatpak-spawn` runs a command on the host and
`virsh` talks to the host's libvirt. no_new_privs stops none of them. A unit
test once started a unit on the host that way: a helper thread outlived the
patch of `subprocess.Popen` that was meant to catch its call.

**Rule:** a unit test runs only fakes of these commands. `tests/run_unit.py`
enforces it at run time: before the tests start it writes a refusing stub
for each of them (`HOST_COMMANDS`) into a temporary folder and puts that
folder first on PATH, for the runner and every child that inherits its
environment. The stub prints "unit test reached the real systemctl" and
exits 97 and writes its command line down, and the run fails when one ran,
even if the test ignored the answer (a helper thread whose result nobody
reads). A test that sets a PATH of its own is neither affected nor
protected: when that PATH goes on to `/usr/bin`, its folder of fakes comes
first and holds a fake of each of these commands the code may run. `HostCommandGuardTest` in `tests/test_run_unit.py` checks the
stubs. A command run by its absolute path (`/usr/bin/systemctl`) passes the
stubs: production code names these commands bare, and a test never adds a
path to them.
