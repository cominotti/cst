<!-- SPDX-License-Identifier: MPL-2.0 -->
# Developer tools: uv, ruff, ty, zizmor, coverage.py, Hypothesis, cosmic-ray, Quint

CST's helpers are Python scripts that run as root on the system's
`/usr/bin/python3 -I`, with the standard library only. That does not change.
The tools on this page check that code while you work on it and in CI. They
are not part of CST: nothing installed on the machine uses them.

| Tool | What it does here | Pinned in |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) 0.12.0 | installs the tools below, at the exact versions in `uv.lock`, into a virtual environment outside the checkout | `.github/workflows/ci.yml` (CI); any uv 0.12 or later locally (`required-version` in `pyproject.toml`) |
| [ruff](https://docs.astral.sh/ruff/) 0.17.5 | linter: unused or undefined names, likely bugs, old syntax, syntax errors, `subprocess.run` without `check=`; missing type annotations (every Python file). Formatter: one layout for all Python code | `pyproject.toml`, `uv.lock` |
| [ty](https://docs.astral.sh/ty/) 0.0.104 | type checker | `pyproject.toml`, `uv.lock` |
| [zizmor](https://docs.zizmor.sh/) 1.32.0 | audit of the workflows, the composite actions and `dependabot.yml` in `.github` (see "The workflow audit") | `pyproject.toml`, `uv.lock` |
| [coverage.py](https://coverage.readthedocs.io/) 7.17.3 | statement and branch coverage of the unit suite, for `invowk cmd test coverage` (its own `coverage` group; the nightly runs it; see "Coverage") | `pyproject.toml`, `uv.lock` |
| [Hypothesis](https://hypothesis.readthedocs.io/) 6.170.17 | property and state-machine tests of `tests/mbt`, for `invowk cmd test mbt` (its own `mbt` group; CI runs it on every pull request and in the nightly; see "Property and model-based tests") | `pyproject.toml`, `uv.lock` |
| [cosmic-ray](https://cosmic-ray.readthedocs.io/) 8.9.0 | mutation testing, for the optional `invowk cmd test mutation` (its own `mutation` group; CI does not install it; see "Mutation testing") | `pyproject.toml`, `uv.lock` |
| [Quint](https://quint-lang.org/) 0.33.1, [Apalache](https://apalache-mc.org/) 0.62.4 (its jar also runs TLC), Quint's evaluator 0.7.1 | model checking of the Quint specs with TLC, and the traces their replay tests read, for the optional `invowk cmd test quint` (not through uv: npm and two downloads checked by SHA-256, in `~/.cache/cst-quint`; see "Model checking with Quint") | `tests/quint/package.json` and `package-lock.json`; `tests/quint/cst-quint` (the two downloads) |

**ty is beta.** Its versions are 0.0.x and a new one can report new errors on
unchanged code. That is why it is pinned exactly and upgraded on purpose (see
"Upgrading a pin").

## Running them

```
invowk cmd test pylint        # ruff check (the select list of pyproject.toml), ruff format --check, then the ruff budgets: every Python file
invowk cmd test types         # ty check on the ty gate list (with the mbt group), then ruff check --select ANN on every Python file
invowk cmd test workflows     # zizmor --offline on .github, with the exceptions in .github/zizmor.yml
invowk cmd test coverage      # the unit suite under coverage.py, its reports, then COVERAGE_FLOORS (slow; the nightly runs it)
invowk cmd test mbt           # the Hypothesis tests of tests/mbt (see "Property and model-based tests")
tests/uv-dev ruff format      # rewrite the files the format check complains about
tests/uv-dev ruff check --fix # any tool command, by hand
tests/uv-dev ty check swap/domain.py
tests/uv-dev --group mutation cosmic-ray --version   # --group NAME adds a group to "dev"
```

You need `uv` on `PATH` (for example from Homebrew or
`https://docs.astral.sh/uv/getting-started/installation/`). The first run
downloads ruff, ty and zizmor; later runs start at once. `--locked` makes uv stop
with an error when `uv.lock` does not match `pyproject.toml`, and uv checks
the hash of every package against the lock.

The unit tests do not need any of this: `python3 -I -B tests/run_unit.py`
still runs on the system Python with the standard library (in parallel by
default, one worker per CPU the run may use: its CPU affinity, so a toolbox or
cgroup pinned to 16 of 24 CPUs gets 16; pass `--jobs 1` to force a single
process, or let it fall back to 1 automatically when run under `coverage.py`).
`CST_TEST_JOBS=N` in the environment sets that default worker count for a
run that passes no `--jobs` (`invowk cmd check` passes none): an explicit
`--jobs N` wins over it, coverage's 1 too, and an empty value or 0 keeps the
default; any value that is not a whole number stops the run with a usage error.
On a workstation shared with other work, see
[Test performance on a workstation](#test-performance-on-a-workstation). For faster local iterations,
`invowk cmd test affected [--list] [--base REF]` (`tests/run_unit.py --affected`)
selects only the static gates and unit modules statically reachable from your
changed files, falling back conservatively to the full suite for `cst_core` or
shared files, and then runs the affected `tests/mbt` modules (see
[Property and model-based tests](#property-and-model-based-tests)); `invowk cmd test e2e tier1` supports `--match`, `--tag`,
`--list_tests`, `--shard I/N`, and `--jobs N`.

## Test performance on a workstation

What the defaults are, and how to run the suite on a machine that is doing
other work at the same time (another test run, a build, an agent).

- **Workers follow the CPU affinity.** With no `--jobs`, `tests/run_unit.py`
  starts one worker per CPU it may run on (`os.process_cpu_count()`), not per
  CPU of the machine. A toolbox pinned to 16 of 24 CPUs gets 16 workers. 24
  workers there would buy 9% of wall time for 7% more CPU time and about 2
  more CPUs held. CI runners have an affinity equal to their CPU count, so CI
  does not change.
- **`CST_TEST_JOBS=N` caps a run that passes no `--jobs`**, such as
  `invowk cmd check`. An explicit `--jobs N` wins over it, and under
  `coverage.py` the run is still a single process. Use it when something else
  runs on the same machine; two agent runs on one 16-CPU machine use
  `CST_TEST_JOBS=6`. The full suite at each worker count, under `nice -n 10`:

  | workers | wall time | CPUs busy (average) |
  |---:|---:|---:|
  | 8 | 91 s | 5.6 |
  | 12 | 68 s | 8.0 |
  | 16 | 56 s | 10.5 |
  | 24 | 51 s | 12.3 |

  Each worker keeps less than one CPU busy: many tests wait on timers and on
  fsync, not on the CPU.
- **TMPDIR on tmpfs**: `TMPDIR=$(mktemp -d /tmp/cc.XXXXXX)` (on Fedora `/tmp` is
  tmpfs), not a folder in the home. The suite fsyncs its temporary files. On a
  btrfs home (RAID1 on dm-crypt, `flushoncommit`) one run wrote 0.65 to 1 GB
  and caused 6,400 to 8,200 cache flushes per disk; on tmpfs, 47 to 73 MB and
  226 to 314. The suite took 13% less wall time at 4 and 8 workers, and 8% less
  at 16; the tests that fsync most were about 12 times faster. Keep the folder
  name short: the tests bind unix sockets below it, whose path may be at most
  108 bytes. Remove the folder when done. tmpfs is RAM: virtual
  environments, caches, downloads and any output that can grow large (the
  logs or results of a harness) stay under `~/.cache`. The peak RAM the folder
  uses was not measured.
- **`nice -n 10` for local test runs.** It cost nothing measurable when nothing
  competed (at 8 to 24 workers the times with and without it were within each
  other's spread). How much it helps the machine's other users was not
  measured.
- **The longest work first.** A parallel run of the whole suite (no NAME,
  no `--gates`) that passes writes how long each test took to
  `${XDG_CACHE_HOME:-~/.cache}/cst-unit-durations/<key>.json`, one file per
  checkout or worktree (the key as `tests/uv-dev` makes it; a relative
  `XDG_CACHE_HOME` is ignored, so the file never lands in the checkout). The
  next parallel run, whole or named, queues its work units longest first by
  those times, so a long unit (a class of real sleeps or timeouts) does not
  start last and hold the run open; a test with no time yet counts as the
  average one. With no file, or one it cannot read, the order is the old one:
  most tests first. A run in one process (`--jobs 1`, `CST_TEST_JOBS=1`, a
  load error, a single unit) neither reads nor writes the file, so it cannot
  seed the times. A run that fails (a test, a gate, a lost worker) leaves
  the file as it was: tests that error at once would otherwise record a
  slow unit as a quick one. CI neither reads nor writes the file. Only the order
  changes: the same tests and gates run, and a failure is the same failure.
  Deleting the file costs one run in the old order. Measured on 2026-11-24 on
  `main` (3,692 tests), under `nice -n 10` with TMPDIR on tmpfs, 5 runs each,
  the old and the new order interleaved on a shared machine: at 16 workers
  the median went from 56.1 s to 50.8 s (9.5% less; each order's runs spread
  over 18%), at 8 workers it did not change (88.2 s and 89.1 s, within the
  spread of 5% to 11%). With fewer workers the long units are a smaller part
  of each worker's share, so their place in the queue matters less.

Conditions of the numbers above, except the longest-first bullet's, which
gives its own (so: the affinity numbers, the worker table, and the TMPDIR and
`nice -n 10` bullets): Python 3.15; 3 runs per cell, the median shown;
a shared machine, with another project's load at times (the spread was 1% to
3% at 8 workers or more, up to 39% at 2 to 4). The tree measured was not
`main`: it was `main` plus the verification proof-of-concept tests (3,354
tests), among them the fault injection tests in the form they had then, with
every fsync real. `main` has had those tests since, with fsync stubbed
([Fault injection tests](#fault-injection-tests)), so they no longer depend
on where TMPDIR is. The "about 12 times" is those tests as they were then;
the suite-level 8% to 13% was measured on that tree and may be smaller on
`main`.

## Fault injection tests

Some protocols must survive a process that dies or a disk that fills up
halfway: docker-volumes-nocow's boot migration, `cst btrfs scrub-limit set`
and `off` with their rollback, `cst btrfs iocost apply`'s "done last",
`cst btrfs cache attach` and `detach` (no line in /etc/fstab for a bind
that is not mounted), `cst toolchains prune apply rust:build` (a folder
renamed to the trash before it is deleted, the leftovers of a run that
died), `cst bluetooth apply` and `revert` with their restarts and the
record of the restarts still owed, and btrfs-bulk's moves of an item into
`~/Bulk` with the journal that finishes or undoes an interrupted one.
`tests/fault_injection.py` tests them at every step, with the standard
library only.

**What it does.** Every mutating call of a run (a port call, a module
function an adapter calls, a command that changes something) goes through
`FaultPlan.step`. A run with no fault lists them (`cut_points`); then, for
each call in turn (`every_fault`), a fresh fixture runs again with the plan
armed there, in one of three modes:

- `KILL`: the call takes effect, then the process dies (`Killed`, a
  `BaseException`, so the code's `except Exception` does not catch it);
- `FAIL`: the call fails with ENOSPC and no effect; the run goes on;
- `FAIL_FROM`: that call and every later one fail (a full disk stays full).

`FaultPlan(dead_after_kill=True)` makes a `KILL` final: every later call
raises `Killed` again, with no effect and no record, as a dead process runs
no handler. Code that cleans up in an `except BaseException` handler
(btrfs-bulk's copy) needs it: without it the cleanup runs after the kill,
and the state a real kill leaves is never tried.

A plan can hold several faults: a `FAIL` and then a `KILL` at each later
call is a run that dies while it rolls back, and a `FAIL` then a second
`FAIL` one that fails again there (`btrfs/tests/test_cache_faults.py`). A port that reports a failure
without raising passes `failed` (a command answers exit 1; `ignore_error`
for an `ignore_errors=True` removal); `FaultyRunner` does that for a command
runner. The test then checks its invariants after the fault, after the next
run (recovery), and at the end (the same command again converges).

**How to add a target.**

- Wrap each mutating call: subclass the real adapter and send each method
  through `plan.step` (`btrfs/tests/test_docker_volumes_nocow_faults.py`),
  send each mutating method of an in-memory fake of a use case's ports
  through it (`ScrubWorld` and `IocostWorld` in `btrfs/tests/fakes.py`,
  which `btrfs/tests/test_scrub_limit_faults.py` and
  `btrfs/tests/test_iocost_faults.py` drive; a contract test runs the same
  body on the fake and on the real adapter, so the fake cannot drift:
  `btrfs/tests/test_scrub_limit_ports.py`, `btrfs/tests/test_iocost_ports.py`),
  patch the names an adapter module calls (`ensure_dir`, `write_atomic`:
  `btrfs/tests/test_cache_faults.py`), put `FaultyOs(plan, names)` in
  place of a module's `os` (the functions named are points, the rest of
  `os` as it is: `toolchains/tests/test_prune_faults.py`; `kill_only=True` for calls
  whose errors the code does not handle: a failure armed there does not
  fire, a kill does), or put
  `FaultyRunner(plan, inner, reads=...)` in front of a fake runner, with
  `reads` naming the commands that only read (`btrfs/tests/test_cache_faults.py`).
- Model the outside state the invariants need in the fake under it: what
  systemd has loaded and whether it needs a reload, the install record,
  the service, what a daemon read at its last start. When the usual fake
  is a shell script (bluetooth's FAKE), a run per command costs too much:
  write a Python twin and a parity test that runs one scenario on both
  and compares commands, exits, output and files (`SetupModel` and
  `SetupModelParityTest` in `bluetooth/tests`).
- Give each run its own clock time when the code names files by it (a
  backup, a trash name): two runs in the same second would share a name a
  real machine never shares.
- Replace a sleep that waits out a clock tick by a wait for the next tick,
  not by a shorter sleep: btrfs-bulk sleeps 0.05 s so that a ctime taken as
  a stamp never shares its tick with an earlier change, and
  `btrfs/tests/bulk_fakes.py` `tick_wait` creates files until one's ctime
  moves on (exact on a coarse clock and on a fine one, with no constant to
  measure).
- Call `stub_fsync(self)` in `setUp`: these tests check what a kill leaves,
  not durability, and an fsync per write makes them many times slower on a
  btrfs home. The fsync probes of `tests/probes.py` stay for the tests that
  check durability.
- One test class per start state and fault mode (or mode family): a class
  of up to 10 tests is one work unit of `tests/run_unit.py`, so one big class
  would run on one worker. Pin each class's number of cases with
  `assertEqual`, so a change of the protocol's calls, or a loop that runs
  nothing, is seen.

**What it cannot model.** A power loss and writes not yet flushed (`KILL`
keeps every call that finished; the fsync probes cover durability); a call
torn half-way (each call is atomic here; the nocow fakes' `copy_into` has
its own `stop_after`, and btrfs-bulk's deletion of a tree is two points,
its first file and the rest: `arm_points(..., torn_rmtree=True)`); threads and timing (a thread that outlives a
cancel is no failure at a call); the real systemd, btrfs and udev (the E2E
tests, such as tier 1's `test_161` for scrub-limit's reload).

**btrfs-bulk.** `btrfs/tests/test_btrfs_bulk_faults.py` runs the
program over the unit tests' scratch home (`btrfs/tests/bulk_fakes.py`
`BulkHome`: the real moves, cp included, with fakes only for subvolumes,
+C, the busy check, flatpak and btrfs-snapshot's convert), from ten start
states (a plain move, the in-place +C rewrite, a +C copy out of /home, an
old subvolume, a stale cache, an empty folder, a late write into the old
tree, a write into the source during its copy, writes on both sides, and
the undo of a Steam game's move: `bulk steam apply` over a minimal Flatpak
Steam library, a late write into the old game folder, from the switch on). `arm_points` makes the user adapter's
`os` calls (`FaultyOs`), the state files' writes and removals, cp
(`FaultyRunner`), +C on a folder, the creation of `~/Bulk` and each
tree's deletion (torn in two) points of the plan, which is dead after a kill. After a kill every mode is tried again
inside the repair; after a failure or a full disk, only a kill. No gap is
let through: a fault that cuts the deletion of a checked copy (a rollback's,
a Steam undo's, or the switch's when the source changed) must converge too, because the
copy is renamed to `<target>.bulk-new.bulk-drop` before it is deleted and a
repair deletes a marked copy without the "written to" check.

**Cost.** The eight modules (`tests.test_fault_injection`,
`btrfs.tests.test_docker_volumes_nocow_faults`,
`btrfs.tests.test_scrub_limit_faults`, `btrfs.tests.test_iocost_faults`,
`btrfs.tests.test_cache_faults`, `toolchains.tests.test_prune_faults`,
`bluetooth.tests.test_setup_faults`, `btrfs.tests.test_btrfs_bulk_faults`)
together: 10.5 to 10.8 s wall at `--jobs 4` (`nice -n 10 /usr/bin/python3
-I -B tests/run_unit.py --jobs 4` on the eight, 3 runs, on a workstation
whose load average was 3 to 4 on 16 CPUs; 6.5 to 10.1 s there without the
Steam undo's classes, and 4.3 to 4.8 s at an earlier measurement under a
load average of 20). btrfs-bulk's alone is about 20 s of CPU in 30
classes: the Steam undo's three take about 9.5 s of it (SteamUndoKillTest,
the longest, 5.7 s: each case runs `bulk steam apply` and its survey of the
library again; the library already holds only a game folder and its
manifest), LateWriteKillTest 1.7 s. fsync is stubbed, so the times are about
the same on tmpfs and on a btrfs home.

## Where the virtual environment lives

Never inside the checkout: `~/Workspace` is snapshotted, and a `.venv` there
would end up in every snapshot unless it is marked as a cache (below). `tests/uv-dev` (used by the invowk tasks) sets

- `UV_PROJECT_ENVIRONMENT=${XDG_CACHE_HOME:-~/.cache}/cst-dev-venv/<key>`
- `RUFF_CACHE_DIR=${XDG_CACHE_HOME:-~/.cache}/cst-dev-ruff/<key>`

where `<key>` is the first 12 hex digits of the SHA-1 of the checkout's
physical path (`pwd -P`): one environment per checkout or worktree. The
cache folder, here and for every dev tool (`tests/uv-dev`, `tests/run_unit.py`'s
durations file, the mbt profiles, `cst-mutation`, `cst-quint`, the default
folder of `invowk cmd test coverage`), follows one
rule (`tests/xdg_cache.py`, and the same in `tests/uv-dev`'s bash): an
absolute `XDG_CACHE_HOME`; else `.cache` under an absolute `HOME`; else
under the home folder of the password database, when it is absolute. A relative `XDG_CACHE_HOME`
is ignored, as the XDG spec says, and a relative or empty `HOME` is no
home: the tools run from the top of the checkout, so either would put the
cache inside it. With none of the three the tool stops with "no cache
folder". The `${XDG_CACHE_HOME:-~/.cache}` in this document is the short
form of that rule. Deleting these folders is safe; the next run creates them again. `.venv/` and
`.ruff_cache/` are in `.gitignore` in case a tool is run some other way.

An IDE can make one anyway. A JetBrains IDE (PyCharm, IntelliJ with the
Python plugin) that opens the checkout reads `pyproject.toml`, offers a uv
interpreter, and runs uv in the checkout without `UV_PROJECT_ENVIRONMENT`:
it creates `.venv/` there and installs the `dev` group into it (that is how a
`.venv/` with ruff and ty appeared next to a new `.idea/`). Point the IDE at
the environment `tests/uv-dev` made instead
(`${XDG_CACHE_HOME:-~/.cache}/cst-dev-venv/<key>/bin/python`, after one
`tests/uv-dev ruff --version`), or delete the `.venv/` it made; `.idea/` is
in `.gitignore`. No CST command makes one: `tests/uv-dev` is the only
place in the repository that runs a tool through uv on this project (`uv run`; the invowk
tasks, `tests/ruff_budget.py` and CI all go through it; the one other uv
command on it is `uv audit`, below, which reads `uv.lock` and makes no
environment; `cst toolchains`
runs only `uv --version` and `uv cache prune`, outside any project). uv writes a `CACHEDIR.TAG` into a `.venv/`
it makes, so new snapshots drop one that uv made (unless
`SNAPSHOT_EXCLUDE_CACHES=no`, [btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md#cache-folders-cachedirtag-dropped-from-every-new-snapshot)).

uv uses the system Python (`python-preference = "only-system"`) and never
downloads one. `requires-python = ">=3.15"`: Fedora 45 ships Python 3.15 in
the CI image, in the E2E images and on the workstation host. ruff and ty
both take their target Python version from that lower bound, so it is the
only place to change when the minimum Python moves.

Where to run `test pylint` and `test types`: on the host, or in a Fedora 45
toolbox. A Fedora 44 toolbox (such as `devbox`) has Python 3.14, which is
below `requires-python`, so uv stops there with "No interpreter found for
Python >=3.15 in search path". The unit tests, `test lint` and the E2E
tasks do not use uv and run anywhere they ran before.

## What each gate checks

Three lists live in `pyproject.toml` and one in `tests/test_python_files.py`
(a unit test, standard library only), which checks them against the
repository:

- **`[tool.ruff] extend-include`**: the extensionless helpers (`btrfs/btrfs-bulk`,
  `vm/cst-vm`, ...). ruff finds `*.py` files by itself, but not these. A new
  helper must be added here; the test fails until it is.
- **`[tool.ruff] required-version`** is the exact ruff pin of the `dev`
  group (`==0.17.5`): a different ruff that reads `pyproject.toml` (an
  editor's, one on `PATH`) refuses to run instead of linting or formatting
  by other rules. `tests/test_python_files.py` fails when the two differ.
- **`[tool.ruff] per-file-target-version`**: the files CI runs on its
  ubuntu-24.04 runner's python3 (`tests/e2e/**/*.py`, `tests/__init__.py`,
  `tests/quint/cst-quint`, and what they import, such as
  `cst_core/domain/errors.py`, `values.py` and `tests/xdg_cache.py`) are formatted and linted as Python 3.12 (`py312`),
  not as `requires-python`'s 3.15: `ruff format` would otherwise write
  `except A, B:` (3.14) there, and `ruff check` reports it as a syntax
  error in them. `tests/test_e2e_host_python.py` finds that set from the
  code and fails when a file of it has no `py312` entry
  ([tests/e2e/README.md](../tests/e2e/README.md#the-python-that-runs-these-files)).
- **`[tool.ruff.lint] typing-extensions = false`**: no ruff rule suggests,
  and no fix writes, an import from the third-party `typing_extensions`
  (by default ruff falls back to it for names newer than the target
  Python). CST uses the standard library only. No hit today;
  `tests/test_python_files.py` fails when the setting is not `false`.
- **ruff rules**: `F` (pyflakes), `B` (bugbear), `UP` (pyupgrade), `E9`
  (syntax errors), `PLW1510`, and the families that had no hit when they
  were turned on: `PGH` (no blanket ignore comments), `A` (no builtin name
  shadowed), `T10` (no debugger call), `TID` (no relative import from a
  parent package), `LOG` and `G` (logging calls), `PLE` (pylint errors),
  `W` (pycodestyle warnings) and `C4` (comprehensions), for every Python
  file (`test pylint`). Since 2026-11 also `E7` (statements), `PLC` and
  `PLR` (pylint conventions and refactors), `S` (bandit: no `os.system`,
  `shell=True`, `exec`, `tempfile.mktemp`), `TRY` (exception handling),
  `ISC` (implicit string joins: inside a list or a tuple a join needs
  parentheses, so a comma forgotten between two lines of a unit file or a
  plan is an error), `ARG` (unused arguments, production code only: fakes
  in tests ignore arguments on purpose), `SLOT`, `YTT`, `FLY`, `RSE`,
  `ASYNC`, `INT`, `INP`, `COM818` and `COM819`, and the families of
  lint group 2 once their last hits were fixed: `DTZ` (naive datetimes),
  `PLW`, `RUF`, `SIM`, `PERF`, `FURB`, `PIE`, `RET`, `PYI`, `EXE` (a
  shebang on a file that is not executable) and `ERA` (commented-out
  code). The rules of these families that are left out, and the files
  that keep a hit on purpose, are in `[tool.ruff.lint] ignore` and
  `per-file-ignores` of `pyproject.toml`, each with its reason in a
  comment (the size rules `PLR09xx` and `PLR2004` are there because the
  budgets below count them). `ANN`
  (annotations) is not in the selected rules: `test types` runs it on its
  own (`ruff check --select ANN`), on every Python file (see "ANN applies
  to every file" below).
- **ruff preview**: `[tool.ruff.lint] preview = true` with
  `explicit-preview-rules = true`, so a preview rule is on only where
  `select` names it by its full code (a family selector such as `RUF` does
  not pull in the about 145 others): `B901`, `B909`, `PLE1141`, `PLE4703`,
  `PLW3201`, `PLW0244`, `RUF031`, `RUF039`, `RUF045`, `RUF056`, `RUF066`,
  `RUF072` and `RUF075`, which find bugs (a list changed while it is
  iterated, a value returned from a generator and dropped, ...) and had few
  or no hits. Never `PLC1901`: it asks for `not x` instead of `x == ""`,
  the opposite of the house style. Preview also changes some stable rules a
  little (it added one `F811` hit). The formatter stays on its stable style:
  `[tool.ruff.format]` has no `preview`, and neither has the top-level
  `[tool.ruff]` (it would turn on both). `tests/test_python_files.py`
  fails when preview is on without `explicit-preview-rules`, when
  `PLC1901` is selected and when either formatter preview is on. A preview
  rule can change or disappear in any ruff release; the exact pin means only
  an upgrade pull request sees that (see "Upgrading a pin").
- **`PLW1510`: every `subprocess.run` passes `check=`.** Without it, a
  command that fails (exit status not 0) does not stop the program, and
  nothing in the code says whether that was meant. At a new call site write:
  - `check=True` when a failure must stop the program (`subprocess.run`
    then raises `CalledProcessError`);
  - `check=False` when the code reads `returncode` itself, or when a failure
    really does not matter (say why in a comment).

  When the rule was turned on, every call that had no `check=` got
  `check=False`, which is what it did before (Python's default), so no
  behaviour changed.
  Turning one into `check=True` changes behaviour: do it in its own pull
  request, with a test. ruff does not lint the Python examples in Markdown
  files; write `check=` in them by hand.
- **Ruff budgets** (`tests/ruff_budget.py`, the end of `test pylint`): rules
  with hits not fixed yet, which therefore cannot be in `select`, are counted
  per package with `--ignore-noqa` (a `# noqa` does not hide a hit) and
  compared with `RUFF_BUDGETS`, which they may not go above: `BLE001` (blind
  `except Exception`), `SLF001` (a private member used outside its class;
  production code only), `FBT001`/`FBT002` (a bool positional parameter:
  make it keyword-only), `PLR2004` (a number compared with a literal;
  production code only: name it, see
  [.agents/rules/magic-values.md](../.agents/rules/magic-values.md)) and `C901` (a function over
  `[tool.ruff.lint.mccabe] max-complexity`, 15). C901 counts
  cyclomatic complexity and is only a rough local hint for SonarCloud's
  cognitive complexity rule (S3776): a survey found about five times as
  many production functions over 15 by cognitive complexity as C901 flags,
  and not the same ones. SonarCloud's S3776 stays the gate for cognitive
  complexity; a green C901 budget says little about it. Ratchets on the size of a function: `PLR0911`, `PLR0912`,
  `PLR0913`, `PLR0915`, `PLR0917` (more than 6 returns, 12 branches, 5
  arguments, 50 statements or 5 positional arguments, ruff's defaults), with `FBT003` (a bare `True`/`False`
  passed by position) and the `N` family (pep8-naming), counted under `N`:
  an entry of RULES made of letters only counts every code of its family.
  A rule or family that reaches 0 moves to `select` (the lint group 2
  families did in 2026-11). A second ruff run fails on any `# noqa` that
  suppresses nothing (`RUF100`, which `select` leaves to it: the noqa
  comments name budgeted rules that a plain `ruff check` does not turn
  on). When a budget fails, the script prints the measured table;
  see [tests/budgets.py](../tests/budgets.py) for how budgets work.
- **The typing budget** (`tests/test_typing_budget.py`, in the unit suite):
  `Any` (inside generics: `ANN401` already refuses it as a whole
  annotation) and `cast()` are counted per package and layer, the tests
  apart, in `ANY_BUDGETS` and `CAST_BUDGETS`, which they may not go above.
  A package and layer without an entry may have none; the tables in that
  file say which production code still has one, and why. Type the value
  instead: a `TypedDict` for a record read from JSON, a frozen dataclass
  for one built in code, `isinstance` narrowing instead of a cast.
- **The magic values gate** (`tests/test_magic_values.py`, in the unit
  suite): in production code, a string compared with a word of a StrEnum
  (of the package or of cst_core), an if/elif chain or match with 3 or more
  string arms on one subject, a unit number (3600, 86400, 604800, the exit
  statuses 124, 126, 127, 137), a signature with a loose
  `dict[str, Any | object | Json]`, a `Literal` of 3 or more strings and an
  `int()` of text (its argument not a number by its form, or with a base;
  cst_core's parsers and bash arithmetic's readers are exempt: text goes
  through `parse_whole_number` or `parse_integer`, a float through
  `math.trunc`) are
  counted per package and kind in `MAGIC_CEILINGS`, which they may not go
  above. A package and kind with no entry may have no hit, so a new hit
  there fails.
  `MAGIC_EXEMPT` names the scopes that keep one, with the reason; a stale
  entry fails. See
  [.agents/rules/magic-values.md](../.agents/rules/magic-values.md).
- **The format check** (`ruff format --check`, the second part of
  `test pylint`): every Python file must already look the way `ruff format`
  writes it. Lines are up to 120 characters (`line-length` in `[tool.ruff]`;
  ruff does not split long strings or comments), and the rest is ruff's
  default style: double quotes, four spaces, and a trailing comma keeps a
  call split over several lines. When the check fails, run
  `tests/uv-dev ruff format` and commit the result. It changes only the
  layout, never what the code does. Inside docstrings it can change
  whitespace (a space after an opening quote, trailing spaces, tabs, the
  indentation of the lines), so check the diff when a docstring is printed
  as help text (`print(__doc__)`, argparse). Do not use `# fmt: off` or
  `# fmt: skip` to get a green run: `tests/test_python_files.py` fails on
  any `# fmt: skip`, `# fmt: off`, `# fmt: on` (and the `# yapf: disable`
  and `enable` ruff also reads) in a Python file. Markdown files are excluded
  (`[tool.ruff.format] exclude`): ruff would also format the Python code
  blocks in them, which are short examples wrapped to read well next to the
  text around them, not code that runs.
- **ANN applies to every file**: every function of every Python file has
  type annotations. Record dicts are `TypedDict` classes, so ty can check the
  keys read with `[]` and the value types (not the keys read with `.get()`,
  nor whether a key read with `[]` is `NotRequired`). Nothing switches ANN
  off: `tests/test_python_files.py` fails on a
  `[tool.ruff.lint.per-file-ignores]` (or `extend-per-file-ignores`) entry,
  or a `[tool.ruff.lint] ignore`/`extend-ignore` (or the older top-level
  `[tool.ruff]` form of each), that names an ANN rule or `ALL`, since ruff would then skip those files and still report "All checks
  passed". A new file needs no list edit for ANN. (Until 2026-11 a one-line
  `"!{...}" = ["ANN"]` entry named every typed file; every file had joined it,
  and each parallel change conflicted on that one line.)
- **The ty gate, `[tool.ty.src] include`**: every Python file, and ty
  reports nothing on any. `error-on-warning` is on, so a warning fails it
  too. It lists the ten folders with Python code (`.github/scripts`, the
  eight modules, `tests`) and the extensionless helpers by name: ty finds
  the `*.py` files below a folder by itself (leaving out git-ignored ones,
  such as `tests/e2e/.artifacts/`), but not a file without the extension.
  A new `*.py` file in one of these folders is in the gate with no list
  edit; a new helper, or a new top-level folder, needs its entry, and
  `tests/test_python_files.py` fails until it has one. An entry is a plain
  path to a folder with Python files or to a Python file (no pattern, no
  `.`). Never remove one, or add an ignore comment, to get a green run.
  ty also leaves out a file that a git ignore rule or an `.ignore` file
  matches, even a tracked one and even one named in the list, while git
  keeps tracking it: the test fails on a tracked Python file a git ignore
  rule matches (`git ls-files -ci --exclude-standard`) and on any
  `.ignore` file, so that no file leaves the gate that way.
  (Until 2026-11 the list named every file, 330 entries: ty's compiled
  pattern for it passed its 1 MB limit, and it printed a warning on every
  run.)
- **`NOT_YET_CLEAN` in `tests/test_python_files.py`**: the historical exclusion set,
  now empty (`frozenset()`). Every Python file in the repository is in the ty
  gate (`[tool.ty.src] include`). A file below a listed folder could not
  leave it anyway (ty checks it; the test fails on a file in both). The
  set sits in the test, not in `pyproject.toml`, so that adding a file to it
  would be a visible edit of the test. Never add one.
- **Nothing else switches ty off**: the test also fails when a `ty.toml`
  exists at the top of the repository (ty would read it instead of
  `pyproject.toml`), when `[tool.ty]` sets anything but `src.include`,
  `terminal.error-on-warning`, `environment.python-platform` (only
  `"linux"`) and rules raised to `"error"` (such as
  `src.exclude`, `overrides`, a rule set to `"warn"` or `"ignore"`, or
  `analysis.allowed-unresolved-imports`), when `error-on-warning` is not
  `true`, and when a ty-gated file has a `ty: ignore` or `type: ignore`
  comment.
- **`[tool.ty.environment] python-platform = "linux"`**: ty checks the code
  for Linux, wherever ty runs. Its default is the platform it runs on, and
  with every platform (`--python-platform all`) it reports a few hundred
  calls that exist only on Linux or Unix (`os.geteuid`, `os.killpg`). CST
  runs on Linux only. (`python-version` needs no setting: ty takes it from
  `requires-python`.)
- **`[tool.ty.rules]`** makes every rule an error (`all = "error"`, what
  `ty check --error all` does): also the rules that are warnings or off by
  default (`missing-type-argument`, `redundant-condition-strict`, ...) and
  each rule a newer ty adds. The rules it names as well, with a comment on
  each, are the ones raised on purpose before `all`: `deprecated`,
  `division-by-zero`, `possibly-missing-attribute`,
  `possibly-missing-import`, `possibly-unresolved-reference`,
  `unused-ignore-comment` (an ignore comment that suppresses nothing),
  `redundant-cast` (a `cast()` to the type the value already has),
  `missing-override-decorator` (a method that overrides one of a base class,
  `setUp` included, says so with `@override` from `typing`: a base method
  renamed or removed is then an error, not a method nobody calls),
  `truthiness-test-of-none-union` (below), and `unsound-return` and
  `unsound-assignment` (below). A rule may be added there only at `"error"`.
- **`truthiness-test-of-none-union`**: a boolean test (`if x:`, `not x`,
  `x and y` in a condition) on a value that may be None and may also be
  empty or zero (`str | None`, `int | None`, `list[str] | None`,
  `bool | None`). Such a test cannot tell None from `""`, 0, False or an
  empty container: a refusal reason that is `""` let the step go ahead, and
  `--duration 0` meant no limit. Write which case is meant:
  - `x is not None` / `x is None` where None alone means "none" (a reason,
    a problem, a refusal: `""` is still a reason). A bare `is not None` on a
    value whose falsy form used to count as absent needs its reason (why
    that value cannot reach the test) in the commit message; without one,
    use a form equal by type;
  - an optional bool key of a TypedDict or JSON record: `d.get(k, False)`;
  - an environment variable or a str-valued mapping where empty means
    unset: `os.environ.get(X, "")`, `d.get(k, "")`, then `if x:`;
  - another `str | None` where `""` means absent: `x is not None and x != ""`
    (or `x is None or x == ""`);
  - a number where 0 means unknown or unlimited: `x is not None and x != 0`;
  - a container where empty means no data: `x is not None and len(x) > 0`;
  - a JSON value (`Json`): None first (`x is not None and x`), then the
    value as before;
  - or, better where it is cheap, the producer returns one absent form.

  ruff's RUF056 refuses a `.get()` with a falsy default as the test itself
  where it sees a dict (`if not d.get(k, ""):`): compare instead
  (`d.get(k, "") == ""`, `len(d.get(k, [])) > 0`), which satisfies both.

  The rule does not see value forms: `x or DEFAULT` and `x and y` as a
  value, `bool(x)`, or a value typed `Any`. Never use them to get past
  it, and no ignore comment, no `cast`, no `TypeIs[str]` "has text" helper
  (its else branch would claim the value is not a str).
- **`unsound-return`, `unsound-assignment`**: a value whose type holds `Any`
  (what `json.loads` and `tomllib.loads` return, typeshed's `m.group(n)`,
  `re.split` items and `sys.stdout`, `struct.unpack`, `getattr`, a module
  loaded by path) returned from a function, or assigned to a local, whose
  declared type is narrower. ty took such a value as the declared type without
  a look; now the code checks it first: the parsers are called only in
  `cst_core.domain.shapes` (`parse_json`, `parse_toml`, which return `object`;
  the architecture gate's `PRIMITIVE_OWNERS`), and a reader narrows with
  `json_object`, `has_shape` or a `TypeIs` guard of its own; a regex group is
  `cst_core.domain.text`'s `group` or `group_or_none`; the streams are
  `cst_core.adapters.terminal`'s `stdin()`, `stdout()`, `stderr()`. What the
  rules do not catch: an `Any` passed as a call argument (ty checks no
  argument against `Any`), `Any` inside a container built elsewhere and
  returned whole (`list[Any]` from a helper typed so), and a `cast` (counted
  in `CAST_BUDGETS`). That is why the parsers have one owner: through it every
  reader must narrow, whatever it passes its value to.

## The workflow audit (zizmor)

`invowk cmd test workflows` (part of `invowk cmd check`, so of CI's fast
gates) runs `zizmor --offline --strict-collection --config .github/zizmor.yml
.github`. zizmor
reads the workflows, the composite actions and `.github/dependabot.yml` and
reports what an attacker could use: a token left in `.git/config` where an
artifact could carry it (`artipacked`), a script that writes
`GITHUB_ENV`/`GITHUB_PATH` from data it did not make (`github-env`), a
template expanded into a shell script, an action not pinned by its full
SHA, permissions wider than the job needs, Dependabot taking a release the
day it is published (`dependabot-cooldown`), and more
(`https://docs.zizmor.sh/audits/`). `--offline`: no token and no network,
so the audits that ask the hosting service (such as impostor commits) do
not run. Any finding fails the task, whatever its severity.
`--strict-collection`: a file zizmor cannot parse fails the task (without
it zizmor skips that file with a warning and reports no finding for it).

What the audit asked for when it was turned on:

- every `actions/checkout` sets `persist-credentials: false`. No step
  after a checkout uses git with the job's token (the jobs that need a
  token, for the image registry or a release, pass it to that step), and
  several jobs upload files from the checkout;
- `.github/dependabot.yml` waits 7 days before proposing a new action
  version (`cooldown: default-days: 7`).

**Exceptions** live in `.github/zizmor.yml` only, as `ignore` lists of
`FILE` (every finding of that audit in that file) or `FILE:LINE` (one
finding), by the file's base name only (a folder in the entry matches
nothing), so an entry must name exactly one file of `.github`: the test
fails when several files share its base name (for `FILE:LINE`, several
long enough to have that line), since the entry would hide the finding in
each, and when no file has it. Each list starts with a comment that
says why, and so does a per-audit `config` block (it can weaken an audit
as much as an ignore list: a cooldown of 0 days, actions pinned by tag).
The file is plain block YAML, which the test reads line by line: no flow
lists (`ignore: [a, b]`), anchors, aliases, tags or quoted keys (below a per-audit `config` block, quoted keys and keys with / * . @ are read, as zizmor's settings take them: `"actions/*": hash-pin`; a quoted `"disable"` still counts as `disable`). A line entry moves with its file: when a change above it shifts
the line, the audit fails and names the new line; move the entry. Today:
the `GITHUB_PATH` write of `setup-invowk` (it checks the binary against
its pinned SHA-256 first), the style finding `self-repository` (`./...`
for local actions) and `superfluous-actions` on the release step.
`tests/test_workflow_audit.py` (a unit test) fails when an ignore list or a
config block has no reason, on a line it cannot read, when an audit is disabled (`disable: true`, which also hides it
from zizmor's count of ignored findings), on an inline `zizmor: ignore`
comment in `.github`, and when `check` stops running the task or the task
runs anything but exactly that command. The words are compared one by one:
dropping `--offline`, the config or `--strict-collection` fails, and so
does any word added, such as an option that leaves findings out or keeps
zizmor from failing (`--min-severity`, `--persona`, `--no-exit-codes`,
`--no-ignores`, `--fix`), a variable, `|| true` or a narrower input
than `.github`. `tests/uv-dev zizmor
--offline --no-ignores .github` shows what the exceptions hide.

## Coverage

`invowk cmd test coverage` runs the whole unit suite (`tests/run_unit.py`,
with its gates) under coverage.py, from the `coverage` group, counting
statements and branches of the production code (`[tool.coverage.run]` in
`pyproject.toml` leaves the tests out). It takes as long as a unit run, so
`check` and CI's fast gates do not run it; the nightly workflow does, in
the same Fedora 45 image and as the same user as the fast gates, and
uploads the reports as the `nightly-coverage-report` artifact.

`--out DIR` (absolute, outside the checkout; by default a new folder under
`cst-coverage/` in the cache folder `tests/xdg_cache.py` finds:
`$XDG_CACHE_HOME` only when it is absolute, else `.cache` in the home
folder; with none, the task stops) receives `report.txt` (per
file), `coverage.json`, `html/index.html` (each file with the lines no
test ran), `floors.txt` and coverage.py's data file. A relative folder, or
one inside the checkout (also through a symlink), is refused before
anything is created; `tests/test_coverage_floors.py` runs the task's
script from `invowkfile.cue` to check it. The task stops when a
unit test fails.

The last step, `tests/coverage_floors.py`, adds the numbers up per package
and layer (the keys of the budgets, such as `swap domain`) and fails when a
**domain** or **application** layer is below its entry in
`COVERAGE_FLOORS`: the percentage of statements and branches covered,
together. The other layers are only reported. Many of their tests start
the real program as a child (`/usr/bin/python3 -I ...`), which coverage.py
cannot follow, so a low number there (`btrfs/pending_alerts` shows 0 %)
says nothing about the tests. Domain and application code is tested in the
test process, with fakes behind the ports.

The floors are the reverse of a budget: a measurement may not go **below**
its entry. A key without an entry passes (a new package; add its floor
from the next nightly report); an entry whose key is no longer measured
fails (rename it with its package, or delete it). They were set from one
run in a Fedora 45 toolbox, rounded down to a whole percent, minus 2, since
CI skips a few tests a workstation runs. Raise a floor by hand when a
report shows room; `invowk cmd fix budgets` never touches this table, and
nothing lowers it to get a green run.

## Property and model-based tests

`tests/mbt/` holds Hypothesis tests: properties of pure functions
(`@given`) and state machines over a use case and its port fakes
(`RuleBasedStateMachine`, invariants checked after every step). Hypothesis
is not in the standard library, so they live apart from the unit suite:

- Their modules are named `mbt_*.py`, not `test_*.py`: the unit suite's
  discovery never imports them, and `invowk cmd test unit` stays
  standard-library only. `tests/mbt/__init__.py` imports nothing.
- `invowk cmd test mbt [--profile dev|ci|nightly] [--examples N] [--seed N]
  [--jobs N]` runs them through `tests/run_unit.py`, so its gates apply,
  on uv's Python with the `mbt` group:
  `tests/uv-dev --group mbt python3 -I -B tests/run_unit.py`. `check`
  does not run it; CI runs it as a step of its own, after the fast gates.
- `invowk cmd test affected` runs them too, after the affected unit tests:
  the modules whose import closure reaches a changed file, or every one
  when the unit selection falls back to the full suite (`tests/affected.py`
  says why). It asks for uv only when the change affects one of them.
  `invowk cmd test mbt --affected [--base REF]` runs only those, and
  `--list` of `test affected` shows them under `mbt`.
- ty resolves `import hypothesis` only when the group is in the tools'
  environment, so `invowk cmd test types` runs `tests/uv-dev --group mbt ty
  check`. Without it a fresh environment (CI) fails with
  `import-not-found`, while one that once had the group installed passes
  (`uv run` adds packages to an environment, it does not remove them).

### Profiles

`tests/mbt/profiles.py` holds the settings; every module calls its
`load()` at import, before its tests (a `@given` test takes the profile
that is loaded when it is decorated).

| Profile | Examples per test | Draws | Example database | Where it runs |
|---|---|---|---|---|
| `dev` | 100 | random | `${XDG_CACHE_HOME:-~/.cache}/cst-mbt/<key>/examples` | a workstation (the default outside CI) |
| `ci` | 100 | derandomized: the same on every run of the same code | none | every pull request, in fast-gates (the default under CI) |
| `nightly` | 500 | random, with a seed the task prints | none | the nightly workflow, in a job of its own |

None has a deadline: on a shared machine one slow example is load, not a
bug. `--examples N` (`CST_MBT_EXAMPLES`) changes the count of any profile;
a pull request never runs fewer than 100: in the verification pilot, 5
examples missed a known bug in one copy of the code.
`tests/test_workflow_audit.py` keeps it so: it refuses a `CST_MBT_`
variable anywhere in ci.yml or the nightly (an `env:` the task would
pass on), and an `if:` or `continue-on-error:` on the mbt step or on the
fast-gates job.

The `dev` database keeps the examples that failed, so the next run tries
them first. Hypothesis's own folder (its `constants/` and `unicode_data/`
caches) is the same `cst-mbt/<key>` folder, with the key `tests/uv-dev`
gives the checkout's virtual environment: Hypothesis's default is
`./.hypothesis`, in the checkout. Deleting the folder is safe.

### Seeds and replaying a failure

Hypothesis prints a failing example, shrunk, and with `print_blob` a
`@reproduce_failure(...)` line that replays it exactly on the same code.
A nightly run picks a seed and prints it first:

```
mbt: profile nightly, seed 2584909277 (replay: invowk cmd test mbt --profile nightly --seed 2584909277)
```

`--seed N` (`CST_MBT_SEED`) seeds every test of the run, so the run draws
the same examples again. Hypothesis has no setting for a seed: a TestCase of
`tests/mbt` derives from `profiles.MbtTestCase`, which seeds its `@given`
tests, and runs a state machine with `profiles.run_machine(Machine)`.
`tests/mbt/mbt_profiles.py` fails on a TestCase of `tests/mbt` that does
not derive from it.

### What it covers

- `mbt_install_service.py`: a state machine over `InstallService` with the
  unit tests' in-memory target and record (`tests.test_install.MemoryTarget`,
  `tests.test_install_record.MemoryRecord`): any sequence of install, put,
  mark, remove and forget, failed writes, files changed behind CST's back, a
  checkout whose payloads change and a record of a newer CST. Its first run
  found that `remove()` deleted the files before it refused a newer record.
- `mbt_record.py`: the install record's text round trip and its updates
  (no component state without a file).
- `mbt_snapshot_domain.py`: the snapshot retention plan, its safeguards and
  the circuit breaker, over generated policies, zones, timelines and clocks.
- `mbt_profiles.py`: the profiles and the seeding themselves.
- `mbt_cache_rollback.py`: `cst btrfs cache` attach and detach for two
  users and the three kind selections, through the unit tests' fake
  findmnt, mount and systemctl (`btrfs/tests/fakes.py`), with a bind mount
  or the daemon-reload failing: `/etc/fstab` names exactly the mounted
  binds, a failed command leaves `/etc/fstab` and the mounts as they were,
  and the same command with no fault exits 0.
- `mbt_scrub_rollback.py`: `cst btrfs scrub-limit` set and off from four
  starting `snapshots.conf` files, over the in-memory fakes of its ports
  (`ScrubWorld` in `btrfs/tests/fakes.py`: the drop-in through the real
  `InstallService` in memory, and what systemd has loaded), with the
  installer or the daemon-reload failing:
  `snapshots.conf` and the drop-in agree, a failed step leaves both files
  and their folders as they were, exit 0 means systemd loaded the limit,
  and btrfs:core's state never changes.
- `mbt_iocost_apply.py`: `cst btrfs iocost` apply (after an upgrade of
  CST or not) and revert, live or not, with any one step failing (a file's write or removal, the
  io.cost.qos write, daemon-reload, enable, disable, the udev rules' reload,
  the installer's mark), over the in-memory fakes of iocost's ports
  (`IocostWorld` in `btrfs/tests/fakes.py`: the installer and the removal
  through the real `InstallService` in memory, what systemd and udev have
  loaded, and io.cost.qos): the component is marked done only after every
  step it stands for (a7c5e92), exit 0 leaves the whole target, a vendor's
  io.cost.qos line stays, and apply or revert again converges.
- `mbt_game_state.py`: the game profile's `game.state` and
  `game.intended`, whose fields are written as bash's `printf %q` writes
  them, over fields generated as bytes (any but NUL, as a bash variable
  holds them): our reader reads what our writer wrote, an older bash copy's
  `eval` reads back the exact bytes, `record_line` is bash's own
  `printf %q` line, a line bash wrote reads back, a record cut short is
  left out and removed before the next one, and `game.intended`'s drift
  records keep their six fields. A known limit of the reader is pinned: a
  field that is not UTF-8 reads back decoded with U+FFFD. No writer is left
  for one: the game profile leaves a cgroup folder whose name is no unit
  name alone, and systemd names the scopes.
- `mbt_game_profile.py`: a state machine over the game profile's `on`,
  `tick` and `off` (through `locked`), with the unit tests' scratch and fake
  systemctl, whose daemon-reload sets each value from the override files and
  the drop-ins; between them the game starts and ends, Docker containers,
  Steam scopes and boxes appear (also a folder whose name is no unit name:
  no record or systemctl call ever names it), settings drift and someone
  else reloads, and
  each call may fail or die at any of its set-property calls and file writes
  (injected at the ports of `cpu/game/ports.py`: the `Faulty*` adapters of
  `cpu/tests/fakes.py` over the real ones, and `FaultyRunner` over the fake
  systemctl; no module is patched and nothing calls fsync):
  every change has its record in `game.state`, an `off` with no fault
  restores everything, `on` twice is `on` once, and a tick leaves no box
  half handled, also a box whose set-property failed (or whose process died
  between the record and the set) and a scope whose setting an `off` that
  was killed undid: the next tick sets them again. An `off` whose undo
  step fails undoes everything else and keeps exactly that record in
  `game.leftovers`, and the next tick while off (or the next `off`) puts
  it back.
- `mbt_swap_apply.py`: a state machine over `cst swap apply` through the
  unit tests' scratch and fakes (`swap/tests/test_swap.py`), over drawn and
  hand-edited settings, crypttab and fstab lines (CST's, an older CST's,
  twice, other options, written by hand, a folder), the five files apply
  writes whole (absent, CST's, someone else's, a folder), kernels,
  partitions, zram and install records: each refusal leaves every file as it was and
  runs no systemctl, sysctl or zram call, and an apply that exits 0 records
  swap's files and changes nothing more when run again. Its first run, on
  the code before, found that apply wrote crypttab before it refused an
  fstab it could not read, and that with an install record of a newer CST
  it wrote crypttab and fstab and started swap0 before the installer
  refused; apply now refuses both before its first write. Run on the code
  before R17's fix, it found that a folder in place of
  `/etc/tmpfiles.d/zswap.conf` was refused only after crypttab and fstab
  were written and swap0 started; apply now refuses a file it writes whole
  that it cannot read before its first write too. `mbt_game_state.py`,
  `mbt_game_profile.py` and `mbt_swap_apply.py` took 6.2 s together at
  `--jobs 1` and 2.8 s at `--jobs 4` (`--profile ci`, a shared workstation).
- `mbt_bulk_items.py`: btrfs-bulk's classify, apply and recover over four
  items (a plain move, one nested in it, a NOCOW item, an old subvolume),
  through the unit tests' scratch home (`btrfs/tests/bulk_fakes.py`: real
  renames and `cp`, fakes only for subvolumes, +C, the busy check, flatpak
  and the subvolume convert). The start puts each item absent, with data,
  empty, in place, as a subvolume, with copy-on-write files or with data on
  both sides; the rules are apply and recover with a kill, a failure or a
  full disk at one of their first 20 points, an app's write into an item's
  folder, busy, keep and settle. After every step no file the model holds
  is lost (it is at the item's path, or at a path its journal names), no
  `.bulk-old-` tree is without a journal, classify agrees with an oracle
  over the disk, and a run leaves a kept item alone; settle converges, and
  a further apply or recover changes nothing. It has no torn calls and no
  writes during a call: the fault module above has those.

The targets above the adapter machines are in memory, so they do not
depend on `TMPDIR`. A machine over adapters that write through
`write_atomic` fsyncs every file, many times slower on the btrfs home
than on tmpfs (the pilot measured about 20 times; see
[Test performance on a workstation](#test-performance-on-a-workstation)).
The cache machine writes real files under a temporary
folder; the bulk machine too, and it runs `cp` there. The scrub-limit and
iocost machines run over in-memory port fakes, so they cover neither how
the adapters write their files (modes, fsync, relabel) nor the commands
they run: the ports' contract tests (`btrfs/tests/test_scrub_limit_ports.py`,
`btrfs/tests/test_iocost_ports.py`) do. The game profile's machine runs
over its real adapters, and its contract test
(`cpu/tests/test_game_ports.py`) checks that each failure a `Faulty*`
adapter stages has the shape the real one gives, and pins what the real
adapters pass on. Each other machine over real files stubs fsync
(`tests.fault_injection.stub_fsync` in its `setUp`), so they do not
depend on `TMPDIR` either; the game profile's writes in `/run` make no
fsync (`durable=False`).

### Writing one: typing without ignore comments

Every file of `tests/mbt` is ty-clean and passes ruff's annotation rules,
like the rest of the repository:

- ty needs the `mbt` group in its environment (`invowk cmd test types` has
  it; by hand, `tests/uv-dev --group mbt ty check`).
- `assume(x is not None)` does not narrow the type: follow it with
  `assert x is not None`.
- A state machine runs through `profiles.run_machine(Machine)` in a test
  method of an `MbtTestCase`, not through `Machine.TestCase`: Hypothesis
  creates that class in `hypothesis.stateful`, where `tests/run_unit.py`'s
  workers cannot load it by name, and ty refuses a subclass of it
  (`unsupported-base`).
- A `teardown` of a machine takes `@typing.override`.
- A `bool` parameter of a rule or a `@given` test is keyword-only (`*,`):
  ruff's FBT001 counts positional ones against `RUFF_BUDGETS`, tests
  included.
- Reuse the unit tests' fakes by public names only: the architecture gate
  refuses a private name imported from another module. Never import a
  TestCase class into an mbt module: the loader would run its tests there
  too.
- Naive `datetime` bounds of `st.datetimes()` need a `DTZ001` entry in
  `per-file-ignores`, with its reason (the snapshot names are naive local
  wall clock).

## Mutation testing (optional)

A green test suite says the tests pass; it does not say what they check.
Mutation testing measures that. It makes one small change to a helper at a
time (a `<` becomes `<=`, an `and` becomes `or`, a number changes, a `return`
value goes away) and runs the helper's unit tests on each changed copy, a
"mutant":

- a test fails: the mutant is **killed**, the tests notice that change;
- every test passes: the mutant **survived**. No test checks what that line
  does. Some survivors are harmless ("equivalent" mutants, such as a change
  in a message nobody tests, or one that cannot change the result); the
  others point at a missing test.

One kind of equivalent mutant is common in the typed helpers: cosmic-ray
also changes the `|` of a type annotation (`den: float | None` becomes
`den: float + None`). Python 3.14 and later does not evaluate a function's
annotations when the function is defined or called (only when code reads
them, as `typing.get_type_hints` does), so those mutants survive. In
`survivors.txt` they are the `ReplaceBinaryOperator_BitOr_*` mutants on the
lines of a `def` (in `share` of `swap/domain.py`, 22 of its 35
mutants).

Survivors are a report to read, not a gate: the task exits 0 when the run
finishes, whatever the number of survivors. `test all` and CI do not run it.

```
invowk cmd test mutation swap/domain.py swap.tests.test_swap                     # lines changed since main
invowk cmd test mutation swap/domain.py swap.tests.test_swap --base HEAD~3       # lines changed since a ref
invowk cmd test mutation swap/domain.py swap.tests.test_swap --functions share,pct
invowk cmd test mutation swap/domain.py swap.tests.test_swap --all               # the whole file (slow)
```

The first argument is the Python file to mutate, relative to the top of the
checkout; the second is the unittest module that tests it. Only one of
`--base`, `--functions` and `--all`. `--functions` takes def names: every def
with that name counts, methods and nested defs too, with their decorators;
a name with no def is an error. `--base` uses `git diff REF` against the
working tree, so uncommitted changes count. It compares with REF itself, not
with the point where the branch left it: when `main` has moved on since, the
lines `main` changed count too (rebase first, or give
`--base "$(git merge-base main HEAD)"`). A file git does not track yet is new
as a whole, so all its lines count; a renamed file is new too, under its new
name. `--timeout SEC` (default 60) is the time one mutant's tests get before
the mutant counts as killed. `--out DIR` chooses the output folder (new or
empty, outside the checkout); by default it is a new folder under
`${XDG_CACHE_HOME:-~/.cache}/cst-mutation/`. Give it as an absolute path:
the task runs from the top of the checkout, so a relative one lands inside
the checkout and is refused.

It needs uv, bubblewrap (`bwrap`, Fedora package `bubblewrap`) and git. It
runs `tests/uv-dev --group mutation python3 -I -B tests/mutation/cst-mutation
...`, which takes cosmic-ray from the `mutation` group of `pyproject.toml`.
Only Python helpers (a `.py` file or a python shebang): there is no mutation
tool for the bash scripts.

**What a run does.** It never changes the checkout:

1. It copies the checkout's files (what `git ls-files -co --exclude-standard`
   lists, with their modes and symlinks; not `.git`) into a temporary folder.
   cosmic-ray only mutates files named `*.py`, so the copy gets a
   `HELPER.py` symlink to the helper, and the mutants are written through
   it, into the copy.
2. It writes `cosmic-ray.toml` into the output folder: the module, the
   timeout, the test command (`python3 -B -m unittest TEST_MODULE`, the
   system Python, like the unit tests) and the chosen lines.
3. Inside the sandbox, one after the other: a check that `bwrap` can build
   the sandbox here; `cosmic-ray baseline` (the tests on the unmutated copy);
   `cosmic-ray init` (find every possible mutation); `cr-filter-lines` (drop
   those outside the chosen lines); `cr-filter-pragma` (drop lines marked
   `# pragma: no mutate`); `cosmic-ray exec` (run the tests on each mutant,
   one at a time); `cr-report` twice.
4. It prints the numbers and deletes the copy.

The output folder keeps `survivors.txt` (each surviving mutant with its
diff: start there), `report.txt` (every mutant), `counts.json` (the numbers),
`session.sqlite` (cosmic-ray's session) and `cosmic-ray.toml`.

Exit status: 0 when the run finished (with or without survivors, and when
`--base` finds no changed lines: "nothing to mutate"); 1 when a step failed
or some mutants could not run normally ("incompetent" or "not run" in the
numbers); 2 for a usage error, a missing tool or a sandbox `bwrap` cannot
build, and when the baseline fails. A failing baseline stops the run before
anything is mutated: with tests that already fail, every mutant would count
as killed.

How long it takes: the tests run once per mutant, serially (there is no
`--jobs`). A few minutes for the lines of a pull request or a few
functions; tens of minutes for a whole helper.

**The sandbox.** A mutant can flip exactly the check that keeps a helper
away from the machine (`if dry_run:` becomes `if not dry_run:`), and some
helpers delete or move files or talk to systemd, Docker or the host. So
every cosmic-ray command runs inside bubblewrap, with this profile
(`sandbox_argv` in `tests/mutation/cst-mutation`):

- the whole system read-only (`--ro-bind / /`), a fresh `/dev` and `/proc`;
- `/run`, `/tmp`, the home folders (`/var/home`, `/home`) and the folders
  other filesystems are mounted in (`/var/mnt`, `/mnt`, `/media`) replaced by
  empty folders (tmpfs). One that is a symlink is skipped: on Silverblue
  `/home`, `/mnt` and `/media` point into `/var/home`, `/var/mnt` and `/run`,
  which are hidden. That hides every daemon socket (D-Bus, Docker, libvirt,
  journald, systemd and, in a toolbox, the whole host under `/run/host`),
  the real home and its sockets (such as `~/.cache/ibus`);
- every other place where the home can be seen, found in
  `/proc/self/mountinfo` and hidden too: the same filesystem mounted
  elsewhere with the home in it (CST's snapshot module mounts the whole
  btrfs, with the home subvolume, at `/var/mnt/btrfs-top`), a bind mount of
  a folder of the home, and every automount point (what it would mount is
  not known yet);
- bound back in after those, so that they are not hidden: uv's virtual
  environment and a folder of stubs, read-only; the copy and the output
  folder, writable. Nothing else is writable;
- no network (`--unshare-net`), its own process, IPC and session
  namespaces, and it dies with its parent;
- an empty environment (`--clearenv`) except: `HOME` and the `XDG_*_HOME`
  folders inside the copy (`COPY/.home`), `XDG_RUNTIME_DIR=/tmp/run`,
  `TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE=1`, and `PATH` with the stubs
  first, then `/usr/bin`;
- the stubs: `sudo`, `flatpak-spawn`, `systemctl`, `docker`, `podman`,
  `virsh`, `btrfs`, `restic` and `journalctl` fail with exit status 97 and
  say that they are not available. A call by absolute path
  (`/usr/bin/systemctl`) passes the stubs by, but finds no daemon to talk to,
  since `/run` is empty.

Without `bwrap` the task refuses to run (exit 2); there is no fallback
without the sandbox.

What the sandbox does **not** contain:

- CPU and memory: a mutant can loop or allocate until its `--timeout`
  ends it;
- reading: everything outside the hidden folders above can be read (but
  not changed), such as `/etc`, `/usr`, `/var/log` and `/proc`. A file (not
  a folder) of the home bind-mounted somewhere else would be readable too:
  bwrap can only put an empty folder over a folder.

The unit tests in `tests/mutation/tests/` check the profile option by
option (the binds after the tmpfs mounts, only the copy and the output
folder writable), the order of the steps with a fake `bwrap` and fake
cosmic-ray tools, the exit statuses, and that the checkout is unchanged
after a run, also when a mutant leaves symlinks in the output folder (the
reports are written as new files, never through a link). One test uses the
real `bwrap` when it can build the whole profile (CI's container usually
cannot, so it is skipped there): the copy is writable; a write under
`/var/tmp` is refused (the root is read-only) and a folder under `/tmp`
outside stays empty; and, six folders deep under `/run`, `/tmp`, the home
folders, `/var/mnt`, `/mnt` and `/media`, there is no Unix socket and no
folder that is the real home.

## Model checking with Quint (optional)

A unit test checks the runs someone thought of. A model checker checks every
run a model of the protocol can take. CST has two such models, each a
[Quint](https://quint-lang.org/) spec: `btrfs/tests/quint/docker_volumes_nocow.qnt`,
of docker-volumes-nocow's install and boot migration, with a crash or a
fault after any step; and `vm/tests/quint/nas.qnt`, of `cst vm backup
--nas` (see "The NAS backup's spec" below). TLC (the TLA+ model checker, run
through quint) explores every state each instance of a spec can reach and
checks that the spec's properties hold in all of them. What follows up to
that subsection is about the nocow spec, and holds for both where it says
how the tools run.

```
invowk cmd test quint                     # TLC on every instance, every property (a line per instance)
invowk cmd test quint --instance nocow_ab # one instance
invowk cmd test quint --spec FILE         # a planted copy of the spec instead of the checked-in one (with --instance when the checks come from more than one spec)
invowk cmd test quint --traces            # write the checked-in traces again, then run the replay
invowk cmd test quint --keep              # keep the work folder (its path is printed)
```

`invowk cmd check` does not run it; CI runs it when a change touches what it
checks, and the nightly runs it on main (see "In CI" below). It needs node and
npm, java (17 or later) and curl.

**The tools.** `tests/quint/cst-quint` puts every tool in place itself, under
`${XDG_CACHE_HOME:-~/.cache}/cst-quint`, never in the checkout:

- quint 0.33.1 with `npm ci --ignore-scripts` from `tests/quint/package.json`
  and `package-lock.json` (the lock pins every package with its hash, which
  `npm ci` checks), into a folder named after the lock's hash;
- the two programs quint would otherwise download on first use without any
  check: Apalache 0.62.4 (`apalache.tgz`, whose jar also runs TLC) and the
  Rust evaluator 0.7.1. Each download is checked against the SHA-256 written
  in `cst-quint`; a mismatch removes it and stops with both hashes. They are
  unpacked into `QUINT_HOME` (`~/.cache/cst-quint/quint-home`), where quint
  finds them and fetches nothing. A tool already there is used only when the
  stamp beside it records the hash that was checked.

About 240 MB in all, fetched once. quint, Apalache and TLC run from a new
work folder under `~/.cache/cst-quint/work` (they write `_apalache-out/` in
the folder they run in, and quint's TLC translation goes to `TMPDIR`, which
is set to it), removed at the end. Each `quint verify` (the TLC checks and
the Apalache traces) gets `--server-endpoint localhost:PORT`, with a port
free at that moment on IPv4 and IPv6, picked outside the kernel's ephemeral
range (`ip_local_port_range`: IANA's dynamic ports above it, 61000 to 65535
on Linux's default, so no outgoing connection is given it before Apalache
binds it): quint starts its own Apalache server there and stops it
when it ends, so two runs at once (two checkouts or agents) neither share a
server nor collide on quint's default port 8822, and a server something else
left on 8822 is never used. Every run is under `nice -n 10` and a time
limit (TLC 5 minutes per instance, Apalache 10 minutes, the simulator 5). TLC
gets an explicit config, `{"maxHeap": "-Xmx4g", "workers": 4}`, and
`JVM_ARGS=-Xmx4g`: quint's defaults are an 8 GB heap and a worker per CPU.
Each line gives the instance's verdict, its distinct states, the depth, the
time and the largest resident memory of its process tree (the JVM included).

The unit tests in `tests/quint/tests/` check the checksums (a match, a
mismatch, a folder without the stamp), the `npm ci` call, the command
lines, the verdicts, the trace normalizer and the refusal of a trace run that
did not reach its witness, with a fake runner; no tool or
network is used. `tests/test_quint_task.py` checks the task's flags.

**What the spec models.** One action per call of
`btrfs/docker_volumes_nocow/application.py` that changes the disk or the
helper's files (the methods of `VolumeTreePort` and `NocowStatePort`), in
the code's order: `install`'s `mark` (with copy-on-write data, the request
first: an old count reset, then the flag, then +C on the volumes folder;
without, the count forgotten, then +C; a forget that fails ends mark there,
with the count kept and no +C), after which `btrfs-setup.sh` writes
nothing whatever mark's exit status; each boot's
`migrate` (the recovery of an interrupted migration, volume by volume; the
folder's +C and its request; the give-up check; the counter; the rewrite of
each volume: twin, label, copy, sync, the two renames, the old copy's
removal; `give_up`); and Docker recreating a volume. A crash can come after
any step: a kill, or a power loss that also loses a copy not yet synced. A
step can fail where the code checks the outcome (a rename, the flag's
removal, the counter, install's forget of it, the copy). The label each action leaves in `step` is
the call the replay records from the real code, so the spec's traces can be
replayed against it. A start without the flag may hold a count of boots an
older helper left (any count up to `GIVE_UP_AFTER`): with an install that
touched the flag beside it after a failed reset, `attemptsMatch` fails. A
volume's folders hold contents, not files ("cow",
"copy", "partial", "empty", "absent"). The spec's header lists what it does
not model: `scan(fix)`, the postponed record, the outcome record (written
and removed best effort, read by no decision of `migrate` or `mark`),
journal lines and alerts,
`daemon.json`, a dockerd outside docker.service, `status` and `unmark`,
a failing `remove_tree`, containers writing during an install, the
durability of metadata, and the start an older install could leave (+C on
the folder, copy-on-write data, no flag), which `requestKept` rules out:
unit and tier 2 tests check that an install requests the migration again
from there. A power loss loses only a copy's unsynced content:
the flag (`/etc`), the counter (`/var`) and the data root may be on
different file systems and roll back apart, and the code keeps the pairs
that matter in order by fsync (the flag before +C, the counter's reset
before the flag), which the unit tests check instead.

Five instances bound the search: two volumes (`nocow_ab`), one
(`nocow_a`), two with boots only (`nocow_ab_boots`: no install, Docker
never recreates a volume), two with more crashes and installs
(`nocow_ab_wide`), three volumes (`nocow_abc`). Each allows
`GIVE_UP_AFTER = 2` boots, as the code does. All five together take a
little over a minute and under 2 GB (about 6.5 million distinct states).

**The properties**, checked in every state of every instance:

- `noDataLoss`: each volume's `_data` holds the original or a complete
  copy of it, or an old copy (the original, which the next boot's recovery
  puts back) is there.
- `requestKept`: +C on the volumes folder with copy-on-write data left
  means the flag is there, unless the helper gave up. +C first and the flag
  after would lose the request for good: no later boot looks again.
- `attemptsMatch`: while the flag is there, the counter holds no boot its
  round did not have (no early give-up).
- `boundedTries`: at most `GIVE_UP_AFTER` rewrites in one round.
- `cleanRunConverges`: a run with no fault and no crash that chose to
  rewrite ends with every volume a NOCOW copy, no leftovers, no flag and no
  counter.

**Why two of them count per round, not per request.** The code removes the
counter before the flag (at the give-up and when every volume is
rewritten), so a later request never inherits a finished one's boots. A stop
between the two removals leaves the flag with no counter, and the next boot
starts that request's count over: up to `GIVE_UP_AFTER` more rewrites. The
code also saves the counter before a rewrite starts, so a stop between the
two counts a boot that rewrote nothing. "The counter equals the rewrites of
this request" and "at most `GIVE_UP_AFTER` rewrites per request" therefore
do not hold, on purpose. The spec states both per round instead, with ghost
variables (not on disk) that change only on a decision or an outcome of the
code, never on a write of the counter (a ghost that moved with the counter
would make the properties hold by construction): a round starts at a new
request (a flag created where there was none), at install's decision to
request again, and at the decision to end the request (the give-up, or every
volume rewritten); `boots` counts the round's boots that chose to rewrite,
`rewrites` the rewrites started, and `carried` the count when the code
decided to end the round (a failed removal of the flag puts it back). The
witness `wNewRound` shows that a round started after a stop between the two
removals is reachable. The properties still catch the orders that matter: a
planted copy of the spec that removes the flag before the counter violates
`attemptsMatch`, and one that saves the counter after the rewrite violates
`boundedTries`.

The owner accepted (2026-11-24) two things the spec shows here, and both stay
as they are. The bound is per round, not per request: a stop between the
counter's removal and the flag's starts the count over, while removing the
flag first would leave a counter that a later request inherits, so that
request would give up too early. And `mark()` may run while a rewrite is
between its two renames: the volume then has no `_data`, so `mark()` does
not see its data and may remove the counter while the flag stays. Nothing is
lost: the next boot's recovery puts the original back as `_data`, and the
request (the flag) is still there, so that boot migrates it, with its count
started over. Closing either gap would need a lock or a journal shared by
install and the boot, for a case that costs at most `GIVE_UP_AFTER` more
rewrites.

**Where `sync()` may go.** The copy is synced before `rename(_data,
old)`. Moving the sync to just after that rename (still before
`rename(new, _data)`) is safe, and the checker says so: a power loss before
the second rename leaves the original as an old copy, which recovery puts
back. Unsafe are the sync after the second rename, or no sync at all: a
power loss then leaves a partial `_data` beside the old copy, the next
boot's recovery sees a `_data` and removes the old copy, and
`noDataLoss` fails.

**The replay.** `btrfs/tests/test_docker_volumes_nocow_replay.py`, in the
unit suite, replays the checked-in traces of the spec
(`btrfs/tests/quint/traces/*.itf.json`) against the real code: the use
case over its real adapters, with the fakes of the module's unit tests
(NOCOW ioctls, the commands, `os.sync`). Each trace's start becomes the
volumes on disk, the folder's +C, the flag and the counter; each run of the
trace (a boot, an install, Docker recreating a volume) gets the faults the trace picked, each at its
call's position in the run, and dies where the trace crashes (a power loss
also spoils the copies the spec says it loses). Every call the code makes
to the volume tree and to the flag and counter files is recorded with the
spec's label; the calls of each run must be the spec's, in its order, and
after each run each volume's `_data`, twin and old copies, the folder's +C,
the flag and the counter must be the spec's state. A second test checks
that the traces, together, replay every recorded call and outcome of the
spec. `CST_NOCOW_TRACES` names a folder of more traces to replay.

The traces are two random walks of the simulator, the shortest trace
Apalache finds to two install witnesses (its bounded check answers in
seconds for a few steps, but took more than 7 minutes for a 32-step one),
and the first trace the simulator finds to each other witness a random walk
rarely reaches (a restore, a failed
recovery, a kept flag after a give-up, a power loss with a partial copy, a
failed install request, a new round, ...). The new round's trace comes from
the spec's `step_round_end`, the steps of `step_` with a crash only where a
request ends: a walk of `step_` spends its crashes long before a rewrite
ends and almost never stops there. `TRACE_JOBS` in
`tests/quint/cst-quint` lists them with their seeds and bounds, and
`invowk cmd test quint --traces` writes the same bytes again (one thread,
the seed fixed, the absolute path and the time taken out). A witness's run
must end with a violation of its negated invariant, and a walk's with exit 0:
otherwise (the witness not reached within its bounds, a time limit, an
error) the checked-in trace is left as it is and the run stops with exit 1. About 300 KB in
all; the replay takes a fraction of a second.

What the replay does not compare: `scan(fix)` setting +C in place on
folders and empty files, the postponed record, the outcome record, journal
lines and the alert, `daemon.json`, a dockerd outside docker.service, `status` and `unmark`,
a failing `remove_tree`, containers writing during an install, and the
durability of metadata (the unit tests and the fault tests cover those).

**A change of order in the module** changes the spec and its traces in the
same commit (one action per call, in the code's order): the replay fails
on a code change the spec does not follow, and the CI job on a spec change
that breaks a property.

### The NAS backup's spec

`vm/tests/quint/nas.qnt` models `cst vm backup --nas`
(`vm/application/nas.py`: `backup_nas`, `nas_run` and what they call), one
action per port call that changes the machine, the repository or the
records, or whose answer the code branches on, in the code's order: the
preflight's refusals, `restic_ready`, each attempt of the live snapshot
(`virsh snapshot-create-as` and its poll: every disk switched, none, or a
timeout with some not switched), the stray files' drop, `restic backup`,
the shut-off VM's check after the read, `forget_torn` (the record, the
forget, the record's entry dropped), the merge back (`find_overlays`,
`domstate`, `blockcommit` per disk, the two `domblklist` listings, the
overlay's removal, the alert), `retry_torn`, the check of the snapshots no
record names (`unaccounted`: the listing and its alert), the retention and
the index, or, after a stop that came once the read was checked, the index
of that backup alone before the stop ends the run.
Between any two steps the environment can stop the run (SIGTERM, Ctrl-C:
it ends the run before the read, is the read's failure during it, waits for
the end of a held block, is ignored during the merge back and its alert,
and ends the run after it; one per run, as `on_signal` ignores the rest), kill it, shut the
guest down, pause or resume it, start a shut-off VM during its read, or
switch a disk late to a file of a snapshot that timed out (before the next
run). The user can start or shut down the VM between runs, and adopt
(`restic tag --add cst-adopted`) or forget a snapshot the strays alert
named. A disk's source
is its image or the overlay of run r, in the live and in the persistent
definition; snapshot ids are run numbers; ghost variables (not on the
machine) hold what the properties need: the writes an overlay holds, an
overlay older than its image after a pivot that left the definition on it,
the files libvirt may still switch to, what each alert and message named.
The spec's header lists what it does not model: `--atomic` is assumed (a
settled answer switched every disk or none), two VMs and the record's lock,
several hosts, the notes and words of the messages, the manifest, the UEFI
and TPM files read live, the clock, libvirt crashing, other index entries,
an index or a record of a newer cst vm, and restic's monthly buckets (the
retention keeps the `KEEP` newest, one run a week, and every kept one).

Six instances are checked: a running VM with one disk and three runs, the
quiesced attempt first (`nas_live1`), two disks and two runs
(`nas_live2`), a shut-off VM with three runs and `--keep-tag` or not
(`nas_shutoff`), the VM's state changed by the user between two runs
(`nas_mixed`), `nas_live2` without `--atomic` (`nas_partial`: a
settled answer can switch some disks only), and a shut-off VM with four
runs, one kill and one step of the user and no stop or guest event
(`nas_strays`: a backup indexed, a snapshot the kill left that no record
names, the run that names it, the user adopting or forgetting it, and a
quiet run); `KEEP = 2`. Together about 9.0 million distinct states in
about 3 minutes, each run of TLC at most about 2 GB: `nas_live1` 5.93
million states, depth 47, 54 s, 1.7 GiB; `nas_live2` 998 thousand, depth
35, 21 s; `nas_shutoff` 613 thousand, depth 34, 17 s; `nas_mixed` 330
thousand, depth 33, 18 s; `nas_partial` 1.06 million, depth 35, 25 s, 1.6
GiB; `nas_strays` 24 thousand, depth 40, 17 s (measured on a workstation;
each instance far under `TLC_TIMEOUT`).

**The properties:**

- `mergedOrAlerted`: after a run, however it ends (a kill aside), each
  disk is back on its image in both definitions with its overlay file
  removed (or a note says the file is left), or the alert named the overlay.
- `noOverlayLost`: an overlay file is never removed while a definition
  names it, libvirt may still switch to it, or it holds writes the image
  lacks.
- `staleNamed`: a definition never names a stale overlay unless the alert
  named it, and never as "offline" (`qemu-img commit` of a stale overlay
  would overwrite newer data).
- `unreachableNoOverlay`: a run whose repository did not open made no
  overlay and changed no definition.
- `noStacking`: no snapshot is taken while a disk is on an earlier run's
  overlay (a killed run's included: the next run refuses).
- `tornHandledFull`: every torn snapshot in the repository (incomplete,
  stopped after restic saved it, or of a shut-off VM started during the
  read) is recorded for the next run, or a message named it, or a kill
  left it (the known gap below). `tornHandled` also excuses a stop's gap,
  which nothing leaves any more: it is implied now, and kept checked.
- `indexClean`: the index never names a torn snapshot, and an entry
  leaves it only when restic's listing was read.
- `goodKeptFull`: the retention never forgets a good snapshot while fewer
  than `KEEP` good ones newer than it stay, unless a torn snapshot a
  message named, or a kill left, is in the repository. `goodKept` (a
  stop's gap excused too) is implied now, and kept checked.
- `keepKept`: a snapshot made with `--keep-tag` is never forgotten by the
  retention.
- `consistentRead`: a snapshot is indexed only when its read was
  consistent (also without `--atomic`: `nas_partial`).
- `indexInRepo`: after an index write with the listing read, every entry
  names a snapshot in the repository.
- `strayNotCounted`: a retention that runs never has in the repository a
  snapshot newer than the oldest indexed one that is neither indexed,
  recorded, adopted nor the run's own (it would count it as a backup).
- `goodKeptAll`: `goodKeptFull` without its excuses: the retention never
  forgets a good snapshot while fewer than `KEEP` good ones newer than it
  stay, unless a torn snapshot the check of the snapshots no record names
  cannot see (older than the oldest indexed one, or any while no listed
  snapshot is indexed) or one the user adopted is in the repository. A
  kill's torn snapshot is excused no more: the next run names it.
- `checkedIndexed`: a run that a stop ended after its read was checked
  (the shut-off VM's check passed, or a running VM's read of its snapshot),
  with every disk merged back, has its snapshot in the index, so the next
  run's check of the snapshots no record names does not name a backup; the
  same for a stop after a good read's merge back (in the retention, a
  listing or the index's write).

**Known gaps.** A torn snapshot restic committed can stay in the repository,
neither recorded nor named, after a kill before its record (nothing more
runs). A stop that ended restic once it had committed carries the
snapshot's id (`StopAfterSnapshot`: `Restic.backup` reads it from restic's
last lines), so the snapshot is recorded; assumed, not modelled: restic
ended between saving its snapshot and printing its summary. From restic's
answer with a snapshot on, the stops are held (`_ResticAnswer`: the
staging folder's removal, the shut-off VM's check) until a torn snapshot's
record is written or the check is done: the spec has no stop between two
steps there, only the held stop of `after` and `record`. `tornHandledFull`
and `goodKeptFull` allow the kill only (a stop's gap, `lostStop`, is no
longer set). The instance `nas_partial` drops the `--atomic` assumption:
a settled answer that switched only some disks is refused (`_refuse_partial`: the others' files
dropped, the switched ones merged back), so `consistentRead` holds there;
the stray files of a settled answer are dropped in the snapshot's held
block, so a stop held through the call is raised only after them.

The check of the snapshots no record names compares ids, not restic's
times: ids are run numbers, so in time order. A snapshot whose tags were
changed (`restic tag` saves it under a new id, with `original`) is not
modelled: the unit tests and tier 1's `test_203` cover it. Its gap: a
stray older than every indexed snapshot of the VM (its first runs, or after
the index was moved aside) is not seen, which `goodKeptAll` states.

A check after the read that raises (an error of libvirt or of a file read,
not a change it found) is not modelled: after a stop that ended restic its
snapshot is recorded as torn, else the error ends the run; the unit tests
cover both.

**What the checker found.** Two paths lost the alert of a snapshot whose
outcome stayed unclear (fixed with the spec, in the same pull request): a
stop that came during the held snapshot call was raised before the check
that alerts, and a failed merge back of another disk dropped the unclear
one from the alert. Four more, fixed in a later pull request, each with its
property: a stop that ended restic after it saved its snapshot lost the
snapshot's id (`tornHandledFull`, `goodKeptFull`), a stop between
restic's answer and the torn record left the snapshot unrecorded (the
same), a settled answer that switched only some disks was read as a
backup (`consistentRead` on `nas_partial`), and a stop held through
such an answer, or through a failed one, left its stray files
(`mergedOrAlerted`). With this pull request's `cst-quint`, the spec of
the commit before it fails on `nas_shutoff` and `nas_live1` (a torn
snapshot left by a stop: `tornHandledFull`) and on `nas_partial` (a
stray file after a stopped run: `mergedOrAlerted`). Planted copies of the spec show that the properties
catch the orders that matter: an overlay removed when only the live
listing shows the image (`noOverlayLost`), the retention before
`retry_torn` (`goodKept`), the forget before the record
(`tornHandled`), `restic_ready` after the snapshot
(`unreachableNoOverlay`), the merge back with the stops caught
(`mergedOrAlerted`), the stray files dropped after a timeout
(`noOverlayLost`), no refusal of a leftover overlay (`noStacking`), the
index dropping entries on a listing that failed (`indexClean`), the
two old paths of the fix (`mergedOrAlerted`), and the retention without the
check of the snapshots no record names (`strayNotCounted` and
`goodKeptAll`, on `nas_strays` and `nas_shutoff`), and the run a stop
ends after a checked read without indexing it, as before the index of such a
backup (`checkedIndexed`, on `nas_shutoff`).

**The replay.** `vm/tests/test_vm_nas_replay.py`, in the unit suite,
replays the checked-in traces of the NAS spec
(`vm/tests/quint/traces/*.itf.json`) against the real code: `backup_nas`
once per run of the trace, over the in-process fakes of
`vm/tests/test_vm_nas.py` (libvirt, the files, restic, the stop signals,
the clock), which serve a World: the VM's state, each disk's source in the
live and the persistent definition, the overlay files, the repository's
snapshots, the torn record and the index. Every port call the spec labels
is recorded with the spec's label and gets the trace's outcome at its
position (virsh's exit status, restic's answer, a record that fails); what
libvirt and restic do on their own (which disks a snapshot switched, a
pivot that leaves the definition on the overlay, which snapshots the
retention forgets) is the spec's state after that step. A stop between two
steps is raised by the code's next port call; one that came in a held block
(the snapshot call and its poll, the torn record, the hold from restic's
answer on) at the end of the outermost one (the fakes count the blocks
entered); one during the merge back is dropped. The hold from restic's
answer on holds only the stops, so the environment still acts at each port
call in it; the snapshot's and the record's blocks freeze it, except before
each stray file's drop, a step of the spec of its own, and so does the rest
of the shut-off VM's check after the read once its `domstate` is
recorded (its managed-save look and its fingerprints are of that one step),
until the next recorded call. A stop that
ended restic after its snapshot (`StopAfterSnapshot`) is carried and
raised after the record or the check, as the code does. A kill makes that call and every later
one raise, so the World stays as the kill left it; the guest and a late
switch of libvirt change the World where the trace has them, and the user's
adopt or forget between runs its repository (restic's listing gives each
snapshot the time of its run and, once adopted, the tag `cst-adopted`). After each run
the calls must be the spec's, in its order, the run's end (done, refused,
stopped, killed) the spec's, and the World the spec's state; after an alert
the refusal must name the overlays the spec's alert named, each with its
recovery kind, and a torn snapshot the spec says a message named must be in
the run's message as `restic forget ID`. A second test checks that the
traces, together, replay every recorded call and outcome of the spec.
`CST_NAS_TRACES` names a folder of more traces to replay.

The traces are two random walks (`nas_live1`, `nas_shutoff`), the first
trace the simulator finds (seed 7, 40 steps) to each witness of the spec
(`never...`: a stray file dropped, a snapshot that timed out, an unclear
one alerted, each recovery kind, a failed commit, a disk put back beside
an alerted one (`nas_live2`, two disks), a listing that failed, a
backup restic refused, a VM state that could not be read, a
torn snapshot forgotten or not, a backup indexed after a stop (shut off,
running, and after the merge back), a good run indexed, ...), three from
`step_calm`, the steps of `step_` without the environment's: a torn
snapshot recorded by one run and retried by the next, the retention
skipped for it, and the retry's listing that failed, which a walk of
`step_` rarely reaches within its bounds; and two on `nas_strays` (60
steps): a snapshot no record names named, and its listing failed. The quiet
run after the user adopted or forgot a named snapshot has no trace: TLC
reaches it (`neverStrayAdoptedQuiet` and `neverStrayForgottenQuiet` fail
on `nas_strays`), but no walk of the simulator does within its bounds, so
the replay of the user's adopt and forget is not exercised by a checked-in
trace; the unit tests and tier 1's `test_203` cover that convergence.
`TRACE_JOBS` in `tests/quint/cst-quint` lists them; about 900 KB in
all, and the replay takes a fraction of a second. Planted reorders of
`vm/application/nas.py` fail it: an overlay removed before the definition
is checked, the retention before `retry_torn`, the forget before the
record, `restic_ready` after the snapshot, the merge back with the stops
not ignored.

What the replay does not compare: the notes and the words of the messages
(beyond the recovery kinds and the `restic forget` lines), the progress
lines, the manifest and the staged files, the UEFI and TPM files read live,
the `domfsthaw` calls, the poll's timing, the record's lock, the index
entries' fields other than their snapshot, and the real signal masks (the
fake signals port follows the modes of `StopSignals`; the tests of
`vm/tests/test_vm_nas.py` that send `cst-vm` its signals cover them).

**In CI.** The `quint` job of `.github/workflows/ci.yml` runs `invowk cmd
test quint` when a pull request (or a push to main) changes a path under
`btrfs/docker_volumes_nocow/`, `btrfs/tests/quint/`, `vm/tests/quint/` or
`tests/quint/`, `btrfs/tests/test_docker_volumes_nocow_replay.py`, the code
the NAS spec models (`vm/application/nas.py`, `vm/application/nas_torn.py`, `vm/adapters/restic.py`,
`vm/adapters/backup.py`, `vm/domain/backup.py`, `vm/domain/__init__.py`), its replay
(`vm/tests/test_vm_nas_replay.py`), what runs the job
(`.github/workflows/ci.yml`, `cst.invowkmod/invowkfile.cue`,
`.github/actions/setup-invowk/`), or what `cst-quint` imports (today
`cst_core/domain/errors.py`, `values.py`, `tests/xdg_cache.py` and their
packages' `__init__.py`);
`tests/test_quint_task.py` checks that list, the imports against the
host-Python gate's closure; `CI Status` accepts it
skipped only when none changed. It runs on the runner host, as the runner's
non-root user, with the runner's JDK 21 and node, and fetches its tools
afresh (about 10 s; no GitHub cache, each download checked by its SHA-256).
The nightly's `quint-nightly` job runs it on main too, which catches a
drift of the runner or of a download between such changes. A pull request
that changes the spec and its traces together passes the replay whatever
the spec says; only TLC tells that the changed spec still holds its
properties.

## Layout-only commits and `git blame`

`.git-blame-ignore-revs` at the top of the repository lists the commits
whose code changes are layout only, such as the `ruff format` run of #16.
`git blame` skips them and shows the commit that last changed what each line
does. GitHub reads the file by itself. For `git blame` on your machine, run
once per clone:

```
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

Add a commit there only when its code changes are layout only, by the full
SHA that landed on `main` (the squash commit of a one-change pull request,
or the layout commit itself inside a batch merged with a merge commit), with
a comment naming its pull request. `git blame` skips the whole commit, so any config or doc lines it
changed are skipped too (#16 also added the format gate to the config and
the docs; #32, the SPDX header line in every file, also added the header
gate and its fixer).

## Known vulnerabilities in the pins (`uv audit`)

`invowk cmd ci uv-audit` runs `uv audit --locked --preview-features
audit-command`: every package of `uv.lock`, in every group (`dev`,
`mutation`, `coverage`), is looked up in the public vulnerability database,
and a known vulnerability fails it. `--locked` stops it when `uv.lock` does
not match `pyproject.toml`; `--preview-features audit-command` only silences
the notice that the command is new in uv 0.12. It needs the network, so the
nightly workflow runs it, in a job of its own (`uv-audit`, in the Fedora 45
image: the project needs the system's Python 3.15), so that a new
advisory or a database that does not answer fails that job only and the
nightly's static gates still run; `check` does not run it.
When it fails, upgrade that pin (below), or the tool that pulls it in.
`tests/test_workflow_audit.py` fails when the nightly stops running the task
(the step must be `run: invowk cmd ci uv-audit` on one line, not a comment),
when that step is not the last of its job or the job has an `if:`, a
`continue-on-error:` or a `needs:` (skipped with the job it needs), when a
uv variable (`UV_...`) appears in the nightly or the task sets a variable
(an `env` block, a `UV_...` name), or when the task runs anything but exactly that command: dropping `--locked`
fails, and so does any word added, such as an option that leaves packages
or vulnerabilities out (`--no-group`, `--only-dev`, `--no-extra`,
`--ignore`, `--ignore-until-fixed`, `--frozen`, `--service-url`), a
variable or `|| true`.

## Upgrading a pin

One tool per pull request, so that new findings are clearly the new version's:

1. Change the version in `[dependency-groups]` of `pyproject.toml`. For
   ruff, also `required-version` in `[tool.ruff]` (the same version, with
   `==`; `tests/test_python_files.py` fails when the two differ).
2. Run `uv lock` (it rewrites `uv.lock` with the new hashes).
3. Run `invowk cmd test pylint` and `invowk cmd test types`. Fix what the new
   version finds, or explain in the pull request why it is noise. A new ruff
   can also change or drop the preview rules named in `select` (read their
   findings, and the release notes for the ones gone) and lay out code
   differently: when the format check fails, run
   `tests/uv-dev ruff format` and commit the result in the same pull request.
4. For Hypothesis, run `invowk cmd test mbt --profile ci` and
   `--profile nightly`. A new version can draw other examples, also
   derandomized ones, so a new failure on unchanged code can be a real bug
   the old draws missed: read it before calling it noise.
5. For cosmic-ray, the gates above do not run it: run it for real, with
   `invowk cmd test mutation swap/domain.py swap.tests.test_swap --all`,
   and put its numbers (mutants, killed, timed out, survived, minutes) in the
   pull request next to the previous version's. A new version can add or
   change mutation operators; read the new survivors.

For uv itself, change `version` and `checksum` of the `Setup uv` step in
`.github/workflows/ci.yml` together. The checksum is the content of
`uv-x86_64-unknown-linux-gnu.tar.gz.sha256` in that uv release. The action
`astral-sh/setup-uv` is pinned by its full commit SHA, with the tag in a
comment.
