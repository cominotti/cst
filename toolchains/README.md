# `cst toolchains`: toolchain caches and settings

This folder holds the parts of `toolchains-setup.sh`, the `toolchains` module
of CST (Cominotti Setup Tools). The script lives one level up:
`~/Workspace/cst/toolchains-setup.sh`. You run it as `cst toolchains <command>`
(how to set up `cst`: [../README.md](../README.md#the-cst-command));
`bash toolchains-setup.sh <command>` from the repository folder does the same.

It looks after the **caches of developer toolchains** (cargo and uv today;
Go, npm and Maven are planned) and the **toolchain settings** CST writes into your
home. The disks, snapshots, the NAS backup and where things live (`~/Bulk`)
stay with the `btrfs` module ([../btrfs/README.md](../btrfs/README.md)).

The entry point of the whole repository is [../README.md](../README.md).

> **In short: what this does to your system**
>
> - **Two settings in your home** (`cst toolchains config apply`, also run by
>   `cst btrfs snapshots setup`): cargo's `build-dir` in
>   `~/.cargo/config.toml` and uv's `link-mode = "clone"` in
>   `~/.config/uv/uv.toml`. A setting you already have is never changed.
> - **An hourly user timer**, `cst-toolchains-prune.timer` (`cst toolchains
>   auto on`, also run by `snapshots setup` unless you turned it off). It
>   deletes cargo build folders nobody used for 48 hours, folders whose git
>   worktree is gone, and the least recently used ones while they take more
>   than 150 GiB; in folders unused for 6 hours it deletes only their
>   incremental part. Never a folder used in the last 2 hours or in use
>   right now. Once a day it also runs `uv cache prune`.
> - **Two opt-in settings**: lean debug info for Rust dev builds, only when
>   you ask for it (`RUST_LEAN_DEBUGINFO=yes`), never by `snapshots setup`;
>   and sccache for rustc (`RUST_SCCACHE=yes`), so that many git worktrees
>   reuse the dependencies another one already compiled.
> - **A report of your Rust toolchains** in `status`, with the ones no project
>   pins or names. CST never removes a toolchain.
> - **Everything runs as your user.** Nothing in `/etc`, no `sudo`.
> - **Undo**: `cst toolchains auto off` (see [section 11](#11-how-to-undo)).

---

## 1. Why this exists

`snapshots setup` points cargo (the Rust build tool) at
`~/.cache/cargo-build` for its intermediate build files. `~/.cache` is a
subvolume that is left out of the hourly `/home` snapshots, so these files,
which are rewritten at every build, no longer fill the snapshots with churn
([../btrfs/SNAPSHOTS.md, section 4](../btrfs/SNAPSHOTS.md#4-changes-to-your-cargo-and-uv-settings)).
But cargo makes **one folder per project path** there, every git worktree is
a new path, and cargo **never deletes** these folders (`cargo clean gc` does
not cover them). On this PC they once grew to **356 GiB in 3 days**, mostly
from worktrees that coding agents created and forgot. Something has to delete
them, and that is this module.

Toolchain caches have a different life from storage: they grow with your
work, each tool has its own rules for what is safe to delete, and all of it
belongs to your user. So they have their own module, with one timer for all
caches, instead of one command per cache inside `cst btrfs`.

## 2. Quick start

```
cst toolchains status            # what is there, how big, the timer, the settings
cst toolchains config plan       # the cargo and uv settings: what apply would add
cst toolchains config apply
cst toolchains prune plan        # what would be deleted now, and why (deletes nothing)
cst toolchains auto on           # the hourly timer
```

If you ran `cst btrfs snapshots setup`, `config apply` and `auto on` are done
already. Type these **without `sudo`**. Inside a toolbox they work too: the
program then runs itself on the host (through `flatpak-spawn --host`),
because only the host can see the processes of every toolbox.

## 3. Commands

| Command | What it does |
|---|---|
| `status [--brief]` | Read-only. For each target: what it is, where (a path that is a symlink into `~/Bulk` is shown with where it really leads), its size, the policy and the last prune. Also the timer, the planned targets, the Rust toolchains report ([section 9](#9-the-rust-toolchains-report)), the toolchain settings and the files `auto on` installed. `--brief`: only the timer and the last prune. `cst status` runs it too. |
| `prune plan [TARGET...]` | Dry run: every folder with what `apply` would do (`delete`, `trim` = only its incremental part, or `keep`) and why. |
| `prune apply [TARGET...]` | Deletes now, the same as the hourly run. |
| `auto on` | Copies the program to `~/.local/lib/cst/`, writes the timer, and starts it. `--unless-off`: not if you ran `auto off` (that is what `snapshots setup` uses). |
| `auto off` | Stops the timer, removes what `auto on` installed, and remembers that you turned it off. |
| `config plan` / `config apply` | The toolchain settings ([section 6](#6-the-toolchain-settings-config)). `--lean-debuginfo` / `--no-lean-debuginfo` turn the opt-in lean debug info on or off for this one run, whatever the settings say. `--sccache` sets up the opt-in sccache; `--no-sccache` removes CST's `rustc-wrapper` line again ([section 6](#sccache-opt-in)). |

**TARGET** is `LANG:CACHE` (`rust:build`) or just `LANG` (`rust` = every
target of Rust). Without a TARGET, `prune` runs every target that is enabled
in the settings. A target you name runs even when it is disabled: disabled
means "the timer and a bare `prune` leave it out".

Exit status: `0` fine (also "nothing to do"), `1` refused or failed, `2` a
mistake on the command line. Started as root (for example by `cst status`
after it asked for `sudo`), the module runs again as the user who called
`sudo`.

## 4. Targets

| Target | Where | State |
|---|---|---|
| `rust:build` | `~/.cache/cargo-build/XX/YYYYYYYYYYYYYY`: cargo's intermediate build files, one folder per workspace path | implemented ([section 7](#7-rustbuild-old-cargo-build-folders)) |
| `python:uv` | `~/.cache/uv`: uv's package cache | implemented ([section 8](#8-pythonuv-uv-cache-prune)) |
| `rust:registry` | `~/.cargo/registry`, `~/.cargo/git` (in `~/Bulk`) | planned, low priority: cargo already deletes downloads it has not used for months by itself; a target would only add tighter `cargo clean gc` limits |
| `go:build` | `~/.cache/go-build` | planned, low priority: Go already trims entries unused for 5 days by itself |
| `go:mod` | `~/go/pkg/mod` (in `~/Bulk`) | planned |
| `node:npm` | `~/.npm/_cacache` (in `~/Bulk`) | planned |
| `jvm:maven` | `~/.m2/repository` (in `~/Bulk`) | planned: only old `-SNAPSHOT` builds and superseded versions (see below) |

**Why `jvm:maven` is narrow.** Maven only reads `~/.m2/repository` after a
download, and `/home` is mounted with `noatime`, so nothing records when an
artifact was last used. A file's date is its download date: a rule "delete
what is old" would remove exactly the stable dependencies every build needs.
Some artifacts also come from a company repository that is only reachable
through a tunnel (devbox); deleting them can stop a build until the tunnel is
up. So this target will only remove old `-SNAPSHOT` builds (keeping the newest
few per artifact) and versions replaced by a newer one, never releases by age.
There is no `jvm:gradle` target: recent Gradle versions clean their own cache
(entries unused for 30 days, by default).

The installed Rust toolchains are not a target: `status` only reports them
([section 9](#9-the-rust-toolchains-report)).

The planned ones are **not implemented yet**: nothing is deleted there, and
naming one (`cst toolchains prune plan go:mod`) says so. `status` lists them
with where they are now. Adding one is a small step: a new class in
`cst-toolchains` (it says which methods), its keys in `toolchains.conf`, and
a line in this table.

## 5. Settings

The defaults are in `toolchains/toolchains.conf` in CST. To change one,
create `~/.config/cst/toolchains.conf` with `KEY=value` lines; yours win:

```
# ~/.config/cst/toolchains.conf
RUST_BUILD_MAX_IDLE=3d
RUST_BUILD_MAX_TOTAL=200G
```

| Setting | Default | Meaning |
|---|---|---|
| `RUST_BUILD_ENABLED` | `yes` | `no`: the timer and a bare `prune` leave `rust:build` out. |
| `RUST_BUILD_MAX_IDLE` | `48h` | A folder not used for longer than this is deleted. |
| `RUST_BUILD_MAX_TOTAL` | `150G` | If all folders together are still bigger than this, the **least recently used** are deleted first, until the total is below it. `0` or `off`: no size limit. |
| `RUST_BUILD_MIN_KEEP` | `2h` | A folder used in the last 2 hours is **never** deleted, not even to get under `MAX_TOTAL`, not even as an orphan. This is checked again right before each deletion ([section 7](#what-is-never-deleted)). It cannot be set below `15m`. |
| `RUST_BUILD_ORPHANS` | `yes` | Delete a folder that no workspace on disk claims any more ([section 7](#orphans-folders-no-workspace-claims)). `no`: never. |
| `RUST_BUILD_INCREMENTAL_IDLE` | `6h` | In a folder not used for longer than this, delete only cargo's incremental folders ([section 7](#incremental-folders)). `0` or `off`: never. Not shorter than `MIN_KEEP`. |
| `PYTHON_UV_ENABLED` | `yes` | `no`: the timer and a bare `prune` leave `python:uv` out. |
| `PYTHON_UV_INTERVAL` | `1d` | The timer runs `uv cache prune` at most once per this. Named on the command line (`prune apply python:uv`) it always runs. `0`: every hourly run. |
| `RUST_LEAN_DEBUGINFO` | `no` | `yes`: `config apply` writes lean debug info for dev builds ([section 6](#lean-debug-info-opt-in)). Not a prune setting. |
| `RUST_SCCACHE` | `no` | `yes`: `config apply` sets up sccache for rustc ([section 6](#sccache-opt-in)). `no` does not remove it; `config apply --no-sccache` does. Not a prune setting. |
| `RUST_SCCACHE_CACHE_SIZE` | `25G` | The size cap of `~/.cache/sccache`. sccache itself deletes the least recently used entries above it. A whole number with `K`, `M`, `G` or `T`; `0` is an error. |

Every key is `<LANG>_<CACHE>_<SETTING>`. Durations take `s`, `m`, `h`, `d` or
`w`. Sizes take `K`, `M`, `G` or `T` (`G`, `GB` and `GiB` all mean GiB), and
the unit is required: `RUST_BUILD_MAX_TOTAL=150` is an error, not 150 bytes
(a cap of 150 bytes would delete every folder not built in the last 2 hours).
Only `0` goes without a unit. A wrong line (an unknown key, a typo, a key of a
planned target, a missing unit, a `MAX_IDLE` shorter than `MIN_KEEP`) **stops
the run** with an error, and nothing is deleted; `status` shows it as
`BAD SETTINGS`. The next run reads the file again: no restart is needed.

## 6. The toolchain settings (`config`)

Hardlinks cannot point from one subvolume to another, and `~/.cache` is its
own subvolume. Two tools are affected, and `config apply` adds one setting to
each, **as your user**:

| File | What is added | Why |
|---|---|---|
| `~/.cargo/config.toml` | `[build]` `build-dir = "<home>/.cache/cargo-build/{workspace-path-hash}"` | Rust intermediate build files go to `~/.cache` (left out of snapshots). The final programs stay in each project's `target/`. |
| `~/.config/uv/uv.toml` | `link-mode = "clone"` | uv installs Python packages from its cache in `~/.cache/uv`. It now uses reflink clones instead of hardlinks, which work across subvolumes. |

The rules (the same as when `snapshots setup` wrote them itself):

- If the file already has `build-dir` (or `link-mode`), it prints
  `kept: ...` and changes nothing, also when the value is your own.
- If `config.toml` already has a `[build]` section, or `uv.toml` already has
  tables, it prints a `NOTE:` with the exact line to add by hand (adding it
  automatically could put it in the wrong section).
- Otherwise it appends the setting with a short comment that starts with
  `# Added by btrfs-setup.sh (CST)` (the comment of the first version, kept so
  that both write the same lines). A file that does not end with a line break
  gets one first.
- If a file cannot be read or written (for example a `~/.cargo` owned by
  root after a `sudo cargo ...`), it says `cannot read ...` or
  `cannot write ...: left as it is`, still handles the other file, and exits
  with 1.

`config plan` shows the same without writing: `write`, `ok`, `keep` (your
own value: `rust:build` then prunes nothing) or `BY HAND`.

### Lean debug info (opt-in)

By default a dev build (`cargo build`, `cargo test`) stores full debug
information for every crate, also for every dependency. That is a large part
of each build folder, and few people step through a dependency in a
debugger. With `RUST_LEAN_DEBUGINFO=yes` in `~/.config/cst/toolchains.conf`
(or `cst toolchains config apply --lean-debuginfo` once), `config apply`
appends this to `~/.cargo/config.toml`:

```
# Added by toolchains-setup.sh (CST) for RUST_LEAN_DEBUGINFO=yes: smaller dev builds.
...
[profile.dev]
debug = "line-tables-only"

[profile.dev.package."*"]
debug = false
```

Your own crates keep line tables (a panic's backtrace still shows file and
line); dependencies get no debug information. Things to know first:

- **It is off unless you turn it on.** `config plan` shows it as
  `off (opt-in)`. `cst btrfs snapshots setup` passes `--no-lean-debuginfo`,
  so it never turns it on, whatever your settings say.
- **The first build afterwards compiles everything once.** Debug info is part
  of every compiled crate's fingerprint, so each workspace's next dev build
  rebuilds from scratch, and its old build files become dead weight until the
  pruning removes them. If many worktrees build at the same time (for example
  several coding agents), they all rebuild at once, which needs a lot of
  memory. Turn it on while builds are quiet.
- **It wins over the projects' own settings.** Cargo reads `[profile.dev]` of
  `~/.cargo/config.toml` before the one in a project's `Cargo.toml`. A project
  that sets `debug = true` gets the lean value too (an environment variable
  such as `CARGO_PROFILE_DEV_DEBUG=true` still overrides it for one build).
- **Your own keys are never changed.** If `~/.cargo/config.toml` already sets
  `debug` in `[profile.dev]` or `[profile.dev.package."*"]`, that key stays and
  `config` reports it (`keep ... is yours: left as it is`). If the file
  already has one of these tables (with other keys), appending a second copy
  would break the file, so `config` prints the lines to add by hand instead.
- To undo, delete the lines under the `# Added by toolchains-setup.sh (CST)
  for RUST_LEAN_DEBUGINFO=yes` comment (and set the setting back to `no`);
  the next build compiles everything once more.

### sccache (opt-in)

Every git worktree has its own cargo build folder, so ten worktrees of one
project compile the same registry dependencies (tokio, syn, memchr, ...)
ten times. [sccache](https://github.com/mozilla/sccache) keeps a cache of
compiled crates in `~/.cache/sccache`: a crate compiled once with the same
compiler, the same flags and the same sources is copied from there instead
of being compiled again. Install sccache yourself (for example `cargo
install sccache --locked`, which lands in `~/.cargo/bin` and so is on the
PATH of the host and every toolbox), then turn it on with `RUST_SCCACHE=yes`
in `~/.config/cst/toolchains.conf` or with `cst toolchains config apply
--sccache` once. `config apply` then:

1. copies `toolchains/rustc-wrapper` to `~/.local/libexec/cst/rustc-wrapper`
   (mode 0755, the cache size written in; rewritten when it differs, so do
   not edit the copy), and only then
2. adds one key to the `[build]` table of `~/.cargo/config.toml`, after
   CST's `build-dir` line:

```
[build]
build-dir = "<home>/.cache/cargo-build/{workspace-path-hash}"
# Added by toolchains-setup.sh (CST) for RUST_SCCACHE=yes: rustc goes through sccache (~/.cache/sccache).
rustc-wrapper = "<home>/.local/libexec/cst/rustc-wrapper"
```

If the file has no `[build]` table, one is appended. The new file is parsed
again and must equal the old one plus this one key; otherwise (for example
`build.jobs = 4` written as a dotted key) `config` prints the line to add by
hand. If `rustc-wrapper` is already set to something else, it is yours:
`config` says so and changes nothing (not even with `--no-sccache`).

What the wrapper does for each compiler call (`rustc-wrapper <compiler>
<arguments>`, as cargo runs it):

- **Only `rustc` goes through sccache.** Any other compiler runs directly:
  Kani's `kani-compiler`, `clippy-driver` (which `cargo clippy` runs for
  your workspace members), anything else.
- **No sccache on the PATH: rustc runs directly.** A toolbox without sccache
  builds as before; the wrapper never fails a build by itself.
- **`CST_SCCACHE=0` skips sccache for one build**: `CST_SCCACHE=0 cargo
  build`.
- **One sccache server per environment.** The host and every toolbox share
  `$HOME` and the network, and sccache's client finds its server on
  `127.0.0.1:4226` by default: a server started in one toolbox would compile
  for the others, with the wrong toolbox's files and programs. The wrapper
  gives each environment its own unix socket instead,
  `$XDG_RUNTIME_DIR/cst-sccache-<toolbox name>.sock` (`...-host.sock` on the
  host; the name comes from `/run/.containerenv`; without
  `$XDG_RUNTIME_DIR`, `~/.cache/sccache/run/`). sccache documents
  `SCCACHE_SERVER_UDS` for this; a socket file left by a dead server is
  replaced when the next one starts. All servers share the one cache folder.
- **Defaults it sets, only when you have not set them:**
  `SCCACHE_DIR=~/.cache/sccache`, `SCCACHE_CACHE_SIZE` (from
  `RUST_SCCACHE_CACHE_SIZE`), `SCCACHE_IGNORE_SERVER_IO_ERROR=1` (a server
  that breaks off: compile locally) and `SCCACHE_SERVER_UDS`.
  `SCCACHE_SERVER_UDS` is not set if you set it or `SCCACHE_SERVER_PORT`.

What is cached and what is not (sccache's own limits):

- **The win is registry dependencies.** Cargo compiles them without
  incremental compilation, so they can be cached and shared by every
  worktree that uses the same versions and flags.
- **Except crates with generated code, and everything built on them.** A
  crate whose build script writes code that the crate pulls in
  (`include!(concat!(env!("OUT_DIR"), ...))`, as serde, serde_core and
  serde_json do) records the path of its `OUT_DIR`, and sccache puts that
  path into the cache key. CST's `build-dir` is a different folder for each
  worktree, so such a crate is cached only within one worktree. sccache also
  keys a crate on the exact files of the crates it depends on, so every
  crate above such a crate misses in a new worktree as well. Leaf crates
  and crates without generated code (memchr, itoa, syn, quote, ...) are
  shared. Expect a lower hit rate across worktrees in projects that lean on
  serde.
- **Incremental crates are never cached.** In dev builds cargo compiles your
  workspace members and path dependencies incrementally; the wrapper hands
  those straight to rustc. `CARGO_INCREMENTAL=0` (for example for agent
  builds that start from an empty build folder) makes them cacheable too and
  raises the hit rate.
- **`CARGO_INCREMENTAL=1` would stop sccache** (releases up to 0.19 refuse
  to run with it; later ones accept `SCCACHE_ALLOW_INCREMENTAL=1`). The
  wrapper sets `SCCACHE_ALLOW_INCREMENTAL=1` and removes
  `CARGO_INCREMENTAL` / `CARGO_BUILD_INCREMENTAL` for sccache only; rustc
  never reads them (cargo turns them into `-C incremental`, which the
  wrapper sees).
- **Crates that are linked are never cached**: `bin`, `proc-macro`,
  `dylib` and `cdylib` crates.
- **`cargo check` and `cargo clippy` are cached too, separately.** They
  emit only metadata (`.rmeta`), which sccache caches as well, but under
  other entries than `cargo build`: a check does not fill the cache for a
  build, or the other way round. Under `cargo clippy`, cargo runs
  `clippy-driver` only for your workspace members (those pass straight
  through the wrapper); registry dependencies are still compiled by `rustc`
  and go through sccache.

Things to know:

- **Turning it on rebuilds nothing.** In a test with cargo 1.96 (dev and
  release builds, the wrapper set through `--config build.rustc-wrapper=...`
  and through `RUSTC_WRAPPER`), adding or removing it left every crate
  fresh. The cache fills as crates are
  compiled anew: a new worktree, `cargo update`, a new toolchain, a pruned
  build folder.
- **Pruning is sccache's own.** sccache keeps `~/.cache/sccache` under
  `RUST_SCCACHE_CACHE_SIZE`, deleting the least recently used entries
  first; there is no CST prune target for it. Each server checks the cap on
  its own, so with servers in several environments at once the folder can
  briefly grow past it. `~/.cache` is its own subvolume and not part of the
  `/home` snapshots (see [btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md)), so
  the cache costs no snapshot space.
- **A new `RUST_SCCACHE_CACHE_SIZE`** reaches the wrapper with the next
  `config apply`, and a server reads it when it starts: a running server
  keeps the old cap until it stops (after 10 minutes without work, or
  `sccache --stop-server` with that environment's `SCCACHE_SERVER_UDS`).
- **`status`** shows the item (`off`, `on`, `ok`, `write`), the size of
  `~/.cache/sccache` and, when sccache is on the host's PATH (`status` runs
  on the host), one line of statistics per running server: compile
  requests, hits, misses, cache size.
- **`RUST_SCCACHE=no` does not remove it**; `config` shows it as `on`. To
  turn it off: `cst toolchains config apply --no-sccache`. That removes only
  CST's own line (and its comment, and a `[build]` table CST added only for
  it); `~/.local/libexec/cst/rustc-wrapper` stays, unused and harmless. Set
  `RUST_SCCACHE=no` too, or the next `config apply` adds the line again.
  While the line is there, every `config apply` keeps the wrapper file in
  place, so cargo never names a missing program.
- **`snapshots setup` follows your setting**: it runs `config apply` without
  a flag, so it sets sccache up only if `RUST_SCCACHE=yes`, and never
  removes it.
- **Update the timer copy before setting the keys.** The hourly timer runs a
  copy of the program in `~/.local/lib/cst/`. A copy older than this version
  does not know `RUST_SCCACHE` and stops at the unknown key (nothing is
  pruned) until `cst toolchains auto on` copies the new version.

## 7. `rust:build`: old cargo build folders

### What "used" means

**"Used" means "a build wrote something there".** The program reads the
modification time of every file and folder inside (it visits all of them
anyway, to measure the size) and takes the newest one. Each build that
compiles something writes new files there, so this is the time of the last
real build. A build that has nothing to do writes nothing and does not count;
if such a folder is deleted, the next build simply compiles everything once.
Times in the future (a wrong clock) are ignored.

### What is never deleted

- A folder that a process of yours is using right now: its working folder, its
  program file (a test binary runs from there), or a file it has open or
  loaded. The program reads this from `/proc`. Toolboxes share the host's list
  of processes, so builds inside a toolbox count too.
- A folder whose cargo lock is held. Cargo locks
  `<profile>/.cargo-build-lock` (for example `debug/.cargo-build-lock`, or
  `x86_64-unknown-linux-gnu/debug/.cargo-build-lock` with `--target`) while it
  compiles. The program tries to take each lock without waiting. If one is
  busy, the folder is skipped. Otherwise it keeps the locks while it checks
  the other rules here once more and renames the folder (an instant step),
  then lets them go and deletes the renamed folder. A new build that starts
  later simply makes a fresh folder.
- A folder that a build wrote to **after the run started**. The plan is made
  from the times measured at the start, and a run can take many minutes
  (the workspace search, then one deletion after the other). So, while it
  holds the locks and right before the rename, the program measures the
  folder again and plans again with the fresh time. If the rule that chose
  the deletion (`MAX_IDLE`, orphan, `MAX_TOTAL`, incremental) no longer
  applies, for example because a build ran there a minute ago, the folder is
  kept (`not deleted: ...: measured again: active 1m ago (within MIN_KEEP)`)
  and, for `MAX_TOTAL`, the next folder in line is taken instead.
- A folder that a running cargo works on, even when cargo holds no lock and
  has nothing open there. This matters for tests: while `cargo test`,
  `cargo bench` or `cargo nextest` run the test programs, cargo's lock is
  free, and between two test programs nothing points into the folder at
  all. So for every running process named `cargo` or `cargo-something`
  (`cargo-nextest`, `cargo-clippy`, ...), the program asks cargo which build
  folder that process uses: it runs `cargo metadata --no-deps --offline` in
  the process's working folder, with the same cargo program (so the same
  version computes the same folder name), its `--manifest-path`, `+toolchain`
  and cargo settings from its environment. This only reads; it never
  downloads anything. If the answer cannot be found out (for example a
  `cargo install` that runs outside any project), **nothing** is deleted
  while that cargo runs: `plan` says so, and `apply` stops and tries again
  next hour.
- Anything in `~/.cache/cargo-build` whose name is not cargo's: cargo's names
  are two hex digits, a slash, and 14 hex digits (`3c/e36df2f7349a98`).
  Everything else is shown as `left alone`.
- Symlinks (never followed, never deleted) and anything on another filesystem
  (it is not counted, and `rm --one-file-system` does not cross into it).
- Everything, if `build-dir` in `~/.cargo/config.toml` is not exactly the line
  `config apply` writes. The program then says so and does nothing (`auto on`
  refuses too, when no other target is enabled).

### Orphans: folders no workspace claims

A build folder is named after the path of a workspace's `Cargo.toml`. When the
worktree is removed (`git worktree remove`, or an agent's checkout is
cleaned up), nothing will ever build in its folder again, but it would stay
for `MAX_IDLE` (48 hours). So each run also finds out which folders are still
**claimed**:

1. It looks for git repositories under `~/Workspace`, up to 6 folder levels
   deep. It does not enter `node_modules`, `target`, hidden folders (their
   name starts with a dot) or folders with a `CACHEDIR.TAG` file (caches,
   such as a cargo home inside a project).
2. For each repository it runs `git worktree list --porcelain`. This also
   names linked worktrees that live elsewhere, for example inside a
   `.claude/worktrees` or `target/` folder.
3. In each worktree it looks for `Cargo.toml` files (the same skips) and asks
   cargo, read only, which build folder each one uses:
   `cargo metadata --offline --no-deps --format-version 1` (its
   `build_directory` field). This never downloads or resolves anything, and
   never installs a toolchain (`RUSTUP_AUTO_INSTALL=0`). A workspace's answer
   covers its member crates, so they are not asked again. Cargo resolves
   symlinks first, so `/home/...` and `/var/home/...` give the same folder.

A folder that no answer names, that was not used for `MIN_KEEP` (2 hours), and
that is not in use or locked (the same checks as above) is deleted with the
reason `orphan: no workspace claims it`. The 2 hours matter: a new worktree
can exist for a few seconds before its first build, and a worktree that is
removed and created again at the same path gets the same folder back.

**When the search is incomplete, nothing is an orphan.** If any `git` or
`cargo metadata` call fails or takes too long (60 seconds each, 10 minutes in
all), or if no folder is claimed at all (for example `~/Workspace` is
missing), that run deletes no folder as an orphan, and `plan` and the journal
say why (`orphans: NOT looked for in this run: ...`). The other rules still
apply. One kind of error is not a failure: cargo refusing a crate that sits
inside a workspace that does not list it ("believes it's in a workspace when
it's not", for example a `fuzz/` folder). Cargo cannot build such a crate
where it is, so it has no build folder to claim.

All of this runs at the lowest CPU and disk priority; on this PC it takes
about 10 seconds for some 800 worktrees.

**What is not seen.** A Rust project outside `~/Workspace`, inside a hidden
folder (unless git lists it as a worktree), deeper than the limits above, or
not in a git repository under `~/Workspace`, claims nothing: its folder is
deleted once it was not built for 2 hours, and its next build compiles
everything again. Nothing else is lost (the final programs are in its
`target/`). Set `RUST_BUILD_ORPHANS=no` if that is a problem.

### Incremental folders

Cargo's incremental compilation keeps its working data in
`<profile>/incremental` (for example `debug/incremental`, or
`x86_64-unknown-linux-gnu/debug/incremental` with `--target`). It is often half
of a folder or more, and it only makes the **next** rebuild of the
workspace's own crates faster; the compiled dependencies are in `deps/`. So
in a folder that was not used for `RUST_BUILD_INCREMENTAL_IDLE` (6 hours),
and that is not in use or locked, each run deletes only these incremental
folders (`plan` shows `trim` and how much it frees). Everything else stays:
the next build recompiles only the workspace's own crates, once.

The deletion uses the same locks and checks as a whole folder. The program
puts back the modification time of the folder that held `incremental`, so
the folder's last activity does not change and `MAX_IDLE` still counts from
the last real build.

### Sizes and disk space

Disk space comes back at once: `~/.cache` is never snapshotted. The sizes
shown (and `MAX_TOTAL`) are what `du` shows: the size of the files **before**
btrfs compression. On this PC btrfs compresses with zstd, so the files
usually take less space than that (often about half; `compsize` shows the
real number), and RAID1 then stores that twice. So "freed 80 GiB" does not
mean 80 GiB more free space in `btrfs filesystem usage`. A file that cargo
also linked into your project's `target/` folder (the final programs) is
counted, but its space only comes back once `target/` lets go of it too.

### Limits

- A process of yours that the program cannot inspect is not seen (in
  practice: from inside a toolbox, the processes of *other* containers; that
  is why it runs itself on the host). Cargo's lock is still checked, but a
  `cargo test` that is between two test programs holds no lock.
- A cargo whose working folder exists only inside a toolbox (not on the
  host) cannot be asked about, so it stops the deleting while it runs.
- A cargo that starts while the program holds the lock (at most a second or
  two) waits for it ("Blocking waiting for file lock"). When it gets the
  lock, the old folder has already been renamed away, so it rebuilds
  everything from scratch into a new folder at the same path. The build does
  not fail, but it takes longer, and while it runs that new folder has no
  lock file, so a second cargo on the same project is not kept out by the
  build lock (cargo's lock in `target/` usually still keeps them apart). The
  journal then shows `a build started meanwhile`.

## 8. `python:uv`: `uv cache prune`

uv keeps downloaded and built Python packages in `~/.cache/uv`. Old versions
of packages and temporary environments pile up there. `uv cache prune` is
uv's own command for that: it removes only entries that uv itself no longer
uses, and the next `uv` command downloads again whatever it needs. Each run
of the timer does:

```
uv cache prune --cache-dir ~/.cache/uv --no-progress
```

- **At most once a day** (`PYTHON_UV_INTERVAL`), as your user, at the lowest
  CPU and disk priority. Never with `--force`.
- **uv must be on your PATH** (here it comes from Homebrew); otherwise the
  target refuses and says so, and the run counts as failed. The timer's PATH
  is the one of your user manager (`systemctl --user show-environment`),
  which your login session usually fills in. `auto on` checks uv there, not
  in its own PATH (when `snapshots setup` runs it, its own PATH is root's,
  without Homebrew), and warns if uv is missing.
- **While uv runs, the prune waits.** Every uv command holds a shared lock on
  `~/.cache/uv/.lock`; `uv cache prune` needs it alone. So the run first
  checks the lock without waiting: if a uv command (also a long-running
  `uv run` server) holds it, the run is skipped and tried again next time. If
  a uv command starts in the moment between, uv itself waits for it, at most
  60 seconds (`UV_LOCK_TIMEOUT=60`), and then the run is skipped too. Only
  uv's own timeout message (`Timeout (60s) when waiting for lock on ...`), or
  a lock still held right after the failure, counts as that. Any other error,
  also one about the lock file (for example `Permission denied` after a
  `sudo uv ...`), is a failure: it is logged as an error and the run fails.
- **`plan`** shows the cache size and whether the lock is held. uv 0.12 has
  no `--dry-run` for `cache prune`, so how much it frees is only known
  afterwards (the journal shows uv's own line, `Removed N files (...)`); if a
  later uv has `--dry-run`, `plan` uses it.
- It always works on `~/.cache/uv`. If you moved uv's cache elsewhere
  (`UV_CACHE_DIR`, or `cache-dir` in `uv.toml`), set `PYTHON_UV_ENABLED=no`.

## 9. The Rust toolchains report

rustup keeps every toolchain it ever installed (`~/.rustup/toolchains`, in
`~/Bulk`), about 0.6 to 1.5 GiB each. `status` lists them with their size and
says which are still needed:

- **pinned**: a `rust-toolchain.toml` or `rust-toolchain` file in a git
  worktree under `~/Workspace` (the same search as for orphans) names it, or
  a rustup override does;
- **the default** (`rustup default`);
- **named**: a build file in those worktrees mentions it, for example
  `cargo +nightly fuzz` or `TOOLCHAIN ?= nightly-2026-06-01` in a Makefile.
  The files read are Makefiles (`Makefile`, `*.mk`), justfiles, Taskfiles,
  shell scripts (`*.sh`), the CI workflows (`.github/workflows/*.yml`, ...)
  and `.cargo/config.toml`. A name counts as a whole word (`nightly` inside
  `nightly-2026-06-01` does not count);
- **not pinned or named**: for those it prints the command to remove them
  yourself, for example `rustup toolchain uninstall 1.95.0`. **CST never
  runs it.** The warning comes right before the command: a toolchain can
  still be used by a project outside `~/Workspace`, by a program or script
  that is not among the files read, or by `RUSTUP_TOOLCHAIN`, and none of
  those is checked. If the search of `~/Workspace` was incomplete, no
  command is printed.

It also lists toolchains that a project pins but that are not installed (the
next build there downloads them).

## 10. The timer: files and logs

Each hourly run writes one line per deleted, trimmed or skipped folder (each
starts with the target, `rust:build: ...`), the result of the workspace
search, uv's output, and a summary, to the journal:

```
journalctl --user -u cst-toolchains-prune.service
```

The timer starts at a random moment in each hour (up to 10 minutes late) and
the run has the lowest CPU and disk priority (`Nice=19`,
`IOSchedulingClass=idle`), so it does not get in the way of builds or games.

**Each run has a time budget of 25 minutes**; systemd stops the service
after 30 (`TimeoutStartSec=30min`). With the lowest disk priority on a busy
disk, deleting tens of GiB can take long, so the run keeps to the budget
itself: `python:uv` (at most 10 minutes) runs first, then `rust:build`;
`rust:build` starts no new deletion with less than 2 minutes left (`stopped
before ...: the run's time budget ...`) and stops an `rm` that is still
running when the budget ends (the folder was already renamed away: the next
run deletes the rest as a leftover). Whatever is left is done by the next
runs. A manual `prune apply` has the same budget.
`auto on` writes, as your user:

- `~/.local/lib/cst/cst-toolchains` (a copy of `toolchains/cst-toolchains`)
  and `~/.local/lib/cst/toolchains.conf` (a copy of the defaults), so that the
  timer does not depend on this working copy. `auto on` again updates them
  after a change in CST;
- `~/.config/systemd/user/cst-toolchains-prune.service` and `.timer`.

All of them start with (or contain) `# managed-by: toolchains-setup.sh (CST)`;
files without that line are never overwritten or removed. Each run saves its
summary in `~/.local/state/cst/toolchains-<target>.json` (shown by `status`).

**Coming from `cst btrfs cargo-gc`.** The first version of this cleanup was
`cst btrfs cargo-gc` (timer `cst-cargo-build-gc.timer`, settings
`~/.config/cst/cargo-gc.conf`). It was never turned on here, and the command is
gone. If its files are found (with their `# managed-by: btrfs-setup.sh (CST)`
line), `auto on` and `auto off` stop and remove them, and `status` lists them
as `stale`. A `~/.config/cst/cargo-gc.conf` is not read any more; `status`
says so: put its lines into `~/.config/cst/toolchains.conf` with the
`RUST_BUILD_` prefix.

## 11. How to undo

```
cst toolchains auto off
```

stops the timer and removes the program copies and the unit files. Your
`~/.config/cst/toolchains.conf` stays. `auto off` also writes
`~/.config/cst/toolchains-auto.off`, so that a later `snapshots setup` does
not turn the timer back on (it says `left off` instead); `auto on` removes
that file again. `cst btrfs uninstall` and `cst btrfs snapshots disable` leave
the timer alone: it belongs to the cargo `build-dir` setting, which also
stays.

The two settings of [section 6](#6-the-toolchain-settings-config) are not
removed by any command: delete the lines under the `# Added by
btrfs-setup.sh (CST)` comment by hand. Without the `build-dir` line, cargo
builds into each project's `target/` again (inside the `/home` and
`~/Workspace` snapshots), and `rust:build` does nothing. The same goes for
the opt-in lean debug info: delete the lines under `# Added by
toolchains-setup.sh (CST) for RUST_LEAN_DEBUGINFO=yes` and set
`RUST_LEAN_DEBUGINFO=no` (the next dev builds compile everything once).
The opt-in sccache is the exception: `cst toolchains config apply
--no-sccache` removes CST's `rustc-wrapper` line (set `RUST_SCCACHE=no`
too); delete `~/.local/libexec/cst/rustc-wrapper` and `~/.cache/sccache` by
hand if you want them gone.

## 12. Files in this folder

| File | What it is |
|---|---|
| `cst-toolchains` | The program (Python): `status`, `prune`, `auto`, `config`, the targets and the toolchains report. Its `help` explains the details. |
| `toolchains.conf` | The default settings. |
| `rustc-wrapper` | The opt-in sccache wrapper (POSIX sh) that `config apply` copies to `~/.local/libexec/cst/` ([section 6](#sccache-opt-in)). |
| `tests/` | Unit tests (no root, no systemd, scratch homes only): `python3 -I -B -m unittest discover -s toolchains/tests -t . -v`, or all of CST with `python3 -I -B -m unittest` from the repository folder. |

## 13. Glossary

| Term | Meaning |
|---|---|
| build-dir (cargo) | The folder where cargo keeps its intermediate build files; here `~/.cache/cargo-build/XX/YYYYYYYYYYYYYY`, one per project path. |
| target (here) | One cache of one toolchain that `prune` looks after, named `LANG:CACHE`, e.g. `rust:build`. Not cargo's `target/` folder. |
| prune | Delete the parts of a cache nobody used for a while. |
| orphan (here) | A cargo build folder that no workspace on disk claims any more, because its worktree is gone. |
| incremental (cargo) | Cargo's saved state that makes the next rebuild of a workspace's own crates faster; `<profile>/incremental` in a build folder. |
| pin (rustup) | A `rust-toolchain.toml` or `rust-toolchain` file that tells rustup which toolchain a project uses. |
| user timer | A systemd timer of your own user (`systemctl --user`), not of the system. |
| worktree (git) | A second checkout of the same repository in another folder; for cargo a new project path. |
