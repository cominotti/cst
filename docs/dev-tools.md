<!-- SPDX-License-Identifier: MPL-2.0 -->
# Developer tools: uv, ruff, ty, zizmor, coverage.py, Hypothesis, cosmic-ray

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

Conditions of these numbers: Python 3.15; 3 runs per cell, the median shown;
a shared machine, with another project's load at times (the spread was 1% to
3% at 8 workers or more, up to 39% at 2 to 4). The tree measured was not
`main`: it was `main` plus the verification proof-of-concept tests (3,354
tests), among them the fault injection tests in the form they had then, with
every fsync real. `main` has had those tests since, with fsync stubbed
([Fault injection tests](#fault-injection-tests)), so they no longer depend
on where TMPDIR is. The "about 12 times" is those tests as they were then;
the suite-level 8% to 13% was measured on that tree and may be smaller on
`main`.

Not adopted yet: queueing the longest work units first (6% to 12% less wall
time in the same measurements), to be measured again later.

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
  patch the names an adapter module calls (`ensure_dir`, `write_atomic`:
  `btrfs/tests/test_cache_faults.py`), put `FaultyOs(plan, names)` in
  place of a module's `os` (the functions named are points, the rest of
  `os` as it is: `btrfs/tests/test_scrub_limit_faults.py`,
  `toolchains/tests/test_prune_faults.py`), or put
  `FaultyRunner(plan, inner, reads=...)` in front of a fake runner, with
  `reads` naming the commands that only read (`btrfs/tests/test_iocost_faults.py`).
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
+C, the busy check, flatpak and btrfs-snapshot's convert), from eight start
states (a plain move, the in-place +C rewrite, a +C copy out of /home, an
old subvolume, a stale cache, an empty folder, a late write into the old
tree, and writes on both sides). `arm_points` makes the user adapter's
`os` calls (`FaultyOs`), the state files' writes and removals, cp
(`FaultyRunner`) and each tree's deletion (torn in two) points of the
plan, which is dead after a kill. After a kill every mode is tried again
inside the repair; after a failure or a full disk, only a kill. One known
gap is let through by name and counted per class: a fault that cuts the
deletion of the copy (`<target>.bulk-new`) while a rollback deletes it
leaves a journal that every later repair keeps ("the copy ... was written
to as well"), with the data back in /home.

**Cost.** The eight modules (`tests.test_fault_injection`,
`btrfs.tests.test_docker_volumes_nocow_faults`,
`btrfs.tests.test_scrub_limit_faults`, `btrfs.tests.test_iocost_faults`,
`btrfs.tests.test_cache_faults`, `toolchains.tests.test_prune_faults`,
`bluetooth.tests.test_setup_faults`, `btrfs.tests.test_btrfs_bulk_faults`)
together: 4.3 to 4.8 s wall at `--jobs 4` (`nice -n 10 /usr/bin/python3
-I -B tests/run_unit.py --jobs 4` on the eight, 3 runs, on a workstation
whose load average was 20 on 16 CPUs; the seven before bulk's took 2.7 s
there, and 2.3 to 2.4 s unloaded). btrfs-bulk's alone is about 10 s of CPU
in 24 classes, the longest (LateWriteKillTest) 1.5 s. fsync is stubbed, so
the times are about the same on tmpfs and on a btrfs home.

## Where the virtual environment lives

Never inside the checkout: `~/Workspace` is snapshotted, and a `.venv` there
would end up in every snapshot unless it is marked as a cache (below). `tests/uv-dev` (used by the invowk tasks) sets

- `UV_PROJECT_ENVIRONMENT=${XDG_CACHE_HOME:-~/.cache}/cst-dev-venv/<key>`
- `RUFF_CACHE_DIR=${XDG_CACHE_HOME:-~/.cache}/cst-dev-ruff/<key>`

where `<key>` is the first 12 hex digits of the SHA-1 of the checkout's
physical path (`pwd -P`): one environment per checkout or worktree. Deleting
these folders is safe; the next run creates them again. `.venv/` and
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
  `dict[str, Any | object | Json]` and a `Literal` of 3 or more strings are
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
`${XDG_CACHE_HOME:-~/.cache}/cst-coverage/`) receives `report.txt` (per
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
  starting `snapshots.conf` files, through the unit tests' `SystemdModel`
  (the installer through the real `InstallService`, and what systemd has
  loaded), with the installer or the daemon-reload failing:
  `snapshots.conf` and the drop-in agree, a failed step leaves both files
  and their folders as they were, exit 0 means systemd loaded the limit,
  and btrfs:core's state never changes.
- `mbt_iocost_apply.py`: `cst btrfs iocost` apply (after an upgrade of
  CST or not) and revert, live or not, with any one step failing (a file's write or removal, the
  io.cost.qos write, daemon-reload, enable, disable, the udev rules' reload,
  the installer's mark), through a model of the installer (the real
  `InstallService` in memory), systemd, udev and io.cost.qos: the component
  is marked done only after every step it stands for (a7c5e92), exit 0
  leaves the whole target, a vendor's io.cost.qos line stays, and apply or
  revert again converges. One known gap is let through by name: a full
  revert whose removal fails after its disable leaves btrfs:iocost done
  with the service disabled, until the next full apply or revert.

The targets above the adapter machines are in memory, so they do not
depend on `TMPDIR`. A machine over adapters that write through
`write_atomic` fsyncs every file, many times slower on the btrfs home
than on tmpfs (the pilot measured about 20 times; see
[Test performance on a workstation](#test-performance-on-a-workstation)).
The cache and scrub-limit machines write real files under a temporary
folder. The iocost machine writes only io.cost.qos, its sysfs tree and
the managed-devices file there: btrfs:iocost's installed files and the
install record are in memory (`MemoryTarget`, `MemoryRecord`), so it
does not cover how `LocalInstallTarget` writes them (modes, fsync,
relabel). Each machine's TestCase stubs fsync
(`tests.fault_injection.stub_fsync` in its `setUp`), so they do not
depend on `TMPDIR` either.

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
