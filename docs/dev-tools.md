# Developer tools for the Python code: uv, ruff, ty, cosmic-ray

CST's helpers are Python scripts that run as root on the system's
`/usr/bin/python3 -I`, with the standard library only. That does not change.
The tools on this page check that code while you work on it and in CI. They
are not part of CST: nothing installed on the machine uses them.

| Tool | What it does here | Pinned in |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) 0.12.0 | installs the tools below, at the exact versions in `uv.lock`, into a virtual environment outside the checkout | `.github/workflows/ci.yml` (CI); any uv 0.12 or later locally (`required-version` in `pyproject.toml`) |
| [ruff](https://docs.astral.sh/ruff/) 0.17.5 | linter: unused or undefined names, likely bugs, old syntax, syntax errors, `subprocess.run` without `check=`; missing type annotations in the typed modules. Formatter: one layout for all Python code | `pyproject.toml`, `uv.lock` |
| [ty](https://docs.astral.sh/ty/) 0.0.104 | type checker | `pyproject.toml`, `uv.lock` |
| [cosmic-ray](https://cosmic-ray.readthedocs.io/) 8.9.0 | mutation testing, for the optional `invowk cmd test mutation` (its own `mutation` group; CI does not install it; see "Mutation testing") | `pyproject.toml`, `uv.lock` |

**ty is beta.** Its versions are 0.0.x and a new one can report new errors on
unchanged code. That is why it is pinned exactly and upgraded on purpose (see
"Upgrading a pin").

## Running them

```
invowk cmd test pylint        # ruff check (F, B, UP, E9, PLW1510), then ruff format --check: every Python file
invowk cmd test types         # ty check on the ty gate list, then ruff check --select ANN on the typed list
tests/uv-dev ruff format      # rewrite the files the format check complains about
tests/uv-dev ruff check --fix # any tool command, by hand
tests/uv-dev ty check swap/swap-status
tests/uv-dev --group mutation cosmic-ray --version   # --group NAME adds a group to "dev"
```

You need `uv` on `PATH` (for example from Homebrew or
`https://docs.astral.sh/uv/getting-started/installation/`). The first run
downloads ruff and ty; later runs start at once. `--locked` makes uv stop
with an error when `uv.lock` does not match `pyproject.toml`, and uv checks
the hash of every package against the lock.

The unit tests do not need any of this: `python3 -I -B tests/run_unit.py`
still runs on the system Python with the standard library.

## Where the virtual environment lives

Never inside the checkout: `~/Workspace` is snapshotted, and a `.venv` there
would end up in every snapshot. `tests/uv-dev` (used by the invowk tasks) sets

- `UV_PROJECT_ENVIRONMENT=${XDG_CACHE_HOME:-~/.cache}/cst-dev-venv/<key>`
- `RUFF_CACHE_DIR=${XDG_CACHE_HOME:-~/.cache}/cst-dev-ruff/<key>`

where `<key>` is the first 12 hex digits of the SHA-1 of the checkout's
physical path (`pwd -P`): one environment per checkout or worktree. Deleting
these folders is safe; the next run creates them again. `.venv/` and
`.ruff_cache/` are in `.gitignore` in case a tool is run some other way.

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
  own (`ruff check --select ANN`), and the typed list below limits it to the
  typed modules.
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
- **The format check** (`ruff format --check`, the second half of
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
- **The typed list**: the `"!{...}" = ["ANN"]` entry of
  `[tool.ruff.lint.per-file-ignores]`. ANN is ignored everywhere except the
  files in the braces, whose functions must all have type annotations. Their
  record dicts are `TypedDict` classes, so ty can check the keys and value
  types. Today: `swap/swap-status`. Every typed file must also be in the ty
  gate. Add a file inside the braces of that one entry: no other
  per-file-ignores entry may ignore ANN, since ruff would then skip ANN for
  the typed files too and still report "All checks passed".
- **The ty gate, `[tool.ty.src] include`**: every Python file ty reports
  nothing on. `error-on-warning` is on, so a warning fails it too. A file
  joins once it is clean; never remove one, or add an ignore comment, to
  get a green run.
- **`NOT_YET_CLEAN` in `tests/test_python_files.py`**: the Python files ty
  is not yet clean on. Every Python file is in the ty gate or in this set,
  never both, so a new file has to join the ty gate. The set only shrinks:
  when a file becomes clean, move it to the ty gate and delete it from the
  set (the test says to). A renamed file keeps its entry under the new
  name. The set sits in the test, not in `pyproject.toml`, so that adding
  a file to it is an edit of the test that review sees. Never add one.
- **Nothing else switches ty off**: the test also fails when a `ty.toml`
  exists at the top of the repository (ty would read it instead of
  `pyproject.toml`), when `[tool.ty]` sets anything but `src.include` and
  `terminal.error-on-warning` (such as `src.exclude`, `overrides`, a rule
  set to `"ignore"`, or `analysis.allowed-unresolved-imports`), when
  `error-on-warning` is not `true`, and when a ty-gated file has a
  `ty: ignore` or `type: ignore` comment.

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
lines of a `def` (in `share` of `swap/swap-status`, 22 of its 35
mutants).

Survivors are a report to read, not a gate: the task exits 0 when the run
finishes, whatever the number of survivors. `test all` and CI do not run it.

```
invowk cmd test mutation swap/swap-status swap.tests.test_swap                     # lines changed since main
invowk cmd test mutation swap/swap-status swap.tests.test_swap --base HEAD~3       # lines changed since a ref
invowk cmd test mutation swap/swap-status swap.tests.test_swap --functions share,pct
invowk cmd test mutation swap/swap-status swap.tests.test_swap --all               # the whole file (slow)
```

The first argument is the Python file to mutate, relative to the top of the
checkout; the second is the unittest module that tests it. Only one of
`--base`, `--functions` and `--all`. `--functions` takes def names: every def
with that name counts, methods and nested defs too, with their decorators;
a name with no def is an error. `--base` uses `git diff` against the working
tree, so uncommitted changes count; a file git does not track yet has no
diff (use `--all`). `--timeout SEC` (default 60) is the time one mutant's
tests get before the mutant counts as killed. `--out DIR` chooses the output
folder (new or empty, outside the checkout); by default it is a new folder
under `${XDG_CACHE_HOME:-~/.cache}/cst-mutation/`.

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
- `/run`, `/tmp` and the home folders (`/var/home`; `/home` too where it is a
  real folder, not a symlink) replaced by empty folders (tmpfs). That hides
  every daemon socket: D-Bus, Docker, libvirt, journald, systemd and, in a
  toolbox, the whole host under `/run/host`;
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
- reading: everything outside `/run`, `/tmp` and the home folders can be
  read (but not changed), such as `/etc`, `/usr` and `/proc`.

The unit tests in `tests/mutation/tests/` check the profile option by
option (the binds after the tmpfs mounts, only the copy and the output
folder writable), the order of the steps with a fake `bwrap` and fake
cosmic-ray tools, the exit statuses, and that the checkout is unchanged
after a run. One test uses the real `bwrap` when it can build a sandbox
(CI's container usually cannot, so it is skipped there): a second temporary
folder stays empty, the copy is writable, and no Unix socket is visible
under `/run`, `/tmp` or the home folders.

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
the docs).

## Upgrading a pin

One tool per pull request, so that new findings are clearly the new version's:

1. Change the version in `[dependency-groups]` of `pyproject.toml`.
2. Run `uv lock` (it rewrites `uv.lock` with the new hashes).
3. Run `invowk cmd test pylint` and `invowk cmd test types`. Fix what the new
   version finds, or explain in the pull request why it is noise. A new ruff
   can also lay out code differently: when the format check fails, run
   `tests/uv-dev ruff format` and commit the result in the same pull request.
4. For cosmic-ray, the gates above do not run it: run it for real, with
   `invowk cmd test mutation swap/swap-status swap.tests.test_swap --all`,
   and put its numbers (mutants, killed, timed out, survived, minutes) in the
   pull request next to the previous version's. A new version can add or
   change mutation operators; read the new survivors.

For uv itself, change `version` and `checksum` of the `Setup uv` step in
`.github/workflows/ci.yml` together. The checksum is the content of
`uv-x86_64-unknown-linux-gnu.tar.gz.sha256` in that uv release. The action
`astral-sh/setup-uv` is pinned by its full commit SHA, with the tag in a
comment.
