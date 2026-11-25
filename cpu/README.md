<!-- SPDX-License-Identifier: MPL-2.0 -->
# `cst cpu`: smooth games while builds, Docker and the VM run

This folder holds the parts of `cpu-setup.sh`, the `cpu` module of CST
(Cominotti Setup Tools). The script lives one level up, at the top of
the CST folder: a shim whose header comment is the help, which runs
`cpu/cpu-setup` (Python, the `cpu.setup` package). You run it as `cst cpu <command>` (how to set
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
>   toolboxes, toolboxes on the E-cores only, connected display GPUs kept awake
>   (`GPU_PM_MODE='always'` across monitor hotplug via udev + `cst-cpu-power.service`),
>   deep CPU C-states capped at `200 µs` (`CPU_CSTATE_MAX_LATENCY_US`),
>   `ntsync` autoloaded for Wine/Proton, a GameMode hook, and a watchdog
>   timer. The files are listed in section 6.1.
> - **Only while a game runs** (and cleared by a reboot): the VM and Docker
>   move to CPUs 12-23 with a very low priority, toolboxes and the other
>   boxes of your session that sit next to the game (GNOME's file indexer,
>   `systemd-run --user` jobs) get the lowest priority, the game's memory is
>   protected, desktop apps' swapped pages stay in RAM (zswap) instead of
>   the swap disk, P-cores get EPP `performance` and a `50 µs` C-state cap,
>   and active game GPUs (discovered dynamically via open `/dev/dri/*` fds with
>   connected-display fallback) get `power/control=on` and AMD `3D_FULL_SCREEN`.
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
│           │                       flatpak-session-helper.service,
│           │                       `systemd-run --user` jobs (run-*)
│           ├── session.slice       gnome-shell
│           ├── background.slice    GNOME background tasks (the file
│           │                       indexer, localsearch)
│           ├── user.slice          "inner user.slice": the toolboxes
│           └── X.slice             any slice a job made for itself
│                                   (`systemd-run --user --slice=X.slice`)
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
| `status` | needs sudo | The whole picture (sample in 6.5), including a `== diagnosis` block for CPU/GPU responsiveness ([../perf/README.md](../perf/README.md)). |
| `uninstall` | needs sudo | Removes what `install` added. |
| `game on\|off\|state` | needs sudo | Switch the game profile by hand, or show it (`state` also flags each setting that drifted, 7.5). |
| `vm-pin plan\|apply\|status` | needs sudo | Pin the VM to CPUs 12-23 permanently, and keep CET/MBEC off for a host-passthrough VM (8.1). |
| `docker-slice install\|status\|uninstall` | needs sudo | Put Docker containers in `builds.slice`. |
| `experiment list\|on NAME\|off NAME` | needs sudo | Runtime-only memory settings for A/B tests. |
| `power [status\|plan\|apply\|revert]` | `status`/`plan`: your user; `apply`/`revert`: needs sudo | Inspect or apply baseline connected-GPU runtime PM (`GPU_PM_MODE`) and CPU C-state latency caps (`CPU_CSTATE_MAX_LATENCY_US`). |
| `irq [status\|plan [--no-pkg]\|apply [--no-pkg]\|revert]` | `status`/`plan`: your user; `apply`/`revert`: needs sudo | Configure adaptive `irqbalance` CPU ban-list (`IRQBALANCE_BANNED_CPULIST`) from `BUILD_CPUS` intersected with online CPUs (keeping `>= 2` unbanned CPUs), install `irqbalance` if needed, and keep a drop-in after `/etc/sysconfig/irqbalance`. |
| `watch -l LABEL [...]` | your user | Record game wait, memory stalls, frequencies. |
| `report [RUN...]` | your user | Compare recorded runs. |
| `quiet [--for S] [--wait MAX] [--json] [...]` | your user | Is the machine quiet enough for a benchmark? Exit 0 if yes (11.1). |
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
- `watch`, `report` and `quiet` work on the host and inside a toolbox. Inside
  a toolbox they run on the host through `flatpak-spawn --host`.

---

## 6. What `install` changes on your system

### 6.1 Files

| Path | Purpose |
|------|---------|
| `/etc/cpu-setup/cpu-setup.conf` | Settings (6.2). Written only if missing. |
| `/usr/local/sbin/cpu-game-profile` | The game profile (from `cpu/game-profile`). |
| `/usr/local/sbin/cpu-irq` | Adaptive `irqbalance` ban-list helper (from `cpu/cpu-irq`). |
| `/usr/local/sbin/cpu-power` | Baseline & hotplug GPU runtime PM + CPU C-state latency helper (from `cpu/cpu-power`). |
| `/usr/local/sbin/cpu-vm-pin` | VM pinning and CPU-feature tool (from `cpu/vm-pin`). |
| `/usr/local/bin/cpu-watch` | Measurement tool (from `cpu/cpu-watch`). |
| `~/.local/bin/cst` | Link to the `cst` of the CST folder, so the hints of these tools (`cst cpu ...`) work as typed. Created for the user who ran `sudo`, only if nothing else is there ([README.md, The `cst` command](../README.md#the-cst-command)). |
| `/etc/systemd/system/cpu-game.service` | Runs `cpu-game-profile on` when started, `off` when stopped. |
| `/etc/systemd/system/cpu-game-watchdog.service` + `.timer` | Watchdog, every 30 s (first run 2 min after boot). Enabled and started. |
| `/etc/systemd/system/cst-cpu-power.service` | Applies baseline GPU runtime PM and CPU C-state limits at boot (`cpu-power apply`). Enabled, and restarted by each `install`: it applies them now too, also when the unit already ran (states turned back on since, or a changed `CPU_CSTATE_MAX_LATENCY_US`). When that `cpu-power apply` fails (for example its run lock still held by the game profile after 60 s), `install` stops with systemctl's exit status before it marks `cpu:core`, `cpu:toolbox` and `cpu:gamemode` done, also on a second run; `systemctl status cst-cpu-power.service` shows why. Run `install` again once the cause is gone. |
| `/etc/cpu-setup/irqbalance.env`, `/etc/systemd/system/irqbalance.service.d/10-cst-ban.conf` | Managed by `cst cpu irq apply` (component `cpu:irq`): sets `IRQBALANCE_BANNED_CPULIST` from `BUILD_CPUS` & online CPUs. |
| `/etc/udev/rules.d/65-cst-gpu-pm.rules` | Re-runs `cpu-power apply --gpu-only` on DRM card `add`/`change` (monitor hotplug). |
| `/etc/systemd/system/machine.slice.d/10-cpu-setup.conf` | VM baseline weights `CPUWeight=50`, `IOWeight=50`. |
| `/etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf` | `Delegate=cpuset` for user managers (6.3). |
| `/etc/polkit-1/rules.d/50-cpu-setup-game.rules` | Lets your session start/stop only `cpu-game.service`. |
| `/etc/tmpfiles.d/cpu-setup-hwp-boost.conf` | Turns on intel_pstate's HWP dynamic boost at every boot (6.3). |
| `/etc/modules-load.d/cst-ntsync.conf` | Autoloads `ntsync` at boot (and via `modprobe ntsync` during `install`) when built as a kernel module, exposing `/dev/ntsync` for Wine/Proton. |
| `~/.config/systemd/user/user.slice.d/10-cpu-setup.conf` | Toolboxes: `CPUWeight=30`, `AllowedCPUs=8-23`. |
| `~/.config/systemd/user/flatpak-session-helper.service.d/10-cpu-setup.conf` | Host commands of toolboxes: `AllowedCPUs=8-23`. |
| `~/.config/gamemode.ini` | GameMode config (only if you do not have your own). |
| `/etc/group`, `/etc/gshadow` | `gamemode` group copied in, your user added (6.4). |

A **drop-in** is a small extra file in a `<unit>.d/` folder that changes one
part of a systemd unit, without editing the unit itself.

**Why the baseline drop-ins start with `10-`.** systemd reads all drop-ins
of a unit sorted by file name, whatever folder they are in, and when two set
the same thing, the one read last wins. The game profile's changes are
drop-ins too: `systemctl set-property --runtime` writes them as
`50-CPUWeight.conf`, `50-AllowedCPUs.conf` and so on. Up to 2026-11 the
baseline was named `50-cpu-setup.conf`, which sorts **after** those
(upper-case letters come before lower-case ones), so every `daemon-reload`
put the baseline back over the running game profile. `10-cpu-setup.conf`
sorts first, so the game profile wins. `install` deletes the old
`50-cpu-setup.conf` files (it prints `removed '...'`) and reloads both systemd
instances; `uninstall` deletes both names. The `user@.service` drop-in keeps
its name `50-cpu-setup-cpuset.conf`: it sets `Delegate=`, which the game
profile never changes.

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

Every file in the table but the settings file (which you may edit) and the
group lines is listed in the install record, so `cst doctor` reports one
that is missing or was changed by hand: root's files in
`/var/lib/cst/installed.json` (the hwp file under the component `cpu:core`),
the two user drop-ins in your `~/.local/state/cst/installed.json` (component
`cpu:toolbox`, marked done at the end of every `install`). A file an older
CST installed is adopted by the next `install`, without being written again.

### 6.2 Settings file

`/etc/cpu-setup/cpu-setup.conf`, exactly as `install` writes it on this
machine (a Core Ultra 9 285K: P-cores 0-7, E-cores 8-23, `intel_idle`):

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
VM_CPU_DISABLE='cet-ss cet-ibt vmx-mbec vmx-exit-save-cet vmx-entry-load-cet'
GPU_PM_MODE='always'
CPU_CSTATE_MAX_LATENCY_US='200'
GAME_CSTATE_MAX_LATENCY_US='50'
```

`install` writes the file only when it is missing. It reads three values
from the machine's sysfs (`BUILD_CPUS` and `P_CORES` from the CPU topology,
`CPU_CSTATE_MAX_LATENCY_US` from the cpuidle driver) and derives `VM_VCPUS`
from `BUILD_CPUS`:

- `BUILD_CPUS` and `P_CORES`: on a hybrid Intel CPU (`/sys/devices/cpu_atom/cpus`),
  `P_CORES` is `/sys/devices/cpu_core/cpus` (or the online CPUs that are not
  E-cores) and `BUILD_CPUS` the E-cores, without the first four when there are
  12 or more (the 285K: `12-23`). On any other CPU (AMD, older Intel), the
  physical cores in order, each with its SMT sibling
  (`topology/thread_siblings_list`): the first half is `P_CORES`, the second
  `BUILD_CPUS` (on a two-CCD Ryzen, one CCD each); an online CPU whose
  siblings cannot be read is a core of its own. With one core, its logical
  CPUs split the same way; with one CPU, both are that CPU. With no readable
  online list (`/sys/devices/system/cpu/online`), the values above.
- `VM_VCPUS`: as many as `BUILD_CPUS` has CPUs, at most 12 (`vm-pin` pins
  that many vCPUs onto `BUILD_CPUS`). It reads no sysfs file of its own and
  is set once, when `install` writes the file: it does not follow a later
  change of the host or of `BUILD_CPUS`.
- `CPU_CSTATE_MAX_LATENCY_US`: `200` with the `intel_idle` cpuidle driver (or
  when it cannot be read), `off` with any other (AMD's `acpi_idle`, whose
  states the 200 µs was not chosen for). `cpu-power` applies the same rule
  when the file has no such line; a value the file sets always counts. A file
  an older CST wrote always has `CPU_CSTATE_MAX_LATENCY_US='200'`, and
  `install` keeps an existing file, so an AMD host installed before keeps the
  200 µs cap after an upgrade: set the line to `'off'` (or remove it), then
  run `cst cpu power apply` (or `cst cpu install`, or reboot), which turns
  the states it disabled back on. After any change of the line, `cst cpu
  install` applies the new value at once (it restarts
  `cst-cpu-power.service`).

(Files written before `VM_CPU_DISABLE` existed lack that line: `vm-pin` then
uses the same five names as its default.) A byte that is not UTF-8 in this
file, or in a mapped file's name that `cpu-watch` reads in `/proc/PID/maps`,
is replaced and no longer stops `vm-pin` or `cpu-watch`.

The game profile, `vm-pin` and `cst doctor` read this file as bash reads
it when it sources it (cst_core's settings reader) (`BUILD_CPUS='12-23' # E-cores` is `12-23` for all of
them). Keep to plain `KEY=value` lines: quote a value with blanks in single
quotes. Blanks around a word or a CPU list are spaces and tabs; a no-break
space or another Unicode blank at the start or end of a value
(`BUILD_CPUS='12-23<no-break space>'`) makes the line a problem line that
keeps the value bash reads (never trimmed): `vm-pin apply` and `cst cpu irq
apply` refuse, the game profile does not switch on, `plan`, `status` and
`cst cpu power` warn with the line and the reason, and `cst doctor` warns. A
blank inside a value is kept. A line bash would read differently, or that it would run as a
command (`BUILD_CPUS = 12-23`, a `$` or a backtick outside single quotes),
sets nothing for `vm-pin`: `plan` and `status` print it with its line
number, `apply` refuses to start, and `cst doctor` warns. The game profile
then does not switch on (`cpu-game.service` fails when GameMode or `cst cpu
game on` starts it, its journal names the line; the by-hand mark `cst cpu game
on` made is dropped, so a profile a later game switches on is switched off
when that game ends). The watchdog does not start
it while a game runs, and with the profile already on its `tick` changes
nothing (it still switches the profile off when the game ends); it names
the line in the journal once per boot (again when the lines change, when it
goes from "not switched on" to "changes nothing", and after a tick found the
file fixed; its mark is `/run/cpu-setup/settings-warned`, a file only root
can write there, replaced and never followed), and its unit does not fail. `off`
still undoes everything (for the user `on` acted for, the uid of its
`user@UID.service` record in `game.state`, also when `TARGET_USER` sets
nothing or names another user now; the watchdog's `tick` acts for that
user too, and never prints `id`'s `no such user` line, neither for a
`TARGET_USER` that names no account nor when the account of the recorded
uid is gone; `off` prints it once), and `cst cpu game state` shows the line.

A `TARGET_USER` that is not set, or that names no account, does not switch
the profile on either: `cpu-game.service` fails, its journal says
`TARGET_USER not set in /etc/cpu-setup/cpu-setup.conf` or `TARGET_USER names
no account: NAME (in /etc/cpu-setup/cpu-setup.conf)`, nothing is changed, and
the by-hand mark is dropped, as above. (Before 2026-11, a name with no account
switched the profile on for no user: it set `user@.service` and
`user-.slice`, which are nobody's.) The watchdog does not start the profile
for such a `TARGET_USER` while a game runs either (it would fail again every
30 s): it says why in the journal once per boot (`GameMode client running,
profile not switched on: ...`; again after a tick found the name fixed; the
same mark as for a settings line, `/run/cpu-setup/settings-warned`, whose
word on a line is still forgotten once the line is fixed), and its unit does
not fail. `cst cpu game state` names the reason too (`settings: TARGET_USER
names no account: NAME (in ...) ('on' refuses until it is fixed)`), and
prints no `toolboxes` lines then: there is no user manager to ask. It looks the name up only when it needs the answer: when it would
switch the profile on, while that mark is there, and with the profile on; not
at every run (on a machine whose accounts come from sssd or LDAP, each lookup
can be slow).

| Key | Meaning | Read by |
|-----|---------|---------|
| `TARGET_USER` | Your user. Needed for everything in your session. | `cst cpu` (`cpu/setup`), game profile |
| `BUILD_CPUS` | Where the VM and Docker go during games; the VM always, after `vm-pin`. | game profile, `vm-pin` |
| `P_CORES` | Cores that get EPP `performance` during games. A range whose end is no number (or has more digits than Python reads, 4300) gives no CPU, as `seq` did. | game profile |
| `VM_WEIGHT` | VM CPU (and I/O, when numeric `1..10000`) weight during games. A value of more digits than Python reads is no number: `IOWeight=10`. | game profile |
| `GAME_MEMORY_LOW` | Memory protection for the game (Dota 2 uses about 10 GB, plus Steam). | game profile |
| `GAME_ZSWAP_WRITEBACK` | `no`: during games, swapped pages of desktop apps stay in zswap, never on the swap disk (7.1). `yes`: leave it alone. Missing (files written before this key existed) means `no`. | game profile |
| `VM_NAME` | The libvirt VM name. | `vm-pin` |
| `VM_VCPUS` | Number of virtual CPUs `vm-pin` gives the VM. | `vm-pin` |
| `VM_CPU_DISABLE` | CPU features `vm-pin` keeps switched off for a VM with `<cpu mode='host-passthrough'>` (section 8.1). Empty (`VM_CPU_DISABLE=''`): `vm-pin` leaves the CPU features alone. Missing: the default list. | `vm-pin`, `cst doctor`, `cst vm status` |
| `GPU_PM_MODE` | `always` (default): force `power/control=on` on every GPU with connected monitors and track hotplug via udev; `game`: only boost active game GPUs during the game profile; `off`: never modify GPU runtime PM. | `cst cpu power`, `cst-cpu-power.service`, game profile |
| `CPU_CSTATE_MAX_LATENCY_US` | Baseline `cpuidle` exit-latency ceiling in microseconds (`200` default disables states with `latency >= 200` such as `C3_ACPI`; `off`/`0` leaves all C-states enabled; without the line, `off` unless the cpuidle driver is `intel_idle`, 6.2). | `cst cpu power`, `cst-cpu-power.service` |
| `GAME_CSTATE_MAX_LATENCY_US` | Per-game `cpuidle` ceiling on `P_CORES` while the game profile runs (`50` default disables `C2_ACPI` `127 µs` on P-cores while preserving baseline-disabled states across `off`; `off`/`0` disables). | game profile |

You may edit the values. Rules:

- **No comment on the same line as `TARGET_USER`.** `cst cpu` finds your
  user only on a line that holds nothing but `TARGET_USER='...'`. The game
  profile, `vm-pin` and `cst doctor` accept a comment after a value (see
  above), but a comment on its own line, starting with `#`, is safest.
- Keep the quotes as shown.

`install` never overwrites this file, and `uninstall` keeps it.

### 6.3 Baseline: always active, even without a game

| Box | Setting | Why |
|-----|---------|-----|
| `machine.slice` (VM) | `CPUWeight=50`, `IOWeight=50` | The VM never ties with the desktop for CPU (default 100). `IOWeight` prepares the cgroup for I/O arbitration, though it has no effect on this host yet (see below). |
| toolboxes (inner `user.slice`) | `CPUWeight=30`, `AllowedCPUs=8-23` | GNOME gives its own background tasks (`background.slice`) weight 30 too, so toolboxes get the same low priority as other background work. Builds stay off the P-cores. |
| `flatpak-session-helper.service` | `AllowedCPUs=8-23` | Commands that toolboxes start on the host with `flatpak-spawn --host` run here, outside the toolbox's box. Without this, they would escape to the P-cores. |
| `builds.slice` (only after `docker-slice install`) | `CPUWeight=20`, `IOWeight=20`, `AllowedCPUs=8-23` | Docker builds and tests stay low and on the E-cores (`IOWeight=20` is likewise dormant without `bfq` or `iocost`). |
| CPU frequency (intel_pstate) | `hwp_dynamic_boost=1` | When a task wakes up after waiting for the disk (a build doing many fsyncs, a game loading files), the CPU raises its frequency at once instead of ramping up. Costs a little power during those moments. `install` sets it now and a tmpfiles.d file keeps it after reboots; `uninstall` removes the file and sets it back to `0`. Skipped (with a NOTE) on CPUs without intel_pstate in HWP mode. |
| Connected GPUs (`cst cpu power`, udev `65-cst-gpu-pm.rules`) | `power/control=on` (when `GPU_PM_MODE='always'`) | Prevents PCIe runtime PM sleep/wakeup stalls on any GPU currently driving a connected monitor, while leaving unplugged GPUs (`auto`) free to sleep and handling monitor hotplug automatically via PCI slot tracking in `/run/cpu-setup/gpu-pm-managed`. |
| Deep CPU C-states (`cst-cpu-power.service`) | `cpuidle/state*/disable=1` for `latency >= 200 µs` | Keeps `C3_ACPI` (`1048 µs` wakeup latency) off while keeping shallow `C1`/`C2` states available; `cpu-power` records modified states in `/run/cpu-setup/cstate-disabled` so `revert` / `uninstall` only re-enables states CST disabled. |

**Note on `IOWeight` (`machine.slice` and `builds.slice`):** cgroup I/O weights
(`io.weight`) are only enforced when the block device uses the **BFQ** I/O
scheduler or when the kernel's **`blk-iocost`** controller is enabled. To
enable `blk-iocost` (`enable=1 ctrl=auto`) on SSDs without an upstream `hwdb`
entry so `IOWeight` arbitrates NVMe traffic, run `cst btrfs iocost apply`
([../btrfs/README.md](../btrfs/README.md#92-iocost-enable-kernel-blk-iocost-qos-for-slice-ioweight)).
When keeping **`irqbalance`** active, run `cst cpu irq apply` to ban `BUILD_CPUS`
(intersected with online CPUs, keeping `>= 2` unbanned IRQ handler CPUs) via
`/etc/cpu-setup/irqbalance.env` and `irqbalance.service.d/10-cst-ban.conf`.
`apply` refuses (exit 1, nothing written, removed or restarted) when
`BUILD_CPUS` is empty or not a CPU list (a CPU index is at most 8191, as
systemd's CPU sets: `12-8192` is no CPU list), or `cpu-setup.conf` has a syntax
error; `plan` shows the same error. Otherwise it goes in this order and
stops at the first failure: it installs the `irqbalance` package when
`irqbalance.service` is missing (`rpm-ostree install --apply-live`; skipped
with `--no-pkg`; a failed install writes nothing), writes the two files
through CST's installer (install record component `cpu:irq`), reloads
systemd for a new drop-in, enables `irqbalance.service` and restarts it when
it was stopped or the files changed. `cpu:irq` is recorded done only then.
It stays pending after a failed restart, and when there is no
`irqbalance.service` yet (`--no-pkg`, or a package rpm-ostree applies only
at the next boot): `apply` then says
`pending:   cpu:irq (irqbalance.service is not there yet: run apply again once it is)`.

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
If the system knows the group (through NSS, for example from a directory
service) but neither `/etc/group` nor `/usr/lib/group` has its line, install
stops with
`the 'gamemode' group comes from NSS, not /etc/group or /usr/lib/group: add its line to /etc/group by hand, then run install again`.

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
    machine.slice   cpus=         weight=50   io=50
    builds.slice    cpus=8-23     weight=20   io=20
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
  machine.slice   cpus=12-23    weight=10   io=10
  builds.slice    cpus=12-23    weight=idle io=20
  toolboxes       weight=idle
  app.slice       zswap writeback=no
  toolboxes       cpus effective=12-23
  other boxes     idle: background.slice lushtext.slice run-u7.service
  EPP cpu0 performance, cpu12 balance_performance
  drift: none (every setting the profile made still holds)
```

`(by hand)` appears only after `game on`. `other boxes` lists the boxes of
your user manager next to the game that the profile made idle (7.1). The last line compares what the
profile set with what systemd reports now (section 7.5); a setting that
changed shows as a `DRIFT:` line instead.

---

## 7. The automatic game profile

### 7.1 What it does while a game runs

`cpu-game-profile on` makes these changes. All are **runtime-only**: the
systemd changes are **runtime overrides** (drop-ins under `/run`, written by
`systemctl set-property --runtime`), and EPP is a value in sysfs (the kernel's
`/sys` folder). A reboot clears both.

A runtime override is not guaranteed to hold until the game ends, though:
every `systemctl daemon-reload` (an install, `systemctl enable`, a package
update) makes systemd read all drop-ins again, and another drop-in can win
over the override. The watchdog checks for that every 30 s and sets the
value again (section 7.5).

The first column says which finding of section 3 the change fixes. The values
are the defaults from `/etc/cpu-setup/cpu-setup.conf` (`BUILD_CPUS`,
`VM_WEIGHT`, `GAME_MEMORY_LOW`, `GAME_ZSWAP_WRITEBACK`, `P_CORES`).

| Fixes finding | Change | Value |
|---|--------|-------|
| 1, 2, 3 | VM: `machine.slice` | `AllowedCPUs=12-23`, `CPUWeight=10`, `IOWeight=10` (uses `VM_WEIGHT` when `1..10000`, otherwise `10`, because systemd's `IOWeight` does not accept `idle`; `IOWeight` only takes effect if `bfq` or `iocost` is enabled, see 6.3) |
| 1, 2, 3 | Docker, if `builds.slice` is active | `AllowedCPUs=12-23`, `CPUWeight=idle` |
| 1, 2 | Docker containers **outside** `builds.slice` (`docker-*.scope`) | `AllowedCPUs=12-23` (no weight change) |
| 3 | Toolboxes (inner `user.slice`) | `CPUWeight=idle` |
| 3 | The other boxes of your user manager next to the game (see below): `background.slice`, any other slice of its own at the top of the user manager, and `systemd-run --user` jobs straight in `app.slice` (`run-*.scope`, `run-*.service`); a box started after the profile gets it at the next watchdog run, within 30 s | `CPUWeight=idle`; with cpuset delegated (6.3) also `AllowedCPUs=12-23` |
| 2 | Only if cpuset is delegated (6.3): toolboxes and `flatpak-session-helper.service` | `AllowedCPUs=12-23`; if your user manager is older than the delegation (no reboot since `install`), it ignores this, and the profile writes `12-23` straight into their cgroup file `cpuset.cpus` |
| 4 | Memory chain: system `user.slice`, `user-<uid>.slice`, `user@<uid>.service`, then `app.slice` in your user manager, and **every** Steam scope `app-flatpak-com.valvesoftware.Steam-*` (the client has one, and each game gets its own; a game started after the profile gets it at the next watchdog run, within 30 s) | `MemoryLow=16G`; for the first three also a drop-in, `/run/systemd/system/<unit>.d/zz-cst-game.conf`, that wins at every `daemon-reload` (7.5) |
| 4 | Desktop apps and the game: `app.slice` in your user manager (only if `GAME_ZSWAP_WRITEBACK=no`, and only if systemd knows `MemoryZSwapWriteback`) | `MemoryZSwapWriteback=no` |
| - | P-cores (`P_CORES`, 0-7) | EPP `performance`, and `cpuidle` states with `latency >= GAME_CSTATE_MAX_LATENCY_US` (`50 µs` -> disables `C2_ACPI` on P-cores only) |
| - | Active game GPUs (`/proc/<pid>/fd/*` DRM `renderD*`/`card*` discovery, falling back to connected display cards; the Steam client's own processes (`steam`, `steamwebhelper`, the shader pre-compiler `fossilize_replay` and the driver probes `vulkandriverquery` and `gldriverquery`) do not count, and their GPUs are used only when no card is connected) | `power/control=on` (when `GPU_PM_MODE != off`), and on AMD GPUs `power_dpm_force_performance_level=manual` + `pp_power_profile_mode` `3D_FULL_SCREEN` (discovered dynamically by profile name; left alone when `pp_power_profile_mode` marks no current profile with `*`); watchdog `tick` also picks up GPUs opened after launch |

In short: the game gets the P-cores and E-cores 8-11, the VM and Docker move
to 12-23 with a low priority, toolboxes and the other boxes next to the game
get the idle class, and the kernel avoids taking the game's memory.

**The other boxes next to the game.** Your user manager holds more than
`app.slice` (the game, desktop apps) and the toolboxes. A fixed list of boxes
would miss the rest, and what it misses runs at the normal weight (100) on
all 24 CPUs, right next to the game. An example from this machine: a project
ran its jobs with `systemd-run --user --slice=lushtext.slice`. That slice sat
at the top of the user manager, beside `app.slice`, and used 69,000
CPU-seconds during games. So `on` looks at the user manager's cgroup folder
and makes idle:

- every top-level slice except `app.slice`, `session.slice` (gnome-shell:
  the desktop must stay responsive) and `user.slice` (the toolboxes, handled
  above). That includes `background.slice`: on this machine it holds only
  GNOME's file indexer (`localsearch-3.service`, often 46 % of a CPU), which
  serves neither the game nor the desktop's responsiveness;
- `systemd-run --user` jobs started without `--slice`: they land straight in
  `app.slice` as `run-*.scope` or `run-*.service`, next to the game.

The watchdog picks up such boxes that appear later. A slice keeps the
setting even if it goes away (stopped, or unloaded once nothing uses it) and
a later job makes it again: the runtime override stays in
`/run/user/<uid>/systemd/user.control/<slice>.d/` until `off`. A box that
holds a GameMode client (the game itself, started in such a box) is left
alone. That check runs when the box is first picked up, and again when the
watchdog sets a box again (7.2): a game started later in a box that is
already idle (its settings holding) stays idle until `off`, and so does a
game that does not use GameMode (with `game on` by hand). Start such a game
from the desktop or Steam, or with `--slice=app-NAME.slice`. Apps that GNOME
or Flatpak start (`app-*.scope`) and `flatpak-session-helper.service` are
not touched. Nor is a folder whose name is no unit name (a blank, a byte that
is not UTF-8, a character systemd never puts in a unit name): systemd never
makes one, but a process of yours can `mkdir` one in your user manager's
cgroup tree, and `set-property` could never act on it. (Versions before
2026-11 wrote a record for it and ran a `set-property` that fails; for a
name that is not UTF-8, one more of each at every watchdog run.)

To keep a job out of this, give it a slice under one of the boxes CST
already handles. `systemd-run --user --slice=user-NAME.slice` nests under the
toolboxes' `user.slice` (weight 30 and the E-cores always, idle during
games, as the toolboxes). `--slice=app-NAME.slice` nests under `app.slice`,
next to the game, and is left alone. `cst doctor` warns about top-level
slices it does not know ([../doctor/README.md](../doctor/README.md)).

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

**systemd-oomd watches `app.slice` too.** Fedora's `systemd-oomd` kills a
whole cgroup when memory pressure stays high, before the kernel's OOM killer
would act. On this PC `oomctl` lists `app.slice` of your user manager under
"Memory Pressure Monitored CGroups", with a limit of 80% for 20 s (Fedora's
defaults, not set by CST). If a heavy game plus desktop apps keep the memory
pressure of `app.slice` above 80% for 20 s, oomd kills one cgroup under it,
and that can be the game. Writeback off makes this more likely: pages that
compress badly stay in RAM, and the kernel works harder to reclaim memory
there (the zswap documentation calls this "reclaim inefficiency"). This has
not been seen here; it is a risk to watch for, not a measured problem.

- Check the limits: `oomctl` (look for `app.slice`).
- Check whether oomd killed something: `journalctl -u systemd-oomd -g Killed`.
- If it ever kills a game, set `GAME_ZSWAP_WRITEBACK='yes'` first. CST does
  not set `ManagedOOMPreference=avoid` on the game's scope: that waits for a
  measurement that shows it is needed.

Note: `machine.slice` (the VM) also has `MemoryLow=48G`, set by the `swap`
module (`cst swap apply`, see [../swap/README.md](../swap/README.md)), not by
this script. So the game's 16G protection competes with a large VM protection.
`cst cpu status` shows neither value; `cst swap status` shows both, in the
`low` column of its per-cgroup table.

The profile writes this message to the journal (the system log):
`on: VM/Docker on 12-23, toolboxes and other user boxes idle, game memory protected (16G), app zswap writeback no, P-cores EPP performance`

and, before it, one line per other box it made idle, for example
`lushtext.slice: idle while the game runs (a box of the user manager next to the game)`.

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
  is a fallback if the GameMode hook failed. It does not when `on` would
  refuse (6.2: a settings line that sets nothing, a `TARGET_USER` that is
  unset or names no account); it says why once per boot.
- Profile on: Docker containers started in the meantime get `AllowedCPUs=12-23`,
  Steam scopes started in the meantime (the game's own) get `MemoryLow`, and
  other boxes next to the game that appeared in the meantime get the idle
  class (7.1). A box it already handled gets no new record, so `game.state`
  does not grow at each run. Handled means that `game.state` holds a record
  of that box and setting which `off` can read, compared field by field
  (manager, unit, property): a line `off` cannot read (an unclosed quote)
  does not count, and a unit name that `printf %q` writes quoted (systemd's
  `\x2d`) still does. Each run reads the recorded settings of each handled
  box back from systemd (one `systemctl show` per box) and sets again, with
  `set-property` and no new record, each one whose value is not the
  profile's: its `set-property` failed, or an `off` that was killed undid it
  while the profile stayed on. The first record still says how `off` undoes
  it. The journal says it at level notice (`UNIT: PROP is 'NOW', not 'WANT'
  (its set failed, or something undid it): set PROP=VALUE again`), or at
  level warning when that `set-property` fails too (tried again at the next
  run). The value is the one the settings give now, so a `BUILD_CPUS` edited
  while the profile is on reaches these boxes at the next run (the fixed
  units keep what `on` set). When every value holds, the run changes
  nothing and writes nothing. Left alone: a setting `game.intended` covers
  (the drift repair sets it, section 7.5), every setting of a slice when
  `game.intended` is of a newer format, a unit systemd has not loaded, a box
  that holds a GameMode client by then, and a value the profile cannot
  compare (a `GAME_MEMORY_LOW` given as a percentage). A setting of the
  profile that something undid in the meantime is set again (section 7.5). When the user the profile acts for has
  no account (6.2: `TARGET_USER` names none, or the account was deleted
  while the profile was on), the Steam scopes and the boxes of its user
  manager are left alone (there is no such user manager); the journal says
  why once per boot (`profile on, its user manager's side not handled:
  ...`), and `off` still undoes everything.
- Profile off: if the profile's `zz-cst-game.conf` drop-ins (7.5) are still
  there, it removes them and reloads systemd (logged at level warning).

`on`, `off` and `tick` never run at the same time: they share a lock file,
`/run/cpu-setup/lock`, and wait up to 60 s for it. `cpu-power apply` and
`revert` (the boot unit, the udev hotplug rule, `cst cpu power`) take the same
lock, also for up to 60 s (then exit 1, nothing changed), so they never write
GPU runtime PM or C-state files while the profile switches. `state` takes no lock, so
it never waits: it reads `game.state` and `game.intended` once, and every line
it prints (on or off, the user, the idle boxes, unreadable lines, drift) judges
that one reading, also when `off` or a tick changes the files meanwhile.
In that reading, a missing `game.state` means off; a `game.state` that is
there but cannot be read still means on. Only "no such file" (or a path
through a file) counts as missing: any other error opening it, such as no
permission to search a parent folder (EACCES), a loop of links (ELOOP) or a
name too long (ENAMETOOLONG), reads as unreadable, and `state` shows the
profile ON. This matters only for a user who cannot search
`/run/cpu-setup`; root, and a user who can search it, see a missing file as
off.

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
so if `on` stops halfway, `off` can still undo what was done. `on` sets the
memory chain (7.1) first, `user@<uid>.service` before its parents, so the
first line of `game.state` names the user it acts for, whatever step `on`
stopped at (`off` acts for that user, 6.2, and so do the watchdog's `tick`
and `state`, also when `TARGET_USER` names another user now). The user
manager's records of `game.state` are undone only in the manager of that
uid, never in another user's (`TARGET_USER`'s):

- that uid's user manager runs (`/run/user/UID/systemd` is there) and an
  account has the uid: undone there;
- that manager is not running (a logout took its runtime folder, and its
  runtime overrides with it): nothing is left to undo there, so `off` calls
  no `systemctl --user` for it and drops its records, in one line
  (`<4>off: the user manager of uid UID, the user 'on' acted for, is not
  running: N setting(s) of it went with it, dropped`). Versions before
  2026-11 ran `set-property` and `daemon-reload` into the stopped manager;
- no account has the uid any more but its manager still runs (the account
  was removed during the game): the settings are still live there and no
  name reaches that manager, so `off` says so (`<5>off: no account has uid
  UID, the user 'on' acted for, now, and its user manager still runs: its N
  setting(s) are not undone in another user's manager`) and keeps the
  records in `game.leftovers` under that uid (below), done once that
  manager is gone. Versions before 2026-11 dropped them as if undone.

With no such record (an `on` that died before its first record), the user
manager's side is `TARGET_USER`'s, as before. While the profile is on and no
account has the recorded uid, the watchdog's `tick` leaves the user
manager's side alone (no Steam scope, no box next to the game, no drift of
the user manager set in `TARGET_USER`'s manager, which is another user's)
and says why once per boot (`<4>profile on, its user manager's side not
handled: no account has uid UID, the user 'on' acted for (fix it; said once
per boot)`); `cst cpu game state` compares no drift of it either, and
says so (`drift: the user manager's settings not checked (no account has
uid UID, the user 'on' acted for)`, after `drift: none in the system
manager's settings and the files` when nothing else drifted), and so does
`cst doctor` (`drift not checked for UNIT (...)`). It still
pins Docker, the GPU and the system manager's settings. When the line
cannot be written (`/run` full), `on` leaves that change out and logs
`<unit>: <settings>: not done, its record could not be written to
/run/cpu-setup/game.state`; it carries on with the rest and still exits 0,
so `cpu-game.service` stays active and its `off` (the unit's ExecStop) runs
when the game ends. (A failed oneshot unit never runs its ExecStop.)

`off` reads the file from the newest line to the oldest:

- property that had **no** runtime override before: the override file is
  deleted (`/run/systemd/system.control/<unit>.d/50-<Property>.conf`, or the
  same under `/run/user/<uid>/systemd/user.control/`), then systemd re-reads
  its files (`daemon-reload`). The unit goes back to its defaults or its
  `/etc` drop-ins. No leftover override stays behind to hide a drop-in later.
  For a **transient** unit (a scope such as a Docker container or a Steam
  game, or the `run-*.service` of `systemd-run`) systemd keeps the override
  next to the unit's own file instead, in its transient folder
  (`/run/systemd/transient/<unit>.d/`, or `/run/user/<uid>/systemd/transient/`):
  that is the file `off` deletes for those. (Versions before 2026-11 looked
  in the control folder for them too, found nothing to delete, and left such
  a container on 12-23, or a Steam scope protected, until it ended.);
- property that **had** an override: its old value is set back;
- EPP: each P-core gets its old value back;
- sysfs GPU/C-state records (`sys FILE OLD`): written back in reverse order (so AMD `pp_power_profile_mode` is restored while `power_dpm_force_performance_level` is still `manual`, and baseline-disabled C-states like `C3_ACPI` remain untouched while P-core `C2_ACPI` is re-enabled);
- a `cpuset.cpus` file written directly (7.1, toolboxes): its old value is
  written back (usually empty, which means "no limit of its own").

A box that is gone by then (a `systemd-run` job that ended) is no problem:
its override file is deleted if it is still there, and nothing else is done.
A line of `game.state` that holds no record (cut short when `on` died while
writing it, an unclosed quote) is named in the journal (`<4>... line N: no
record 'off' can read, skipped: TEXT`) and skipped; `off` undoes every other
record. `cst cpu game state` names such lines of `game.state` and
`game.intended` (`unreadable: PATH line N (skipped)`); the drift check
compares the records around them. (The bash version stopped `off` at such a
line, with exit 1 and `game.state` left in place.) The last line of the file
with no newline at its end is such a line, also when its fields look whole:
`on` writes each record before the change it describes, so that change was
never made, and its value may be cut short (`1` of `100`), which `off` must
not set. A record written after such a line (a box the watchdog handles
later) first removes it, so the two never glue into one line `off` would
read as whole; the journal names it (`<4>... line N: a record cut short
(its change was never made), removed before the next record: TEXT`). The
same holds for `game.intended`, whose cut line the drift repair never read.
A cut line is no record of a box handled either: a box whose own record was
cut short (a Docker container, a Steam scope, a box next to the game) is set
by the next tick, whose record of it removes the cut line first. A box next
to the game counts as handled only when each of its settings has a whole
record: with cpuset delegated it gets two, `CPUWeight` and `AllowedCPUs`,
which are recorded one after the other before either is set, so a box whose
`CPUWeight` record is whole but whose `AllowedCPUs` record was cut short (or
is missing) is set again, both settings.
The `zz-cst-game.conf` drop-ins (7.5) are removed **first, and always**, even
when `game.state` is missing: a drop-in left behind would keep the game's
memory protection until the next reboot. Then `off` reloads systemd.

Then it logs `off: previous CPU, weight, memory, zswap writeback and EPP settings restored`
and deletes `game.state` and `game.intended` (section 7.5).

**An undo that fails is kept and retried.** When one step of `off` fails (a
`set-property` that systemd refuses, a sysfs or EPP file that cannot be
written, a runtime override that cannot be removed), `off` still undoes
every other record and removes `game.state`, so the profile is off. An
override that is still there gets no `daemon-reload` of its own, neither
from `off` nor from the ticks that retry it. It writes its record
to `/run/cpu-setup/game.leftovers`, then names the failed step in the journal
(`<4>off: could not undo machine.slice CPUWeight=70: kept in
/run/cpu-setup/game.leftovers for the next off and tick`), and ends with `..., except N setting(s)
kept in /run/cpu-setup/game.leftovers`. The next `off` and every watchdog
`tick` while the profile is off undo the kept records again and keep only
those that fail again; the file is removed when none is left. An `off` with
no `game.state` and the tick log `<5>undid ..., kept in
/run/cpu-setup/game.leftovers by an earlier off` for each one done now: one
that works now, and also one whose unit or folder is gone (below), which had
nothing left to set. An `off` with a `game.state` undoes the kept records
with its own and names only those that fail again (a kept record that is
also one of its `game.state`, for the same user, is undone once: an `off`
killed before it removed `game.state` left both; another user's entry of
the same text, such as `prop usr user.slice CPUWeight new -`, stays its own
entry, which versions before 2026-11 dropped as a duplicate). A user manager's record keeps the user it was for (every
entry of the file is under a user line: `user UID`, or `user -` for none), so it is undone for that user whatever
`TARGET_USER` says later, and never in another user's manager: when no
account has that uid any more, the record stays kept while that uid's user
manager is there (`/run/user/UID/systemd`) and is done once it is gone.
Each manager an undo removed an override in gets its own `daemon-reload`:
an `off` or a tick that undoes records of several users (its own and an
earlier `off`'s for another user) reloads each of those users' managers.

**A `daemon-reload` that fails, or never runs, is kept too.** Until systemd
reads its files again, it still shows the game's value of an override or
drop-in that was removed. So before `off` (and a tick or an `off` that
retries kept records) removes a drop-in or a runtime override, it writes to
`game.leftovers` a reload due for each manager it may remove a file in
(`reload sys`, or `reload usr` under that user's line; quietly, and only
when the file can be read): an `off` killed after a removal leaves the
reload for the next `off` and tick. After the removals, each manager that
lost a file is reloaded, and so is each one a reload due of an earlier run
names; a manager marked in which nothing was removed gets no reload, and its
entry goes. A reload whose exit status says it failed is kept
(`<4>off: the daemon-reload of the system manager failed: kept in
/run/cpu-setup/game.leftovers for the next off and tick`, or `... of uid
UID's user manager ...`), and the next `off` and tick run it again; its
entry goes once the reload works (`<5>ran the daemon-reload of ..., kept in
/run/cpu-setup/game.leftovers by an earlier off`). A user manager that is
not running needs none: its entry goes with no call. When the file cannot
be written, the journal says so (`<3>off: could not keep the daemon-reload
of ... in /run/cpu-setup/game.leftovers: not done`). (Versions before
2026-11 never read a reload's exit status and kept nothing for it: the
game's values stayed until a reboot or someone else's `daemon-reload`.)
A kept new override of the user manager with no `user` line (kept while
`TARGET_USER` named no account) is undone in the manager of the user the
pass acts for when it starts: in `off`, the user game.state records when it
records one, else `TARGET_USER`'s; in the tick, `TARGET_USER`'s. It is
dropped only while that user has no account: no user's runtime folder holds
it and no user manager can be reloaded, so nothing is said undone (`<4>... could not undo ...: TARGET_USER names no
account, no user manager to undo it in: dropped`). An entry of no user is
judged so whatever entry of another user's the same pass undid before it:
it is never undone in that user's manager (versions before 2026-11 went on
acting for the user of the entry undone last).
What the tick then decides (switching the profile on for a running game) is
still judged for `TARGET_USER` as the settings file has it, so a kept
record never makes the tick start a `cpu-game.service` whose `on`
refuses. A record whose unit is gone is done, not kept: a
transient unit that is no longer loaded, or a user manager whose runtime
folder is gone (a logout), took its runtime override with it; so is a
cgroup, sysfs or EPP file whose folder is gone (a cgroup that ended, a CPU
taken offline), so a folder made again later (the next login's cgroup) never
gets the old session's value. A fixed unit's override outlives the unit, so
it stays kept. `cst cpu game state` lists the
kept settings (`leftovers: N setting(s) 'off' could not undo, ...`), and
`cst doctor` warns about them, by name
([doctor/README.md, section 8.4](../doctor/README.md#84-settings-off-could-not-undo)). When
`game.leftovers` cannot be written (`/run` full), the old file is left as
it is while none of its records was undone since (a tick that undid
nothing, an `off` whose own records failed too): its records stay kept and
are retried once `/run` has room. Otherwise it is removed, so a record
undone now is neither undone nor said again at every tick. The journal says
each failed setting the file does not hold is not undone, once, instead of
kept (`<3>off: could not keep ... in /run/cpu-setup/game.leftovers: not
undone`, without `off:` from the tick); `off` then ends with `..., except N
setting(s) not undone` (`..., except N setting(s): K kept in
/run/cpu-setup/game.leftovers, M not undone` when the old file still holds
some of them). The file is format 2 (`cst-game-leftovers 2` on its first line); format 1
files, which have a `user UID` line only where the user changes (so an entry
of no user written after one of a uid reads as that uid's), are still read,
and every write is format 2. A CST that reads only format 1 leaves a format
2 file alone. A file in a newer format is left alone, neither undone nor removed
(a failed setting is said not undone in the same words), and so is one that
cannot be read (a folder in its place, a read error: `<4>... cannot be read
(...): left alone, neither undone nor removed`), which is never taken for a
missing one. The file is read byte for byte, as it is written back.
(Versions before 2026-11 removed
`game.state` with the failed record in it, and the setting kept the game's
value until a reboot.)

### 7.5 When something undoes a setting (drift)

"Drift" means: the profile is on, but a setting it made is no longer in
effect. Two things cause it:

- **`daemon-reload`.** systemd reads all drop-ins again, sorted by file name,
  and the last one wins (6.1). With CST's own baseline named `10-` this no
  longer undoes the CPU and IO settings, but another drop-in that sorts after
  `50-<Property>.conf` still could.
- **uresourced** (a Fedora service that gives the active user's session some
  memory protection). It keeps its own `50-MemoryLow.conf` files for
  `user.slice`, `user-<uid>.slice` and `user@<uid>.service` (on this machine
  `MemoryLow=0`), in `/etc/systemd/system.control`. That
  folder comes before `/run/systemd/system.control` (where the profile's
  overrides are), and a file there with the same name hides the profile's
  file. So after every `daemon-reload` those three would get `MemoryLow=0`
  back. uresourced also changes these values by itself at times (for example
  at login, or when the active session changes). Since `memory.low` only
  protects as far as every parent box also protects, one of them at `0`
  voids the game's 16 GiB protection.

**The drop-in that always wins.** systemd applies drop-ins with *different*
names all together, sorted by file name whatever their folder, and the last
one wins (`systemd.unit(5)`). Only a file with the *same* name in a folder
that comes first hides another. So `on` also writes, for those three units,
`/run/systemd/system/<unit>.d/zz-cst-game.conf` with `MemoryLow=16G`
(`[Slice]` for the two slices, `[Service]` for `user@<uid>.service`). `zz-`
sorts after `50-MemoryLow.conf`, so after a `daemon-reload` the protection
holds. `on` needs no reload for it: `set-property` already set the live value.
(Until the next reload, `systemctl status` of those units may say their
drop-ins "changed on disk"; that is this file, and harmless.)
On this machine uresourced set `user-1000.slice` only once, at login, so the
reloads, not its live calls, were what undid the protection. A live call of
uresourced during a game is still possible; the drift repair below catches
it.

`off` and the watchdog (with the profile off) remove these files whatever
else happened (7.4), and `uninstall` removes any left behind; `cst doctor`
warns if one is still there while the profile is off.

What else the profile does about it:

- Right before `on` sets a property of a fixed unit, it writes the value to
  `/run/cpu-setup/game.intended` as systemd will show it (`16G` as
  `17179869184` bytes, a CPU list with blanks between its ranges), one line
  per property: `prop sys user.slice MemoryLow 16G 17179869184`. So a
  `set-property` that fails in `on` is a drift like any other, which the
  next tick sets, `cst cpu game state` shows and `cst doctor` warns about
  (versions before 2026-11 wrote the line only after a set that worked, and
  a failed one stayed unset until `off`). Right after the set it reads the
  value back from systemd (`systemctl show`); when systemd shows another
  form, a second line with that form follows (an empty answer, a `show`
  that failed, adds none: the line written before the set stays the
  last). The last line of a setting
  wins, everywhere `game.intended` is read (the drift repair, `game
  state`, `cst doctor`). When a repair's set works and systemd then shows
  yet another form, the tick adds that line too, so it does not set the
  same value again every 30 s. A repair that fails is said once per boot,
  in the same mark as the settings it does not set again (below), and
  tried again at every tick. (An installed bash copy of an older CST, during
  an upgrade inside one game session, reads two lines of one setting as two
  records.) A `cpuset.cpus` file it
  wrote directly (7.1) gets a `cgw` line. The transient units (the scopes
  of Docker containers and Steam, the `run-*.service` of `systemd-run`) are
  not recorded: no drop-in can win over theirs; the watchdog's check of the
  boxes it handled (7.2) sets them again instead. The slices made idle in the
  user manager (7.1) are recorded, so the repair covers them too.
- Every watchdog run (`tick`, every 30 s) asks systemd once per unit and
  compares. Each setting that differs is set again with
  `systemctl set-property --runtime` and logged at level notice, for example:
  `drift: user.slice MemoryLow was '0' instead of '17179869184' (daemon-reload or uresourced?): set MemoryLow=16G again`.
  It does not add a line to `game.state`, so `off` still restores the value
  from before `on`. A setting `game.state` holds no record of (the file was
  edited, or cut) is not set again, since `off` could not undo it: the tick
  says so once per boot at level notice
  (`drift not repaired: UNIT PROP: game.state holds no record of it, ...`;
  said again when the list of such settings changes, and when one comes
  back after a tick that found none, for example while systemd did not
  answer about its unit; the same mark as below, 7.6), and `game state` marks its `DRIFT:` line
  `(not set again: ...)`. `cst doctor` still warns about it and proposes
  switching the profile off and on again, which writes fresh records. A
  unit that systemd has not loaded (or a user manager
  that is gone after a logout) is skipped, and so is a `cpuset.cpus` file
  whose cgroup is gone.
- `cst cpu game state` prints a `DRIFT:` line per setting that differs
  (section 6.5), and `cst doctor` shows a `WARN` in its `cpu` area
  ([../doctor/README.md](../doctor/README.md)).

So after a reload, the settings can be wrong for up to 30 s. If `DRIFT:` lines
stay (the watchdog timer is off, or something sets the value back every
time), switch the profile off and on again:

```bash
sudo systemctl restart cpu-game.service
```

That is for a profile the game switched on. For one switched on **by hand**
(`game state` shows `(by hand)`, 7.3) use `cst cpu game off && cst cpu game on`
instead: `off` removes the by-hand mark, so after a plain restart the watchdog
would switch the profile off when no game runs. `game state` and
`cst doctor` print the right one of the two. (`game off && game on` on a
profile the game switched on leaves it on by hand: it then does not switch
off when the game ends.)

**A profile switched on by an older version.** A version without
`game.intended` writes no intended values. Until the profile is switched off
and on again, `tick` skips the comparison, and `game state` prints
`drift: not checked (switched on by an older version ...)`. `off` works as
before. A profile switched on by a version before the `zz-cst-game.conf`
drop-ins has none until it is switched on again; the drift repair still
covers it. The new watchdog makes the other boxes idle (7.1) at its next run
either way.

### 7.6 For other projects: is the game profile on?

**Stable interface:** the file `/run/cpu-setup/game.state` **exists** while
the game profile is on, and does not exist while it is off. Another project
that wants to hold back its own work during games may test for it:

```bash
if [[ -e /run/cpu-setup/game.state ]]; then echo "a game runs"; fi
```

Key on existence only. The contents of `game.state` (and of
`game.intended`) are internal to `cpu-game-profile` and may change in any
version. `game.intended` (what `on` set, for the drift repair) names its
format on its first line (`cst-game-intended 1`; a file without it, from an
older version, is format 0 and is read as before). A copy that finds a newer
format there leaves the drift alone: `tick` says so in the journal once
per boot (again when the text changes; its mark is
`/run/cpu-setup/drift-warned`, removed by `on` and `off`), `state` and
`cst doctor` show "drift: not checked". `game.leftovers` (7.4) is internal too
(format `cst-game-leftovers 2` on its first line; a CST that reads only format 1 leaves such a file
alone); it may exist while the
profile is off, so it says nothing about a game. To be covered all the time, not only during games, run such work in
a slice under the toolboxes' box: `systemd-run --user --slice=user-NAME.slice`
(7.1).

---

## 8. Optional: pin the VM (`vm-pin`)

**libvirt** is the system that manages the VM. It stores the VM's settings as
an XML text file (the "definition"). `virsh` is libvirt's command-line tool,
and `virsh define` saves a new version of the definition.

`vm-pin` owns two parts of the definition: the CPU pinning (below) and, for
a VM with `<cpu mode='host-passthrough'>`, the CPU features that must stay
off for Windows to boot under QEMU 11 (section 8.1).

**Why:** during games the VM is limited to 12-23 anyway. `vm-pin` makes this
permanent. It gives the VM 12 virtual CPUs (vCPUs), as many as the 12 host
CPUs it may use. Each vCPU may run on any of CPUs 12-23 (not one fixed CPU
per vCPU). If Windows has more virtual CPUs than real CPUs, some virtual CPUs
must wait, and Windows can slow down in strange ways. With `vm-pin` that
cannot happen.

```bash
cst cpu vm-pin status   # state, CPU mode and features, vCPU count (live and saved), the saved pinning
cst cpu vm-pin plan     # show the XML diff, change nothing
cst cpu vm-pin apply    # same diff, asks [y/N], then virsh define
```

At the `[y/N]` question, Ctrl-D (end of input) answers no.

An optional VM name can follow (`vm-pin plan OTHER-VM`); the default is
`VM_NAME`. (`status` shows only part of `vm-pin status`.)

What `apply` changes in the VM definition:

- `<vcpu placement="static">12</vcpu>` (any `current=` is removed);
- CPU topology: `sockets=1`, `cores=12`, `threads=1` (and `dies`/`clusters`
  = 1 if present);
- `<cputune>`: one `<vcpupin cpuset="12-23">` per vCPU and
  `<emulatorpin cpuset="12-23">` (old pins are replaced). The *emulator
  threads* are QEMU's helper threads for the VM;
- with `<cpu mode='host-passthrough'>` only: one
  `<feature policy='disable' name='...'/>` for each name in `VM_CPU_DISABLE`
  (section 8.1). A feature already listed keeps its place and only gets
  `policy='disable'`; the others are added at the end. `<topology>`, other
  `<feature>` lines and the attributes of `<cpu>` stay as they are. Other CPU
  modes (`host-model`, `custom`) are left alone.

When nothing would change, `plan` says so in one line, for example
`win11-devbox: already 12 vCPUs pinned to 12-23; CPU features disabled: cet-ss cet-ibt vmx-mbec vmx-exit-save-cet vmx-entry-load-cet.`

Safety:

- If the VM has `<cputune/vcpusched>`, `<cputune/cachetune>`, `<cpu/numa>` or
  `<vcpus>`, it stops: `... adjust it by hand (virsh edit ...); nothing changed`.
- virsh runs in the C locale (its answers are compared, and it translates
  them). A virsh call that gives no answer within 60 s stops the run with
  `virsh ...: no answer within 60 s` (in `status`, a call after the VM
  state leaves its line empty instead).
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

The old settings return at the next start of the VM. Careful: that file is
the definition from *before the first* `apply`, so it may also bring back
the CET/MBEC features (8.1). A newer copy of the definition is in each
`cst vm backup` (`domain.xml`, see [vm/README.md](../vm/README.md)).

### 8.1 CET and nested MBEC: why `vm-pin` switches them off

With `<cpu mode='host-passthrough'>` the guest sees the host CPU's features,
as far as QEMU and KVM pass them on. QEMU 11 (Fedora 45) started to pass
Intel **CET** (`cet-ss` shadow stacks, `cet-ibt` indirect branch tracking)
and nested **MBEC** (`vmx-mbec`, `vmx-exit-save-cet`, `vmx-entry-load-cet`)
to the guest. Windows 11 with Hyper-V, VBS (Memory Integrity) or WSL2 then
no longer boots: it loops in Automatic Repair (WinRE). This happened to
`win11-devbox` right after the upgrade from Fedora 44 to 45 (QEMU 10.2.2 ->
11.1.3); [vm/README.md](../vm/README.md) tells the whole story. Upstream
reports:
[QEMU issue 3215](https://gitlab.com/qemu-project/qemu/-/issues/3215) and the
[Proxmox forum thread](https://forum.proxmox.com/threads/windows-11-bootloop-with-kernel-7-0-works-with-kernel-6-17.185608/).

The guest loses only what it never had under QEMU 10. `cst doctor` warns when
a host-passthrough VM lacks one of these disables while this QEMU offers the
feature (see [doctor/README.md](../doctor/README.md)).

The list is `CPU_DISABLE_DEFAULT` in `cpu/domain.py` (`VM_CPU_DISABLE` in
`cpu-setup.conf` overrides it). Retire it once QEMU, KVM or Windows fix the
problem upstream. **How to undo** (give the features back to the guest):
set `VM_CPU_DISABLE=''` in `/etc/cpu-setup/cpu-setup.conf`, then remove the
five `<feature policy='disable' .../>` lines with `sudo virsh -c
qemu:///system edit win11-devbox` (with an empty list `vm-pin` no longer
touches them, in either direction), or define the `domain.xml` of a
`cst vm backup` made before the change.

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
| Windows VM | `machine.slice` | weight 50, io 50† (+ 12-23 with `vm-pin`) | weight 10, io 10†, 12-23 |
| Docker with `docker-slice` | `builds.slice` | weight 20, io 20†, E-cores | idle, io 20†, 12-23 |
| Docker without `docker-slice` | `system.slice/docker-*.scope` | not covered | 12-23 only |
| `systemd-run --user` jobs without `--slice` | `app.slice/run-*.scope`, `app.slice/run-*.service` | not covered | idle, 12-23* |
| `systemd-run --user --slice=X.slice` (X without a dash), and GNOME's `background.slice` | top-level `X.slice` of the user manager | not covered (`background.slice`: GNOME's weight 30) | idle, 12-23* |
| `systemd-run --user --slice=user-NAME.slice` | under the inner `user.slice` | as the toolboxes | as the toolboxes |
| Rust compiles through CST's sccache wrapper ([toolchains/README.md](../toolchains/README.md)) | the shared server's scope: `user.slice/cst-sccache-<toolbox>.scope` from a toolbox, `sccache.slice/cst-sccache-host.scope` on the host, wherever the build was started | toolbox: as the toolboxes; host: not covered | toolbox: as the toolboxes; host: `idle, 12-23*` (top-level slice demoted by the game profile) |

\* the CPU limits need the cpuset delegation (6.3), so they work only after
the user manager restart.

† `IOWeight` (`io`) only takes effect with `bfq` or `iocost`; on this host
(`none`, `iocost` off) it is recorded on the cgroup as a no-op (see 6.3).

Note on host commands from toolboxes: they live in `app.slice`, next to the
game. Only their CPU list is limited, not their weight. Without delegation
they are not limited at all. The same is true for `watch -- CMD` started from
a toolbox (section 11).

**Not covered:**

- Commands in a **host terminal** (not in a toolbox): they run in `app.slice`,
  the same box as the game, with normal weight and on all CPUs.
- `systemd-run --user` jobs **outside games**: without `--slice` they go to
  `app.slice` too, and with `--slice=X.slice` they land in `X`, at the top of
  the user manager. Either way they run at the normal weight on all CPUs
  until a game starts; during games the profile makes them idle (7.1). To
  cover them all the time, start them with `--slice=user-NAME.slice` (such as
  `user-builds.slice`): that nests under the toolboxes' `user.slice`.
  `cst doctor` points at such slices and jobs.
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

`report` prints one line per run (default: the last 10). It only reads, like
the help (`cpu-watch` with no command): both end quietly when their reader
goes away (`cst cpu report | head -3`). An **illustrative** line (one line on
screen, wrapped here):

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

### 11.1 Is the machine quiet? (`quiet`)

A before/after figure (a benchmark, the disk writes of a build) is only fair
when nothing else uses the machine at the same time. `quiet` tells you if the
machine is quiet now, or waits until it is. It only reads: it writes no file
and leaves nothing running in the background. "Announcing" a quiet window
simply means that the command returns.

`cst cpu quiet` runs the copy in this folder (`cpu/cpu-watch`), so it works
at once. The installed `/usr/local/bin/cpu-watch` only has `quiet` after the
next `cst cpu install`.

```bash
cst cpu quiet [--for SECONDS] [--wait MAX] [--json]
              [--cpu PCT] [--cpu-pressure PCT] [--io-pressure PCT]
              [--memory-pressure PCT] [--write-mbps MB]

# Wait up to 2 hours for 2 quiet minutes, then start the benchmark:
cst cpu quiet --wait 2h --for 120 && make bench
```

It takes one reading per second for `SECONDS` seconds in a row (default 60).
`SECONDS` is a whole number in ASCII digits (`0`-`9`), from 1 to the most a
window can hold (2**63 - 2). `MAX` is a number of seconds in ASCII digits,
a fraction allowed (`120`, `1.5`), or a number with the unit `s`, `m`, `h`,
`d` or `w` (`90s`, `30m`, `2h`, `1.5h`; blanks and either case allowed:
`2 H`), above 0 and not too large to hold. Anything else is a usage error.
The window is quiet when **all** of these hold:

| Test | Read from | Quiet when | Flag | Environment variable |
|------|-----------|------------|------|----------------------|
| CPU busy | `/proc/stat` (all CPUs) | the window's mean is below 10 % | `--cpu` | `QUIET_CPU_PCT` |
| CPU pressure | `/proc/pressure/cpu` (`some avg10`) | the mean of each second's value is below 5 % | `--cpu-pressure` | `QUIET_CPU_PSI` |
| IO pressure | `/proc/pressure/io` (`some avg10`) | the same, below 5 % | `--io-pressure` | `QUIET_IO_PSI` |
| memory pressure | `/proc/pressure/memory` (`some avg10`) | the same, below 1 % | `--memory-pressure` | `QUIET_MEMORY_PSI` |
| disk writes | `/proc/vmstat` (`pgpgout`) | the window's mean is below 10 MB/s | `--write-mbps` | `QUIET_WRITE_MBPS` |
| game profile | `/run/cpu-setup/game.state` | off in every reading | - | - |

- **Means, not peaks.** On an idle machine, a btrfs commit or the hourly
  snapshot writes up to 87 MB/s for a few seconds, and a desktop app can use
  a few CPUs for a moment. A mean over the window lets such short bursts
  through; a build or a copy that lasts does not get through.
- **CPU busy** is the share of CPU time that was not idle. Time waiting for
  a disk (`iowait`) counts as idle. The VM's CPU time (`guest`) is counted
  once: the kernel already includes it in `user`.
- **Disk writes**: `pgpgout` counts the KiB the kernel sends to the devices
  under the filesystem, once per device. On a btrfs RAID1 like this
  machine's, every write goes to both mirrors and is counted twice: 10 MB/s
  here is about 5 MB/s of data. `below`'s "Page Out" shows half of this
  value (checked over one minute: 293 MiB/s from `pgpgout`, 150 MB/s in
  `below`), so the baseline below is given in this tool's unit, twice
  `below`'s figures. Here 1 MB = 1024 KiB. Not `/proc/diskstats`: added up over all
  devices, it counts each write again for every layer (dm-crypt, the
  partition and the disk).
- **Pressure** (PSI, see section 11): the share of the last 10 seconds in
  which at least one task waited for a CPU, for the disks, or for memory.
- A limit is a number above 0 in ASCII digits, a fraction and an exponent
  allowed (`3`, `2.5`, `.5`, `1e1`), blanks or tabs around it; anything else
  (another script's digits, `1_0`, `+3`, `inf`) is a usage error, from a flag or a
  variable.
- A limit given as a flag wins over its environment variable, and the
  variable wins over the default. Inside a toolbox, `quiet` runs on the host
  (`flatpak-spawn --host`), so it reads the host's files and sees the game
  profile. `flatpak-spawn` does not pass the environment on, so `cst cpu`
  hands the `QUIET_*` variables that are set over by itself.

The output has one line per test, with its value beside its limit, then the
verdict and the window's start and end time (an illustrative run):

```
  CPU busy         3.08 %       limit < 10 %      ok
  CPU pressure     0.31 %       limit < 5 %       ok
  IO pressure      1.42 %       limit < 5 %       ok
  memory pressure  0.00 %       limit < 1 %       ok
  disk writes      5.02 MB/s    limit < 10 MB/s   ok
  game profile     off          limit off         ok
quiet (60 s, 2026-11-15 04:46:00 to 2026-11-15 04:47:00)
```

When a test fails, its line ends in `FAIL`, and the last line names it:
`NOT quiet: CPU busy, disk writes (60 s, ...)`.

- **Exit code**: 0 quiet; 1 not quiet, or `--wait` ran out; 2 usage error (an
  unknown flag, a value that is not a number above 0, `--wait` shorter than
  `--for`); 130 after Ctrl-C.
- `--wait MAX` (`120`, `90s`, `30m`, `2h`, `1d`; the whole time, at least
  `--for`): when a window is not quiet, the oldest second drops out and the
  next one is added, until the window is quiet or `MAX` has passed. Only the
  last window is printed (on standard output; a "waiting up to ..." line goes
  to standard error).
- A file that cannot be read or parsed (for example `/proc/pressure/*` on a
  kernel booted with `psi=0`) fails its test, with the reason in the line;
  the other tests are still measured. `--wait` then stops after the first
  window: waiting longer cannot fix it.
- `--json` prints one JSON object instead: `quiet`, `seconds`, `start`,
  `end`, `waited_s`, `tests` (each with `name`, `label`, `unit`, `limit`,
  `value`, `ok`, `error`) and `failed` (the names of the failed tests).

**Why these defaults.** They come from this machine: a `below` recording of
3 days (51,839 samples, one every 5 seconds), and `btrfs-stall-report` over
the quietest part of it. The CPU and write limits pass the machine's quiet
hours with room to spare, and a build or a long copy goes above them. The
pressure limits are reasoned from the quietest window (see below).

- CPU busy: median 3.0 %, 25th percentile 2.2 %, 90th percentile 21.8 %. The
  quietest 10-minute windows have a mean of 2.4 % and peaks of 5.0 to 5.2 %.
  A limit of 10 % on the mean leaves room above them. There is no limit per
  second: the recording has one sample every 5 seconds, so a per-second value
  has nothing to be checked against.
- Disk writes, in this tool's unit (twice `below`'s "Page Out"): median
  3.2 MB/s. Over all 3,218 10-minute windows with a CPU mean below 5 %, the
  window's mean is 4.4 MB/s (10th percentile), 5.1 MB/s (median) and
  7.2 MB/s (90th percentile). The quietest window (2026-11-12 04:37-04:47)
  has 5.0 MB/s. Hence 10 MB/s: above the quiet windows, far below a build or
  a copy (hundreds of MB/s). Single samples in quiet windows reach 170 MB/s
  (a btrfs commit, the hourly snapshot), which is why the limit is on the
  mean.
- Pressure, in that quietest window: IO 1.4 % on average (10-second peak
  2 %), memory 0.0 %, CPU 0.3 %. From 03:00 to 07:00 the averages are the
  same, with IO peaks of 8 % over 10 seconds. Hence IO below 5 %, memory below
  1 % and CPU below 5 %. There is no longer pressure history to check them
  against, so these three are reasoned defaults.
- On another machine, take your own idle reading at a quiet hour
  (`cst cpu quiet --for 600 --json`) and set the limits with the flags or the
  `QUIET_*` variables.

**What it cannot tell you.**

- Quiet before a run does not mean quiet during it: a timer (the hourly
  snapshot, a backup, the toolchains prune) can start in the middle of your
  benchmark. Afterwards, check that nothing else ran:
  `btrfs-stall-report --since '<start>' --until '<end>'` lists `below`'s top
  writers and readers for the run
  ([../btrfs/README.md, section 7](../btrfs/README.md#7-report-was-storage-the-bottleneck)).
  Only your benchmark should be there.
- It sees the VM only through the host's CPU and disk figures. For a figure
  measured on the host, that is what counts.
- The first reading's `avg10` still covers up to 10 seconds before the window.

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
  time), then writes the new one. When the old value cannot be read, `on`
  stops with `sed: can't read FILE: REASON` (exit 2) and saves nothing, so
  `off` then says `not on` instead of writing an empty value.
- `off` writes the saved value back. A **reboot** also reverts everything.
- `uninstall` does **not** switch experiments off. Use `off` or reboot.

A **winner** is a setting that gives lower `mem stall` and a better `1% low`
across a few runs, compared with runs without it. `minfree-256m` is now enabled
by default in `swap/sysctl/99-swap-tuning.conf`, and `swap/tmpfiles/cst-mm-responsiveness.conf`
sets THP `defrag` to `defer+madvise` (while `thp-defer` is active, `cst swap`
shows it as `exp` and leaves `defer` alone). To keep another sysctl tweak after
a reboot, edit `swap/sysctl/99-swap-tuning.conf`, then run `cst swap plan` and
`cst swap apply`. The `swap` module owns the boot-persistent memory settings;
see [../swap/README.md](../swap/README.md).

---

## 13. How to undo everything

```bash
cst cpu game off               # optional; uninstall also stops the profile
cst cpu experiment off NAME    # for each experiment that is ON
cst cpu docker-slice uninstall # if installed; then: sudo systemctl restart docker
cst cpu irq revert             # optional; uninstall also runs it
cst cpu uninstall
```

`uninstall`:

- stops `cpu-game.service` (this runs the exact revert), disables the timer;
  a setting an earlier `off` could not undo and the watchdog could not
  either (still listed by `cst cpu game state` as `leftovers`, 7.4) is then
  retried no more, and keeps its value until the next reboot clears `/run`;
- disables and stops `cst-cpu-power.service`, runs `cpu-power revert` to restore CST-managed GPU runtime PM and CPU C-states, runs `cpu-irq revert` (as `cst cpu irq revert`: removes CST's `irqbalance` ban-list files and restarts `irqbalance.service` if it is active), and reloads udev rules;
- removes the units, the `machine.slice` and `user@.service` drop-ins (also
  `machine.slice.d/50-cpu-setup.conf`, the name before 2026-11), the udev rule,
  the polkit rule and the five programs;
- removes any `/run/systemd/system/*.d/zz-cst-game.conf` left behind (7.5);
- removes the two user drop-ins (both names, `10-` and `50-cpu-setup.conf`)
  and `~/.config/gamemode.ini` (only if it is the script's file);
- removes `/etc/tmpfiles.d/cpu-setup-hwp-boost.conf` and sets HWP dynamic
  boost back to `0` (only when the file was there);
- removes `/etc/modules-load.d/cst-ntsync.conf` and unloads `ntsync` (`modprobe -r`, best-effort) when it was installed;
- drops the install record lines of what it removed (`removed:   PATH` for
  each file the installer removed), also of a file deleted by hand; when the
  installer cannot change your record (one written by a newer CST), the two
  user drop-ins still go, with a warning;
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
   file is broken: fix `TARGET_USER`. `TARGET_USER names no account: NAME`
   means NAME is no user of this machine: set it to yours (`id -un`).

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
install the `gamemode` package first. If it stops with `the 'gamemode' group
comes from NSS, not /etc/group or /usr/lib/group`, copy the group's line
(`getent group gamemode`) into `/etc/group` by hand and run `install` again.

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

**`game state` shows `DRIFT:` lines, or `cst doctor` warns about the game
profile.** A `daemon-reload` or uresourced undid a setting (section 7.5).
Wait 30 s: the watchdog sets it again and logs a `drift:` line
(`journalctl -b -u cpu-game-watchdog.service`). If the lines stay, check
`systemctl is-active cpu-game-watchdog.timer`, then switch the profile off
and on again with the command `game state` prints (section 7.5).

**`cst doctor` warns about a slice of the user manager** (`user manager:
X.slice is a slice of its own next to app.slice ...`). Something started jobs
with `systemd-run --user --slice=X.slice`. They run at the normal weight on
all CPUs whenever no game runs (during games the profile makes them idle,
7.1). Change that project to use `--slice=user-X.slice`, which nests under
the toolboxes' box. The `info` line about `systemd-run` jobs straight in
`app.slice` is the same story for jobs without `--slice`.

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
| `cpu-setup` | What `cpu-setup.sh` runs (checkout only): `cli.main` of `setup/` |
| `setup/domain.py` | `cst cpu`'s rules, no I/O: its commands and where each goes, the paths it uses, the lines it writes (`cpu-setup.conf` and its first values from the CPU topology, the drop-ins, the HWP tmpfiles line), how `status` reads what it shows, the `daemon.json` edit, the memory experiments |
| `setup/adapters.py` | `CpuSetup`: install, status, uninstall, game, vm-pin, docker-slice and experiment on the machine, through the installer (`cst_core/installer.py`, as the user through runuser for the home's files), systemctl and the checkout's programs; a step that failed ends the command with its exit status, as under `set -e` |
| `setup/wiring.py` | What the shim hands over (`CST_SELF`, `CST_ARG0`), the environment, the command lines it execs (cpu-watch, flatpak-spawn from a toolbox, sudo) and `CpuSetup` over the real adapters |
| `setup/cli.py` | Help, cpu-watch's commands, the toolbox refusal, the sudo re-run and the root commands |
| `game-profile` | `/usr/local/sbin/cpu-game-profile` (`on`, `off`, `tick`, `state`): a zipapp of this entry point with the `cpu.game` and `cst_core` packages; `cst cpu game state` runs the checkout's copy |
| `game/domain/__init__.py` | The game profile's rules, no I/O: its settings and paths, the records of `game.state` and `game.intended` (fields as bash's `printf %q` writes them: an installed bash copy of an older CST reads them with `eval`), the drift and its report |
| `game/ports.py` | The boundaries of the game profile's protocol, one per thing on the machine: `systemctl` for both managers, `game.state`, `game.intended` and the marks, `game.leftovers`, systemd's runtime unit files (the overrides and the `MemoryLow` drop-ins), the cgroup tree and `/proc`, sysfs, the run lock, the output |
| `game/application/profile.py` | `GameProfileSteps`: `on`, `off`, `tick` and `state` over the ports, no I/O: each change recorded before it is made, the exact revert, the watchdog |
| `game/adapters/profile.py` | `GameProfile`: `GameProfileSteps` over the real adapters below (`machine_ports`), and `load_profile`, which reads the settings first |
| `game/adapters/systemd.py` | `systemctl` for both managers through the command runner (`systemctl --user -M USER@` for a user's) |
| `game/adapters/files.py` | The files in `/run`: `game.state`, `game.intended` and the marks, `game.leftovers` (read byte for byte, replaced whole, removed), systemd's runtime unit files (the overrides and the `MemoryLow` drop-ins), and the run lock |
| `game/adapters/kernel.py` | The cgroup tree and `/proc`: the boxes, their processes, the GameMode clients, the cgroup writes; `GameSysValues`, the one-value file reads and writes the sysfs adapter (`game/adapters/power.py`) builds on |
| `game/adapters/output.py` | The journal's lines on stdout, errors on stderr |
| `game/wiring.py` | The composition root `cli.py` calls: Ctrl-C, the locale's collation, the paths, the command runner and the profile over the real adapters |
| `game/domain/boxes.py` | The watchdog's check of the boxes it handled, no I/O: whether a recorded setting still holds as systemd shows it (`MemoryLow` by its bytes, `AllowedCPUs` as a set of CPUs, `CPUWeight`), and the journal's words when it sets one again |
| `game/application/boxes.py` | `set_again`: a handled box's recorded settings read back (one `systemctl show`) and each one that does not hold set again, with no new record; no I/O |
| `game/domain/sysfs.py` | The DRM card names, no I/O: whether a sysfs entry is a `cardN` and its number, and a name's number suffix |
| `game/domain/leftovers.py` | The format of `game.leftovers` (7.4), no I/O: the records `off` could not undo with the user each is for, how `off` merges them with `game.state`'s, and the journal's and `state`'s words for them |
| `game/application/drift.py` | `repair_drift`: the watchdog sets again each setting of `game.intended` that `GameProfileSteps.drift` lists as no longer holding, straight with `set-property`; no I/O |
| `game/application/leftovers.py` | `game.leftovers`' steps, no I/O: read, undo again (the next `off`, the tick while off), write back what still fails, and whether a failed unit is gone |
| `game/application/report.py` | `state`'s report (`show_state`): what is applied now and which settings drifted, read from one snapshot of the state files, with no lock; no I/O |
| `game/application/power.py` | AMD `3D_FULL_SCREEN` activation, P-core EPP, per-game C-state caps and the game's processes, each old value recorded first; no I/O |
| `game/adapters/power.py` | sysfs as the power steps read and write it, and the dynamic active-GPU DRM discovery (including render-offload `/dev/dri/renderD*`) |
| `game/adapters/host.py` | What the game profile reads besides systemd and its state files, and `cst cpu` reads too: `cpu-setup.conf`, the accounts (uid and name), a file's text, sysfs writes, the locale's collation |
| `game/cli.py` | The command line of `cpu-game-profile`: checks the action and hands it to `wiring.py` |
| `cpu-irq` | `/usr/local/sbin/cpu-irq`: a zipapp of this entry point with `cpu.irq`, `cpu.game`, and `cst_core`; `cst cpu irq` runs the checkout's copy |
| `irq/domain.py`, `irq/adapters.py`, `irq/cli.py` | Adaptive `irqbalance` ban-list calculation (`BUILD_CPUS` intersected with online CPUs, keeping `>= 2` unbanned CPUs), RPM handling first, then `/etc/cpu-setup/irqbalance.env` and the drop-in written in-process through `InstallService`, then the service; `cpu:irq` done last (section 6.3) |
| `cpu-power` | `/usr/local/sbin/cpu-power`: a zipapp of this entry point with the `cpu.power`, `cpu.game`, and `cst_core` packages; `cst cpu power` runs the checkout's copy |
| `power/domain.py`, `power/adapters.py`, `power/cli.py` | Baseline and hotplug GPU runtime PM (`GPU_PM_MODE`) plus baseline CPU `cpuidle` latency ceiling (`CPU_CSTATE_MAX_LATENCY_US`, by default only with `intel_idle`); the sysfs CPU topology `install` reads for `cpu-setup.conf`'s first values |
| `vm-pin` | `/usr/local/sbin/cpu-vm-pin`: a zipapp of this entry point with the `cpu` and `cst_core` packages |
| `cpu-watch` | `/usr/local/bin/cpu-watch`: a zipapp, as `vm-pin` |
| `domain.py` | The rules, no I/O: the domain definition rewrite of `vm-pin` (`pinned`, the CPU features list `CPU_DISABLE_DEFAULT`, which `cst vm` and `cst doctor` import), the numbers of `cpu-watch` (the columns of `samples.csv`, frame lows, the `quiet` tests and limits) |
| `adapters.py` | What reads the host: `cpu-setup.conf`, `virsh` (cst_core's `SubprocessVirshAdapter`), sysfs, procfs, cgroups, the recorded runs |
| `cli.py` | The command lines of `vm-pin` and `cpu-watch` (the two entry points above only call it) |
| `gamemode.ini` | `~/.config/gamemode.ini` |
| `cpu-game.rules.in` | polkit rule (`@USER@` replaced by your user) |
| `udev/65-cst-gpu-pm.rules` | `/etc/udev/rules.d/65-cst-gpu-pm.rules` (hotplug GPU runtime PM) |
| `units/cpu-game.service`, `units/cpu-game-watchdog.service`, `units/cpu-game-watchdog.timer`, `units/cst-cpu-power.service` | `/etc/systemd/system/` |
| `units/machine.slice.d-10-cpu-setup.conf` | `machine.slice.d/10-cpu-setup.conf` |
| `units/user@.service.d-50-cpu-setup-cpuset.conf` | `user@.service.d/50-cpu-setup-cpuset.conf` |
| `units/builds.slice.in` | `/etc/systemd/system/builds.slice` (by `docker-slice install`; `@ALLOWED@` becomes `AllowedCPUs=` with the E-cores, or nothing when there are none) |

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
