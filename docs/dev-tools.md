<!-- SPDX-License-Identifier: MPL-2.0 -->
# Developer tools: uv, ruff, ty, zizmor, coverage.py, cosmic-ray

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
| [cosmic-ray](https://cosmic-ray.readthedocs.io/) 8.9.0 | mutation testing, for the optional `invowk cmd test mutation` (its own `mutation` group; CI does not install it; see "Mutation testing") | `pyproject.toml`, `uv.lock` |

**ty is beta.** Its versions are 0.0.x and a new one can report new errors on
unchanged code. That is why it is pinned exactly and upgraded on purpose (see
"Upgrading a pin").

## Running them

```
invowk cmd test pylint        # ruff check (F, B, UP, E9, PLW1510), ruff format --check, then the ruff budgets: every Python file
invowk cmd test types         # ty check on the ty gate list, then ruff check --select ANN on every Python file
invowk cmd test workflows     # zizmor --offline on .github, with the exceptions in .github/zizmor.yml
invowk cmd test coverage      # the unit suite under coverage.py, its reports, then COVERAGE_FLOORS (slow; the nightly runs it)
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
still runs on the system Python with the standard library.

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
place in the repository that runs uv on this project (`uv run`; the invowk
tasks, `tests/ruff_budget.py` and CI all go through it; `cst toolchains`
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
- **ruff rules**: `F` (pyflakes), `B` (bugbear), `UP` (pyupgrade), `E9`
  (syntax errors) and `PLW1510` for every Python file (`test pylint`). `ANN`
  (annotations) is not in the selected rules: `test types` runs it on its
  own (`ruff check --select ANN`), on every Python file (see "ANN applies
  to every file" below).
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
  make it keyword-only) and `C901` (a function over
  `[tool.ruff.lint.mccabe] max-complexity`, 15). C901 stands in for
  SonarCloud's cognitive complexity rule (S3776), which only a Sonar run
  measures. A second ruff run fails on any `# noqa` that suppresses nothing
  (`RUF100`). When a budget fails, the script prints the measured table;
  see [tests/budgets.py](../tests/budgets.py) for how budgets work.
- **The typing budget** (`tests/test_typing_budget.py`, in the unit suite):
  `Any` (inside generics: `ANN401` already refuses it as a whole
  annotation) and `cast()` are counted per package and layer, the tests
  apart, in `ANY_BUDGETS` and `CAST_BUDGETS`, which they may not go above. Type the
  value instead: a `TypedDict` for a record read from JSON, a frozen
  dataclass for one built in code, `isinstance` narrowing instead of a cast.
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
  `# fmt: skip` to get a green run. Markdown files are excluded
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
- **The ty gate, `[tool.ty.src] include`**: every Python file ty reports
  nothing on. `error-on-warning` is on, so a warning fails it too. Every
  Python file joins it; never remove one, or add an ignore comment, to
  get a green run.
- **`NOT_YET_CLEAN` in `tests/test_python_files.py`**: the historical exclusion set,
  now empty (`frozenset()`). Every Python file in the repository is in the ty
  gate (`[tool.ty.src] include`), so every new file has to join both the ty
  gate directly. The set sits in the test, not in
  `pyproject.toml`, so that adding a file to it would be a visible edit of the
  test. Never add one.
- **Nothing else switches ty off**: the test also fails when a `ty.toml`
  exists at the top of the repository (ty would read it instead of
  `pyproject.toml`), when `[tool.ty]` sets anything but `src.include`,
  `terminal.error-on-warning` and rules raised to `"error"` (such as
  `src.exclude`, `overrides`, a rule set to `"warn"` or `"ignore"`, or
  `analysis.allowed-unresolved-imports`), when `error-on-warning` is not
  `true`, and when a ty-gated file has a `ty: ignore` or `type: ignore`
  comment.
- **`[tool.ty.rules]`** raises rules that are warnings or off by default to
  errors: `deprecated`, `division-by-zero`, `possibly-missing-attribute`,
  `possibly-missing-import` and `possibly-unresolved-reference`. A rule may
  be added there only at `"error"`.

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
finding), by the file's base name. Each list starts with a comment that
says why. A line entry moves with its file: when a change above it shifts
the line, the audit fails and names the new line; move the entry. Today:
the `GITHUB_PATH` write of `setup-invowk` (it checks the binary against
its pinned SHA-256 first), the style finding `self-repository` (`./...`
for local actions) and `superfluous-actions` on the release step.
`tests/test_workflow_audit.py` (a unit test) fails when an ignore list has
no reason, when an audit is disabled (`disable: true`, which also hides it
from zizmor's count of ignored findings), on an inline `zizmor: ignore`
comment in `.github`, and when `check` stops running the task, the task
drops `--offline`, the config or `--strict-collection`, or it gives zizmor
an option that leaves findings out (`--min-severity`,
`--min-confidence`, `--persona`, `--collect`). `tests/uv-dev zizmor
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
test ran), `floors.txt` and coverage.py's data file. The task stops when a
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
SHA that landed on `main` (the squash commit), with a comment naming its pull
request. `git blame` skips the whole commit, so any config or doc lines it
changed are skipped too (#16 also added the format gate to the config and
the docs; #32, the SPDX header line in every file, also added the header
gate and its fixer).

## Upgrading a pin

One tool per pull request, so that new findings are clearly the new version's:

1. Change the version in `[dependency-groups]` of `pyproject.toml`.
2. Run `uv lock` (it rewrites `uv.lock` with the new hashes).
3. Run `invowk cmd test pylint` and `invowk cmd test types`. Fix what the new
   version finds, or explain in the pull request why it is noise. A new ruff
   can also lay out code differently: when the format check fails, run
   `tests/uv-dev ruff format` and commit the result in the same pull request.
4. For cosmic-ray, the gates above do not run it: run it for real, with
   `invowk cmd test mutation swap/domain.py swap.tests.test_swap --all`,
   and put its numbers (mutants, killed, timed out, survived, minutes) in the
   pull request next to the previous version's. A new version can add or
   change mutation operators; read the new survivors.

For uv itself, change `version` and `checksum` of the `Setup uv` step in
`.github/workflows/ci.yml` together. The checksum is the content of
`uv-x86_64-unknown-linux-gnu.tar.gz.sha256` in that uv release. The action
`astral-sh/setup-uv` is pinned by its full commit SHA, with the tag in a
comment.
