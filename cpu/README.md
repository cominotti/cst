# `cst cpu`: smooth games while builds, Docker and the VM run

This folder holds the parts of `cpu-setup.sh`, the `cpu` module of CST
(Cominotti Setup Tools). The script lives one level up:
`~/Workspace/cst/cpu-setup.sh`. You run it as `cst cpu <command>` (how to set
up `cst`: [../README.md](../README.md#the-cst-command));
`bash cpu-setup.sh <command>` from the repository folder does the same. It is
written for one machine, the workstation **sophia**:

- CPU: Intel Core Ultra 9 285K. It has two kinds of cores.
  **Performance cores (P-cores)** are the fast, big cores: here CPUs 0-7.
  **Efficient cores (E-cores)** are smaller and slower, but there are more of
  them: here CPUs 8-23. Each core runs one thread at a time (no SMT, also
  called Hyper-Threading).
- Fedora Silverblue, GNOME, Steam as a Flatpak, Docker, podman toolboxes,
  and a Windows VM called `win11-devbox` (managed by libvirt).

Many values (CPU numbers, VM name, user) are set for this machine. Read the
whole document before you use it on another computer.

The entry point of the whole repository is [../README.md](../README.md). The
storage side (disks, btrfs, snapshots, Docker volumes) is `cst btrfs`,
described in [../btrfs/README.md](../btrfs/README.md).

> **In short: what this does to your system**
>
> - **Permanent** (until `uninstall`): lower CPU priority for the VM and the
>   toolboxes, toolboxes on the E-cores only, a GameMode hook, and a watchdog
>   timer. The files are listed in section 6.1.
> - **Only while a game runs** (and cleared by a reboot): the VM and Docker
>   move to CPUs 12-23 with a very low priority, the game's memory is
>   protected, desktop apps' swapped pages stay in RAM (zswap) instead of
>   the swap disk, the P-cores react faster.
> - **Optional, only if you run them**: `vm-pin` (section 8), `docker-slice`
>   (section 9), memory experiments (section 12).
> - **Undo**: `cst cpu uninstall` (section 13).
> - The kernel and its scheduler are not changed.

---

## 1. The problem

A game (here mostly Dota 2) shows **microstutter**: short, irregular frame
hiccups. The average FPS (frames per second) looks fine, but some frames
arrive late. This happens while heavy work runs at the same time:

- builds and test suites (cargo, Go, Gradle, Maven, ...) in toolboxes;
- Docker containers;
- the Windows VM.

The goal: the game stays smooth, and the background work still finishes. It
can be a bit slower while a game runs.

---

## 2. Basic ideas you need

You do not need to be a Linux expert. These ideas are enough.

- **cgroup** ("control group"): a box of processes. In this document,
  **box** always means cgroup. The kernel can limit or favour a whole box:
  its CPU share, the CPUs it may use, its memory protection. Boxes are
  nested, like folders.
- **systemd** names boxes by their kind:
  - a **slice** is a box that only holds other boxes;
  - a **service** is a background program that systemd starts;
  - a **scope** is a box systemd makes for processes that someone else
    started, for example one Docker container;
  - **unit** is the general word for all of these.
- **user manager**: the systemd instance that runs your own session
  (`user@1000.service`, if your user ID is 1000). It normally starts at
  login. It has its own boxes inside.

This is the tree of boxes that matters here (simplified):

```
-.slice (the root)
├── user.slice                      "system user.slice": all user sessions
│   └── user-1000.slice
│       └── user@1000.service       your user manager
│           ├── app.slice           desktop apps, Steam and the game,
│           │                       flatpak-session-helper.service
│           ├── background.slice    GNOME background tasks
│           └── user.slice          "inner user.slice": the toolboxes
├── machine.slice                   the Windows VM (libvirt)
├── system.slice                    system services; Docker containers
│                                   (docker-*.scope) by default
└── builds.slice                    Docker containers, only after
                                    'docker-slice install'
```

There are two boxes named `user.slice`. This document always says
**system `user.slice`** (the top one) or **inner `user.slice`** (the
toolboxes, next to `app.slice`). The inner one sits right next to the game's
box, so it competes directly with the game.

Settings of a box:

- **CPUWeight**: a share, not a limit. The default is 100. It only matters
  when the CPUs are busy. A box with weight 100 gets twice the CPU time of a
  box with weight 50. `idle` is the lowest class: such a box gets only a very
  small share while other boxes want the CPU.
- **AllowedCPUs** (also called the **cpuset**): the list of CPUs a box may use
  at all.
- **MemoryLow** (`memory.low`): "please do not take back this much memory from
  this box while other boxes still have memory to give". It only works if
  every parent box in the chain has at least the same protection.

Also: **EPP** (Energy Performance Preference) is a hint to the CPU, per core.
`performance` means "reach a high frequency fast". Other values save power.

---

## 3. What causes the stutter (the diagnosis)

The script is based on four findings:

| # | Finding | Why it hurts the game |
|---|---------|-----------------------|
| 1 | The VM and Docker also run on the **P-cores**. | The game's main threads need the fast P-cores. Busy VM or build threads share the P-cores with them. A game thread that has to wait a few milliseconds makes a late frame. |
| 2 | The game also uses some **E-cores** for its own worker threads. | So moving builds "to the E-cores" is not enough. While a game runs, the VM and Docker (and, with delegation, the toolboxes; see 6.3) move to CPUs **12-23**, so CPUs 8-11 stay available for the game's E-core workers. |
| 3 | All boxes had the **same weight** (priority). | When all CPUs are busy, Linux shares CPU time by weight. With equal weights, the VM gets as much CPU time as the whole desktop, game included. |
| 4 | The kernel took the game's **memory** back ("reclaim"). | When RAM is tight, the kernel takes memory pages from programs to give to others. If it takes them from the game, the game must wait to get them back. That wait is a stutter. |

So the fix is about **placement** (which CPUs), **priority** (weights) and
**memory protection**.

The script does not change the kernel scheduler or any kernel setting. It
only changes where programs run, their priority, and memory protection.
(Details: section 15.)

---

## 4. Quick start

Run every command on the **host** (not in a toolbox), as your normal user:

```bash
cst cpu install
```

Then, by hand:

1. **Restart your user manager.** This makes the cpuset delegation
   (section 6.3) active, and makes sure that your running session has the
   `gamemode` group. The simplest way is a **reboot**.
   Logging out and in is only enough if lingering is off and no other session
   of yours is open (for example an ssh login). Check lingering with:
   ```bash
   loginctl show-user $USER -p Linger
   ```
   `Linger=yes` (true on sophia) means the user manager keeps running after
   logout, so you must reboot. Note: the message at the end of `install` only
   says "log out and in once"; with lingering on, that is not enough.
2. In Steam: game > Properties > Launch options:
   ```
   gamemoderun %command%
   ```
3. Check with `cst cpu status`. You should see
   `cpuset delegated: yes`, `group: member`, `ini: managed`, and
   `effective (cgroup): toolboxes [8-23]` (sample output in section 6.5).
4. Optional. Each one is a separate decision:
   - **Pin the VM to CPUs 12-23 for good** (section 8):
     `vm-pin plan` (preview only), then `vm-pin apply` (asks y/N), then shut
     down the VM and start it again.
     ```bash
     cst cpu vm-pin plan
     cst cpu vm-pin apply
     ```
   - **Move Docker containers to a low-priority box** (section 9):
     `docker-slice install`, then restart Docker at a time when no important
     container runs.
     ```bash
     cst cpu docker-slice install
     sudo systemctl restart docker
     ```
5. Measure (while a build runs; see section 11):
   ```bash
   cst cpu watch -l baseline --game dota2 --seconds 300
   ```

---

## 5. Commands

```
cst cpu help
```

| Command | Runs as | What it does |
|---------|---------|--------------|
| `install` | needs sudo (asks for password) | Installs everything in section 6. Safe to run again (idempotent: running it twice gives the same result; unchanged files print `unchanged:`). |
| `status` | needs sudo | The whole picture (sample in 6.5). |
| `uninstall` | needs sudo | Removes what `install` added. |
| `game on\|off\|state` | needs sudo | Switch the game profile by hand, or show it. |
| `vm-pin plan\|apply\|status` | needs sudo | Pin the VM to CPUs 12-23 permanently. |
| `docker-slice install\|status\|uninstall` | needs sudo | Put Docker containers in `builds.slice`. |
| `experiment list\|on NAME\|off NAME` | needs sudo | Runtime-only memory settings for A/B tests. |
| `watch -l LABEL [...]` | your user | Record game wait, memory stalls, frequencies. |
| `report [RUN...]` | your user | Compare recorded runs. |
| `help` | anyone | Show the help text. |

Rules:

- **Run every command as your normal user.** Commands marked "needs sudo"
  call `sudo` themselves: they ask for your password and then run as root.
- Do **not** run `install` from a root shell (for example after `sudo -i`).
  It needs to know your user (from `SUDO_USER`) and stops with:
  `install: run through sudo from your user account (needs SUDO_USER)`.
  Later commands find your user in the settings file (`TARGET_USER`).
- "needs sudo" commands must run **on the host**. Inside a toolbox they stop
  with: `'<command>' must run on the host, not inside a toolbox/container.`
- `watch` and `report` work on the host and inside a toolbox. Inside a toolbox
  they run on the host through `flatpak-spawn --host`.

---

## 6. What `install` changes on your system

### 6.1 Files

| Path | Purpose |
|------|---------|
| `/etc/cpu-setup/cpu-setup.conf` | Settings (6.2). Written only if missing. |
| `/usr/local/sbin/cpu-game-profile` | The game profile (from `cpu/game-profile`). |
| `/usr/local/sbin/cpu-vm-pin` | VM pinning tool (from `cpu/vm-pin`). |
| `/usr/local/bin/cpu-watch` | Measurement tool (from `cpu/cpu-watch`). |
| `~/.local/bin/cst` | Link to `~/Workspace/cst/cst`, so the hints of these tools (`cst cpu ...`) work as typed. Created for the user who ran `sudo`, only if nothing else is there ([README.md, The `cst` command](../README.md#the-cst-command)). |
| `/etc/systemd/system/cpu-game.service` | Runs `cpu-game-profile on` when started, `off` when stopped. |
| `/etc/systemd/system/cpu-game-watchdog.service` + `.timer` | Watchdog, every 30 s (first run 2 min after boot). Enabled and started. |
| `/etc/systemd/system/machine.slice.d/50-cpu-setup.conf` | VM baseline weight `CPUWeight=50`. |
| `/etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf` | `Delegate=cpuset` for user managers (6.3). |
| `/etc/polkit-1/rules.d/50-cpu-setup-game.rules` | Lets your session start/stop only `cpu-game.service`. |
| `/etc/tmpfiles.d/cpu-setup-hwp-boost.conf` | Turns on intel_pstate's HWP dynamic boost at every boot (6.3). |
| `~/.config/systemd/user/user.slice.d/50-cpu-setup.conf` | Toolboxes: `CPUWeight=30`, `AllowedCPUs=8-23`. |
| `~/.config/systemd/user/flatpak-session-helper.service.d/50-cpu-setup.conf` | Host commands of toolboxes: `AllowedCPUs=8-23`. |
| `~/.config/gamemode.ini` | GameMode config (only if you do not have your own). |
| `/etc/group`, `/etc/gshadow` | `gamemode` group copied in, your user added (6.4). |

A **drop-in** is a small extra file in a `<unit>.d/` folder that changes one
part of a systemd unit, without editing the unit itself.

The E-core list (`8-23`) is read from `/sys/devices/cpu_atom/cpus`, not from
the settings file. If that file is missing, the script prints
`NOTE: no E-cores found (/sys/devices/cpu_atom/cpus): toolbox CPU restriction skipped`
and only sets the weight.

The units, drop-ins, polkit rule, settings file, `builds.slice` and
`gamemode.ini` start with a comment that contains `Managed by cpu-setup.sh`.
The three programs say `Installed by ...` or `Installed as ...` instead.
`daemon.json` and the lines in `/etc/group` and `/etc/gshadow` have no
comment. `uninstall` uses the marker only for one decision: whether
`~/.config/gamemode.ini` is the script's file (and may be deleted).

### 6.2 Settings file

`/etc/cpu-setup/cpu-setup.conf`, exactly as `install` writes it:

```
# Managed by cpu-setup.sh (CST); values may be edited.
TARGET_USER='<your user>'
BUILD_CPUS='12-23'
P_CORES='0-7'
VM_WEIGHT=10
GAME_MEMORY_LOW='16G'
GAME_ZSWAP_WRITEBACK='no'
VM_NAME='win11-devbox'
VM_VCPUS=12
```

| Key | Meaning | Read by |
|-----|---------|---------|
| `TARGET_USER` | Your user. Needed for everything in your session. | `cpu-setup.sh`, game profile |
| `BUILD_CPUS` | Where the VM and Docker go during games; the VM always, after `vm-pin`. | game profile, `vm-pin` |
| `P_CORES` | Cores that get EPP `performance` during games. | game profile |
| `VM_WEIGHT` | VM weight during games. | game profile |
| `GAME_MEMORY_LOW` | Memory protection for the game (Dota 2 uses about 10 GB, plus Steam). | game profile |
| `GAME_ZSWAP_WRITEBACK` | `no`: during games, swapped pages of desktop apps stay in zswap, never on the swap disk (7.1). `yes`: leave it alone. Missing (files written before this key existed) means `no`. | game profile |
| `VM_NAME` | The libvirt VM name. | `vm-pin` |
| `VM_VCPUS` | Number of virtual CPUs `vm-pin` gives the VM. | `vm-pin` |

You may edit the values. Rules:

- **No comments on the same line as a value.** Put comments on their own line,
  starting with `#`. A comment after a value breaks the parsers (for example,
  `vm-pin` would read `BUILD_CPUS` as `12-23'   # ...`).
- Keep the quotes as shown.

`install` never overwrites this file, and `uninstall` keeps it.

### 6.3 Baseline: always active, even without a game

| Box | Setting | Why |
|-----|---------|-----|
| `machine.slice` (VM) | `CPUWeight=50` | The VM never gets the same share as the desktop (the default would be 100). |
| toolboxes (inner `user.slice`) | `CPUWeight=30`, `AllowedCPUs=8-23` | GNOME gives its own background tasks (`background.slice`) weight 30 too, so toolboxes get the same low priority as other background work. Builds stay off the P-cores. |
| `flatpak-session-helper.service` | `AllowedCPUs=8-23` | Commands that toolboxes start on the host with `flatpak-spawn --host` run here, outside the toolbox's box. Without this, they would escape to the P-cores. |
| `builds.slice` (only after `docker-slice install`) | `CPUWeight=20`, `IOWeight=20`, `AllowedCPUs=8-23` | Docker builds and tests stay low and on the E-cores. |
| CPU frequency (intel_pstate) | `hwp_dynamic_boost=1` | When a task wakes up after waiting for the disk (a build doing many fsyncs, a game loading files), the CPU raises its frequency at once instead of ramping up. Costs a little power during those moments. `install` sets it now and a tmpfiles.d file keeps it after reboots; `uninstall` removes the file and sets it back to `0`. Skipped (with a NOTE) on CPUs without intel_pstate in HWP mode. |

**Why the E-core limit helps builds too:** programs like Claude Code, shells,
cargo, go, gradle and maven look at the CPUs they are *allowed* to use to
decide how many parallel jobs to start. Inside a toolbox they see 16 CPUs
(8-23), so they start fewer jobs. Fewer parallel jobs means less fighting for
CPU and memory. Builds are a little slower, but the desktop stays responsive.
No per-tool configuration is needed.

**Delegation and the user manager restart:** your user manager may not set
`AllowedCPUs` on its boxes unless the system *delegates* the cpuset controller
to it (delegation = the system lets your session manage that feature itself).
`install` adds `Delegate=cpuset`. This only takes effect when your **user
manager starts again**: reboot, or log out and in if lingering is off (see
section 4, step 1).

Until then:

- the toolbox **weight 30 already works** (install reloads your user manager);
- only the **E-core limit waits**: `status` shows
  `cpuset delegated: no (next login)`.

**Side effect:** other Flatpak apps that have the `org.freedesktop.Flatpak`
permission also start host commands through `flatpak-session-helper`. On this
machine that is OBS, Remmina, RustConn, Resources and TestCenter. Their host
commands also run on the E-cores.

### 6.4 GameMode

**GameMode** is a small service (`gamemoded`) that does things when a game
starts and ends. The game must ask for it; `gamemoderun %command%` in Steam
does this.

Here GameMode does almost nothing by itself (see `cpu/gamemode.ini`):

- it runs the hooks in `[custom]`:
  ```
  start=systemctl start --no-block cpu-game.service
  end=systemctl stop --no-block cpu-game.service
  ```
- `disable_splitlock=1`: turns off the kernel's split-lock penalty while a game
  runs. (A *split lock* is a memory operation that crosses two cache lines, the
  small blocks the CPU reads memory in. The kernel normally slows down
  programs that do it.)
- `inhibit_screensaver=1`.
- No CPU governor change (`desiredgov=powersave` is the normal one; EPP is set
  per core type by the profile), no `renice`, no I/O priority, no core
  pinning or parking (Source 2 games, like Dota 2, place their own P- and
  E-core threads).

If `~/.config/gamemode.ini` already exists and is **your own** file, install
keeps it and prints:
`NOTE: ... is your own: add [custom] start/end from .../gamemode.ini by hand`.

**The `gamemode` group: why.** GameMode needs this group to switch off the
split-lock penalty without asking for a password. Fedora's polkit rule
(`/usr/share/polkit-1/rules.d/gamemode.rules`) lets members of `gamemode` run
GameMode's helpers (`com.feralinteractive.GameMode.procsys-helper` and
others) with no password.

**The `gamemode` group on Silverblue: how.** On Silverblue, system groups live
in `/usr/lib/group`, but `usermod` only works with groups in `/etc/group`. So
install copies the `gamemode` line from `/usr/lib/group` to `/etc/group` (and
adds `gamemode:!::` to `/etc/gshadow`), then adds your user. If the group does
not exist at all, it prints
`no 'gamemode' group on this system (is the gamemode package installed?)`.

**polkit** decides which user may do which privileged action. Normally only
root may start system services. The rule from 6.1 lets your user start or stop
**only** `cpu-game.service`, and only from the active local session. Nothing
else.

Not verified by the script: that `gamemoderun` works inside the Steam Flatpak
on this machine. Section 14 shows how to check it.

### 6.5 Sample output

The values below are **illustrative**. The format follows the code.

`cst cpu status`, with no game running, after the user manager
restart:

```
kernel 7.2.9-200.fc44.x86_64, scheduler EEVDF, preempt lazy

== game profile
  game profile: off
    machine.slice   cpus=         weight=50
    builds.slice    cpus=8-23     weight=20
    toolboxes       weight=30
    app.slice       zswap writeback=yes
    toolboxes       cpus effective=8-23
    EPP cpu0 balance_performance, cpu12 balance_performance
  watchdog timer: active

== GameMode
  group: member; ini: managed; gamemoded: inactive

== baseline weights
  machine.slice 50, toolboxes 30, app.slice 100, background.slice 30

== E-cores for toolboxes and host commands
  cpuset delegated: yes; toolboxes AllowedCPUs=8-23; flatpak-spawn --host AllowedCPUs=8-23
  effective (cgroup): toolboxes [8-23]

== VM
  ...
== Docker
  builds.slice: active; daemon.json cgroup-parent: builds.slice; containers in it: 2
...
```

Lines to look at:

- `cpuset delegated: yes` and `toolboxes [8-23]`: the E-core limit works.
- `group: member` means "listed in `/etc/group`". It does not prove that your
  running session already has the group.
- `gamemoded` may show `inactive` when no game runs; it starts on demand.
- `builds.slice` lines appear only after `docker-slice install`.

`cst cpu game state` while the profile is on:

```
game profile: ON (by hand)
  machine.slice   cpus=12-23    weight=10
  builds.slice    cpus=12-23    weight=idle
  toolboxes       weight=idle
  app.slice       zswap writeback=no
  toolboxes       cpus effective=12-23
  EPP cpu0 performance, cpu12 balance_performance
```

`(by hand)` appears only after `game on`.

---

## 7. The automatic game profile

### 7.1 What it does while a game runs

`cpu-game-profile on` makes these changes. All are **runtime-only**: the
systemd changes are **runtime overrides** (drop-ins under `/run`, written by
`systemctl set-property --runtime`), and EPP is a value in sysfs (the kernel's
`/sys` folder). A reboot clears both.

The first column says which finding of section 3 the change fixes. The values
are the defaults from `/etc/cpu-setup/cpu-setup.conf` (`BUILD_CPUS`,
`VM_WEIGHT`, `GAME_MEMORY_LOW`, `GAME_ZSWAP_WRITEBACK`, `P_CORES`).

| Fixes finding | Change | Value |
|---|--------|-------|
| 1, 2, 3 | VM: `machine.slice` | `AllowedCPUs=12-23`, `CPUWeight=10` |
| 1, 2, 3 | Docker, if `builds.slice` is active | `AllowedCPUs=12-23`, `CPUWeight=idle` |
| 1, 2 | Docker containers **outside** `builds.slice` (`docker-*.scope`) | `AllowedCPUs=12-23` (no weight change) |
| 3 | Toolboxes (inner `user.slice`) | `CPUWeight=idle` |
| 2 | Only if cpuset is delegated (6.3): toolboxes and `flatpak-session-helper.service` | `AllowedCPUs=12-23`; if your user manager is older than the delegation (no reboot since `install`), it ignores this, and the profile writes `12-23` straight into their cgroup file `cpuset.cpus` |
| 4 | Memory chain: system `user.slice`, `user-<uid>.slice`, `user@<uid>.service`, then `app.slice` in your user manager, and **every** Steam scope `app-flatpak-com.valvesoftware.Steam-*` (the client has one, and each game gets its own; a game started after the profile gets it at the next watchdog run, within 30 s) | `MemoryLow=16G` |
| 4 | Desktop apps and the game: `app.slice` in your user manager (only if `GAME_ZSWAP_WRITEBACK=no`, and only if systemd knows `MemoryZSwapWriteback`) | `MemoryZSwapWriteback=no` |
| - | P-cores 0-7 | EPP `performance` (E-cores keep their EPP) |

In short: the game gets the P-cores and E-cores 8-11, the VM and Docker move
to 12-23 with a low priority, toolboxes get the idle class, and the kernel
avoids taking the game's memory.

**Why zswap writeback is switched off for `app.slice`.** `MemoryLow` keeps the
game's pages in RAM. When the kernel still takes some (from the game or from
the browser next to it), they go to zswap, the compressed pool in RAM. Normally
zswap later writes cold pages on to the NVMe swap. With
`MemoryZSwapWriteback=no` it does not, for `app.slice` only:

- **Gain:** a page that comes back is decompressed from RAM (microseconds)
  instead of read from the encrypted NVMe (about 0.1 ms or more, and it can
  wait behind a btrfs fsync). On this PC desktop apps read back about half of
  what was written to disk, more than any other cgroup (`cst swap status`).
- **Cost:** those pages keep using RAM (compressed, about 3:1). The kernel
  takes more from the VM and the builds instead, which may still go to disk.
  Pages that zswap cannot store stay in RAM uncompressed. If `app.slice` alone
  outgrows the RAM, the OOM killer acts sooner than with disk swap.
- Only during games. The rest of the time desktop apps may use the swap disk.

To keep the old behaviour, set `GAME_ZSWAP_WRITEBACK='yes'` in
`/etc/cpu-setup/cpu-setup.conf`.

Note: `machine.slice` (the VM) also has `MemoryLow=48G`, set by the `swap`
module (`cst swap apply`, see [../swap/README.md](../swap/README.md)), not by
this script. So the game's 16G protection competes with a large VM protection.
`cst cpu status` shows neither value; `cst swap status` shows both, in the
`low` column of its per-cgroup table.

The profile writes this message to the journal (the system log):
`on: VM/Docker on 12-23, toolboxes idle, game memory protected (16G), app zswap writeback no, P-cores EPP performance`

### 7.2 How it switches on and off

```
game starts with gamemoderun
   -> gamemoded runs the [custom] start hook
   -> systemctl start cpu-game.service   (allowed by polkit)
   -> cpu-game-profile on
game ends
   -> [custom] end hook -> systemctl stop cpu-game.service -> cpu-game-profile off
```

**The watchdog** (`cpu-game-watchdog.timer`, every 30 s) runs
`cpu-game-profile tick`. It looks for a "GameMode client": **any** process
that has GameMode's library (`libgamemode`) loaded.

- Profile on, no client left (game crashed or was killed), not switched on by
  hand: it switches off (`no GameMode client left: switching off`). So the VM
  and builds are not left slowed down.
- Profile off, a client runs, and you did not switch it off by hand: it
  switches on (`GameMode client running but profile off: switching on`). This
  is a fallback if the GameMode hook failed.
- Profile on: Docker containers started in the meantime get `AllowedCPUs=12-23`,
  and Steam scopes started in the meantime (the game's own) get `MemoryLow`.

`on`, `off` and `tick` never run at the same time: they share a lock file,
`/run/cpu-setup/lock`, and wait up to 60 s for it.

### 7.3 By hand (manual)

```bash
cst cpu game state   # what is applied now
cst cpu game on      # switch on by hand
cst cpu game off     # switch off now
```

- `game on` marks the profile as manual (`/run/cpu-setup/manual`), and
  `game state` shows `game profile: ON (by hand)`. The **watchdog** will not
  switch it off, even with no game. Only the watchdog respects this mark.
  The profile stays on until one of these happens:
  - you run `game off`;
  - a GameMode game ends (its end hook stops `cpu-game.service`);
  - you run `uninstall`;
  - a reboot.
- `game off` switches the profile off and deletes the manual mark. It also
  writes `/run/cpu-setup/suppress`: the **watchdog** will not switch the
  profile on again while a GameMode client runs. When no client is left, the
  next watchdog run deletes the suppress file. Note:
  - if no game runs when you type `game off`, the suppress file is gone after
    at most 30 s;
  - the GameMode **start hook** ignores the suppress file: a game that starts
    later still switches the profile on.

### 7.4 The exact revert

In short: `off` puts every setting back exactly as it was before `on`, even if
`on` stopped halfway.

**Details (for experts).** Before each change, `on` writes a line to
`/run/cpu-setup/game.state`: which property, and whether a runtime override
already existed (with its old value). It writes the line *before* the change,
so if `on` stops halfway, `off` can still undo what was done.

`off` reads the file from the newest line to the oldest:

- property that had **no** runtime override before: the override file is
  deleted (`/run/systemd/system.control/<unit>.d/50-<Property>.conf`, or the
  same under `/run/user/<uid>/systemd/user.control/`), then systemd re-reads
  its files (`daemon-reload`). The unit goes back to its defaults or its
  `/etc` drop-ins. No leftover override stays behind to hide a drop-in later;
- property that **had** an override: its old value is set back;
- EPP: each P-core gets its old value back;
- a `cpuset.cpus` file written directly (7.1, toolboxes): its old value is
  written back (usually empty, which means "no limit of its own").

Then it logs `off: previous CPU, weight, memory, zswap writeback and EPP settings restored`.

---

## 8. Optional: pin the VM (`vm-pin`)

**libvirt** is the system that manages the VM. It stores the VM's settings as
an XML text file (the "definition"). `virsh` is libvirt's command-line tool,
and `virsh define` saves a new version of the definition.

**Why:** during games the VM is limited to 12-23 anyway. `vm-pin` makes this
permanent. It gives the VM 12 virtual CPUs (vCPUs), as many as the 12 host
CPUs it may use. Each vCPU may run on any of CPUs 12-23 (not one fixed CPU
per vCPU). If Windows has more virtual CPUs than real CPUs, some virtual CPUs
must wait, and Windows can slow down in strange ways. With `vm-pin` that
cannot happen.

```bash
cst cpu vm-pin status   # vCPU count (live and saved) and the saved pinning
cst cpu vm-pin plan     # show the XML diff, change nothing
cst cpu vm-pin apply    # same diff, asks [y/N], then virsh define
```

An optional VM name can follow (`vm-pin plan OTHER-VM`); the default is
`VM_NAME`. (`status` shows only part of `vm-pin status`.)

What `apply` changes in the VM definition:

- `<vcpu placement="static">12</vcpu>` (any `current=` is removed);
- CPU topology: `sockets=1`, `cores=12`, `threads=1` (and `dies`/`clusters`
  = 1 if present);
- `<cputune>`: one `<vcpupin cpuset="12-23">` per vCPU and
  `<emulatorpin cpuset="12-23">` (old pins are replaced). The *emulator
  threads* are QEMU's helper threads for the VM.

Safety:

- If the VM has `<cputune/vcpusched>`, `<cputune/cachetune>`, `<cpu/numa>` or
  `<vcpus>`, it stops: `... adjust it by hand (virsh edit ...); nothing changed`.
- `apply` needs a terminal to ask. `y`/`Y` mean yes; so do `s`/`S`
  (`s` = "sim", Portuguese for yes). Anything else changes nothing.
- Before the first apply, the old definition is saved to
  `/etc/cpu-setup/<VM>.xml.before-vm-pin` (mode 0600, because it may contain
  graphics passwords). It is never overwritten.
- It takes effect at the **next start of the VM**. Shutting down and starting
  the VM is left to you.

Undo (replace `<VM>` with the VM name; the default is `win11-devbox`):

```bash
sudo virsh -c qemu:///system define /etc/cpu-setup/<VM>.xml.before-vm-pin
# default VM:
sudo virsh -c qemu:///system define /etc/cpu-setup/win11-devbox.xml.before-vm-pin
```

The old settings return at the next start of the VM.

---

## 9. Optional: Docker in `builds.slice` (`docker-slice`)

**Why:** `dockerd` is a system service. Its containers do not live in your
user session, so the toolbox rules do not reach them. By default they land in
`system.slice` with normal weight, on all CPUs.

```bash
cst cpu docker-slice install
cst cpu docker-slice status
cst cpu docker-slice uninstall
```

`install`:

- writes `/etc/systemd/system/builds.slice` (`CPUWeight=20`, `IOWeight=20`,
  `AllowedCPUs=8-23`) and starts it. It is a top-level slice, next to the
  system `user.slice`, so its weight really competes with the desktop;
- then adds `"cgroup-parent": "builds.slice"` to `/etc/docker/daemon.json`.
  Other keys stay. The file is rewritten as JSON with 2-space indentation.
- If `cgroup-parent` already has another value, it stops with
  `... already sets cgroup-parent=...: left alone` and does not touch
  `daemon.json`. But `builds.slice` was already written and started at that
  point. Remove it with `docker-slice uninstall` if you do not want it.

It does **not** restart Docker, because a restart stops running containers.
Do it at a time that suits you:

```bash
sudo systemctl restart docker
```

Containers **started** after the Docker restart go to `builds.slice`: new
ones, and also existing ones that start again (for example, containers with a
restart policy come back in `builds.slice`). Not verified on this machine:
this is based on how Docker (Moby) applies the default cgroup parent when a
container starts. Containers that run outside `builds.slice` still get
`AllowedCPUs=12-23` during games (section 7.1).

`uninstall`:

- removes the `cgroup-parent` key, but only if its value is exactly
  `builds.slice` (it deletes the whole `daemon.json` if it becomes empty);
- deletes the slice file and reloads systemd. It does **not** stop
  `builds.slice`: containers in it stay there until they restart.
- Again, restart Docker yourself.

This also covers Docker commands that Claude Code or a toolbox start through
`flatpak-spawn --host`, because `dockerd` creates the containers.

The storage side of Docker is in `cst btrfs`: Docker volumes without
copy-on-write, and the optional weekly cleanup `docker-gc` (see
[../btrfs/README.md](../btrfs/README.md), sections 4.10 and 11). The NOCOW
part reads only `data-root` from `/etc/docker/daemon.json`, and `docker-gc`
does not read that file, so they work together with `docker-slice`.

---

## 10. What is covered, and what is not

| Workload | Where it runs | Baseline | During games |
|----------|---------------|----------|--------------|
| Toolboxes (podman, rootless) and everything inside | inner `user.slice` | weight 30, E-cores* | idle, 12-23* |
| Host commands from toolboxes (`flatpak-spawn --host`) | `app.slice/flatpak-session-helper.service` | E-cores*, normal weight | 12-23*, normal weight |
| Windows VM | `machine.slice` | weight 50 (+ 12-23 with `vm-pin`) | weight 10, 12-23 |
| Docker with `docker-slice` | `builds.slice` | weight 20, E-cores | idle, 12-23 |
| Docker without `docker-slice` | `system.slice/docker-*.scope` | not covered | 12-23 only |

\* the CPU limits need the cpuset delegation (6.3), so they work only after
the user manager restart.

Note on host commands from toolboxes: they live in `app.slice`, next to the
game. Only their CPU list is limited, not their weight. Without delegation
they are not limited at all. The same is true for `watch -- CMD` started from
a toolbox (section 11).

**Not covered:**

- Commands in a **host terminal** (not in a toolbox): they run in `app.slice`,
  the same box as the game, with normal weight and on all CPUs.
- `systemd-run --user` jobs: they go to `app.slice` too.
- IDE builds: an IDE started from the desktop runs in `app.slice`, and so do
  its builds. (An IDE inside a toolbox is covered.)
- Podman **Quadlets** (containers that run as systemd user services): user
  services go to `app.slice` by default, so they are not covered.
- **Rootful** podman (`sudo podman`, run as root; normal toolboxes are
  *rootless*): the script does nothing for it on purpose. Not verified here:
  Podman's default box for rootful containers is `machine.slice`. In that case
  they would get the VM's rules without that being planned.
- Docker without `docker-slice`: only the 12-23 limit during games.

---

## 11. Measuring: `watch` and `report`

Measure, then compare: the feeling of smoothness is not reliable. `watch`
only reads; it changes nothing on the system.

This `watch` measures CPU and memory waits of the game. It does not look at
the disks. If you suspect the disks, use `cst btrfs report` (and its
own `watch`) instead: see
[../btrfs/README.md, section 7](../btrfs/README.md#7-report-was-storage-the-bottleneck).

**Pressure** (PSI, "Pressure Stall Information") is a kernel counter. It shows
how long programs were ready to work but had to wait (for a CPU, or for
memory).

```bash
cst cpu watch -l LABEL [--game NAME] [--seconds N] [--mangohud DIR] [-- CMD...]
cst cpu report [RUN...]
```

`watch` takes one sample per second until `CMD` exits, `N` seconds pass, or
you press Ctrl-C.

- `--game NAME`: the game's exact process name (for example `dota2`). Without
  it, the game is the process with `libgamemode` loaded that has the most
  threads.
- `-- CMD`: runs a command (for example a build) during the recording.
  - Inside a toolbox, `watch` itself runs on the host, so **CMD runs on the
    host too**, not in the toolbox.
  - If CMD is still running when the time is up or you press Ctrl-C, `watch`
    stops it (SIGTERM). Ctrl-C in the terminal also reaches CMD directly. If
    the build must finish, leave out `--seconds`, or use `--seconds` without
    CMD and start the build yourself.
  - When CMD ends by itself, `watch` exits with CMD's exit code.
- `--mangohud DIR`: after the run, the newest `*.csv` file directly in `DIR`
  (no subfolders) that was changed after "5 s before the start" is copied
  into the run. **MangoHud** is an overlay that can log every frame time to a
  CSV file. Setting up MangoHud and its logging is up to you; this script
  does not do it.

Runs are saved in `~/.local/state/cpu-setup/runs/<time>-<label>/`
(`samples.csv`, `meta.json`, maybe `mangohud.csv`).

What is recorded per second:

| Column | Meaning |
|--------|---------|
| `game_cpu_wait_ms` | Time the game's threads were ready to run but waited for a CPU (CPU pressure of the game's box). |
| `game_mem_stall_ms` | Time when all of the game's threads were waiting for memory at the same moment. |
| `game_invol_switches` | How often the game's threads were pushed off a CPU (involuntary context switches). |
| `sys_cpu_some_ms` | CPU pressure of the whole system. |
| `p_mhz`, `e_mhz` | Mean frequency of P-cores and E-cores. |
| `vm_cpus_used`, `builds_cpus_used` | CPUs used by the VM and by Docker (`builds.slice` + `system.slice/docker-*` scopes). Toolbox builds are not counted here. |
| `profile_on` | 1 if the game profile is on. |

Note: the "game's box" is the Steam app scope, so the Steam client counts
too.

`report` prints one line per run (default: the last 10). An **illustrative**
line (one line on screen, wrapped here):

```
20261013T201500-profile-build              299s  game wait   1.2% (max   6.0%)  mem stall       0 ms
  invol/s     850  P  5100 MHz  E  4300 MHz  VM  2.1 cpu  builds  9.8 cpu  profile on
                                         frames: avg 160 fps, 1% low 98, 0.1% low 71
```

How to read it:

- `game wait X%`: the share of time the game waited for a CPU. Lower is better.
- `mem stall`: the total memory stall time. Zero is best.
- `profile on`/`profile off`: whether the game profile was on for most of the
  run. Use this to confirm that a "baseline" run really ran without it.
- With a MangoHud log you also get the `frames:` line. The **1% low** is the
  FPS of the slowest 1% of frames. Stutter is much more visible in the 1% low
  than in the average FPS.

An example test plan (an **A/B test**: the same workload twice, once without
and once with the profile):

```bash
# Start the game first (with gamemoderun) and start the same build each time.
# 1. game + build, profile off.
#    game off: the profile stays off, and the watchdog does not switch it on,
#    until this game ends.
cst cpu game off
cst cpu watch -l baseline-build --game dota2 --seconds 300
# 2. game + the same build, profile on
cst cpu game on
cst cpu watch -l profile-build --game dota2 --seconds 300 --mangohud ~/mangohud-logs
# 'game on' stays on until 'game off', the end of a GameMode game, or a reboot
cst cpu game off
cst cpu report     # check the 'profile on/off' column of each run
```

---

## 12. Memory experiments

**When to try these:** only if `watch` shows memory stalls (`mem stall` above
zero) during a game. Try one at a time.

Three kernel memory settings you can test with `watch`:

| Name | File | Value | Plain meaning |
|------|------|-------|---------------|
| `thp-defer` | `/sys/kernel/mm/transparent_hugepage/defrag` | `defer` | Huge pages (2 MB memory pages): if none is free, do not stop the program to make one; do it later in the background. |
| `wsf-300` | `/proc/sys/vm/watermark_scale_factor` | `300` | The kernel starts freeing memory in the background when free memory drops below a mark (a "watermark"). A higher value starts this earlier, so programs less often have to free memory themselves. |
| `minfree-256m` | `/proc/sys/vm/min_free_kbytes` | `262144` | Always keep at least 256 MiB free. |

```bash
cst cpu experiment list          # current values, (ON) marks
cst cpu experiment on wsf-300
cst cpu experiment off wsf-300
```

- `on` saves the old value to `/run/cpu-setup/exp-<name>` (only the first
  time), then writes the new one.
- `off` writes the saved value back. A **reboot** also reverts everything.
- `uninstall` does **not** switch experiments off. Use `off` or reboot.

A **winner** is a setting that gives lower `mem stall` and a better `1% low`
across a few runs, compared with runs without it. To keep a winner after a
reboot, add it by hand to `swap/sysctl/99-swap-tuning.conf` (for `wsf-300`,
change the `vm.watermark_scale_factor` line already there), then run
`cst swap plan` and `cst swap apply`. The `swap` module owns the memory
settings that apply at boot; see [../swap/README.md](../swap/README.md).
`thp-defer` is not a sysctl (it lives in `/sys/kernel/mm`), so it has no place
there yet.

---

## 13. How to undo everything

```bash
cst cpu game off               # optional; uninstall also stops the profile
cst cpu experiment off NAME    # for each experiment that is ON
cst cpu docker-slice uninstall # if installed; then: sudo systemctl restart docker
cst cpu uninstall
```

`uninstall`:

- stops `cpu-game.service` (this runs the exact revert), disables the timer;
- removes the units, the `machine.slice` and `user@.service` drop-ins, the
  polkit rule and the three programs;
- removes the two user drop-ins and `~/.config/gamemode.ini` (only if it is the
  script's file);
- removes `/etc/tmpfiles.d/cpu-setup-hwp-boost.conf` and sets HWP dynamic
  boost back to `0`;
- reloads systemd.

It **keeps**: `/etc/cpu-setup/cpu-setup.conf`, your `gamemode` group
membership (and the `gamemode` line in `/etc/group`), `builds.slice` and
`daemon.json` (use `docker-slice uninstall`), the VM definition (restore it as
in section 8), your recorded runs in `~/.local/state/cpu-setup/runs/`, and
the link `~/.local/bin/cst` (the other modules use it too).

The cpuset delegation ends when your user manager starts again (reboot; see
section 4). After that, toolboxes can use all CPUs again. Remove the Steam
launch option by hand if you want.

**Optional manual cleanup.** The script does not do these; they are
suggestions:

- leave the `gamemode` group: `sudo gpasswd -d $USER gamemode` (harmless to
  keep);
- delete `/etc/cpu-setup/` (settings and the VM backup) with
  `sudo rm -r /etc/cpu-setup`, but **only after** you restored the VM
  definition, if you want it back;
- delete your recorded runs: `rm -r ~/.local/state/cpu-setup/runs`.

---

## 14. Troubleshooting

**The profile does not switch on when the game starts.**

1. `cst cpu game state` while the game runs.
2. Is `gamemoderun %command%` in the launch options?
3. `cst cpu status`, section "GameMode": `group: member`,
   `ini: managed`? (See the next entries if not.)
4. Wait 30 s: the watchdog switches on if it sees a GameMode client.
5. Logs (the journal):
   ```bash
   journalctl -b -u cpu-game.service -u cpu-game-watchdog.service
   ```
   `TARGET_USER not set in /etc/cpu-setup/cpu-setup.conf` means the settings
   file is broken: fix `TARGET_USER`.

**Does GameMode work inside the Steam Flatpak?** Start the game with
`gamemoderun %command%`, wait 30 s, and run `game state`. Then look at the log
above:

- `on: ...` right after the game started, with no watchdog line: the GameMode
  hook works.
- `GameMode client running but profile off: switching on`: GameMode sees the
  game (so `gamemoderun` works), but the start hook did not switch the profile
  on. The watchdog did it instead, up to 30 s late.
- Neither line and `game profile: off`: no GameMode client was found. Check
  the launch option.

**`status` shows `group: NOT a member`.** Your user is not in the `gamemode`
group in `/etc/group`. Run `install` again. If it prints
`no 'gamemode' group on this system (is the gamemode package installed?)`,
install the `gamemode` package first.

**`status` shows `ini: own/absent`.** `~/.config/gamemode.ini` is missing or
is your own file. If it is missing, run `install` again. If it is your own,
copy the `[custom]` lines from `cpu/gamemode.ini` into it by hand.

**The profile stays on after the game.** Within about 30 s the watchdog
switches it off, unless you used `game on` (manual). Then run `game off`. If it
still stays on, some other process still has `libgamemode` loaded (another
game or tool started with `gamemoderun`, or a leftover Steam launcher
process). Close it, or use `game off`.

**The game still stutters with the profile ON.** Record with `watch` (with and
without the profile), compare with `report`, and look at `game wait` and
`mem stall`. If `mem stall` is above zero, try one memory experiment
(section 12).

**`lock busy` in the journal.** Another `on`/`off`/`tick` ran for more than 60 s.
Run `game state`, then `game off` if needed.

**Toolboxes still use the P-cores**, or `status` still shows
`cpuset delegated: no (next login)` after you logged out and in. Check
lingering: `loginctl show-user $USER -p Linger`. With `Linger=yes` your user
manager keeps running after logout: reboot. Then check
`effective (cgroup): toolboxes [8-23]`.

**I cannot restart my user manager right now.** What already works: the
baseline weights (VM 50, toolboxes 30) and the whole game profile, except the
toolbox CPU limit. Only the E-core limit for toolboxes and their host commands
waits for the restart. Not verified: whether GameMode's split-lock switch
already works before the restart (it depends on the `gamemode` group).

**A build in a toolbox became slower.** Expected: it now has 16 E-cores, and
during games 12 CPUs at the idle class.

**The VM is slow during games.** Expected: weight 10 on CPUs 12-23. It goes
back to normal when the profile switches off.

**`docker-slice install` says `already sets cgroup-parent=...: left alone`.**
Someone set another parent. Decide by hand which one you want. Note that
`builds.slice` was still created (section 9).

**`docker-slice status` shows 0 containers in `builds.slice`.** Docker was not
restarted yet, or no container was started since the restart.

**`vm-pin apply` says `apply asks for confirmation: run it in a terminal`.**
Run it in a real terminal, not from a script or pipe.

**`report` shows `no game found (--game NAME?)`.** Give the process name with
`--game`, for example `--game dota2`.

**`watch -- CMD` from a toolbox ran CMD on the host.** Expected: inside a
toolbox, `watch` runs on the host, so CMD does too. Start the build yourself
in the toolbox, and run `watch` with `--seconds` and no CMD.

**`experiment off` says `could not restore ...`.** The saved value was not
accepted. Reboot to get the defaults back.

**A command says it must run on the host.** Leave the toolbox (open a host
terminal) and run it again.

**Something feels wrong and you want a clean state now.** `game off`, switch
off experiments, or simply reboot: everything the game profile and the
experiments change is runtime-only.

---

## 15. Background: the kernel scheduler

The **scheduler** is the part of the kernel that decides which thread runs on
which CPU, and for how long. Fedora's kernel uses **EEVDF** ("Earliest
Eligible Virtual Deadline First"), the standard Linux scheduler.

**Preemption** means: the kernel may stop a running thread to let a more
urgent one run. The kernel's default preemption mode on this machine is
**lazy** (`CONFIG_PREEMPT_LAZY=y`). It can be switched at boot, because
`CONFIG_PREEMPT_DYNAMIC=y`. The script keeps it.

The script **does not change any of this**:

- no other scheduler, no **sched_ext** (a kernel feature that loads custom
  schedulers from user space). The kernel supports it, but the schedulers
  themselves do not come from Fedora's official repositories, and this setup
  uses only official repositories;
- no change of the preemption mode, no kernel parameters.

The first line of `status` always prints `scheduler EEVDF`. It adds
`+ sched_ext!` if `/sys/kernel/sched_ext/state` says `enabled`. The
`preempt` value is read from `/sys/kernel/debug/sched/preempt`, in debugfs (a
special kernel folder that is not always readable). The field can be empty;
this is harmless.

---

## 16. Files in this folder

| File | Installed as / used by |
|------|------------------------|
| `game-profile` | `/usr/local/sbin/cpu-game-profile` (`on`, `off`, `tick`, `state`) |
| `vm-pin` | `/usr/local/sbin/cpu-vm-pin` |
| `cpu-watch` | `/usr/local/bin/cpu-watch` |
| `gamemode.ini` | `~/.config/gamemode.ini` |
| `cpu-game.rules.in` | polkit rule (`@USER@` replaced by your user) |
| `units/cpu-game.service`, `units/cpu-game-watchdog.service`, `units/cpu-game-watchdog.timer` | `/etc/systemd/system/` |
| `units/machine.slice.d-50-cpu-setup.conf` | `machine.slice.d/50-cpu-setup.conf` |
| `units/user@.service.d-50-cpu-setup-cpuset.conf` | `user@.service.d/50-cpu-setup-cpuset.conf` |
| `units/builds.slice` | `/etc/systemd/system/builds.slice` (by `docker-slice install`) |

---

## 17. Glossary

- **1% low**: FPS of the slowest 1% of frames; a stutter indicator.
- **A/B test**: run the same workload twice, changing one thing, and compare.
- **AllowedCPUs / cpuset**: the CPUs a box may use.
- **box**: in this document, a cgroup.
- **Cache line**: the small block (64 bytes) the CPU reads memory in.
- **cgroup**: a box of processes with shared limits and priorities.
- **CPUWeight**: CPU share when CPUs are busy; `idle` = lowest class.
- **daemon-reload**: tells systemd to read its unit files again.
- **debugfs**: a special kernel folder for debugging, often not readable by
  normal users.
- **Delegation**: the system lets your user manager manage a cgroup feature
  itself.
- **drop-in**: a small extra file that changes one part of a systemd unit.
- **EEVDF**: the default Linux scheduler. It gives each thread a fair share and
  a deadline.
- **EPP**: per-core hint between power saving and fast frequency.
- **Flatpak**: a way to install desktop apps in a sandbox; Steam runs as one.
- **`flatpak-spawn --host`**: runs a command on the host from inside a
  container or Flatpak.
- **FPS / frame time**: frames per second / how long one frame took (ms).
- **GameMode / gamemoded**: service that runs actions when a game starts/ends.
- **Host**: the real operating system, outside any toolbox or container.
- **Idempotent**: running it twice gives the same result as running it once.
- **Involuntary context switch**: a thread was pushed off the CPU; it did not
  give it up itself.
- **Journal / journalctl**: the system log, and the command that reads it.
- **libvirt / virsh**: the system that manages the VM / its command-line tool.
- **Lingering**: your user manager keeps running after you log out.
- **MangoHud**: overlay and logger for FPS and frame times.
- **MemoryLow / memory.low**: protection from memory reclaim.
- **Microstutter**: short, irregular frame hiccups.
- **P-core / E-core**: Performance cores (fast, big) and Efficient cores
  (slower, small, many). Here P = CPUs 0-7, E = CPUs 8-23.
- **polkit**: decides which user may do which privileged action.
- **Preemption / lazy preemption**: the kernel stopping a running thread for a
  more urgent one. "Lazy" means it may wait for a good moment for normal
  threads.
- **Pressure (PSI)**: kernel counters of how long tasks waited for CPU, memory
  or I/O.
- **Quadlet**: a way to run podman containers as systemd services.
- **Reclaim**: the kernel taking memory pages back from programs.
- **Rootful / rootless**: a container run by root / by a normal user.
- **Runtime override**: a drop-in under `/run`, gone after reboot
  (`systemctl set-property --runtime`).
- **sched_ext**: kernel feature to load a custom scheduler. Not used here.
- **Scheduler**: kernel part that decides which thread runs where and when.
- **Silverblue**: a Fedora edition where the system files are read-only and
  updated as one image.
- **slice, scope, service, unit**: systemd's kinds of boxes (section 2).
- **SMT**: two threads on one core ("Hyper-Threading"). This CPU has none.
- **Source 2**: the game engine of Dota 2.
- **Split lock**: a memory operation over two cache lines; normally slowed
  down by the kernel.
- **THP (Transparent Huge Pages)**: 2 MB memory pages managed automatically.
- **Toolbox**: a podman container for development, sharing your home folder.
- **User manager**: the systemd instance that runs your own session
  (`user@<uid>.service`).
- **vCPU / pinning / emulator threads**: virtual CPU of the VM / fixing it to
  certain host CPUs / QEMU's helper threads.
- **Watermark**: a free-memory level at which the kernel starts freeing memory.
- **XML**: a text format with `<tags>`; libvirt stores VM settings in it.
