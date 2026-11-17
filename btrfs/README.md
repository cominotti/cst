<!-- SPDX-License-Identifier: MPL-2.0 -->
# Storage tools for this PC (`cst btrfs`)

This folder holds the parts. The script `../btrfs-setup.sh`, the `btrfs`
module of CST, installs them, shows their state and removes them. You run it
as `cst btrfs <command>` (how to set up `cst`:
[../README.md](../README.md#the-cst-command)); `bash btrfs-setup.sh <command>`
from the repository folder does the same. This document explains what each part does,
why it exists, and what to do when it reports a problem.

Snapshots and the backup to the NAS (a network storage box, here a
Synology) are only summarised here. The details are in
[SNAPSHOTS.md](SNAPSHOTS.md). The entry point of the whole repository, with
the CPU side (`cst cpu`), is [../README.md](../README.md).

> **If you only read one thing**
>
> 1. Run `cst btrfs install` once, on the host (section 4).
> 2. Run `cst btrfs status` to see that everything is fine (section 5).
> 3. When you get an alert, go to [section 13, "What to do if..."](#13-what-to-do-if).

## Contents

1. [The PC and the problem](#1-the-pc-and-the-problem)
2. [Before you start](#2-before-you-start)
3. [All commands at a glance](#3-all-commands-at-a-glance)
4. [`install`: what it puts on the system](#4-install-what-it-puts-on-the-system)
5. [`status`: everything at a glance](#5-status-everything-at-a-glance)
6. [`uninstall`: how to undo](#6-uninstall-how-to-undo)
7. [`report`: was storage the bottleneck?](#7-report-was-storage-the-bottleneck)
8. [`degraded-boot`: start the PC with one disk dead](#8-degraded-boot-start-the-pc-with-one-disk-dead)
9. [`crypt-perf`: faster encryption path](#9-crypt-perf-faster-encryption-path)
10. [`snapshots` (summary)](#10-snapshots-summary)
    - [10.1 `bulk`: one folder for everything you can download again](#101-bulk-one-folder-for-everything-you-can-download-again)
    - [10.2 Toolchain caches: in `cst toolchains` now](#102-toolchain-caches-in-cst-toolchains-now)
11. [`docker-gc`: weekly, careful Docker cleanup](#11-docker-gc-weekly-careful-docker-cleanup)
12. [The Claude Code rule file](#12-the-claude-code-rule-file)
13. [What to do if...](#13-what-to-do-if)
14. [Glossary](#14-glossary)
15. [Repository layout](#15-repository-layout)

---

## 1. The PC and the problem

The PC is called **sophia**. It runs Fedora **Silverblue**: a Fedora
edition where the system files are a read-only image that is replaced as a
whole at each update. Its storage is built like this:

```
 NVMe disk 1                NVMe disk 2
     |                          |
 dm-crypt (LUKS)            dm-crypt (LUKS)      <- encryption layer
     \__________   ___________/
                \ /
         one btrfs filesystem, RAID1             <- /, /var, /home
```

Some words first:

| Word | Meaning in plain words |
|---|---|
| **NVMe** | A fast kind of SSD (a disk without moving parts) that plugs directly into the mainboard. |
| **btrfs** | The filesystem: the part of Linux that organises files on the disk. It can keep checksums, compress data and take snapshots. |
| **RAID1** | Every piece of data is stored twice, once on each disk. If one disk dies, the other still has everything. |
| **dm-crypt / LUKS** | Disk encryption. Every block is encrypted before it reaches the disk. You type a passphrase per disk at boot. |
| **mount option** | A setting chosen when the filesystem is attached ("mounted"), for example `compress=zstd:1`. |
| **IO** | Input/output: reading from and writing to the disk. |

The filesystem is mounted with the option `degraded`. This means: if one
disk is missing, keep running on the other one. This is good, because the PC
keeps working. But it has a cost: **you may not notice a broken disk**. The PC
does not stop and shows no error, so there is no reason to check.

Also, btrfs on this PC sometimes makes programs wait (a "stall"). Builds,
tests and Docker workloads write a lot. Later, it is hard to know if the disks
were the reason something was slow.

So `cst btrfs` does three kinds of work:

1. **Watch the health** of the disks and the filesystem, and send a desktop
   alert when something is wrong.
2. **Record, all the time, why programs wait on storage**, so you can ask later:
   "was the disk the problem at 10:30?".
3. **Tune a few settings** so that stalls are shorter and the disk does not fill
   up by surprise.

It also has separate, optional parts: unattended boot with one disk missing,
a faster encryption mode, snapshots, and a weekly Docker cleanup.

---

## 2. Before you start

Two words you need:

- **host** = the real Fedora system of the PC.
- **toolbox** = a container (a separate, small Linux environment) that runs on
  top of the host. You may have a terminal open inside one. Many system
  commands do not work correctly from inside a toolbox.

Rules:

- Commands marked **(root)** need administrator rights. When you start
  `cst btrfs`, it asks `sudo` for your password by itself.
- **Other commands in this README do not do that.** When this README shows
  `sudo` in front of a command, type it. Without it you get errors like
  `Operation not permitted`.
- **(root)** commands, and every `sudo` command in this README, must run **on
  the host**, not inside a toolbox. Inside a toolbox, `cst btrfs` stops
  with the message
  `'<command>' must run on the host, not inside a toolbox/container ...`.
  Open a terminal on the host for them.
- `report` and `watch` work on the host and inside a toolbox.
- Run it as your normal user, through `cst` (not `sudo cst`), for example:

```
cst btrfs help
```

---

## 3. All commands at a glance

| Command | Who | What it does |
|---|---|---|
| `install [--no-below]` | root | Installs monitoring and tuning (section 4). Safe to run again. `--no-below` skips the tool "below" (a recorder of per-process disk use, see 4.8). |
| `status` | root | One overview of everything (section 5). Read-only. |
| `uninstall` | root | Removes what `install` added (section 6). |
| `test-alert` | root | Sends a test desktop alert. |
| `degraded-boot [install\|plan\|status\|files-only\|uninstall]` | root | Boot without a dead disk, unattended (section 8). |
| `crypt-perf [status\|enable\|disable]` | root | Faster dm-crypt mode (section 9). |
| `snapshots ...` | root | Hourly snapshots and NAS backup (section 10, details in [SNAPSHOTS.md](SNAPSHOTS.md)). |
| `bulk plan\|apply\|status\|keep ID\|unkeep ID` | root | `~/Bulk`: one folder for everything that can be downloaded again, with no snapshots and no NAS backup (section 10.1). Also the first step of `snapshots setup`. `keep ID` leaves one item in /home on purpose. |
| `bulk steam plan\|apply [--app ID] [--copy]` | root | Moves your installed Steam games into the `~/Bulk/steam` library, with Steam closed (section 10.1). `plan` is a dry run. `apply` also installs the boot step that repairs an interrupted move. `--copy` allows a real copy of NOCOW game data (it cannot be reflinked). |
| `docker-gc enable\|status\|run [--dry-run]\|cleanup-now\|disable [--purge]` | root | Weekly Docker cleanup (section 11). |
| `report [args]` | user | "Was storage the bottleneck in this time window, and why?" (section 7). |
| `watch [args]` | user | Run a command and save the report of its time window (section 7.5). |
| `help` | anyone | Prints the built-in help. |

Without any command, the script only prints the help (like `help`) and
changes nothing. To install, type `install` yourself. The old flags still
work: `--status`, `--uninstall`, `--test-alert`, and `--no-below` (which means
`install --no-below`).

---

## 4. `install`: what it puts on the system

```
cst btrfs install            # everything
cst btrfs install --no-below # everything except "below"
```

`install` is **idempotent**: you can run it many times, and the result is the
same. It only rewrites a file when its content changed, and prints `written:`
or `unchanged:` for each file. Some things happen at **every** run, even when
no file changed:

- it restarts `btrfs-stall-monitor.service` and `below.service`, so the
  recording has a gap of a few seconds;
- it applies the writeback limits again (section 4.6).

Before it changes anything, it checks that every script has no syntax errors
and that every unit file exists. It never touches the initramfs (the small
system that runs before the real system at boot) or the boot path.

### 4.1 What changes on my system

Two systemd words first. **systemd** is the program that starts and stops
services on Linux.

- A **unit** is a small text file that describes one job for systemd. A
  `.service` unit runs a program. A `.timer` unit starts another unit on a
  schedule.
- A **drop-in** is a small extra file that changes one setting of an existing
  unit, without editing the original file.

Unit files go to `/etc/systemd/system/`.

| Part | Files | Runs when |
|---|---|---|
| Health check (4.2) | `/usr/local/sbin/btrfs-health-check`, `btrfs-health-check.service` + `.timer` | 2 min after boot, then every hour |
| Weekly scrub (4.3) | `btrfs-scrub-weekly.service` + `.timer` | Sunday 02:00, up to 15 min later (or after the next boot if the PC was off); speed limit: `SCRUB_LIMIT` |
| fstrim (4.4) | Fedora's own `fstrim.timer` (only enabled, not changed) | weekly |
| Block-group reclaim (4.5) | `/etc/tmpfiles.d/btrfs-automatic-reclaim.conf` | at every boot, and once now |
| Writeback limits (4.6) | `/etc/sysctl.d/90-btrfs-writeback.conf` | at every boot, and at every `install` |
| Stall monitor (4.7) | `/usr/local/sbin/btrfs-stall-monitord`, `btrfs-stall-monitor.service` | always |
| Stall report (section 7) | `/usr/local/bin/btrfs-stall-report`, and a small launcher `~/.local/bin/btrfs-stall-report` | when you run it |
| The `cst` command | the link `~/.local/bin/cst` -> the `cst` of the CST folder, only if nothing else is there, so the hints of alerts and logs (`cst btrfs ...`) work as typed ([README.md, The `cst` command](../README.md#the-cst-command)) | when you run it |
| below (4.8) | the `below` package (added with `rpm-ostree`, see 4.8), the drop-in `/etc/systemd/system/below.service.d/10-cst.conf` | always |
| Desktop alerts (4.9) | `/usr/local/sbin/btrfs-notify`, `btrfs-stall-alert.service`; for alerts kept until the next login: `/usr/local/bin/cst-pending-alerts`, the user unit `/etc/systemd/user/cst-pending-alerts.service` (enabled for every user) and the folder `/var/lib/cst-alerts/` | when a part has an alert; the user unit at each desktop login |
| Docker volumes NOCOW (4.10) | `/usr/local/sbin/docker-volumes-nocow`, `docker-volumes-nocow.service` | at every boot, before Docker |

In CST's install manifest (`cst_core/domain/install.py`) these files form
three **components**, each installed as a whole by one installer call:
`btrfs:alerts` (`btrfs-notify`, `cst-pending-alerts` and its user unit,
written first: the units of the next one run `btrfs-notify`), `btrfs:core`
(the other programs and units, the writeback and reclaim files) and
`btrfs:below` (the below drop-in). `snapshots setup` installs
`btrfs:snapshots` and `btrfs:top-mount` (and `btrfs:alerts` again);
`docker-gc enable`, `btrfs:docker-gc`. `uninstall` removes the files of the
manifest's components, not a list of its own, and their lines in the install
record (also of a writeback or reclaim file that was already deleted by hand).
`docker-gc disable` stops with an error, before it disables anything, when the
installer cannot list docker-gc's units.

The launcher `~/.local/bin/btrfs-stall-report` and the link
`~/.local/bin/cst` are created for **the user who ran `sudo`** (the script
reads this from `sudo`). If you run `install` directly as root (not through
`sudo`), neither is created.

At the end, `install` runs the health check once and prints its result, checks
that the stall monitor is running (it stops with `ERROR:` if not), and prints
the reclaim state and the state of `below`.

### 4.2 Health check (every hour)

**Why:** with `degraded` mounting, a dead disk does not stop the PC. The
health check makes sure you hear about it.

**How:** `btrfs-health-check` reads the state of the filesystem and the disks.
It **never changes the filesystem**. It only writes its own small state files in
`/var/lib/btrfs-health-check` (and markers of the degraded-boot part, see
section 8). It alerts about:

| Problem | Example message (start) |
|---|---|
| A disk of the RAID1 is missing | `member device MISSING (running degraded)` |
| Some data is on a disk that is missing | `N bytes on missing device` |
| Data written while a disk was missing (two layouts mixed) | `mixed chunk profiles (degraded writes?)` |
| The filesystem was switched to read-only after an error | `mounted READ-ONLY (forced after an error?)` |
| Device error counters are not zero | `non-zero device error counters` |
| The kernel log has btrfs or disk errors since the last check: btrfs errors, checksum failures (`csum failed`), IO errors, or a task that was stuck (`blocked for more than N seconds`) | `kernel: N btrfs/IO error message(s) since last check` |
| Last scrub found errors, did not finish, never ran, or is older than 10 days | `last scrub reported errors` / `last scrub (started ...) did not finish: status ...` / `no scrub has ever completed` / `last scrub is N days old` |
| Little unallocated space (below 50 GiB on a disk; critical below 5 GiB) | `only N GiB unallocated on ...` / `CRITICAL: only N MiB unallocated` |
| Metadata almost full | `CRITICAL: metadata almost full` |
| Automatic reclaim is configured but switched off, or it failed | `automatic reclaim is configured but data/... is off` / `automatic ... reclaim failed` |
| The NVMe disk's own health log (SMART, read with `nvme smart-log`): a critical warning, media errors, spare blocks below the disk's own threshold, or wear (`percentage_used`) of 80% or more. **Spare blocks** are reserve blocks the disk uses to replace worn-out ones. It is read for the btrfs disks and for the disks under every swap device in `/proc/swaps` (see below). | `NVMe nvme0n1: ...` / `NVMe nvme1n1 (swap): ...` |
| A software RAID (md) is degraded | `md RAID degraded` |
| This boot or a past boot was degraded (only with the degraded-boot part) | `this boot is DEGRADED` / `booted DEGRADED on ...` |
| The degraded-boot rule was made for another filesystem (only with the degraded-boot part) | `btrfs-last-resort: /etc/udev/rules.d/65-btrfs-last-resort.rules is for ... but the root fs is ...; re-run 'cst btrfs degraded-boot'` |
| A Fedora update changed the udev rule that degraded-boot depends on (only with the degraded-boot part) | `udev: .../64-btrfs.rules changed semantics; review btrfs/btrfs-degraded-boot-setup.sh (CST)` |
| Hourly snapshots stopped working (only with snapshots set up) | `snapshots: no successful hourly run for over 3 hours` |

**Swap disks too.** Swap has no second copy: if the swap disk fails, the
programs whose memory was swapped out crash or hang. So the SMART checks also
run for the NVMe disk under each swap device listed in `/proc/swaps`. An
encrypted swap (`dm-2`) is followed down to its disk (`nvme1n1`), like the
btrfs disks. Swap files and zram are skipped: a swap file lives on a
filesystem that is already checked, and zram is memory. A disk that holds
both btrfs and swap is checked once. Only SMART is read for swap disks; the
other checks in the table are about btrfs.

When everything is fine it writes one line to the **journal** (the system log
of systemd; you read it with `journalctl`):
`btrfs-health-check: OK (<mounts>; disks: nvme0n1 nvme2n1; swap disks: nvme1n1)`.
When there are problems it sends **one critical desktop alert** titled
`Disk health: problems detected`. Its text starts with the number of problems
(`2 problem(s):`), then one line per problem.

**How often an alert comes back:**

- **State problems repeat every hour** while the problem is still there: a
  missing disk, error counters, low space, old or failed scrub, NVMe health,
  degraded boot. This is on purpose. The alert stops when the cause is fixed.
  Device error counters never go back to zero by themselves; they keep
  alerting until you reset them (`sudo btrfs device stats -z /var`, see
  section 13).
- **Kernel-log errors and reclaim failures are reported once**, at the first
  check after they happen. The check remembers how far it has read and only
  counts new messages.

### 4.3 Weekly scrub

**Scrub** = btrfs reads every block on both disks and compares it with its
checksum. If one copy is bad, btrfs repairs it from the good copy on the other
disk. A **checksum** is a short fingerprint of the data; if the data changes by
accident, the fingerprint no longer matches.

**Why:** a disk can lose data silently. Without a scrub you notice only when you
need that file, and by then the other copy may be bad too.

**How:** `btrfs-scrub-weekly.timer` runs `btrfs scrub start -B /var` every
Sunday at 02:00, in the idle IO class (`IOSchedulingClass=idle`). That class
only works with the BFQ or Kyber IO schedulers; these NVMe disks use `none`,
so on this PC the scrub reads at the disks' full speed (see
[cpu/README.md, section 6.3](../cpu/README.md#63-baseline-always-active-even-without-a-game)) unless you set a speed limit (below). If the PC was off, it runs after the
next boot, up to 15 minutes later (`RandomizedDelaySec=15min`), so not in
the first minutes after login. After the scrub, the health check runs at once.
If the scrub fails, the health check also runs and alerts.

**Speed limit (`SCRUB_LIMIT`).** A full-speed scrub reads both disks as fast
as they go (here 4.5 GiB/s for 1.7 TiB in 6 minutes), which can stall builds
running at the same time. btrfs has its own limit per device
(`btrfs scrub limit`, sysfs `devinfo/<id>/scrub_speed_max`), which works with
any IO scheduler. Set it in `/etc/btrfs-setup/snapshots.conf` (create the
file if it is not there; other keys can stay out) and run `cst btrfs install`:

```
SCRUB_LIMIT=1G      # per second, per device; 500M, 2G ...; empty or 0 = no limit
```

- The default is **no limit**: choose the number after measuring (for
  example a scrub during a build, with `cst btrfs report`). 1G per device
  makes 1.7 TiB take about 30 minutes.
- `install` writes the drop-in
  `/etc/systemd/system/btrfs-scrub-weekly.service.d/10-cst-limit.conf`: it
  sets the limit on every device before the scrub (`ExecStartPre`) and sets
  it back to 0 after it (`ExecStopPost`, which also runs when the scrub
  fails or is stopped; the kernel forgets the limit at the next mount anyway).
  With no limit there is no drop-in, and `install` removes an old one.
  `uninstall` removes it too.
- The value is read with the same rules as `cst doctor` reads this file: a
  plain size (digits, then K, M, G or T), as `SCRUB_LIMIT=1G`,
  `SCRUB_LIMIT='1G'` or `SCRUB_LIMIT="1G"`. Any other form, or a value
  that is not a size, makes `install` stop before it changes anything.
- If setting the limit fails (a btrfs-progs without `scrub limit`), that
  scrub does not run and the unit fails: it never runs at full speed by
  mistake. The health check alerts when the last scrub gets too old.
- `status` shows the setting, the limit the service sets and the value in
  sysfs now; `cst doctor` shows the same as `info` and warns when the
  setting and the service differ.

To start one by hand (on the host):

```
sudo systemctl start btrfs-scrub-weekly.service
sudo btrfs scrub status /var      # progress and result
```

### 4.4 fstrim (Fedora's timer)

**Trim** tells an SSD which blocks are free, so it can keep its speed and wear
evenly. btrfs here already trims all the time (mount option `discard=async`).
Fedora's `fstrim.timer` also trims weekly. It covers `/boot` and the EFI
partition (the small partition the PC's firmware reads at power-on), and it
is a second, backup trim for btrfs. `install` only makes sure this timer is
on. `uninstall` leaves it alone.

### 4.5 Automatic block-group reclaim

btrfs reserves disk space in big pieces called **block groups** (or chunks),
about 1 GiB each for data. Space that is reserved but not yet used is
"allocated". Space that is not reserved at all is "unallocated". btrfs needs
**unallocated** space to grow its metadata (the index of all files). If it runs
out, writes fail with `ENOSPC` ("no space left") even when the disk looks half
empty.

**Why:** many half-empty block groups can use up all unallocated space.

**How:** the file `/etc/tmpfiles.d/btrfs-automatic-reclaim.conf` sets two kernel
switches for data block groups of the root filesystem:

- `dynamic_reclaim = 1`: when unallocated space drops below about 10 GiB, btrfs
  starts to compact half-empty data block groups, harder the closer it is to full.
- `periodic_reclaim = 1`: lets btrfs run that check regularly in the background.

This is an emergency measure for the last few GiB, not routine work. The health
check warns much earlier (50 GiB). Only data block groups are reclaimed, not
metadata.

What to do when it happens anyway, and why nothing here ever runs a balance on
a schedule: [When btrfs says 'No space left' but df shows free space](#when-btrfs-says-no-space-left-but-df-shows-free-space)
(section 13). `cst doctor` shows the same numbers without `sudo` (area
`btrfs`).

### 4.6 Writeback limits (`vm.dirty_*`)

When a program writes, Linux first keeps the data in memory ("dirty pages") and
writes it to disk later ("writeback").

Two more words:

- **tuned** is a Fedora service that applies performance settings, grouped in
  "profiles". This PC uses the profile `throughput-performance`.
- **`flushoncommit`** is a btrfs mount option. It makes btrfs write all pending
  data before each commit (see below), so that after a crash a new file never
  shows old content.

**Why:** the tuned profile `throughput-performance` sets `vm.dirty_ratio=40`.
On this 96 GiB PC that allows about 37 GiB of unwritten data in memory.
With `flushoncommit`, every btrfs **commit** (the moment btrfs makes its changes
permanent, about every 30 s or on demand) must first write all of that. A 4.4 s
commit was observed. During those 4.4 s, every program that wants to write must
wait, and the desktop can freeze.

**How:** `/etc/sysctl.d/90-btrfs-writeback.conf` sets fixed limits. These are
**global** kernel settings: they apply to all filesystems and disks, not only to
btrfs. The real file is a comment block (the same reasons as above) and these
two lines:

```
vm.dirty_background_bytes = 536870912
vm.dirty_bytes = 2147483648
```

- `vm.dirty_background_bytes` = 512 MiB: above this, Linux starts writing in
  the background.
- `vm.dirty_bytes` = 2 GiB: above this, programs that write must wait.

This does not change how much is written in total. It only limits the size of
each burst (2 GiB is about 0.5 s of NVMe writes). Setting these values turns
off tuned's `vm.dirty_ratio`. tuned is configured with `reapply_sysctl=1`, so
the file in `/etc/sysctl.d` wins over the tuned profile. `install` applies the
values again every time it runs. `status` shows `DRIFT` if the live values
differ.

### 4.7 Stall monitor (`btrfs-stall-monitord`, always on)

A **stall** is when programs have to wait for the disk or the filesystem.
A task that waits like this is in **D state** ("uninterruptible sleep"):
it cannot do anything else and cannot even be killed until the wait ends.

**Why:** when a build was slow, you want to know later *why*, without having
run it inside a special script.

**How:** `btrfs-stall-monitor.service` runs all the time. It is **read-only**:
it only reads kernel counters in `/proc` and `/sys` (special folders where the
kernel shows its own state as files) and in the cgroup tree.

- Every 5 s it records pressure, disk counters, memory and swap, btrfs commit
  times, and IO per cgroup. A **cgroup** is a group of processes that the kernel
  counts together, for example one Docker container or one systemd service.
- When something looks like a stall, it opens an **episode**: it samples every
  second and saves the kernel stack of each waiting task. A **kernel stack** is
  the list of kernel functions the task is inside; it tells *what* it waits for.
  An episode opens when IO pressure is 10% or more, when 3 or more tasks wait on
  storage, or when a btrfs commit takes 1 s or more.
- It follows the kernel log (`/dev/kmsg`) for btrfs and disk errors.
- It sorts each waiting task by what it waits for: storage, a network share
  (FUSE), btrfs maintenance (trim, scrub, balance), **memory** (memory
  reclaim, reading a page back from swap or zswap, a page fault or an `mmap`
  call waiting for the program's memory map lock), or nothing in particular
  (idle). A thread that waits only for the memory map lock waits for another
  thread of the same program: it counts as memory if a thread of that program
  is in reclaim or swap, else as storage (or FUSE) if one waits there. Only
  storage waits count as "tasks waiting on storage" and open an episode. Daemons
  before version 8 counted memory waits as storage; the report corrects old
  logs (see 7.4).
- Fedora kernels have no "hung task" detector, so the monitor does this job: a
  task waiting on storage for 30 s **without making progress** is reported. A
  task stuck the same way in memory reclaim or swap gets its own alert, not a
  storage one.
- Data goes to `/var/log/btrfs-stall-monitor`, one file per day, **8 days** kept.
  Only members of the `wheel` group (the users who are allowed to use `sudo`)
  can read it, because it contains process names and kernel messages.
- Note on zswap numbers: daemons before version 7 read only the first 4 KiB
  of `/proc/vmstat`, so they recorded `zswpin`, `zswpout` and `zswpwb` as 0.
  The report ignores those counters in old logs: it prints `n/a (daemon < v7)`,
  or counts zswap only from the first version 7 record. The new daemon
  reaches the machine with `cst btrfs install`.
- It is strongly sandboxed (no network, no writes outside its own folders, limited
  to 192 MiB of memory and 25% of one CPU) and runs with a higher priority, so it
  keeps sampling while everything else is stuck.

**Desktop alerts from the monitor.** The same kind of alert is sent at most once
every 15 minutes.

| Title | When | Level |
|---|---|---|
| `Kernel: btrfs/device error` | a severe btrfs or disk error on these disks in the kernel log | critical |
| `btrfs commit stall on <label>` | a btrfs commit took (or is still running after) 5 s or more | normal |
| `Task stuck in an uninterruptible wait (likely storage)` | a task waited on storage 30 s with no progress | normal |
| `Task stuck in an uninterruptible wait (memory reclaim / swap)` | a task waited 30 s with no progress in memory reclaim, swap or zswap: memory ran short; the disks are not the cause | normal |
| `A program has waited on storage for 10+ minutes (progressing)` | a task was busy with storage at every sample for 10 min, but still progressing (for example a program that calls fsync again and again, see 4.10) | normal |
| `btrfs maintenance (fstrim/scrub) stuck for 15 min` | trim, scrub or balance waited 15 min with no progress | normal |
| `Program waiting on a FUSE filesystem (network share / portal)` | a program waited 30 s on a network share or on the document portal (the Flatpak service that gives sandboxed apps access to your files) | normal |

### 4.8 below

**below** is a separate tool (a Fedora package) that records, every 5 s, how much
each process and cgroup read and wrote. The stall monitor knows *that* the disk
was busy; below knows *who* did it.

`install` adds it with `rpm-ostree install --apply-live below`. **rpm-ostree** is
the tool that manages the Silverblue system image. Extra packages are
"layered" on top of the read-only image. If another system update is already
waiting for a reboot, the package cannot be added at once. Then `install`
prints `below: staged for the next boot` and you must **reboot and run
`install` again**.

The drop-in file keeps **7 days** of history, at most **8 GiB**, in
`/var/log/below`. Use `install --no-below` if you do not want below.

### 4.9 Desktop alerts (`btrfs-notify`)

All parts send alerts through `/usr/local/sbin/btrfs-notify`: the health
check, the stall monitor, the Docker NOCOW service and the snapshot programs.
It shows a desktop alert to **every normal user (uid 1000 or more) who is
logged in to the desktop** at that moment. When a service starts it, the alert
text is also written to the journal. When you start it from a terminal (as
`test-alert` does), that text goes to the terminal instead.

The stall monitor cannot show alerts itself (it is sandboxed), so it leaves a
small file in `/run/btrfs-stall-monitor` and starts `btrfs-stall-alert.service`,
which sends it. That folder survives a restart of the monitor, also an automatic
one after a crash (`RuntimeDirectoryPreserve=restart`), so a queued alert is not
lost; stopping the monitor removes it. The monitor's journal lines carry its
own name: `journalctl -t btrfs-stall-monitord` (or `-u btrfs-stall-monitor`).

**When nobody is logged in.** Some alerts happen when no desktop is there to
show them: at boot, before the login screen, or at night when you are logged
out. Then `btrfs-notify` **keeps the alert** and you see it **at your next
login**:

1. It writes the alert to a small file in `/var/lib/cst-alerts/pending/`.
   Only root can write there; every user can read it.
2. If the same alert (same program, same title) comes again before you log in,
   the file is replaced and a counter goes up. So a problem that repeats every
   hour during the night gives **one** notification in the morning, not ten.
3. When you log in to the desktop, the small user service
   `cst-pending-alerts.service` runs `/usr/local/bin/cst-pending-alerts` as
   you. It shows each kept alert with the time it happened, for example
   `Happened at boot, Tue 13 Oct 03:12, while no desktop session could show it.`
   followed by the original text. It does not run on the login screen
   itself, only in your own desktop session.
4. If many alerts wait, you do not get dozens of notifications. **Critical**
   alerts always come one by one. The other ones fill up to 8 notifications
   (the newest first), and one more notification says how many older,
   non-critical ones there are. `cst-pending-alerts --list` shows those too.
5. It remembers what it has shown you in `~/.local/state/cst-alerts/delivered`,
   so you see each alert once. If the problem comes back later, you see it
   again, and the text counts only the times since you last saw it (for
   example `Happened 3 more times since you last saw it`).
6. Kept alerts are deleted after **7 days**. The hourly health check cleans
   up; without it (only `snapshots setup` installed), the next kept alert
   does. Either way, an alert older than 7 days is never shown. A kept alert
   is also deleted as soon as the same alert is shown live on a desktop.
   On a computer with several desktop users this means: whoever sees it live
   first "uses it up", and the others do not get it at their login.

**Coming from f44-tools.** Older versions of this repository were called
f44-tools, and these parts had other names: `/usr/local/bin/f44-pending-alerts`,
the user unit `f44-pending-alerts.service`, the folder `/var/lib/f44-alerts/`
and your record `~/.local/state/f44-alerts/`. `cst btrfs install` (and
`cst btrfs snapshots setup`) changes that once, and running it again changes
nothing more:

- alerts still waiting in `/var/lib/f44-alerts/pending/` move to
  `/var/lib/cst-alerts/pending/` with the same file names and times, so none
  is lost and none you have already seen comes back. Then, still holding the
  old folder's lock, the old folder is deleted and its name becomes a link to
  the new folder. An old `btrfs-notify` that was still running (it waited for
  that lock) then puts its alert in the new folder, instead of losing it. The
  next `install` deletes the link. If a file cannot be moved, the old folder
  stays and a `WARNING` says so; run `install` again;
- the old user unit is disabled for every user and deleted, and so is the old
  program;
- below's drop-in `10-f44-tools.conf` becomes `10-cst.conf`;
- your record moves from `~/.local/state/f44-alerts/` to
  `~/.local/state/cst-alerts/` at your next desktop login (the new program does
  that, as you). Until then, `--list` and `status` read the old one.

Until `install` has run, `status` points at the old folder if it is still
there.

To see what is waiting, run this as your normal user in a terminal on the
host (not inside a toolbox):

```
cst-pending-alerts --list
```

`status` shows the same list (section 5).

To check that alerts reach your desktop:

```
cst btrfs test-alert
```

You should see `Disk health: test alert`. If nobody is logged in to the
desktop at that moment (for example you run it over `ssh` while logged out),
the alert is kept and you see it at your next desktop login.

### 4.10 Docker volumes without copy-on-write (NOCOW)

**Copy-on-write (CoW)** is how btrfs normally writes: it never overwrites data in
place. It writes the new version somewhere else and then points to it. This is
what makes checksums and snapshots possible.

**fsync** is a program's request: "write my data to the disk now and tell me
when it is safe". A database rewrites the same small pages again and again and
calls fsync each time. With copy-on-write, this makes the file very fragmented
and slow.

**NOCOW** (the file attribute `+C`) turns copy-on-write off for a file: it is
written in place.

**Why here:** Docker volumes on this PC hold **disposable test data**
(databases of test containers). Speed matters more than protection for them.

**Trade-offs (accepted for this data):**

| You lose | Meaning |
|---|---|
| Checksums | btrfs cannot detect if the data is damaged. Scrub cannot check or repair it. |
| Compression | the data takes its full size on disk. |
| Pure in-place writes after a snapshot | if a snapshot includes a NOCOW file, the next write to each part of that file is copy-on-write once (so the snapshot keeps the old data). |

**After a degraded RAID1 episode** (one disk was missing for a while),
**recreate these volumes; do not trust them.** The reason: one disk missed some
writes. Without checksums, btrfs cannot tell which of the two copies is the
correct one. To recreate a volume, remove it and start the containers again,
for example:

```
docker compose down -v          # in a compose project: removes its volumes
docker volume rm <name>         # or one volume by name
```

**How:**

- `install` sets `+C` on the volumes folder (usually `/var/lib/docker/volumes`,
  or the `data-root` from `/etc/docker/daemon.json`). New volumes get it
  automatically.
- `install` also sets `+C` at once on the folders and empty files inside
  existing volumes.
- `+C` only works on **empty** files. Files that already hold data must be
  copied to become NOCOW. That cannot happen while containers use them, so for
  those `install` only leaves a flag file `/etc/btrfs-setup/docker-nocow-pending`.
  It also resets the counter of attempts (see below).
- At the **next boot**, `docker-volumes-nocow.service` runs **before Docker
  starts**. For each volume with copy-on-write data it makes a full NOCOW copy
  (the empty copy folder first gets the volume folder's SELinux label with
  `chcon --reference`; if that fails while the volume has a label, the copy is
  refused before anything is copied; without labels, SELinux disabled, the
  failure changes nothing),
  checks that the copy has the same number of entries and bytes and the same
  SELinux label on every entry (`cp -a` drops a label it cannot set without a
  word, and a container cannot open a file whose label changed), swaps the
  names and deletes the old copy. It skips a volume if free space is less than its size
  plus 20 GiB. One failing volume does not stop the others.
- It copies only when systemd answers that `docker.service` is stopped
  (`inactive` or `failed`; at boot Docker's start job waits for this service,
  so Docker reads `inactive` then). Any other answer, an error, or no
  `systemctl` at all postpones the migration to the next boot, with the reason
  in the journal. A postponed boot does not count as an attempt.
- It also postpones while a `dockerd` outside `docker.service` (started by
  hand, or by another unit) uses the same volumes folder. It finds such a
  daemon in `/proc` by its name or program file, reads its `--data-root` (or
  the `data-root` of its config file, or the default `/var/lib/docker`) in the
  daemon's own view (`/proc/PID/root`), and compares that folder with the
  volumes folder by device and inode. So a Docker inside a container, which
  keeps its data elsewhere, does not count; a container that bind-mounts this
  data root does. A daemon that does not run as root does not count either,
  because it cannot write to the root-owned data folder. That is how a
  rootless Docker is left out: it runs a plain `dockerd` as your user, keeps
  its data in your home folder, and sees this machine's `/`, so by its path
  alone it would look like the system Docker. A daemon whose user or data
  folder cannot be read counts. One limit: an absolute symbolic link on a containerised
  daemon's data path is followed from this machine's `/`, not the
  container's, so that one case can be judged wrongly.
- A postponed boot sends no alert, so it is recorded instead, when there was
  work to do (a scheduled migration, or a volumes folder without `+C`):
  `/var/lib/docker-volumes-nocow/postponed` keeps the latest reason, the time
  and how many boots were held back (counted once per boot; written to disk
  with fsync, like the count of attempts). `status` shows
  it as `migration postponed at N boot(s) in a row, last ...: <reason>`, and
  `cst doctor` reports it (`WARN` from two boots on; see
  [../doctor/README.md, section 6.2](../doctor/README.md#62-docker-volumes-nocow-migration-postponed)).
  The first boot that is not held back removes the record, and so does a
  boot that finds no volumes folder (Docker removed, `data-root` moved). To run the
  migration by hand once nothing uses the volumes:
  `sudo systemctl stop docker.socket docker.service`, then
  `sudo systemctl start docker-volumes-nocow.service`, and when it is done
  start Docker again with `sudo systemctl start docker.socket docker.service`.
- If the migration does not finish after two boots, it gives up with the
  message `Docker volumes: NOCOW migration gave up`. The count of boots is
  written to disk (fsync) before each rewrite starts, so a boot that hangs or
  is reset during the rewrite still counts. This runs early at boot,
  before anyone has logged in, so the desktop alert is **kept and shown at
  your next login** (section 4.9). It is also in the journal
  (`journalctl -u docker-volumes-nocow`). Run `install` again to retry.
- An interrupted copy (power loss) is cleaned up at the next boot; the original
  is kept.
- This service runs at every boot. So if Docker is installed after `install`,
  its volumes folder is also marked, and its volumes are migrated at that
  boot. A volumes folder without `+C` is the only sign of those volumes, so
  the service first writes the flag file (it creates `/etc/btrfs-setup` if
  needed), flushes it to disk (the data folder may be on another filesystem
  than `/etc`, and a power loss must not keep `+C` and lose the flag), and
  only then sets `+C`. If it cannot write or flush the flag, it leaves the
  folder without `+C` and logs why, so the next boot tries again.
- Older copies of the helper set `+C` first. On a machine without
  `/etc/btrfs-setup` (install creates it only when it finds copy-on-write
  data; the snapshots setup creates it too), writing the flag then failed,
  and the request was lost. Such a machine shows `migration not scheduled`
  and stays that way. Run `cst btrfs install` once: its `mark` finds the
  copy-on-write data, install creates the folder and the flag, and the next
  boot migrates the volumes (the same `install` also puts the fixed helper in
  place).

Note: `cst cpu docker-slice install` also edits
`/etc/docker/daemon.json` (it adds `cgroup-parent`, see
[../cpu/README.md, section 9](../cpu/README.md#9-optional-docker-in-buildsslice-docker-slice)).
The NOCOW part only reads `data-root` from that file, so the two do not get in
each other's way.

**Check the result** with `status` after the first boot that follows an
`install`. The state is shown under `docker volumes (NOCOW)`. If you see
`N with copy-on-write data (...), migration not scheduled`, the migration gave
up or was never scheduled: see section 13.

---

## 5. `status`: everything at a glance

```
cst btrfs status
```

Read-only. Sections:

| Section | What you see |
|---|---|
| `services and timers` | next/last runs of the timers; whether `btrfs-stall-monitor.service` and `below.service` are active |
| `last health check` | the last result lines of the health check |
| `last scrub` | start, status, duration, error summary |
| `weekly scrub speed limit (per device)` | `SCRUB_LIMIT` (section 4.3), the limit the scrub service sets (`DIFFERS from the setting (run install)` when they do not match; `INVALID` with the line for a bad setting), and `scrub_speed_max` of each device in sysfs now (0 = no limit) |
| `last fstrim` | the last trim results |
| `docker volumes (NOCOW)` | how many volumes are fully NOCOW, and if a migration is pending (`at the next boot`) or `not scheduled`; `migration postponed at N boot(s) in a row` with the reason when boots had to wait (section 4.10) |
| `docker-gc` | last cleanup, or `off` |
| `space and automatic reclaim` | unallocated space per disk, data/metadata use, reclaim switches and counters |
| `mount options and writeback limits` | each of `/sysroot`, `/var`, `/var/home` must carry `compress=zstd:1 flushoncommit noatime discard=async degraded space_cache=v2 ssd`; `vm.dirty_*` must match the file |
| `degraded boot` | the summary of the degraded-boot part |
| `alerts raised while nobody was logged in` | alerts of the last 7 days that no desktop could show when they happened (section 4.9), with time, program and title. Each line says whether **you** (the user who ran the script) have seen it: `[shown to you]`, `[NOT shown to you yet: next login]`, or `[only counted in the summary at login; text not shown]` (when many alerts waited). Lines marked `[shown to you]` need nothing from you; they stay listed until they expire. `none` if there were none. `shown at login: NOT installed` means the login part is missing: run `install` |
| `snapshots` | the snapshot summary |
| `Bulk (re-downloadable data: ...)` | the output of `bulk status` (section 10.1): `~/Bulk` is a subvolume, `CACHEDIR.TAG`, each link, the `+C` state of the emulator folders (`~/Bulk/emulators`), items waiting for the next boot, game data still in the old Steam library (with the command that moves it: `bulk steam plan`), an interrupted Steam game move |
| `stalls, last hour` | the verdict lines of `report --since '1 hour ago' --no-below` |

At the end: the size of the recorded data and the btrfs features.

**Example** (two sections, from this PC; the disk lines are shown as
`<...>` here because they were not captured for this README):

```
== space and automatic reclaim
  <disk>       size <N> GiB, unallocated <N> GiB (<N>%)       <- one line per disk
  data        727.0 of   816.0 GiB allocated in chunks used (89%)
  metadata     13.9 of    15.0 GiB allocated in chunks used (93%)
  automatic reclaim (data): dynamic=1 periodic=1, reclaimed 0 block group(s), errors 0

== mount options and writeback limits
  /sysroot   ok (compress=zstd:1 flushoncommit noatime discard=async degraded space_cache=v2 ssd)
  /var       ok (compress=zstd:1 flushoncommit noatime discard=async degraded space_cache=v2 ssd)
  /var/home  ok (compress=zstd:1 flushoncommit noatime discard=async degraded space_cache=v2 ssd)
  vm.dirty_bytes             2147483648 ok
  vm.dirty_background_bytes  536870912 ok
```

How to read it:

- The `<disk>` lines: the **unallocated** value is the one that matters (see
  4.5). The health check warns below 50 GiB.
- `data ... (89%)`: 89% of the space reserved for data is really used. A high
  value is normal.
- `metadata ... (93%)`: the same for metadata. A high value is fine as long as
  there is enough unallocated space for btrfs to reserve more.
- `dynamic=1 periodic=1`: automatic reclaim is on. `errors` should be 0.
- `ok` after each mount: all expected mount options are there. Otherwise you see
  `MISSING:` and the names.
- `ok` after `vm.dirty_*`: the live values match the file. Otherwise `DRIFT`.

---

## 6. `uninstall`: how to undo

```
cst btrfs uninstall
```

It does not ask for confirmation; it starts at once. It stops and removes:

- the health-check and scrub timers, the stall monitor and its alert service.
  The last run that systemd keeps for a timer (`/var/lib/systemd/timers/stamp-*`)
  goes too: `systemctl clean --what=state` while the timer is still loaded. So
  does the stamp of `btrfs-scrub-monthly.timer`, a timer of older versions (that
  one also at `install`). Each unit is disabled on its own (as by `docker-gc
  disable` and `snapshots disable`): a unit that is not there is skipped
  quietly, and one that fails to stop is named in a `WARNING:` line while the
  others still go;
- the scrub's speed-limit drop-in (`btrfs-scrub-weekly.service.d/10-cst-limit.conf`,
  section 4.3); `SCRUB_LIMIT` stays in `snapshots.conf`;
- the installed scripts in `/usr/local/sbin` and `/usr/local/bin`, and the
  `~/.local/bin/btrfs-stall-report` launcher;
- the below drop-in (also `10-f44-tools.conf`, its name in versions called
  f44-tools), and it disables `below.service`;
- the writeback file. Then it restarts `tuned` so its own values come back. If
  tuned is not running, the old values come back at the next boot;
- the reclaim file, **only if this script created it**. The switches stay on in
  the running kernel until the next reboot;
- the Docker NOCOW service and its record of postponed boots. The `+C` flag is
  removed from the volumes folder, so **new** volumes are copy-on-write again. **Existing** volumes (their files and
  their folders) stay NOCOW, so new files inside them are still NOCOW;
- `/var/lib/btrfs-health-check`;
- the alerts kept for the next login (section 4.9): the user unit
  `cst-pending-alerts.service`, `/usr/local/bin/cst-pending-alerts` and the
  folder `/var/lib/cst-alerts/`, and the same parts under their old names
  (`f44-pending-alerts`, `/var/lib/f44-alerts/`) if they are still there. Your
  own record in `~/.local/state/cst-alerts/` (or `~/.local/state/f44-alerts/`)
  stays (it is tiny); delete it by hand if you want.

It keeps (and tells you so):

- the recorded data: `/var/log/btrfs-stall-monitor` and `/var/log/below`;
- the layered `below` package;
- `fstrim.timer` (it is Fedora's);
- `btrfs-notify` and the alerts kept for the next login, if snapshots are set
  up (they still use them). Those are brought to the current names first,
  like `install` does (section 4.9);
- snapshots, the NAS backup, docker-gc, the pruning of cargo build folders
  (`cst toolchains`, section 10.2) and the degraded-boot part. Each has its own
  command to stop or remove it;
- `~/Bulk` and its links (plain folders and symlinks; how to undo one link:
  [SNAPSHOTS.md, section 12](SNAPSHOTS.md#undo-one-bulk-link));
- the link `~/.local/bin/cst` (the other modules use it too; `rm` it by hand).

To remove the data and the package too:

```
sudo rm -rf /var/log/btrfs-stall-monitor /var/log/below
sudo rpm-ostree uninstall below
```

`rpm-ostree uninstall` prepares a new system image. The package is removed at
the **next reboot**.

---

## 7. `report`: was storage the bottleneck?

### 7.1 When to use it

Use it when a build, a test run or a container was slow, timed out, or the
desktop froze. You do **not** need to prepare anything: the monitor records all
the time. Just ask afterwards.

```
cst btrfs report --since '10:30' --until '10:45'
# or, after install, directly (also inside a toolbox):
btrfs-stall-report --since '20 min ago'
```

`cst btrfs report` uses the copy in this repository. The command
`btrfs-stall-report` uses the installed copy. Your user must be in the `wheel`
group to read the data (check with `id -nG`).

### 7.2 Options

| Option | Meaning |
|---|---|
| `--since`, `-s` | start of the window. Anything the command `date -d` understands: `'20 min ago'`, `10:30`, or `@<epoch>` (seconds since 1970, as printed by `date +%s`). Default: `30 min ago`. |
| `--until`, `-u` | end of the window. Same formats. Default: now. |
| `--no-below` | skip below's "who wrote/read" part (much faster). |
| `-v`, `--verbose` | more episodes, the dm-crypt rows, example kernel stacks. |
| `--json` | machine-readable output; verdict and coverage first. |

### 7.3 Choosing the window

- Start about **1 minute before** the slow part began.
- Keep the window **short**. Percentages are averaged over the whole window, so a
  long window hides a short stall.
- Data reaches the disk about every 10 s. Ask **15 s or more after** the slow
  part ended.

### 7.4 How to read it

**An example first.** This is real output from this PC, shortened (`...`
marks removed text). Two minutes, with some fsync-heavy desktop apps:

```
$ btrfs-stall-report --since '13:59' --until '14:01' --no-below
window   2026-10-13 13:59:00 -> 14:01:00  kernel 7.2.9-200.fc44.x86_64     (1)
recorded 13:58:55 -> 14:00:57 (121 s of 120 s)                             (2)

verdict  Blocked tasks: up to 3 tasks waiting on storage in D state (...)  (3)
verdict  Where blocked tasks waited (share of sampled D-state tasks): fsync log-tree sync (btrfs_sync_log) 86%, ...   (4)
verdict  fsync-heavy workload: every fsync writes the btrfs log tree and flushes the disk cache of both RAID1 members (...). The lever is fsync frequency in the application, ...   (5)
verdict  Worst episode: 13:59:30-13:59:57 (26 s, 3 D tasks, commit 101 ms).   (6)
verdict  btrfs disks NOT saturated: dm-crypt layer (what btrfs waits for) await 0.9 ms (...), queue depth 0.5; NVMe below await 0.6 ms (...), information only (...).   (7)

pressure io some/full 1.4% / 1.3%   memory some/full 0.0% / 0.0%   cpu some 0.4%   (% of recorded time)   (8)
btrfs    fedora: 343 commits, avg 14 ms, longest 101 ms @13:59:32; ... new device errors 0   (9)
disks    per device; each RAID1 member carries a full copy, dm-N is the dm-crypt layer above it
  nvme0n1  btrfs:fedora  await   0.6 ms (r ... / w ...)  flush await   3.2 ms (3264 flushes)  queue  0.5  ...   (10)
...
episodes 1 (worst first)
  13:59:30-13:59:57   26s  opened by: 3 tasks waiting on storage; ...       (11)
        5 task-samples [log-sync] barrier_all_devices via fdatasync  comms: tokio-rt-worker(4), ...   (12)
```

1. The time window you asked for.
2. How much of it was recorded. Here: all of it.
3. Storage **was** a reason for waiting: up to 3 tasks waited at the same time.
4. What they waited for: mostly fsync (category `log-sync`, see below).
5. A hint for that category. Here: the programs call fsync very often; the
   programs must change, not the disk settings.
6. The worst moment: 26 s long.
7. The disks were not at their limit (low `await`, short queue at the
   dm-crypt layer, which is what btrfs waits for). So the disks were not slow;
   there were simply many small fsync calls. The `(...)` parts give read and
   write `await` apart.
8. How much of the time tasks waited on IO, memory or CPU (see `pressure` below).
9. btrfs commits: the longest took 101 ms, which is short. No new disk errors.
10. One disk: average time per request (`await`, then read `r` and write `w`
    apart) and the queue length.
11. The episode and why it opened.
12. Which programs waited (`comms`) and on what (`[log-sync]`).

**Read the lines that start with `verdict` first.**

| Verdict line starts with | Meaning | What to do |
|---|---|---|
| `NO DATA for this window` | nothing was recorded: the monitor was not running, or your user cannot read the data | see section 13, "`report` says `NO DATA`" |
| `INCOMPLETE DATA` | part of the window is not recorded yet, or there was a gap | wait a little and run again, or narrow the window. Do not conclude anything yet. |
| `KERNEL/FS PROBLEMS` | a stuck task, a btrfs maintenance task stuck for 15 min, a btrfs error or a disk error was recorded | read the `events` lines marked `!` |
| `btrfs device error counters INCREASED in this window.` | a disk reported new read, write or checksum errors | treat it like a health-check alert (section 13, `non-zero device error counters`) |
| `Storage was NOT a bottleneck` | the disks were not the reason | look at CPU, memory, the network or the program itself. Extra lines may say `CPU contention` or `Memory pressure`. For CPU sharing between games, builds, Docker and the VM, see [../cpu/README.md](../cpu/README.md). |
| `Little activity recorded: check that the window really covers the workload.` | almost nothing happened in this window | the window is probably wrong; check the times |
| `IO wait`, `Blocked tasks`, `Slow btrfs commits` | storage **was** a bottleneck. `Blocked tasks` counts only tasks waiting on storage (not memory, network shares or maintenance) | read the next line, `Where blocked tasks waited`, and the hint after it |
| `Memory, not storage, blocked up to N tasks (...)` | tasks waited on memory: reclaim, reading pages back from swap or zswap, or a page fault or `mmap` call waiting for the program's memory map lock. It can follow `Storage was NOT a bottleneck`. With `estimated from the stacks` the window was recorded by a daemon before version 8, which counted these waits as storage; the report estimates the split from the stacks of each episode | the cause is memory, not the disks: see the `memory` line, `cst swap status` and [../swap/README.md](../swap/README.md) |
| `Memory waits: up to N tasks also waited on memory ...` | some tasks waited on memory, fewer than on storage | read the storage lines first |
| `Blocked tasks: also up to N tasks waiting on storage ...` | printed after the `Memory, not storage` line when storage waits came at the same time, but fewer | memory came first; check storage only if the number is large |
| `Episode still open: since ...` | the monitor is in a stall right now (it samples every second) and 3 or more tasks wait on storage, but the episode has no record yet: the monitor writes it, with the stacks, when the episode ends. The `Blocked tasks` number comes from the samples | wait until it ends, then run the report again for the categories and stacks |
| `Worst episode: ...` | the time of the worst stall moment | narrow the window to it if you want more detail |
| `btrfs disks look SATURATED (...)` | the disks were at their limit: at the dm-crypt layer, which is what btrfs waits for, requests took 5 ms or more on average (`await`), or the queue was 8 or longer. On a PC without dm-crypt the member disks are judged instead | the workload wrote or read more than the disks can do; reduce or spread it |
| `btrfs disks NOT saturated (...)` | the disks had capacity left | the waiting came from btrfs or the program (for example many fsync calls), not from slow disks |
| `Swap-out of ...: memory pressure also loaded the swap disk.` | memory was short, so the kernel moved memory to swap | the cause is memory, not the filesystem; `cst swap status` shows which cgroup swaps and how much comes back from disk ([../swap/README.md](../swap/README.md#6-reading-the-status)) |
| `FUSE waits` | a program waited on a network share or the document portal | usually the network, not the disks |
| `Busy waits` | a program kept the disk busy (for example many fsync calls), but it progressed | the program must change; it is a slow disk only if the disk lines show high `await` or errors |
| `Maintenance` | trim, scrub or balance ran | normal background work; it matters only if storage waits appear at the same time |
| `Caveat: the daemon could not read kernel stacks ...` | the monitor could not see *what* tasks waited for | the categories are rough; check `journalctl -u btrfs-stall-monitor` |
| `Matches the btrfs zstd lost-wakeup bug fixed in 7.2.6 ...` | waits for compression on an older kernel | update the kernel |

The other lines:

- `pressure`: how much of the time tasks waited on IO, memory or CPU. **PSI**
  (pressure stall information) is the kernel's own measure of this.
- `btrfs`: number of commits, average and longest commit time.
- `disks`: per disk, `await` (average time per request; `r` and `w` give reads
  and writes apart) and `queue` (how many requests were waiting). `dm-N` is the
  encryption layer above each NVMe. The saturation verdict uses the `dm-N`
  numbers: that is what btrfs waits for. The NVMe numbers below it are shown
  for information only. On this PC the NVMe write `await` of the btrfs disks
  is higher than the `dm-N` one above it, even when the PC is idle (6 to 8 ms
  against about 1 ms); its time can include disk cache flushes, so do not
  read it as the time btrfs waited.
- `memory`: `zswap in/out` is what was decompressed from and compressed into
  the zswap pool (in RAM, no disk IO, but CPU time and locks), and
  `disk swap in/out` is what was read from and written to the swap disk.
  Disk swap-out already includes what zswap wrote back to the disk. Only
  swap-out that bypassed zswap gets its own figure: `(N of it not through
  zswap)`. Then direct reclaim (memory taken back by the programs that asked
  for it) and allocation stalls. With `n/a (daemon < v7)` the window was
  recorded by a daemon that could not read the zswap counters.
- `cgroups`: which service or container waited and wrote.
- `below top writers/readers`: which processes did the IO.
- `episodes`: the worst stall moments. Each line shows the `[category]` of the
  wait. "task-samples" means roughly "seconds that one task spent waiting".
- `events`: kernel and monitor events. A `!` means severe, on these disks.
- `alert`: alerts the monitor sent in the window.

**Categories** (what blocked tasks were waiting for):

| Category | Plain meaning | Should I worry? |
|---|---|---|
| `commit` | btrfs making its changes permanent. With `flushoncommit` this also writes all dirty data. | Only if commits take seconds. The report proposes options; you decide. |
| `log-sync` | an fsync: btrfs writes a small log and flushes the cache of **both** disks. Many fsync calls cost a lot. | Not a disk problem. The program must call fsync less often. |
| `dirty-throttle` | writers slowed down by the `vm.dirty_bytes` limit | No. Expected under heavy writing; this is the limit doing its job. |
| `writeback` | waiting for one's own data to reach the disk | Usually no; it comes with fsync. |
| `flush` | disk cache flushes (on both RAID1 disks) | Usually no; it comes with fsync and commits. |
| `space` | btrfs had to free or reserve space | Yes, if it is frequent: check unallocated space in `status`. |
| `metadata-read` | reading the file index from disk (something walked many files) | No; a search or a big checkout. |
| `btrfs-locks` | many writers in the same place inside btrfs | Only if frequent; many writers in one folder. |
| `device` | requests queued at the NVMe or encryption layer | Yes, if the disk lines also show high `await`. |
| `wbt` | Linux slowed background writes so that reads stay fast | No; normal under heavy writing. |
| `overlayfs` | containers copying big files from image layers | No; container start-up. |
| `zstd-workspace` | waiting for a free compression work area | See the verdict line about the kernel version. |
| `discard` | trim in the background | No; usually harmless. |
| `page-read` | reading file data that was not in memory | No. |
| `other`, `no-stack` | other kernel waits; unknown (no kernel stack captured) | Only if they are large. |

Four categories are **not counted as storage**:

| Category | Plain meaning |
|---|---|
| `reclaim` | waiting on memory: memory reclaim, reading a page back from swap or zswap, or a page fault or `mmap` call waiting for the program's memory map lock. The cause is memory, not the disks; see `cst swap status`. In logs of a daemon before version 8 these waits were counted as storage; the report takes them out again (an estimate from the stacks of each episode). |
| `fuse` | waiting on a **FUSE** filesystem: a filesystem served by a normal program, here gvfs network shares (`/run/user/<uid>/gvfs`) and the document portal. The cause is usually the network. A typical case is a `find` that walked into a share. |
| `idle` | programs waiting for each other or for events (for example for a keyboard, USB or the graphics card), not for the disk |
| `maintenance` | trim, scrub, balance: background work that may wait minutes on purpose |

Note: the verdict line `Maintenance` and the category `maintenance` mean the
same thing. Only the capital letter differs.

**JSON keys** (`--json`) added with these lines: `peak.mem_tasks` (most tasks
waiting on memory at once), `sample_d_tasks` (most tasks waiting on storage in
one sample), `open_episode` (`null`, or `since`, `last` and `d_tasks` of an
episode without a record yet), `vm.zswap_in_bytes`, `vm.zswap_out_bytes` and
`vm.swap_out_bypass_bytes` (`null` when no sample came from a daemon version 7
or later), and `read_await_ms` / `write_await_ms` per disk. Each episode
has `max_d_mem` (most tasks waiting on memory at once); episodes of a daemon
before version 8 whose numbers were corrected carry `"estimated": true`. The `units` block of the JSON output explains them too.

### 7.5 `watch`: label a run for comparisons

`watch` is optional. It runs a command, remembers its exact time window, and
saves the report. This helps to compare "before" and "after" a change (kernel
update, mount option, sysctl).

```
cst btrfs watch -l 'cargo test, new kernel' -- cargo test
cst btrfs watch -l idle --duration 60
cst btrfs watch --list
```

| Option | Meaning |
|---|---|
| `-l`, `--label` | a name for this run |
| `-- <command>` | the command to run and measure |
| `-d`, `--duration N` | record N seconds **without** running a command. Useful to measure a quiet PC as a baseline (the second example). |
| `--list` | show past runs: label, window, exit code |

Results go to `~/.local/state/btrfs-stall-watch/<timestamp>/` (`workload.log`,
`report.txt`, `report.json`), with an index in `runs.tsv`. `watch` needs the
installed `btrfs-stall-report` command.

The recorder writes to disk about every 10 s, so after the command ends
`watch` waits (usually 5 to 15 s) until the recording has a sample at or after
the end. The saved report's window runs from the start of the command to just
after the newest sample it saw then, which can be a few seconds past the end.
If no such sample comes within about 60 s (the recorder is not running), it
saves the report of the command's own window (NO DATA when the recorder took
no sample during it either).

---

## 8. `degraded-boot`: start the PC with one disk dead

```
cst btrfs degraded-boot            # install or update
cst btrfs degraded-boot plan       # what install would change; changes nothing
cst btrfs degraded-boot status
cst btrfs degraded-boot uninstall
cst btrfs degraded-boot files-only # for development only, see below
```

**Why:** the `degraded` mount option is not enough to boot with a dead disk.
Fedora's udev rule waits until **all** disks of a multi-disk btrfs are present.
(**udev** is the part of Linux that reacts when devices appear; its rules decide
when a disk is "ready".) With one NVMe dead, the boot waits forever and ends in
"Could not boot".

**What you will see if a disk dies** (with this part installed):

1. You type the passphrase of the disk that still works.
2. The boot waits about **45 s**.
3. The system starts normally, and you get the desktop.
4. A critical alert `Disk health: problems detected` appears. Its text says
   `this boot is DEGRADED`. Follow section 13.

**What it does:**

- It adds a udev rule `/etc/udev/rules.d/65-btrfs-last-resort.rules`, a timer, a
  service and a helper script. It puts them into the initramfs with
  `rpm-ostree initramfs-etc --track`.
- It also adds `btrfs-degraded-boot-record.service`, which runs in the real
  system (not in the initramfs) after a degraded boot.
- When the first disk of the root filesystem appears, a **45 s** timer starts.
  If the filesystem is still incomplete after that, the helper checks that no
  passphrase prompt is waiting and that the missing disk is really absent or
  unreadable. **A disk that asks for a passphrase, or where you typed it wrong,
  never counts as dead**: the prompt comes back.
- Only then it allows the degraded mount, logs a critical message, and the boot
  continues.
- After boot, `btrfs-degraded-boot-record.service` writes a persistent marker to
  `/var/lib/btrfs-degraded-boot/`. The health check alerts until RAID1 is
  restored **and** a clean scrub started after that has finished.

**Emergency option `btrfs_last_resort.force=1`.** Use it only if a disk is
physically there and asks for its passphrase, but the correct passphrase no
longer works (its encryption keys are damaged). Then the helper treats that
disk as dead. To use it once:

1. At power-on, in the GRUB boot menu, select the first entry and press `e`.
2. Find the line that starts with `linux`. Go to its end and add
   ` btrfs_last_resort.force=1`.
3. Press `Ctrl+X` (or `F10`) to boot. The change is not saved; the next boot is
   normal again.

**Why it is separate from `install`:** it changes the **boot path**. A mistake
there can stop the PC from booting. So it is its own explicit command, with its
own checks:

- It does not touch `/etc/fstab`, `/etc/crypttab`, kernel arguments (the
  settings given to the kernel at boot) or the filesystem.
- It refuses to run unless all of this is true:
  - the root is a multi-disk btrfs on rpm-ostree;
  - the next boot's `root=` is `UUID=<uuid of the root filesystem>`;
  - `degraded` is in the next boot's `rootflags` (the mount options for `/`,
    given on the kernel command line);
  - Fedora's own rule `64-btrfs.rules` still works the way this part expects.
    If a Fedora update changed it, it stops with `UPSTREAM CHANGED: ...` and
    changes nothing.
- It checks the new udev rule with `udevadm verify` before it writes
  anything: a rule that fails leaves nothing written.
- It writes its files, then checks its units with `systemd-analyze verify`:
  a problem such as a typo (an unknown key) stops the install there, with
  the files written and the initramfs **not** changed.
- Then it runs a self-test: every disk must show as ready, and `lsblk` must
  list as many disks of the root filesystem as btrfs counts (none may go
  unchecked). If the self-test fails, a **newly written** rule is removed
  again and the initramfs is **not** changed. The other helper files stay in
  `/etc`; without the rule they do nothing.
- When the initramfs content must change, install and uninstall create a new
  **deployment** (a new version of the system image, used from the next boot).
  The change takes effect at the **next boot**. Running install again with
  nothing changed does not create one.
- `uninstall` refuses while the current boot is degraded.

`plan` runs the refusals above, `udevadm verify` on the rule and a syntax
check of the helper, and prints `would write:` or `unchanged:` for each file
and `would run: ...` for each change install would make (`rpm-ostree
initramfs-etc`, `systemctl enable` when the record unit is not enabled, the
removal of files an older version left). It writes no file, reloads nothing
and creates no deployment. Two checks need the new files in place, so only
install runs them: `systemd-analyze verify` on the units, and the self-test
(`plan` says how many disks it would check). Install also reloads systemd
and udev on every run; `plan` does not list that.

`status` ends with `verdict: OK - an NVMe failure at boot would mount degraded
after 45 s and alert` when everything is in place.

**To undo:** `cst btrfs degraded-boot uninstall`, then reboot. It
keeps `/var/lib/btrfs-degraded-boot` if markers of a past degraded boot are
there (it tells you so); remove them once RAID1 is restored.

**`files-only`** writes the files and runs the self-test, but does not change
the initramfs. It is for testing changes to this part. Normal users do not need
it.

---

## 9. `crypt-perf`: faster encryption path

```
cst btrfs crypt-perf           # status (default)
cst btrfs crypt-perf enable
cst btrfs crypt-perf disable
```

**Why:** by default dm-crypt sends each read and write through extra kernel
work queues (`kcryptd`). On fast NVMe this adds waiting. Measured here: dm-crypt
added about 0.5 ms on top of the NVMe time.

**What `enable` does:** it sets the dm-crypt flags `no_read_workqueue` and
`no_write_workqueue` on each encrypted disk of the btrfs filesystem. Then
encryption and decryption happen directly, without the queues. Other flags that
were already set (for example `allow_discards`) are kept.

- It asks the **LUKS passphrase of each disk**. A wrong passphrase leaves that
  disk unchanged.
- It is applied at once and saved in the LUKS2 header (`--persistent`), so it
  also holds at the next boot. No crypttab change.
- Applying it pauses each disk for an instant. Best done while the PC is idle.
- It refuses if not every btrfs disk is present and encrypted (so no disk is left
  without the flags). It needs a terminal.
- If only some disks succeeded, run the same command again.

**To undo:** `cst btrfs crypt-perf disable`. It removes the two flags
the same way and also asks the passphrase of each disk.

To compare before and after, look at the `disks` lines (NVMe vs `dm-N` await)
with `btrfs-stall-report --since '<time>' -v` on a similar workload.

---

## 10. `snapshots` (summary)

A **subvolume** is a part of a btrfs filesystem that behaves like a separate
folder tree and can be snapshotted on its own. A **snapshot** is a frozen,
read-only copy of a subvolume at one moment. It costs almost no space at first,
because it shares the data with the original.

`snapshots setup` (run once) takes hourly read-only snapshots of `/home` and of
`~/Workspace`. Before that it keeps big data that can be downloaded again out
of them:

- it fills **`~/Bulk`** (section 10.1): IDE programs, `~/Programs`, the Android
  SDK and emulator disks, AI models, ISOs, a Steam library and toolchain
  caches;
- it turns a few folders that must stay where they are into their own
  subvolumes: `~/.cache` (with `~/.cache/uv` deleted first), Flatpak apps
  (`~/.local/share/flatpak`), `~/Workspace` (snapshotted on its own) and
  `~/Bulk`;
- it tells Chrome (Flatpak) to keep its web cache in RAM, inside the app's
  sandbox, with a 512 MiB limit. That cache is then in no snapshot, without
  one more subvolume ([SNAPSHOTS.md, section 3](SNAPSHOTS.md#chrome-flatpak-its-cache-in-ram-not-a-subvolume)).

Cache folders, marked by a `CACHEDIR.TAG` file (every cargo `target/`,
`.flatpak-builder`, ...), are dropped from each new snapshot: the snapshot is taken
writable, those folders are deleted in it, then it is made read-only. Your
own folders are not touched, and no subvolume is made for them. The NAS
backup left them out already. `snapshots caches` lists them;
`SNAPSHOT_EXCLUDE_CACHES=no` in `/etc/btrfs-setup/snapshots.conf` turns it
off ([SNAPSHOTS.md, section 3](SNAPSHOTS.md#cache-folders-cachedirtag-dropped-from-every-new-snapshot)).

Folders that are in use are handled at the next boot, before the login. The
exact list is in [SNAPSHOTS.md, section 3](SNAPSHOTS.md#3-which-folders-are-left-out-of-home-snapshots-and-why).

`setup` also changes things outside the snapshots. It shows them and asks
before it acts:

- it deletes `~/.cache/uv` (a cache, about 157 GiB on this PC when the script
  was written; uv downloads again what it needs);
- it deletes old cargo `target/` folders in `~/Workspace` (skipped while a Rust
  build, rust-analyzer or RustRover is running);
- through the `toolchains` module, as your user: it adds `build-dir` to
  `~/.cargo/config.toml` and `link-mode = "clone"` to `~/.config/uv/uv.toml`,
  and installs and starts a small user timer that deletes old cargo build
  folders in `~/.cache/cargo-build` (section 10.2);
- it adds two lines (and a comment) to Chrome's
  `~/.var/app/com.google.Chrome/config/chrome-flags.conf`, only if Chrome's
  Flatpak is installed and you did not set a cache folder or size yourself.

Default retention:

- `/home`: `72h 7d 4w 3m` = keep 72 hourly, 7 daily, 4 weekly and 3 monthly
  snapshots.
- `~/Workspace`: `48h 7d` = keep 48 hourly and 7 daily snapshots.

Space guard, based on the free space of the btrfs filesystem: at 250 GiB free or
less, no new snapshots are taken. At 150 GiB or less, an emergency purge
deletes older snapshots (the newest ones and the last 24 hours are kept).
There is an optional daily backup to the NAS with **restic** (a backup program).

Commands: `snapshots setup [--yes]`, `status`, `list [--json]`,
`caches [--json]`, `prune [--confirm]`, `clean-targets [--apply]`, `disable [--purge]`,
`nas setup --path DIR ...`, `nas status|disable|run`,
`chrome-cache [status|apply|off]`.

**All details, and how to restore files: [SNAPSHOTS.md](SNAPSHOTS.md).**

### 10.1 `bulk`: one folder for everything you can download again

```
cst btrfs bulk plan      # dry run: each item and what would happen
cst btrfs bulk apply     # do it (safe to run again)
cst btrfs bulk status    # the state of each item
cst btrfs bulk keep ID   # leave one item in /home on purpose (unkeep ID undoes it)
cst btrfs bulk steam plan    # dry run: which Steam games would move into ~/Bulk/steam
cst btrfs bulk steam apply   # move them (Steam must be closed; --app ID for one game)
```

**The rule: everything in `~/Bulk` can be downloaded (or built) again, so it
gets no snapshots and no NAS backup.** `~/Bulk` is one nested subvolume, owned
by you, with a `CACHEDIR.TAG` file (a standard "this is a cache" marker that
backup programs respect). It looks like this:

```
~/Bulk/
  CACHEDIR.TAG
  apps/              <- ~/Programs (a symlink now)
    jetbrains/       <- ~/.local/share/JetBrains/Toolbox/apps
  models/            <- LM Studio's models folder (Flatpak)
  iso/               <- ~/Downloads/iso
  steam/             a Steam library: `bulk steam apply` moves your games here (no link)
  emulators/         emulator disks: the only NOCOW (+C) part of ~/Bulk
    android/avd/            <- ~/.android/avd
  toolchains/        SDKs and download caches; their settings stay in /home
    android/sdk/            <- ~/Android/Sdk
    jvm/m2/repository/      <- ~/.m2/repository
    jvm/gradle/caches/      <- ~/.gradle/caches
    jvm/gradle/wrapper/     <- ~/.gradle/wrapper
    go/pkg/mod/             <- ~/go/pkg/mod
    rust/rustup/toolchains/ <- ~/.rustup/toolchains
    rust/rustup/tmp/        <- ~/.rustup/tmp       (rustup's scratch folders: it renames
    rust/rustup/downloads/  <- ~/.rustup/downloads  from them into the toolchains)
    rust/cargo/registry/    <- ~/.cargo/registry
    rust/cargo/git/         <- ~/.cargo/git
    node/npm/_cacache/      <- ~/.npm/_cacache
```

The folders are grouped by **how the data is treated**, not by product:
`toolchains/` is all copy-on-write, `emulators/` is all `+C` (NOCOW, for
virtual disks you can throw away). That is why the Android SDK sits in
`toolchains/android/sdk` and its emulator disks in `emulators/android/avd`.
The toolchain caches are grouped as `toolchains/<language>/<tool>/` and keep
their own folder names, so each real place reads like the path it replaces
(`jvm`, not `java`: Maven and Gradle also serve Kotlin, Scala and Groovy).
The full list, with item ids, is in
[SNAPSHOTS.md, section 3](SNAPSHOTS.md#bulk-one-folder-for-everything-you-can-download-again).

What you should know:

- **Your programs keep their usual paths.** Each one is now a symlink into
  `~/Bulk`; the host, toolboxes and IDEs all see the same link. Settings and
  credentials (for example `~/.m2/settings.xml`, `~/.cargo/credentials.toml`)
  stay in /home, with their snapshot history.
- **Copy-on-write stays on**, so checksums and compression keep working, and
  RAID1 can repair a damaged copy. Only `emulators/` (emulator disks,
  rewritten in place all the time and disposable) has `+C`, like the Docker
  volumes of section 4.10, with the same trade-off: no checksums there.
  `emulators/` itself has `+C`, so a folder you add there later for another
  emulator gets it too (`+C` on a folder only affects files created in it
  afterwards).
- **Moving is safe and fast**: data is moved by a reflink clone (no data is
  written), checked, then switched; an interruption is finished or undone at
  the next boot, before login (or by the next run), and no snapshot is taken
  until then. If a program wrote on both sides, or recreated the old folder
  meanwhile, nothing is deleted and it is reported. The emulator disks need a
  real copy (for `+C`).
- **Containers** that mount only a tool's folder (`-v ~/.m2:/root/.m2`) see a
  broken link: mount the `~/Bulk` place too (details in SNAPSHOTS.md, section 3).
- **Idempotent**: what is in place is left alone; tools installed later get
  their link at the next `bulk apply`; anything unexpected (a symlink to
  somewhere else, data in both places, a file where a folder belongs) is
  shown as `NEEDS A LOOK` and **never touched**. One exception: `npm cache
  clean --force` and `go clean -modcache` delete the link itself, and the
  tool then makes a new folder in /home; for those two caches the next `bulk
  apply` deletes the stale copy in `~/Bulk` and moves the new folder in.
- **Keeping one item in /home** is supported: `bulk keep ID` (for example
  `bulk keep android-sdk`; the ids are in SNAPSHOTS.md, section 3) makes
  `bulk apply` leave it alone, and `bulk status` shows `kept in /home (by
  choice)` instead of a problem. Wrong ids are refused, with the valid ones.
- **Busy items** (a running IDE, emulator, LM Studio) are moved at the next
  boot, before the login screen (`btrfs-bulk-boot.service`), or by `bulk
  apply` once you close the program; snapshots wait for that. A bad line in
  `/etc/btrfs-setup/bulk.conf` stops that boot step with a desktop alert that
  names the line (SNAPSHOTS.md, "Nothing moved into ~/Bulk at boot"). If an item
  needs a look, `snapshots setup` says so before starting the snapshots and
  asks first (not for kept items, and not for a problem inside `~/Bulk`
  only, whose data is not in /home).
- **Steam**: close Steam, then `cst btrfs bulk steam plan` and `cst btrfs
  bulk steam apply` move your installed games into `~/Bulk/steam` (a reflink
  copy, checked, then switched; nothing is downloaded again). Before the
  first game is copied, it adds `~/Bulk/steam` as a library in Steam's two
  `libraryfolders.vdf` files (after a backup of each; an entry you already
  added in Steam is reused). Like `bulk apply`, `bulk steam apply` first
  installs the current boot step, which can finish or undo a move that a
  power loss interrupted. Tools such as the Steam Linux Runtime stay where
  they are (name one with `--app ID` to move it anyway), and so do the shader
  caches (Steam builds new ones). Anything unexpected (Steam running, a game
  in the middle of an update, a Proton prefix with data, a file in a form
  Steam does not write) is reported and nothing is touched. **NOCOW game
  data** (`+C`, for example when the whole Steam folder was made NOCOW):
  btrfs cannot reflink a NOCOW file into a copy-on-write folder, and
  `~/Bulk/steam` stays copy-on-write on purpose (checksums, so a RAID1 scrub
  can repair it, and compression). Such a game is copied byte by byte
  instead, which needs its full size free plus 170 GiB (the snapshot
  emergency limit of 150 GiB plus 20 GiB, so the copy cannot trigger the
  emergency purge of old snapshots) and takes minutes per 10 GiB; `plan`
  shows it as `copy N GiB`, with how many files have `+C` and one example
  (one `+C` file is enough to copy its whole folder), and `apply` only does
  it with `--copy`. Each copy is compared byte for byte with the original before
  the original is deleted. One step stays
  manual: make `~/Bulk/steam` the default library in Steam's settings, so new
  games go there too. `bulk status` shows how much game data is still in the
  old library (and so in `/home` snapshots).
- The changes in your home are made **as your user**, never as root.

All details (the Steam and LM Studio steps, why symlinks, why `+C` only for
the emulator disks, how to undo one link, what happens on a machine where an
older version made subvolumes): [SNAPSHOTS.md, section 3](SNAPSHOTS.md#bulk-one-folder-for-everything-you-can-download-again)
and [section 14](SNAPSHOTS.md#how-an-item-moves-into-bulk) (for Steam:
[how a game moves](SNAPSHOTS.md#how-a-steam-game-moves-into-bulksteam)).

### 10.2 Toolchain caches: in `cst toolchains` now

`snapshots setup` tells cargo (the Rust build tool) to keep its intermediate
build files in `~/.cache/cargo-build`, out of the snapshots
([SNAPSHOTS.md, section 4](SNAPSHOTS.md#4-changes-to-your-cargo-and-uv-settings)).
Cargo makes one folder per project path there and never deletes them; on this
PC they once grew to 356 GiB in 3 days. Deleting the old ones, and writing the
cargo and uv settings, is the job of the **`toolchains` module**, not of
`cst btrfs`: storage (snapshots, the NAS backup, disk health, what lives in
`~/Bulk`) stays here, the life of toolchain caches is there.

`snapshots setup` calls it, as your user: `cst toolchains config apply` (the
two settings) and `cst toolchains auto on --unless-off` (the hourly timer
`cst-toolchains-prune.timer`, unless you turned it off with
`cst toolchains auto off`).

```
cst toolchains status        # sizes, policy, last prune, the timer, the settings
cst toolchains prune plan    # what would be deleted now, and why (deletes nothing)
cst toolchains auto off      # stop the hourly pruning
```

The timer also deletes the folders of removed git worktrees, the incremental
part of folders idle for 6 hours, and runs `uv cache prune` once a day.
`snapshots setup` never turns on the opt-in lean debug info for Rust dev
builds (it would make every workspace rebuild once).

Everything else (the policy and its settings, what is never deleted, the
logs, how to undo it): [../toolchains/README.md](../toolchains/README.md).

---

## 11. `docker-gc`: weekly, careful Docker cleanup

Docker keeps old containers, images and volumes until you delete them. On this
PC tests create many of them, and they fill the disk. `docker-gc` removes
only what is clearly disposable. It is **off** until you enable it.

```
cst btrfs docker-gc enable
cst btrfs docker-gc status          # tracking state + what a run would remove now
cst btrfs docker-gc run --dry-run   # only the list of what a run would remove
cst btrfs docker-gc run             # run the weekly job now
cst btrfs docker-gc cleanup-now     # one-off, asks first
cst btrfs docker-gc disable         # stop it; --purge also removes its files (11.3)
```

`status` shows the timer and recorder state on its first line (for example
`timer enabled, recorder active`; `timer disabled` after `disable`, `timer not
installed` when the unit is not there), since when image use is recorded,
the date from which timed image removal is possible, the last run, and then the
list. `run --dry-run` shows only the list, ends with `(dry run: nothing
removed)`, and also works in the first 10 minutes after boot. Both only read,
so they end quietly when their reader goes away (`cst btrfs docker-gc status |
head -3`).

Some Docker words:

- **ephemeral container** = a short-lived container that nobody will need again.
- **anonymous volume** = a volume Docker created without a name.
- **build cache** = saved intermediate steps of `docker build`.
- **restart policy** = a setting that tells Docker to start a container again
  automatically.
- **Testcontainers** = a test library that starts containers for tests; it marks
  them with a label (a tag on the container).
- **compose, k3d, kind, devcontainer** = tools that start groups of containers
  that you manage together.

### 11.1 The policy (weekly run, Monday between 11:00 and 12:00)

| Object | Removed when | Never removed |
|---|---|---|
| Containers | **ephemeral** and stopped for **7+ days**. Ephemeral = a name Docker generated itself (like `eager_turing`) or a Testcontainers label, and no restart policy. | containers with your own name; compose, k3d, kind, devcontainer containers; running containers |
| Volumes | **anonymous** (no name) and used by no remaining container | **named** volumes, never |
| Images | used by no container and **not used for 14+ days** | images from the protected registry `registry.example.com` (pulling them again needs the private tunnel) |
| Build cache | unused for **14+ days** | |

A removal that fails, and a failed `docker builder prune`, is logged (journal
warning) and counted in the run's failures (`status`: `failures N`).

**How "last used" is known.** Docker does not record when an image was last used.
So `enable` also starts `docker-gc-recorder.service`. It follows `docker events`
and saves the time each image was used (container created, started or stopped;
image pulled, built, tagged, loaded) in `/var/lib/docker-gc`.

Because of this:

- **The first timed image removal happens 14 days after `enable`.**
- If the recorder is not running, **no images are removed** at all. A
  `systemctl is-active` that gives no answer within 10 s counts as "not running".
- If the recorder was down for a while with Docker running, the 14-day clock
  starts again.
- If its record in `/var/lib/docker-gc` cannot be read, or holds values
  docker-gc never writes (a time that is not a number), it counts as no record:
  the 14-day clock starts again from now.
- Right before removing an image, it checks again that nothing started using it.

**When the weekly run does not happen.** The run is skipped in the first 10
minutes after boot, and when Docker is not running (it never starts Docker just
to clean it). If the PC was off on Monday, the missed run starts right after the
next boot, and is then usually skipped for these reasons. So **a missed week is
normally not caught up**; the next run is the following Monday.

### 11.2 `cleanup-now`

A one-off cleanup of the existing backlog. It does not wait for the recorder's
14 days: every unused image that was not seen in use in the last 14 days is a
candidate. It lists the containers, volumes and images first and asks once
(`[y/N]`). Protected registry images are asked **separately**, in a second
question. It also removes build cache unused for 14+ days (this is not in the
list). It needs a terminal.

### 11.3 What changes on my system, and how to undo

`enable` adds:

- `/usr/local/sbin/docker-gc`;
- `docker-gc.service`, `docker-gc.timer` and `docker-gc-recorder.service` in
  `/etc/systemd/system`;
- its state in `/var/lib/docker-gc`: `last-used.json` (the recorder's
  record) and `last-run.json`;
- the run lock `/run/docker-gc/run.lock` (one run at a time, shared by the
  weekly run, `run` and `cleanup-now`; in RAM, gone at the next boot, so
  `run --dry-run` and `cleanup-now` add nothing to `/var/lib/docker-gc`).

Both services run sandboxed: no capabilities, the whole file system read-only
except `/var/lib/docker-gc` (`ProtectSystem=strict` with
`StateDirectory=docker-gc`) and, for the weekly run, `/run/docker-gc`
(`RuntimeDirectory=docker-gc`, kept after the run so that a `cleanup-now`
holding the lock keeps its file), `/root` read-only (the docker CLI reads its
settings there), only Unix sockets (`/run/docker.sock`, systemd), and the
usual kernel and system-call limits. `docker builder prune` runs the buildx
plugin, which keeps its state in `~/.docker/buildx`: the weekly run points it
to `/var/lib/docker-gc/buildx` instead (`BUILDX_CONFIG`), so it uses the
default builder (the Docker daemon's own build cache) even if root picked
another one with `docker buildx use`. As root they still own
`/run/docker.sock`, `/var/lib/docker-gc` and `/run/docker-gc`, so they need no capability.
They do not set `NoNewPrivileges=`: Fedora's SELinux policy labels
`/usr/local/sbin/docker*` like dockerd, and the program reaches
`/run/docker.sock` only after its exec moves it into that domain, which
`NoNewPrivileges` forbids (every docker call then fails with "permission
denied"). `tests/test_unit_contract.py` keeps it out.
`systemd-analyze security docker-gc-recorder.service` rates them about 2.3
(it was 9.4). Versions before this one kept the lock in `/run/docker-gc.lock`:
until `enable` installs the new program, an old installed copy and
`cleanup-now` or `run --dry-run` from the checkout do not see each other's
lock, so run `enable` again after an update.

`disable` stops the timer and the recorder, and makes systemd forget the
timer's last run (`systemctl clean --what=state docker-gc.timer`). **The files stay**, including the
recorded state, so `enable` later goes on with the same record of image use.
`status` lists them (`still installed: ...`) while the timer is not enabled.
`uninstall` (section 6) does not touch docker-gc.

`disable --purge` does the same and then removes everything `enable` added:
the program, the three unit files and `/var/lib/docker-gc`. The recorded
image use is lost with it, so after a later `enable` the 14 days start again.
A second `disable --purge` changes nothing.

```
cst btrfs docker-gc disable --purge
```

---

## 12. The Claude Code rule file

The file `~/.claude/rules/btrfs-stall-monitor.md` tells the Claude Code agent how
to use `btrfs-stall-report` on this PC (when to query it, how to choose the
window, how to read the verdict, and not to search the whole filesystem).
`btrfs-setup.sh` does **not** install or change this file. It is maintained by
hand. If the report output changes, update this file too.

---

## 13. What to do if...

**Run the commands in this section in a terminal on the host, not inside a
toolbox.** Commands with `sudo` need your password.

**A word you need here: balance.** `btrfs balance` rewrites block groups (see
4.5). It can change the layout of data (for example back to RAID1) or pack
half-empty block groups together. It can take a long time (hours on a full
disk), and the PC is slower while it runs. It is safe to stop:

```
sudo btrfs balance status /var     # is a balance running, and how far is it?
sudo btrfs balance pause /var      # pause it (resume with: sudo btrfs balance resume /var)
sudo btrfs balance cancel /var     # stop it; the work done so far stays
```

### I got `Disk health: problems detected`

Read the lines of the alert, or run:

```
journalctl -u btrfs-health-check -n 30
cst btrfs status
```

Then find the line in this table. The commands for each case follow the table.

| Message contains | What it means | What to do |
|---|---|---|
| `member device MISSING`, `bytes on missing device` or `this boot is DEGRADED` | One disk is gone. | **Back up important data now.** Then see [A disk is missing](#a-disk-is-missing). |
| `mixed chunk profiles` | Data was written while a disk was missing. | When both disks are present again: [restore RAID1](#a-disk-is-missing) (case a). |
| `mounted READ-ONLY` | btrfs stopped writing to protect itself. | Do not reboot quickly. Save the kernel log first (A). |
| `non-zero device error counters` or `kernel: ... error message(s)` | A disk reported errors. | Look at the details (B). Errors on one disk again and again mean that disk is failing. Run a scrub (C). After you dealt with the cause, reset the counters (B); they do not reset by themselves. |
| `last scrub reported errors` | The scrub found damaged data. | Look at the result (C). "Corrected" errors were repaired from the other disk, but the disk that had them is suspicious. |
| `last scrub (started ...) did not finish` | The last scrub was stopped or failed. | Start a new scrub (C). If it fails again, check the kernel log (A). |
| `last scrub is N days old` or `no scrub has ever completed` | No recent scrub. | Start a scrub (C), or check why the timer did not run (`systemctl list-timers btrfs-scrub-weekly.timer`). |
| `unallocated` or `metadata almost full` | btrfs is close to "no space left". | Free space (old snapshots, caches, Docker images). With a warning (50 GiB), then pack half-empty data block groups (D). With `CRITICAL`, follow [When btrfs says 'No space left' but df shows free space](#when-btrfs-says-no-space-left-but-df-shows-free-space) step by step. |
| `automatic reclaim is configured but ... is off` | The reclaim switches are off. | Switch them on again (E). |
| `automatic ... reclaim failed` | btrfs could not pack block groups by itself. | Check the kernel log (A) and free space. |
| `NVMe ...: critical_warning`, `media_errors`, `available_spare`, `wear` | The disk itself reports trouble. | Back up. Plan to replace the disk (see [A disk is missing](#a-disk-is-missing), case b). |
| `md RAID degraded` | A software RAID (not the btrfs RAID1) lost a disk. | Check `cat /proc/mdstat`. |
| `booted DEGRADED on ...` | A past boot was degraded. | Restore RAID1 if not done yet. The alert clears by itself after RAID1 is complete and a scrub that started after that finished clean. |
| `btrfs-last-resort: ... is for ... but the root fs is ...` | The degraded-boot rule belongs to another filesystem. | Run `cst btrfs degraded-boot` again. |
| `udev: .../64-btrfs.rules changed semantics` | A Fedora update changed the udev rule that degraded-boot builds on. | **The degraded-boot protection may not work at the next boot. Do not rely on it** until `btrfs/btrfs-degraded-boot-setup.sh` has been reviewed and updated. Until then, `degraded-boot` install refuses with `UPSTREAM CHANGED`. |
| `snapshots: no successful hourly run` | Snapshots stopped. | `journalctl -u btrfs-snapshot` (see [SNAPSHOTS.md](SNAPSHOTS.md)). |

The commands:

```
# (A) kernel log: btrfs messages; save the output to a file
journalctl -k -g BTRFS > ~/btrfs-kernel-log.txt

# (B) error counters per disk; reset them only after you dealt with the cause
sudo btrfs device stats /var
sudo btrfs device stats -z /var

# (C) scrub: start, then see the result
sudo systemctl start btrfs-scrub-weekly.service
sudo btrfs scrub status /var

# (D) pack data block groups that are less than 20% full (a balance, see above);
#     only with a warning, not when the alert says CRITICAL (see the next section)
sudo btrfs balance start -dusage=20 /var

# (E) switch automatic reclaim on again
sudo systemd-tmpfiles --create /etc/tmpfiles.d/btrfs-automatic-reclaim.conf
```

How often the alert comes back depends on the problem (see 4.2): state problems
every hour until the cause is gone; kernel-log errors only once.

### When btrfs says 'No space left' but df shows free space

**What you see:** programs fail with `No space left on device` (`ENOSPC`),
or the filesystem switches to read-only, while `df -h` still shows many GiB
free. The health check alerts earlier: `only N GiB unallocated on ...`, and
below 5 GiB `CRITICAL: only N MiB unallocated on ...`.

**Why it happens.** btrfs does not hand out disk space byte by byte. It
reserves it in pieces of about 1 GiB, the **chunks** (block groups, see
4.5): data chunks for file contents, metadata chunks for the index of all
files. So there are two kinds of "free":

- **Free inside chunks**: room in chunks that already exist. A data chunk
  can only take file data, a metadata chunk only metadata. `df` counts this
  room as free.
- **Unallocated**: disk space that is in no chunk yet. Every new chunk comes
  from here, of either kind.

When unallocated space reaches zero, the existing chunks are all there is.
If the metadata chunks are full, btrfs cannot make a new one, even when the
data chunks are only 78% full and `df` shows 63 GiB free. Writes then fail,
and if btrfs is in the middle of a change it cannot finish, it switches the
filesystem to read-only to protect it. A real case on a Fedora system
(reported on the Fedora devel list) looked like this: metadata chunks 95%
full, data chunks 78% full, 1 MiB unallocated.

The fix is to **pack** half-empty data chunks into fewer, full ones, so that
the emptied chunks go back to unallocated space. That is what
`btrfs balance` does. But a balance needs some unallocated space itself, to
write the packed copies. With none left, it finds nothing to do, fails, or,
worse, keeps working without getting anywhere and freezes the machine. The
kernel also remembers an unfinished balance and **resumes it at the next
mount**, so the freeze comes back at boot. The recipe below avoids that.

**How CST prevents it:**

- **Automatic data reclaim** (section 4.5, Linux 6.11 or later): when
  unallocated space gets low, btrfs itself packs half-empty data chunks, a
  little at a time. `cst btrfs install` switches it on for the root
  filesystem.
- **The hourly health check** (section 4.2) warns when a disk has less than
  **50 GiB** unallocated, and alerts `CRITICAL` below **5 GiB**. That leaves
  time to free space calmly.
- **No balance on a schedule.** CST never runs a balance by itself, and
  never a metadata balance: the btrfs developers advise against regular
  metadata balances, because relocating metadata needs metadata space itself
  and can cause exactly this problem. The weekly job is a scrub, which only
  reads and repairs.
- `cst doctor` (area `btrfs`) shows how full the chunks are, whether
  automatic reclaim is on, and an estimate of the unallocated space per disk,
  without `sudo`.

**How to get out of it, step by step** (on the host; `/var` is the root
filesystem here, because on Silverblue `/` itself is not the btrfs mount):

First look at the numbers:

```
sudo btrfs filesystem usage -T /var    # Data/Metadata: Size (allocated) and Used; Unallocated per disk
sudo btrfs device usage /var           # the same per disk
sudo btrfs balance status /var         # is a balance running or paused?
```

**(a) Do not start a full balance or a metadata balance.** No plain
`btrfs balance start /var`, no `-m...` or `-musage=...` filter. They need
room that is not there and can freeze the system. If a balance is already
running and the system still responds: `sudo btrfs balance cancel /var`.

**(b) Free space first**, until at least one data chunk can become empty.
A data chunk becomes empty only when everything stored in it is gone, so
delete a good amount, not a few files. Places that are safe to clear:

```
cst btrfs snapshots list               # which snapshots exist, and which ones the retention would drop
cst btrfs snapshots prune --confirm    # apply the retention now
sudo btrfs subvolume delete /var/mnt/btrfs-top/snapshots/<name>    # one old snapshot by hand
cst toolchains prune apply             # old Rust build folders (see toolchains/README.md)
cst btrfs docker-gc cleanup-now        # unused Docker images, volumes and build cache (lists them, asks first)
podman image prune                     # unused Podman images (per user)
flatpak uninstall --unused             # runtimes nothing uses any more
```

Snapshots hold on to old versions of files: deleting files you also have in
snapshots frees nothing until those snapshots are gone too. Deleted
snapshots are removed in the background; `sudo btrfs subvolume sync /var`
waits until that is done. If the filesystem has gone
read-only, nothing can be deleted: reboot (it mounts writable again) and
start deleting right after the login, before anything else fills it.

Then let btrfs drop the data chunks that are now empty, and look at the
result:

```
sudo btrfs balance start -dusage=0 /var     # only drops chunks that are already empty; copies nothing
sudo btrfs filesystem usage -T /var         # look at Unallocated, per disk
```

Go on to (c) only when `Unallocated` shows **at least about 1 GiB on every
disk**. If it does not, no data chunk has become empty yet: delete more (and
wait with `sudo btrfs subvolume sync /var` after deleting snapshots), then
repeat these two commands. Do not judge by `Size` and `Used` of `Data`: in
exactly this situation `Size` is far above `Used` (data chunks partly full)
while nothing is unallocated, and a balance then has no room to work.

**(c) Pack the data chunks, emptiest first**, one step at a time. Keep a
second terminal open with `sudo watch -n 10 btrfs balance status /var` to see
that it moves.

```
sudo btrfs balance start -dusage=0 /var     # as in (b): drops empty chunks, copies nothing
sudo btrfs balance start -dusage=5 /var     # chunks at most 5% full
sudo btrfs balance start -dusage=15 /var
sudo btrfs balance start -dusage=30 /var
```

After each step, look at `Unallocated` in `sudo btrfs filesystem usage -T
/var`. **Stop once each disk has a few GiB unallocated** (at least 5 GiB,
so the health check is quiet): from there automatic reclaim and normal
deleting do the rest. If a step frees nothing, free more space (b) before
the next one. Go higher than 30 (50, 70) only if you need to and the steps
before went smoothly; higher numbers copy much more data and take long.

**(d) If a balance freezes the PC, or the boot hangs.** The kernel resumes
an unfinished balance at every mount, so the hang repeats. Stop it from
resuming for one boot:

1. Reboot (if nothing responds, hold the power button).
2. In the GRUB boot menu, select the first entry and press `e`. Fedora
   hides the menu: press `Esc` (or hold `Shift`) right after the firmware
   logo to see it.
3. Find the line that starts with `linux`. It already has a `rootflags=`
   option (here: `rootflags=subvol=root,degraded,compress=zstd:1,noatime,flushoncommit`).
   Add `,skip_balance` to the end of its value, with no space:
   `rootflags=subvol=root,degraded,compress=zstd:1,noatime,flushoncommit,skip_balance`.
   Adding to the existing value is the safe way. The systemd-based boot
   stage (initrd) that Fedora uses joins several `rootflags=` with commas,
   so a separate ` rootflags=skip_balance` would most likely work too; but
   other initrds keep only the last `rootflags=`, which would lose
   `subvol=root`, and then the system does not boot. On a system without
   `rootflags=`, add ` rootflags=skip_balance` at the end of the line.
4. Press `Ctrl+X` (or `F10`) to boot. The change is for this one boot only.
   On Silverblue do **not** use `rpm-ostree kargs` for this: that would keep
   it for every boot.
5. After the login the balance is paused. Cancel it:
   `sudo btrfs balance cancel /var` (check with `sudo btrfs balance status /var`).
6. Continue with (b) and then (c). The next normal boot has no
   `skip_balance` and nothing left to resume.

**(e) RAID1: count per disk.** Every RAID1 chunk has one copy on each disk,
so a new chunk needs room on **both** disks. What matters is the disk with
the least unallocated space, not the sum. `btrfs filesystem usage -T` shows
`Unallocated` per disk in its table; the health check and `cst doctor`
check each disk.

### A disk is missing

**First, in every case: back up important data now.** Right now there is only
one copy of everything.

Check which disks the system sees:

```
sudo nvme list
sudo btrfs filesystem show /var
```

**Case (a): the disk is back.** For example, it was a loose cable or a bad slot,
and after a reboot both disks are present again. Then restore RAID1 and check
the data:

```
sudo btrfs balance start -dconvert=raid1,soft -mconvert=raid1,soft /var
sudo btrfs scrub start /var
```

The balance converts only the block groups that are not RAID1 (`soft`). The
scrub then repairs the copy on the disk that missed writes. Also recreate the
NOCOW Docker volumes (section 4.10).

**Case (b): the disk is dead.** **Do not run the balance above with only one
disk:** it cannot restore RAID1, and it would make you believe the problem is
fixed. A new disk must first be prepared and added to the filesystem. **These
tools do not automate this, and this README does not describe it in detail
(not covered, not verified here).** In general terms, the manual work is:

1. Install the new NVMe disk.
2. Encrypt it with LUKS (`cryptsetup luksFormat`) and open it.
3. Make it unlock at boot, like the other disk. On this PC the disks are
   unlocked through `rd.luks.uuid=` kernel arguments; a new disk probably needs
   its own entry (`rpm-ostree kargs`). Not verified.
4. Put it into the btrfs filesystem in place of the dead disk, with
   `btrfs replace start` (see `man btrfs-replace`) or `btrfs device add`
   followed by `btrfs device remove missing`.
5. Only now run the balance and the scrub of case (a).
6. If you use degraded-boot, run `cst btrfs degraded-boot` again.

### I got `btrfs commit stall` or `Task stuck in an uninterruptible wait`

Run the report for the time of the alert, for example:

```
btrfs-stall-report --since '10 min ago'
```

Read the `verdict` lines and the worst episode. Changes to mount options,
`vm.dirty_*` or the kernel are a decision for you; the report only proposes them.

The alert `Task stuck in an uninterruptible wait (memory reclaim / swap)` means
memory ran short, not that a disk is slow. The report shows it in the
`Memory, not storage` line; `cst swap status` shows which cgroup swaps.

### I got `Kernel: btrfs/device error`

Treat it like a health-check error: commands (A), (B) and (C) above.

### I got `Program waiting on a FUSE filesystem`

Something read a network share or the document portal slowly. The message shows
the command. Usually it is the network, or a search tool that walked into
`/run/user/<uid>/gvfs`. The local disks are fine unless the report shows storage
waits at the same time.

### I got `btrfs maintenance (fstrim/scrub) stuck for 15 min`

A trim, scrub or balance made no progress for 15 minutes. Check:

```
sudo btrfs scrub status /var
journalctl -u fstrim
```

Then look at the disk lines in the report.

### `report` says `NO DATA`

Two possible reasons:

1. **Your user cannot read the data.** The data folder is readable only by the
   `wheel` group. Check that `wheel` is in the output of `id -nG`.
2. **The monitor is not running:**

   ```
   systemctl status btrfs-stall-monitor
   journalctl -u btrfs-stall-monitor -n 50
   ```

   Inside a toolbox, put `flatpak-spawn --host` in front of these commands.
   Run `install` again if the service is missing.

### `report` says `/var/log/btrfs-stall-monitor not found: btrfs-stall-monitor is not installed on this host`

The monitor was never installed (or its data folder was deleted). Run
`cst btrfs install` on the host.

### `btrfs-stall-report: command not found`

Run `install` through `sudo` from your normal user (it adds the launcher
`~/.local/bin/btrfs-stall-report` for that user), or use
`cst btrfs report ...` from this folder.

### `install` said `below: staged for the next boot`

Reboot, then run `install` again.

### Docker volumes are not NOCOW (`migration not scheduled`, or `Docker volumes: NOCOW migration gave up`)

You see this in `status` under `docker volumes (NOCOW)`, as
`N with copy-on-write data (...), migration not scheduled`. The "gave up"
message happens at boot, before login, so its desktop alert comes when you log
in (section 4.9). Look at:

```
journalctl -u docker-volumes-nocow
```

Common causes are low free space or a copy error. The volumes are still usable
(copy-on-write). Fix the cause and run `install` again; the migration is then
tried again at the next boot (two more attempts). With no "gave up" line in the
journal, an older helper may have lost the request (section 4.10): `install`
schedules it again too.

### Docker volumes: `migration postponed at N boot(s) in a row`

`status` (and a `cst doctor` warning) shows that the boot-time migration had
to wait, with the reason of the last boot:

- `Docker is running`: something started Docker before
  `docker-volumes-nocow.service` ran at boot (the unit is ordered before
  `docker.service`, so look for another unit or a script that starts it);
- `docker.service state unknown (...)` or `cannot ask systemd ...`: systemd
  gave no clear answer; the journal of that boot says more;
- `dockerd (pid N) uses ... outside docker.service`: a dockerd started by
  hand or by another unit uses the same data folder.

```
journalctl -b -u docker-volumes-nocow
```

Remove the cause, or run the migration by hand once nothing uses the volumes
(section 4.10).

### No desktop alerts arrive

Run `cst btrfs test-alert` in a terminal of your desktop. It goes
to every user who is logged in to the desktop. If nobody is, it is kept and
shown at the next login (section 4.9); the terminal then shows
`kept for the next login`.

Alerts that wait for a login, and whether you have seen them:

```
cst-pending-alerts --list
```

If alerts are kept but never shown when you log in, look at the login
service (as your user, on the host):

```
systemctl --user status cst-pending-alerts.service
journalctl --user -u cst-pending-alerts.service
```

Alerts sent by the services are also in the journal:

```
journalctl -g 'btrfs-health-check|btrfs-stall-monitor|btrfs-snapshot|btrfs-notify'
```

---

## 14. Glossary

| Term | Meaning |
|---|---|
| anonymous volume | A Docker volume created without a name. |
| balance (btrfs) | Rewriting block groups: to change their layout (for example to RAID1) or to pack half-empty ones together. Can take hours. |
| block group / chunk | A big piece of disk space (about 1 GiB for data) that btrfs reserves for data or metadata. |
| below | A tool that records per-process resource use every few seconds. |
| build cache | Saved intermediate steps of `docker build`. |
| build-dir (cargo) | The folder where cargo keeps its intermediate build files; here `~/.cache/cargo-build/XX/YYYYYYYYYYYYYY`, one per project path ([../toolchains/README.md](../toolchains/README.md#7-rustbuild-old-cargo-build-folders)). |
| flock | A Linux file lock. A program can try to take it without waiting; if another program holds it, the try fails. |
| cgroup | A group of processes the kernel measures and limits together (a service, a container). [../cpu/README.md, section 2](../cpu/README.md#2-basic-ideas-you-need) explains them in more detail. |
| checksum | A short fingerprint of data, used to detect damage. |
| commit (btrfs) | The moment btrfs writes its pending changes and makes them permanent. |
| copy-on-write (CoW) | Never overwrite data in place; write the new version elsewhere. |
| D state | A task waiting in the kernel without interruption, usually for IO. |
| degraded | Running a RAID1 with one disk missing. |
| deployment | On Silverblue: one version of the system image. A new deployment is used from the next boot. |
| dirty pages | Written data that is still in memory and not yet on disk. |
| dm-crypt / LUKS | Linux disk encryption; LUKS2 is the on-disk format with a header. |
| document portal | The Flatpak service that gives sandboxed apps access to your files. |
| drop-in | A small extra file that changes one setting of an existing systemd unit. |
| EFI partition | A small partition that the PC's firmware reads at power-on to start the boot. |
| ENOSPC | The error "no space left on device". |
| epoch | Seconds since 1 January 1970; `date +%s` prints the current value. |
| flushoncommit | A btrfs mount option: write all pending data before each commit. |
| fsync | A program asking "put my data safely on the disk now". |
| FUSE | A filesystem served by a normal program (network shares, document portal). |
| host | The real Fedora system of the PC, outside any toolbox container. |
| initramfs | The small system that runs at boot before the real system (it asks the LUKS passphrases). |
| IO | Input/output: reading from and writing to the disk. |
| journal / `journalctl` | The system log of systemd, and the command to read it. |
| kernel arguments | Settings given to the Linux kernel at boot (the kernel command line). |
| metadata | btrfs's index of files, folders and where their data is. |
| NAS | A network storage box; here a Synology. |
| NOCOW (`+C`) | A file attribute: no copy-on-write, no checksums, no compression. |
| NVMe | A fast kind of SSD that plugs directly into the mainboard. |
| `/proc`, `/sys` | Special folders where the kernel shows its own state as files. |
| PSI | Pressure stall information: the kernel's measure of time lost waiting for IO, memory or CPU. |
| RAID1 | Two copies of everything, one per disk. |
| restic | A backup program, used for the optional NAS backup. |
| restart policy | A Docker setting that starts a container again automatically. |
| rootflags | The mount options for `/`, given as a kernel argument. |
| rpm-ostree / layering | Silverblue's system image tool; "layering" adds a package on top of the image. |
| scrub | Read everything, check checksums, repair from the good copy. |
| Silverblue | A Fedora edition whose system files are a read-only image, replaced as a whole at each update. |
| SMART | The health information a disk keeps about itself. |
| snapshot | A frozen read-only copy of a subvolume at one moment. |
| spare blocks | Reserve blocks a disk uses to replace worn-out ones. |
| SSD | A disk without moving parts. |
| subvolume | A separately snapshottable folder tree inside a btrfs filesystem. |
| sysctl | A kernel setting that can be changed while the PC runs (`vm.dirty_bytes`). |
| systemd / unit / timer | systemd starts and stops services. A unit describes one job; a timer starts a unit on a schedule. |
| toolbox | A container (a separate, small Linux environment) on top of the host, for development tools. |
| trim / discard | Telling the SSD which blocks are free. |
| tuned | A Fedora service that applies groups of performance settings ("profiles"). |
| udev | The part of Linux that reacts when devices appear; its rules decide when a disk is "ready". |
| unallocated space | Disk space not yet reserved in any block group. |
| wheel | The group of users who are allowed to use `sudo`. |

---

## 15. Repository layout

Each Python program here is an entry point: the program file that `install`
writes (as a self-contained zipapp) or a setup script runs from the checkout.
A program's file only reads its arguments and calls its package;
the logic lives once, in a sub-package of `btrfs/`, where the tests import it:

| Program | Package | What is where |
|---|---|---|
| `btrfs-snapshot-core` | `snapshot/` | `domain.py`: retention (btrbk's schedule()), the safeguards, snapshot names and wall-clock time, the circuit breaker's limits, parsing of `--source`, `--caches-skip`, the state file and `btrfs subvolume show`, and the values the ports and the presenter pass (`Scan`, `CacheWalk`, `RunOptions`, `Listing`); no I/O. `ports.py`: the host, btrfs, state directory, pin file, run lock and log boundaries. `application.py`: one `Run` per invocation (clock guards, state lists, creation, sealing, deletions under the circuit breaker and their re-checks), the scan of the snapshot folder and the bodies of the six commands. `adapters.py`: the `Host` (commands, clock, directory entries, the cache-folder walk; tests replace it), the btrfs commands, the state directory, the pin file (it resolves pinned paths on the filesystem), the run lock, the journal lines and the local time zone. `wiring.py`: `$TZ`, the time zone and the adapters. `presenter.py`: what `list`, `caches` and `latest` print. `cli.py`: the argument parser and `main()`. |
| `btrfs-bulk` | `bulk/` | `domain.py`: the items (`Item`, `ITEMS`), the CACHEDIR.TAG text, sizes and texts, and Steam's file formats (VDF, `libraryfolders.vdf`, app manifests); no I/O. `adapters.py`: settings and state paths, btrfs and the filesystem (subvolumes, +C, mounts, busy files, copies), the child process that runs as the user (`Ctx`, `as_user`) and the steps it runs (`u_*`), the journals, the boot flag, the keep list, the run lock, and where `btrfs-snapshot` is (`snapshot_program()`: the checkout's copy from a checkout, the installed one from the zipapp). `steam.py`: one game's move into `~/Bulk/steam` with its journal and recovery. `items.py`: each item's state, its move and link, recovery, `apply`, the plan/status report. `cli.py`: `steam plan/apply`, `keep/unkeep`, `boot`, `main()`. |
| `chrome-cache` | `chrome_cache/` | `domain.py`: the flag lines and markers, reading the text of `chrome-flags.conf` and deciding what to write (`decide`, `turn_off`); no I/O. `adapters.py`: the flags file reached from the home without following a symlink (`open_dir_chain`, `read_flags`, `write_flags`), whether the Flatpak is installed, the cache sizes, the one line of `plan` (`plan_line`). `cli.py`: `status`, `apply`, `off` and `main()`. |
| `docker-gc` | `docker_gc/` | `domain.py`: Docker's generated names, its time stamps, which containers are ephemeral, the settings and paths (`GcSettings`, `GcPaths`), the typed records of what docker reports, the plan (`plan_gc`) and the lines that show it; no I/O. `ports.py`: docker (with the events stream), the state files and run lock, the host (uptime, the recorder's unit), the terminal and journal. `application.py`: the use cases (`DockerGc`: record, plan, apply with the last checks before an image is removed, run, cleanup-now, status) and the recorder's state while it follows `docker events` (`Recording`). `adapters.py`: the `docker` commands and their JSON, the last-used state and the last run's summary (each replaced atomically through cst_core's `write_atomic`; a state file docker-gc did not write reads as no state), the run lock (cst_core's `take_lock`), systemctl and `/proc/uptime`, the terminal and the journal lines. `wiring.py`: the environment (`DOCKER_GC_STATE`, `DOCKER_GC_RUNTIME`, `KEEP_*_DAYS`, `DOCKER_GC_PROTECT`, `DOCKER_GC_FLUSH`) and the use cases over the real adapters. `cli.py`: the arguments and `main()`. |
| `docker-volumes-nocow` | `docker_volumes_nocow/` | `domain.py`: the paths (`NocowPaths`), what a scan of a volume found, whether a copy matches its original (entries, bytes, SELinux labels), how a dockerd names its data root, docker.service's states, the record of postponed boots; no I/O. `ports.py`: the volume trees, the helper's state files, the Docker daemons, its output. `application.py`: one volume's NOCOW copy (`migrate_one`), `migrate`, `mark`, `unmark` and `status`. `adapters.py`: their files and NOCOW flags (cst_core's `inode_flags`/`set_nocow`), recovery of an interrupted migration, whether docker.service or another dockerd uses them, the flag, attempts and postponed files, SELinux labels, the journal lines and btrfs-notify. `wiring.py`: where Docker keeps its volumes (`/etc/docker/daemon.json`, read when the program runs) and the use cases over the real adapters. `cli.py`: the arguments and `main()`. |
| `btrfs-stall-monitord` | `stall_monitord/` | `domain.py`: thresholds and intervals, the record version and keys, the kernel-stack frame lists and what a stack waits for (`wait_kind`, `mmap_lock_only`), the episode being recorded (`Episode`); no I/O. `adapters.py`: reading `/proc`, `/sys` and cgroupfs to the end of each file (`read_bytes`), the btrfs/disk/container `Topology`, D-state tasks, the journal lines and the `Writer` thread of the log files. `monitor.py`: the sampling loop (`Monitor`: probes, samples, episodes, hung and busy tasks, the kernel log, alerts). `cli.py`: the arguments and `main()`. |
| `btrfs-stall-report` | `stall_report/` | `domain.py`: the record formats, the wait categories and hints, classifying a stack (with the daemon's frame lists, imported from `stall_monitord/domain.py`, so the two never drift), the open episode, the verdict lines and number formats; no I/O. `adapters.py`: the log folder, `date -d` for `--since`/`--until`, reading the daily log files, below and the container names. `analysis.py`: `analyse()`, one window's coverage, totals, categories, episodes and events (it reads the clock, and runs below when asked). `cli.py`: the text report, the arguments and `main()`. |

Every Python program in `btrfs/` is such an entry point: `btrfs/` holds no
Python code outside these packages and the program files.
