# Developer tools for the Python code: uv, ruff, ty

CST's helpers are Python scripts that run as root on the system's
`/usr/bin/python3 -I`, with the standard library only. That does not change.
The tools on this page check that code while you work on it and in CI. They
are not part of CST: nothing installed on the machine uses them.

| Tool | What it does here | Pinned in |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) 0.12.0 | installs the tools below, at the exact versions in `uv.lock`, into a virtual environment outside the checkout | `.github/workflows/ci.yml` (CI); any uv 0.12 or later locally (`required-version` in `pyproject.toml`) |
| [ruff](https://docs.astral.sh/ruff/) 0.17.5 | linter: unused or undefined names, likely bugs, old syntax, syntax errors; missing type annotations in the typed modules | `pyproject.toml`, `uv.lock` |
| [ty](https://docs.astral.sh/ty/) 0.0.104 | type checker | `pyproject.toml`, `uv.lock` |
| [cosmic-ray](https://cosmic-ray.readthedocs.io/) 8.9.0 | mutation testing (its own `mutation` group; CI does not install it) | `pyproject.toml`, `uv.lock` |

**ty is beta.** Its versions are 0.0.x and a new one can report new errors on
unchanged code. That is why it is pinned exactly and upgraded on purpose (see
"Upgrading a pin").

## Running them

```
invowk cmd test pylint        # ruff check (F, B, UP, E9): every Python file
invowk cmd test types         # ty check on the ty gate list, then ruff check --select ANN on the typed list
tests/uv-dev ruff check --fix # any tool command, by hand
tests/uv-dev ty check swap/swap-status
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

## What each gate checks

All three lists live in `pyproject.toml`, and `tests/test_python_files.py`
(a unit test, standard library only) checks them against the repository:

- **`[tool.ruff] extend-include`**: the extensionless helpers (`btrfs/btrfs-bulk`,
  `vm/cst-vm`, ...). ruff finds `*.py` files by itself, but not these. A new
  helper must be added here; the test fails until it is.
- **ruff rules**: `F` (pyflakes), `B` (bugbear), `UP` (pyupgrade), `E9`
  (syntax errors) for every Python file (`test pylint`). `ANN` (annotations)
  is not in the selected rules: `test types` runs it on its own
  (`ruff check --select ANN`), and the typed list below limits it to the
  typed modules.
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

## Upgrading a pin

One tool per pull request, so that new findings are clearly the new version's:

1. Change the version in `[dependency-groups]` of `pyproject.toml`.
2. Run `uv lock` (it rewrites `uv.lock` with the new hashes).
3. Run `invowk cmd test pylint` and `invowk cmd test types`. Fix what the new
   version finds, or explain in the pull request why it is noise.

For uv itself, change `version` and `checksum` of the `Setup uv` step in
`.github/workflows/ci.yml` together. The checksum is the content of
`uv-x86_64-unknown-linux-gnu.tar.gz.sha256` in that uv release. The action
`astral-sh/setup-uv` is pinned by its full commit SHA, with the tag in a
comment.
