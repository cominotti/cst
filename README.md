# CST (Cominotti Setup Tools)

Scripts for one workstation: **sophia**. They keep its disks safe and watched,
and keep games smooth while heavy work runs in the background.

**CST** stands for **Cominotti Setup Tools**. One command, `cst`, runs all of
them: `cst btrfs ...` for the disks, `cst cpu ...` for the CPU, `cst swap ...`
for swap and memory, `cst bluetooth ...` for Bluetooth audio,
`cst toolchains ...` for the caches of developer tools, and `cst doctor`
for a read-only check-up of the whole system (see
[The `cst` command](#the-cst-command)). Older versions of this repository were
called `f44-tools` and lived in `~/Workspace/f44-tools`; `cst btrfs install`
moves what they left on the system to the new names (see
[btrfs/README.md, section 4.9](btrfs/README.md#49-desktop-alerts-btrfs-notify)).

> **Personal workstation project — easy to adapt.**
> CST is a personal project built for my own workstation (**sophia**). Several
> defaults (swap partition UUID and size, P-core/E-core ranges, VM name,
> headset MAC address) reflect that hardware, so do not run `install` or `apply`
> blindly on another machine. At the same time, the modules are independent and
> all machine-specific values live in a handful of small config files and top-of-file
> constants — see [Adapting CST to another PC](#adapting-cst-to-another-pc).

## Why this repository exists

sophia is a powerful PC with a few special risks:

| The PC has... | The risk | What CST does |
|---|---|---|
| Two NVMe disks in **RAID1** (every piece of data is stored twice, once per disk), encrypted with **dm-crypt** (Linux disk encryption), with one **btrfs** filesystem on top | A dead disk is easy to miss, because the PC keeps running on the other disk. Sometimes programs wait a long time for the disk ("stalls"). | Watches disk health, records why programs wait for the disk, sends desktop alerts, takes hourly snapshots. |
| An Intel Core Ultra 9 285K: fast **P-cores** (CPUs 0-7) and smaller **E-cores** (CPUs 8-23) | Builds, Docker and the Windows VM `win11-devbox` take CPU time from a game, and the game stutters. | Moves background work away from the game's cores and gives it a lower priority while a game runs. |
| 93 GiB of RAM, of which the VM takes 48 GiB, and a 128 GiB swap partition on NVMe | Memory runs short under builds plus the VM. Fedora's default swap (zram, in RAM) keeps the wrong pages in RAM once a disk swap exists. | Compressed swap cache in RAM (zswap) in front of the encrypted NVMe swap, swap-friendly settings, a memory floor for the VM, and a status that shows how much comes back from disk. |
| A Bluetooth headset (JBL Live Beam 4) that can play over LE Audio and over classic Bluetooth with LDAC | LE Audio is off in Fedora's BlueZ by default; a fixed top LDAC bitrate makes this headset stall. LE stereo to true-wireless earbuds does not work on this Intel BE200 yet, and the headset fell into a disconnect loop on LE. | Switches LE Audio on in BlueZ but keeps it off in PipeWire for now (one line turns it on), sets an adaptive LDAC quality for the headset, and checks both. |
| A lot of Rust work, much of it by coding agents in short-lived git worktrees | Cargo's build files fill the disk (once 356 GiB in 3 days) and, inside snapshotted folders, the snapshots. | Keeps cargo's build files out of the snapshots and deletes the build folders nobody uses any more, every hour. |

The PC runs Fedora **Silverblue** (currently Fedora 45): a Fedora edition where the system
files are a read-only image that is replaced as a whole at each update. It also
has GNOME, Steam (as a Flatpak), Docker, podman toolboxes, the Windows VM
`win11-devbox` and a Synology NAS (a network storage box).

## The `cst` command

`cst` is a small script at the top of this folder (`~/Workspace/cst/cst`). It
does nothing by itself: it passes your command to one **module**.

| Command | Module script | Folder | What it is for | Detailed documents |
|---|---|---|---|---|
| `cst btrfs ...` | `btrfs-setup.sh` | `btrfs/` | Storage: disk health, scrub, stall recording, writeback limits, Docker volumes, degraded boot, faster encryption, snapshots, NAS backup, `~/Bulk`, Docker cleanup | [btrfs/README.md](btrfs/README.md), and [btrfs/SNAPSHOTS.md](btrfs/SNAPSHOTS.md) for snapshots and the NAS backup |
| `cst cpu ...` | `cpu-setup.sh` | `cpu/` | CPU: game profile, low priority for the VM, toolboxes and Docker, E-cores for builds, measurements | [cpu/README.md](cpu/README.md) |
| `cst swap ...` | `swap-setup.sh` | `swap/` | Swap and memory: zswap, encrypted NVMe swap, zram off, swap settings, memory floor for the VM, swap status per cgroup | [swap/README.md](swap/README.md) |
| `cst bluetooth ...` | `bluetooth-setup.sh` | `bluetooth/` | Bluetooth audio: LE Audio (BAP/LC3) ready in BlueZ but off in PipeWire by default, adaptive LDAC quality for the headset, checks of both | [bluetooth/README.md](bluetooth/README.md) |
| `cst toolchains ...` | `toolchains-setup.sh` | `toolchains/` | Toolchain caches and settings: hourly pruning of cargo's build folders (old, orphaned, and the incremental part of idle ones) and a daily `uv cache prune`; cargo's `build-dir`, uv's `link-mode` and an opt-in lean debug info for Rust dev builds; a report of installed Rust toolchains (later also Go, npm and Maven caches). Runs as your user | [toolchains/README.md](toolchains/README.md) |
| `cst doctor [--brief]` | none (`cst` runs `doctor/cst-doctor` itself) | `doctor/` | Read-only check-up: layered packages pinned to a version, local RPMs and replaced base packages, failed system and user units, desktop alerts you have not seen, how full btrfs is (chunks, automatic reclaim, unallocated space per disk); most problems with the command that fixes it. Runs as your user, also in a toolbox (the fixes are then marked "on the host") | [doctor/README.md](doctor/README.md) |

Three more commands belong to `cst` itself:

- `cst help` (or `cst` alone): a short overview, with the modules of this copy.
- `cst status`: the status of every module, one after the other. `btrfs`
  and `cpu` need root for it, so it asks for your `sudo` password once.
  Inside a toolbox it skips those two and says so; the `swap`,
  `bluetooth` and `toolchains` status also work there (`swap` without
  zswap's debug counters). Its last section is the short form of
  `cst doctor`: the summary line and the problems only.
- `cst doctor`: a read-only check-up across the modules, with one line per
  check (`ok`, `info`, `WARN`, `ERROR`) and, under each problem, the exact
  command that fixes it. It never changes anything and needs no `sudo`.
  Exit status 0 when all is fine, 1 when something needs attention. It is
  not a module (nothing to install or undo): `cst` runs `doctor/cst-doctor`
  directly. Details: [doctor/README.md](doctor/README.md).

The modules are independent. You can use one without the other. A **module**
(also called a frontend) is one script that installs, shows and removes a
group of parts; the parts live in the folder next to it. Each module also
works on its own (`bash btrfs-setup.sh status` is the same as
`cst btrfs status`), but its messages then show its own name.

**How to call it.** The first time, type the full path:

```
~/Workspace/cst/cst btrfs install      # or: ~/Workspace/cst/cst cpu install
```

`cst btrfs install` and `cst cpu install` then create the link
`~/.local/bin/cst` -> `~/Workspace/cst/cst` for your user (the shared helper
`cst-link.bash` does it), and from then on `cst` works as a command. Fedora
puts `~/.local/bin` in the `PATH` of your user. The link matters: the programs
these modules install (desktop alerts, snapshot logs, `btrfs-stall-watch`,
docker-gc) print hints such as `cst btrfs bulk status`, and those must work
as typed. If something of your own is already at `~/.local/bin/cst`, the
install leaves it alone and says so. A link that points nowhere anymore (the
folder was moved) is replaced. Uninstalling a module keeps the link (the
other modules still use it); `rm ~/.local/bin/cst` removes it. Before any
install, or without the link, make it by hand:

```
mkdir -p ~/.local/bin && ln -s ~/Workspace/cst/cst ~/.local/bin/cst
```

An `alias cst=~/Workspace/cst/cst` in `~/.bashrc` also works for typing, but
not for the hints, which assume a real command. This document and the module
documents write `cst ...`. Nothing is installed into a system folder for it.

Do not type `sudo cst ...`: `sudo` does not look in `~/.local/bin` or at your
aliases. The commands that need root call `sudo` by themselves.

Every module prints its built-in help:

```
cst btrfs help
cst cpu help
cst swap help
cst bluetooth help
cst toolchains help
```

Without a command, a module only prints the help and changes nothing.
To install, you always type `install` (for `swap` and `bluetooth`: `apply`;
for `toolchains`: `config apply` and `auto on`) yourself.

## Before you start

- **host** = the real Fedora system of the PC. A **toolbox** is a container
  (a separate, small Linux environment) on top of the host, for development
  tools.
- Run the commands **on the host**, **as your normal user**.
  Commands that need administrator rights (root) call `sudo` by themselves
  and ask for your password.
- Do not start them from a root shell (for example after `sudo -i`). Some
  commands need to know your user and stop with a message that contains
  `run through sudo from your user account (needs SUDO_USER)`.
- Inside a toolbox, the root commands stop with
  `'<command>' must run on the host, not inside a toolbox/container ...`.
  Only `report` and `watch` (of `btrfs` and `cpu`), `cst toolchains`
  (it runs itself on the host), `cst swap plan` / `cst swap status` and
  `cst bluetooth plan` / `status` / `revert --check` also work inside a
  toolbox.
- `cst bluetooth apply` and `revert` are the exception to "root commands
  call sudo": they run as your user (the audio settings belong to your
  session) and use `sudo` only for `/etc/bluetooth/main.conf` and for
  restarting Bluetooth. `cst toolchains` never needs root at all.

## Start here: the recommended order

Each step is optional except step 1. Read the linked section before each step:
it says what changes on your system and how to undo it.

**1. Disk monitoring and tuning** (no reboot needed in most cases)

```
cst btrfs install
cst btrfs test-alert     # you should see "Disk health: test alert"
cst btrfs status
```

If `install` prints `below: staged for the next boot`, reboot and run
`install` again. Details: [btrfs/README.md, section 4](btrfs/README.md#4-install-what-it-puts-on-the-system).

**2. Boot with one dead disk** (changes the boot path; active from the next boot)

```
cst btrfs degraded-boot
cst btrfs degraded-boot status
```

Details: [btrfs/README.md, section 8](btrfs/README.md#8-degraded-boot-start-the-pc-with-one-disk-dead).

**3. Hourly snapshots of `/home` and `~/Workspace`** (asks once; deletes
`~/.cache/uv` and old cargo `target/` folders)

```
cst btrfs bulk plan          # optional: preview what moves into ~/Bulk
cst btrfs snapshots setup
cst btrfs snapshots status
cst btrfs bulk status
```

`snapshots setup` first fills **`~/Bulk`**: one folder for everything that
can be downloaded again (IDE programs, `~/Programs`, Android SDK and emulator
disks, AI models, ISOs, a Steam library, toolchain caches). **Everything in
`~/Bulk` can be downloaded again: no snapshots, no NAS backup.** The usual
paths become symlinks into it, so your programs do not notice. Busy items move
at the next boot, before the login. Afterwards, move your Steam games into
the new library once, with Steam closed: `cst btrfs bulk steam plan`, then
`cst btrfs bulk steam apply` (add `--copy` if `plan` says a game has NOCOW
files and must be copied instead of reflinked; a copy needs the game's size
plus 170 GiB free, so the snapshot space guard is never pushed into its
emergency purge). Details:
[btrfs/SNAPSHOTS.md, section 3](btrfs/SNAPSHOTS.md#bulk-one-folder-for-everything-you-can-download-again).
Through the `toolchains` module (step 9), it also points cargo's
intermediate build files to `~/.cache/cargo-build` and starts a small hourly
user timer that deletes the build folders of projects (and git worktrees)
not built for 48 hours or whose worktree is gone, deletes only the
incremental part of folders not built for 6 hours, and the least recently
built ones while the folder is over 150 GiB; never one built in the last 2
hours or in use (also by a running `cargo test`). The same timer runs
`uv cache prune` once a day. The lean debug info for Rust dev builds is
opt-in and never turned on by `setup`. `cst toolchains auto off` turns it off for good (a
later `setup` leaves it off).
It also moves Chrome's (Flatpak) web cache into RAM, with a 512 MiB limit, so
it stays out of the snapshots; restart Chrome afterwards, then delete the old
cache as `snapshots status` says
([details](btrfs/SNAPSHOTS.md#chrome-flatpak-its-cache-in-ram-not-a-subvolume)).

Then, if you want a backup on the NAS:
`cst btrfs snapshots nas setup --path /<share>/<folder>`.
**Save the backup password** in your password manager. Details:
[btrfs/SNAPSHOTS.md, section 2](btrfs/SNAPSHOTS.md#2-quick-start) and
[section 7](btrfs/SNAPSHOTS.md#7-optional-backup-on-the-nas-restic).

**4. Smaller storage options** (each one is a separate decision)

```
cst btrfs crypt-perf enable   # asks the passphrase of each disk
cst btrfs docker-gc enable    # weekly, careful Docker cleanup
```

Details: [btrfs/README.md, section 9](btrfs/README.md#9-crypt-perf-faster-encryption-path)
and [section 11](btrfs/README.md#11-docker-gc-weekly-careful-docker-cleanup).

**5. Swap and memory** (no reboot needed)

```
cst swap plan      # what would change; changes nothing
cst swap apply
cst swap status
```

zswap in front of the encrypted NVMe swap, zram off, swappiness 100 and a
48 GiB memory floor for the VM. If `apply-swap-config.sh` (the older,
standalone script) already set this up, `plan` shows `adopt` and `apply`
only renews the comments: no line is added twice and swap is not
restarted. Details: [swap/README.md, section 3](swap/README.md#3-quick-start)
and [section 5](swap/README.md#5-coming-from-apply-swap-configsh).

**6. Smooth games** (needs a reboot afterwards)

```
cst cpu install
# reboot, then:
cst cpu status
```

In Steam, set the launch option of the game to `gamemoderun %command%`.
Details: [cpu/README.md, section 4](cpu/README.md#4-quick-start).

**7. Optional CPU steps**

```
cst cpu vm-pin plan            # preview only
cst cpu vm-pin apply           # asks y/N; active at the next VM start
cst cpu docker-slice install   # then: sudo systemctl restart docker
```

Details: [cpu/README.md, sections 8 and 9](cpu/README.md#8-optional-pin-the-vm-vm-pin).

**8. Bluetooth audio** (no reboot; may restart Bluetooth, after asking)

```
cst bluetooth plan     # what would change; changes nothing
cst bluetooth apply
cst bluetooth status
```

LE Audio (BAP/LC3) on in BlueZ, and the adaptive LDAC quality for the
headset. LE Audio stays **off in PipeWire** by default (`LE_AUDIO=off`,
for all devices: headsets play over A2DP/LDAC and HFP), because LE stereo
to true-wireless earbuds does not work on this controller yet. To turn it
on: `cst bluetooth le-audio on` (and `cst bluetooth le-audio off` to go back;
see [LE Audio: off by default](bluetooth/README.md#le-audio-off-by-default)). If `apply-le-audio.sh` (the older, standalone script) already set
this up, `plan` shows `ok` for `main.conf` and `adopt` for the WirePlumber
rule: `apply` only renews that file's comments and restarts nothing.
Optional: with `LDAC_RATE=48000` in `~/.config/cst/bluetooth.conf`, `apply`
also installs a small user service that puts LDAC back to 48 kHz when the
headset reconnects by itself at 96 kHz (off by default; its profile switch
failed on PipeWire 1.6.11, so keep it off until it is checked again).
Details: [bluetooth/README.md, section 3](bluetooth/README.md#3-quick-start),
[section 5](bluetooth/README.md#5-coming-from-apply-le-audiosh)
and [section 7](bluetooth/README.md#7-ldac-sample-rate-the-optional-watcher).

**9. Toolchain caches** (no reboot; as your user; done by step 3 if you ran it)

```
cst toolchains status        # sizes, policy, last prune, the timer, rustup toolchains, the settings
cst toolchains config apply  # cargo build-dir + uv link-mode (step 3 runs it)
# opt-in, once, while no builds run (every workspace rebuilds once afterwards):
# cst toolchains config apply --lean-debuginfo
cst toolchains prune plan    # what would be deleted now; deletes nothing
cst toolchains auto on       # the hourly pruning (step 3 runs it, unless you turned it off)
```

The hourly user timer `cst-toolchains-prune.timer` deletes old and orphaned
cargo build folders in `~/.cache/cargo-build` with the policy above (and only
the incremental part of folders idle for 6 hours), and runs `uv cache prune`
once a day. Sizes are as `du`
shows them, before btrfs compression. Details and settings:
[toolchains/README.md](toolchains/README.md#2-quick-start).

## Something was slow: which tool?

| Symptom | Tool | Where to read |
|---|---|---|
| A build, test or container was slow, or the desktop froze | `cst btrfs report --since '<time>'` (or `btrfs-stall-report`, installed by step 1). It tells you if the disks were the reason. | [btrfs/README.md, section 7](btrfs/README.md#7-report-was-storage-the-bottleneck) |
| A game stutters while builds, Docker or the VM run | `cst cpu watch -l <label> --game <name>`, then `cst cpu report` | [cpu/README.md, section 11](cpu/README.md#11-measuring-watch-and-report) |
| Everything feels slow under memory pressure, or swap is large | `cst swap status`: how full the zswap pool is, how much went to disk and how much had to be read back, per cgroup (VM, apps, toolboxes, Docker) | [swap/README.md, section 6](swap/README.md#6-reading-the-status) |
| The Bluetooth headset has no LE Audio profiles, or LDAC stutters | `cst bluetooth status`: LE Audio on or off (off is the default: no `bap-*` profiles then), ISO socket, BAP in BlueZ, the headset's codec, LDAC quality and sample rate | [bluetooth/README.md, section 6](bluetooth/README.md#6-reading-the-status) |
| The headset drops in a loop, or plays on a `bap-*` profile with LE Audio off | `cst bluetooth status` (LE Audio, WirePlumber bluez5.roles, PipeWire BAP endpoint, preferred bearer); `cst bluetooth apply` writes the LE Audio file | [bluetooth/README.md, LE Audio: off by default](bluetooth/README.md#le-audio-off-by-default) |
| LDAC sounds worse after the headset reconnected by itself (`status` warns: 96000 Hz) | The optional LDAC rate watcher (`LDAC_RATE=48000`, then `cst bluetooth apply`); its log: `journalctl --user -u cst-bt-ldac48.service` | [bluetooth/README.md, section 7](bluetooth/README.md#7-ldac-sample-rate-the-optional-watcher) |
| You want to know if anything needs attention, or an update (`rpm-ostree upgrade`) fails with "package not found" | `cst doctor`: layered packages pinned to a version, failed units, alerts you have not seen; most with the command that fixes it | [doctor/README.md, section 3](doctor/README.md#3-why-pinned-layered-packages-are-a-problem) |
| The disk fills up fast while you build Rust projects, or a build suddenly compiles everything again | `cst toolchains status` (how big `~/.cache/cargo-build` is, the last prune), `cst toolchains prune plan` (what goes next, and why); its log: `journalctl --user -u cst-toolchains-prune.service` | [toolchains/README.md, section 7](toolchains/README.md#7-rustbuild-old-cargo-build-folders) |

`cst btrfs` and `cst cpu` both have a `watch` and a `report` command, but
they measure different things: `cst btrfs` looks at the disks, `cst cpu`
looks at the game's CPU and memory waits. `cst swap status` is a snapshot of
swap and memory, with counters since boot.

## What to do if...

- **You got a desktop alert that starts with `Disk health:`** or another disk
  alert: [btrfs/README.md, section 13](btrfs/README.md#13-what-to-do-if).
- **btrfs says "No space left" (or went read-only) while `df` still shows
  free space**, or the alert says `unallocated`: follow the recovery steps in
  [btrfs/README.md, "When btrfs says 'No space left' but df shows free space"](btrfs/README.md#when-btrfs-says-no-space-left-but-df-shows-free-space).
  Do not start a full or metadata balance first.
- **You got an alert that starts with `Snapshots`, `Subvolume conversion` or
  `NAS backup`**: [btrfs/SNAPSHOTS.md, section 9](btrfs/SNAPSHOTS.md#9-alerts)
  and [section 10](btrfs/SNAPSHOTS.md#10-what-to-do-if).
- **You want a deleted file back**:
  [btrfs/SNAPSHOTS.md, section 6](btrfs/SNAPSHOTS.md#6-how-to-restore-a-file-or-a-folder).
- **The game profile does not switch on, or stays on**:
  [cpu/README.md, section 14](cpu/README.md#14-troubleshooting).

## How to undo

| Part | Command | Details |
|---|---|---|
| Disk monitoring and tuning | `cst btrfs uninstall` | [btrfs/README.md, section 6](btrfs/README.md#6-uninstall-how-to-undo) |
| Degraded boot | `cst btrfs degraded-boot uninstall`, then reboot | [btrfs/README.md, section 8](btrfs/README.md#8-degraded-boot-start-the-pc-with-one-disk-dead) |
| Faster encryption | `cst btrfs crypt-perf disable` | [btrfs/README.md, section 9](btrfs/README.md#9-crypt-perf-faster-encryption-path) |
| Snapshots and NAS backup | `cst btrfs snapshots disable` (add `--purge` to delete the snapshots) | [btrfs/SNAPSHOTS.md, section 12](btrfs/SNAPSHOTS.md#12-how-to-undo) |
| One `~/Bulk` link | `cst btrfs bulk keep ID`, then remove the link and move the folder back by hand | [btrfs/SNAPSHOTS.md, section 12](btrfs/SNAPSHOTS.md#undo-one-bulk-link) |
| Chrome's cache in RAM | `cst btrfs snapshots chrome-cache off` | [btrfs/SNAPSHOTS.md, section 3](btrfs/SNAPSHOTS.md#chrome-flatpak-its-cache-in-ram-not-a-subvolume) |
| Docker cleanup | `cst btrfs docker-gc disable` | [btrfs/README.md, section 11.3](btrfs/README.md#113-what-changes-on-my-system-and-how-to-undo) |
| Pruning of old cargo build folders | `cst toolchains auto off` (the cargo and uv settings: by hand) | [toolchains/README.md, section 11](toolchains/README.md#11-how-to-undo) |
| CPU tools | `cst cpu uninstall` (and `docker-slice uninstall`, `experiment off NAME`) | [cpu/README.md, section 13](cpu/README.md#13-how-to-undo-everything) |
| Swap and memory | No command: a few steps by hand (switching swap off needs free RAM) | [swap/README.md, section 8](swap/README.md#8-how-to-undo) |
| Bluetooth audio | `cst bluetooth revert` (`--check` first shows what it would do) | [bluetooth/README.md, section 8](bluetooth/README.md#8-how-to-undo) |

Each `uninstall` removes only its own parts. Some things stay on purpose
(recorded data, settings files, layered packages, snapshots). The linked
sections list them.

## Adapting CST to another PC

Because modules are independent, you can pick only the ones you want and preview
everything with `plan`, `status` or `doctor` before changing anything:

| Module | Works as is on another PC? | What to adjust for your hardware |
|---|---|---|
| `cst doctor` | Yes (any Fedora `rpm-ostree` / `btrfs` / `systemd` desktop) | Nothing — it is 100% read-only and discovers deployments, units and btrfs mounts dynamically. |
| `cst toolchains` | Yes | Override retention or size limits in `~/.config/cst/toolchains.conf` (defaults in [`toolchains/toolchains.conf`](toolchains/toolchains.conf)). |
| `cst bluetooth` | Yes, once you set your headset's MAC | Put `DEVICE=AA:BB:CC:DD:EE:FF` (and optional `LDAC_QUALITY`, `LE_AUDIO`, `BEARER`) in `~/.config/cst/bluetooth.conf` (defaults in [`bluetooth/bluetooth.conf`](bluetooth/bluetooth.conf)), or pass `--device`. |
| `cst cpu` | Hybrid Intel / AMD or multi-core desktops | After `cst cpu install` (or in [`cpu-setup.sh`](cpu-setup.sh) / [`cpu/game-profile`](cpu/game-profile)), edit `/etc/cpu-setup/cpu-setup.conf`: `P_CORES`, `BUILD_CPUS`, `VM_NAME`, `VM_VCPUS`, `GAME_MEMORY_LOW`. On a machine without E-cores (`/sys/devices/cpu_atom/cpus`), the toolbox E-core pin is skipped automatically. |
| `cst swap` | Needs your swap partition's UUID and size | In [`swap-setup.sh`](swap-setup.sh), set `DISK_SWAP_PARTUUID` and `DISK_SWAP_SIZE_BYTES` to match your dedicated swap partition, and adjust `MemoryLow=` in [`swap/units/machine.slice.d-50-memory-low.conf`](swap/units/machine.slice.d-50-memory-low.conf) (or zswap's pool cap in [`swap/tmpfiles/zswap.conf`](swap/tmpfiles/zswap.conf)). |
| `cst btrfs` | Btrfs root on Fedora Silverblue / Workstation | Health checks, stall monitoring, writeback sysctls, automatic reclaim, snapshots and `~/Bulk` discover the root btrfs UUID dynamically. Check `EXPECTED_OPTS` in [`btrfs-setup.sh`](btrfs-setup.sh) if your mount options differ (e.g. single-disk btrfs without `degraded`), and pass `--host` / `--port` / `--user` / `--path` to `cst btrfs snapshots nas setup`. |

## Other files in this folder

- `cst-link.bash`: the helper that `cst btrfs install` and `cst cpu install`
  use to create the link `~/.local/bin/cst` ([The `cst` command](#the-cst-command)).
- `fix-anaconda-webui-btrfs-compress.sh`: a one-time helper for **installing**
  Fedora, not for the running system. You run it as root in the Fedora Live
  system, before the installer, so that btrfs mount points created in the
  installer's web interface get the options `compress=...` (Fedora's default,
  usually `zstd:1`), `noatime`, `flushoncommit` and `commit=10`. Run
  `sudo ./fix-anaconda-webui-btrfs-compress.sh --help` for its own help.
  *(Not verified: whether the current installation was made with it.
  `cst btrfs status` does not check `commit=10`.)*
- `btrfs/tests/`, `cpu/tests/`, `swap/tests/`, `bluetooth/tests/`, `toolchains/tests/`, `doctor/tests/`: tests for
  `btrfs-snapshot-core`, `btrfs-bulk`, `chrome-cache`, `cst-link.bash`, the `cpu`
  module, the `swap` module, the `bluetooth` module, the `toolchains` module and
  `cst doctor`. The unit tests need no root and no btrfs:
  `python3 -I -B -m unittest`, run from this folder, runs all of them.

## Glossary

| Term | Meaning |
|---|---|
| btrfs | The filesystem of this PC: the part of Linux that organises files on the disk. It can keep checksums, compress data and take snapshots. |
| Bulk (`~/Bulk`) | The folder for everything that can be downloaded again: no snapshots, no NAS backup. |
| CST | Cominotti Setup Tools: the name of this repository and of its `cst` command. |
| cgroup | A group of processes that the kernel counts and limits together (one service, one container). |
| deployment | On Silverblue, one bootable version of the system. The **booted** one runs now; a **staged** one, prepared by an update, becomes the system at the next boot. |
| dm-crypt / LUKS | Linux disk encryption. You type a passphrase per disk at boot. |
| E-core / P-core | Efficient cores (smaller, slower, many: CPUs 8-23) and Performance cores (fast: CPUs 0-7). |
| frontend, module | One script that installs, shows and removes a group of parts (`btrfs-setup.sh`, `cpu-setup.sh`, `swap-setup.sh`, `bluetooth-setup.sh`, `toolchains-setup.sh`); `cst MODULE` runs it. |
| GameMode | A small service that runs actions when a game starts and ends. |
| host | The real Fedora system of the PC, outside any toolbox. |
| layered package | A package added on top of the Silverblue system image with `rpm-ostree install NAME`; request it by name, never with a version (see [doctor/README.md, section 3](doctor/README.md#3-why-pinned-layered-packages-are-a-problem)). |
| LE Audio / LDAC | Two ways to send good-quality sound to a Bluetooth headset: LE Audio (newer, codec LC3) and classic Bluetooth A2DP with Sony's LDAC codec. |
| NAS | A network storage box; here a Synology. |
| NVMe | A fast kind of SSD (a disk without moving parts). |
| RAID1 | Every piece of data is stored twice, once on each disk. |
| snapshot | A frozen, read-only copy of a subvolume at one moment. |
| stall | Programs have to wait for the disk or the filesystem. |
| subvolume | A part of a btrfs filesystem that looks like a folder but can be snapshotted on its own. |
| symlink | A shortcut: a path that points to another path. |
| Silverblue | A Fedora edition whose system files are a read-only image, replaced as a whole at each update. |
| swap | Disk space (here a 128 GiB NVMe partition) where the kernel moves memory pages nobody used for a while, when RAM is short. |
| toolbox | A container (a separate, small Linux environment) on top of the host, for development tools. |
| zswap / zram | Two ways to compress swapped memory in RAM. zswap is a cache in front of a swap disk (used here); zram is a swap device in RAM (Fedora's default, switched off here). |

Each detailed document has a longer glossary at its end.

## License

Licensed under the [Mozilla Public License 2.0 (MPL-2.0)](LICENSE).
