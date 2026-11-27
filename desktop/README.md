<!-- SPDX-License-Identifier: MPL-2.0 -->
# `cst desktop`: reduce background desktop writes

Browser profiles (Chromium/Chrome/Firefox) and GNOME's file indexer
(`localsearch-3`) write small SQLite WAL checkpoints and metadata continuously in
the background. `cst desktop` targets those two desktop write sources without
removing applications or breaking Flatpak sandboxing.

> **In short**
>
> - **`cst desktop status`** (read-only; works in a toolbox via `flatpak-spawn`):
>   reports both GNOME `localsearch-3` state and `psd` browser overlay status.
> - **`cst desktop gnome localsearch ...`** (user session on the host): restricts GNOME
>   `org.freedesktop.Tracker3.Miner.Files` from indexing all of `$HOME` to
>   standard XDG user folders (`--mode restrict`, default) or stops and masks the
>   three `localsearch` user units (`--mode disable`). `revert` restores your
>   exact pre-CST settings from `~/.config/cst/desktop-gnome-localsearch.json`.
> - **`cst desktop psd ...`** (root via `sudo` except read-only `status`/`plan`,
>   which work as user or in a toolbox): runs browser profiles in RAM using kernel
>   `overlayfs` over a dedicated `tmpfs` at `/run/cst-psd/<uid>`, installed as
>   `cst-desktop` and managed by `cst-desktop-psd@USER.service` plus hourly
>   `cst-desktop-psd-resync@USER.timer`.

---

## 1. Quick start

```text
cst desktop status
cst desktop gnome localsearch plan
cst desktop gnome localsearch apply        # default: --mode restrict
cst desktop psd plan
cst desktop psd apply                      # installs units, enables timer, mounts idle profiles
```

---

## 2. GNOME `localsearch-3`

By default on Fedora Workstation/Silverblue, GNOME `localsearch-3` watches and
indexes `$HOME` recursively (`&DESKTOP`, `&DOCUMENTS`, `&MUSIC`, `&PICTURES`,
`&VIDEOS` or custom `$HOME` trees depending on session defaults), waking up on
every build tree, VCS checkout, or cache write not ignored.

### Commands

```text
cst desktop gnome localsearch status
cst desktop gnome localsearch plan [--mode restrict|disable]
cst desktop gnome localsearch apply [--mode restrict|disable]
cst desktop gnome localsearch revert
```

### What each mode changes

1. On the first `apply`, CST saves the pre-existing values of
   `index-recursive-directories`, `index-single-directories`, `enable-monitors`,
   and the `localsearch-*.service` user unit states to
   `~/.config/cst/desktop-gnome-localsearch.json`.
2. **`--mode restrict`** (default):
   - Unmasks `localsearch-3.service`, `localsearch-control-3.service`, and
     `localsearch-writeback-3.service` if they were masked earlier.
   - Sets `index-recursive-directories` to
     `['&DESKTOP', '&DOCUMENTS', '&DOWNLOAD', '&MUSIC', '&PICTURES', '&VIDEOS']`
     (removing `$HOME` recursive indexing).
   - Leaves `index-single-directories` unchanged.
   - Sets `enable-monitors` to `true`.
3. **`--mode disable`**:
   - Clears `index-recursive-directories` and `index-single-directories` to
     `@as []` and sets `enable-monitors` to `false`.
   - Runs `systemctl --user mask --now` on `localsearch-3.service`,
     `localsearch-control-3.service`, and `localsearch-writeback-3.service`.
4. **`revert`**:
   - Restores every saved `gsettings` key (using `gsettings reset` when a key
     had no explicit value before), restores masked/unmasked unit states, and
     removes `~/.config/cst/desktop-gnome-localsearch.json`.

---

## 3. Browser profiles in RAM (`psd`)

Profile-sync-daemon style write reduction without COPR packages or breaking
Flatpak paths: `cst desktop psd` mounts a kernel `overlayfs` directly over each
configured profile folder in `$HOME`, backed by a dedicated per-UID `tmpfs` at
`/run/cst-psd/<uid>` (`mode=0700,uid=...,gid=...,size=...`).

Unlike symlink-into-`/tmp` designs, Flatpak Chrome (`~/.var/app/com.google.Chrome/...`)
never leaves its home bind permission boundary because the mount point stays at
the original path inside `$HOME`.

### Safety rules

- **Never syncs a running browser directly into the live profile**: while any
  process owned by the target UID has open file descriptors, `cwd`, or memory
  mappings (`/proc/<pid>/maps`) under a profile path, `start` and `stop` refuse
  cleanly, `apply` and `start --skip-busy` leave already-active overlays
  untouched and mount only currently idle profiles, and `sync` writes a
  standalone crash-recovery snapshot under `~/.local/share/cst/psd/recovery/<id>`
  instead of mutating the lower directory under an active upper layer.
- **No external `overlay` merge race**: when a browser is closed (`stop`, or
  idle `sync`), CST unmounts the `overlayfs`, merges `upper/` (including overlayfs
  whiteouts `0:0` char devices and `trusted.overlay.opaque` directories) cleanly
  onto `lower/` via streaming atomic copy (`os.replace()` + `fsync()`), clears any
  stale crash-recovery snapshot for that profile, and either leaves the profile
  at rest (`stop`) or re-mounts fresh (`sync`).
- **Crash recovery**: if power is lost while a snapshot exists under
  `~/.local/share/cst/psd/recovery/<id>`, the next `start` (or explicit
  `cst desktop psd recover [--discard]`) replays or discards it before mounting.
  `cst doctor` warns whenever unmerged recovery snapshots remain on disk.
- **Symlink-safe paths**: real paths are checked; symlinked profile directories
  are refused rather than mounted over unexpectedly, and tmpfs size is capped at
  25% of total RAM (default `4G`, configurable from `256M` to `32G`).

### Supported profiles (`auto` detects existing folders)

| ID | Path under `$HOME` | Included in `auto` |
|---|---|---|
| `chrome-flatpak` | `.var/app/com.google.Chrome/config/google-chrome` | Yes |
| `chrome` | `.config/google-chrome` | Yes |
| `chromium-flatpak` | `.var/app/org.chromium.Chromium/config/chromium` | Yes |
| `firefox` | `.config/mozilla/firefox` | Yes |
| `firefox-legacy` | `.mozilla/firefox` | Yes (when `firefox` absent) |
| `fastmail-flatpak` | `.var/app/com.fastmail.Fastmail/config/com.fastmail.Fastmail` | Opt-in |
| `discord-flatpak` | `.var/app/com.discordapp.Discord/config/discord` | Opt-in |

### Configuration (`~/.config/cst/desktop.conf`)

Optional `KEY=VALUE` settings read by `cst desktop psd`:

```sh
PSD_PROFILES=auto       # or comma-separated profile IDs, e.g. chrome-flatpak,discord-flatpak
PSD_TMPFS_SIZE=4G       # cap for /run/cst-psd/<uid> (256M..32G; clamped to <=25% RAM)
```

CLI flags `--user NAME`, `--profiles LIST`, and `--size SIZE` override config for
a single invocation (`--profiles` and `--size` apply to `plan`, `start`, and
`apply`).

### Commands & installed units

```text
cst desktop psd status [--user NAME]
cst desktop psd plan [--user NAME] [--profiles LIST] [--size SIZE]
cst desktop psd apply [--user NAME] [--profiles LIST] [--size SIZE]
cst desktop psd revert [--user NAME]
cst desktop psd start [--user NAME] [--profiles LIST] [--size SIZE] [--skip-busy]
cst desktop psd stop [--user NAME]
cst desktop psd sync [--user NAME]
cst desktop psd recover [--user NAME] [--discard]
```

`apply` installs `/usr/local/sbin/cst-desktop` (`desktop:psd`, registered in
`cst sync`) together with systemd templates:

- `cst-desktop-psd@.service` — mounts idle overlays at boot
  (`ExecStart=... start --user %i --skip-busy`) and flushes `upper`->`lower`
  cleanly at shutdown (`ExecStop=... stop --user %i`).
- `cst-desktop-psd-resync@.service` & `cst-desktop-psd-resync@.timer` — hourly
  (`OnBootSec=15m`, `OnUnitActiveSec=1h`, `RandomizedDelaySec=5m`) idle merge
  or crash-recovery snapshot.

`revert` closes active overlays (refusing if a browser is still open), disables
the per-user service and timer, and removes the installed helper and template
units. Your profile folders and `~/.config/cst/desktop.conf` remain untouched.

---

## 4. Architecture and tests

- `desktop/domain/`: pure config/CLI parsing (`__init__.py`), GNOME `localsearch`
  models/planning (`localsearch.py`), and `psd` catalog, mount parsing, size
  clamping, and status formatting (`psd.py`).
- `desktop/ports.py`: host port protocols (`LocalsearchHostPort`, `PsdHostPort`).
- `desktop/application/`: use cases (`localsearch.py`, `psd.py`) with zero direct I/O.
- `desktop/adapters/`: Linux host adapters (`localsearch.py`, `overlay.py`, `psd.py`).
- `desktop/wiring.py`, `desktop/cli.py`, `desktop/cst-desktop`, `desktop-setup.sh`:
  composition root, CLI entry point, and root-step-down shim.

Run module unit tests through the repository test runner:

```text
invowk cmd test module desktop.tests.test_localsearch
invowk cmd test module desktop.tests.test_psd
```

See also [../README.md](../README.md), [../doctor/README.md](../doctor/README.md),
and [../sync/README.md](../sync/README.md).
