# `cst swap`: zswap, encrypted swap and memory protection

This folder holds the parts of `swap-setup.sh`, the `swap` module of CST
(Cominotti Setup Tools). The script lives one level up:
`~/Workspace/cst/swap-setup.sh`. You run it as `cst swap <command>` (how to
set up `cst`: [../README.md](../README.md#the-cst-command));
`bash swap-setup.sh <command>` from the repository folder does the same. It is
written for one machine, the workstation **sophia**: 93 GiB of RAM, a Windows
VM (`win11-devbox`) that takes 48 GiB of it, and a 128 GiB swap partition on
its own NVMe disk.

It replaces `apply-swap-config.sh`, a standalone script that lived outside
this repository. What that script wrote is recognised (see
[section 5](#5-coming-from-apply-swap-configsh)).

The entry point of the whole repository is [../README.md](../README.md).

> **In short: what this does to your system**
>
> - **Swap**: a 128 GiB swap partition on NVMe (`swap0`), encrypted with a
>   new random key at every boot. Two lines: one in `/etc/crypttab`, one in
>   `/etc/fstab`.
> - **zswap**: a compressed cache in RAM in front of that swap, switched on
>   at every boot by `/etc/tmpfiles.d/zswap.conf`.
> - **zram off**: Fedora's default compressed RAM swap is masked.
> - **Settings**: `/etc/sysctl.d/99-swap-tuning.conf` (swappiness 100 and
>   three more).
> - **VM protection**: `MemoryLow=48G` for `machine.slice`, where the VM runs.
> - **Undo**: by hand, see [section 8](#8-how-to-undo).

---

## 1. Basic ideas you need

- **Swap**: when RAM is short, the kernel moves memory pages that nobody used
  for a while out of RAM, and brings them back when a program touches them
  again. A **page** is 4 KiB of memory.
- **Swap partition**: a part of a disk that only holds swapped pages. Here:
  `swap0`, 128 GiB on the third NVMe disk.
- **zswap**: a cache in front of the swap partition. A page that goes to swap
  is first **compressed** and kept in RAM, in the **pool**. Only pages that
  stay cold are written on to the disk later (**writeback**). Getting a page
  back from the pool takes microseconds; from the disk it takes much longer.
- **zram**: a different idea, a swap "disk" that lives in RAM, compressed.
  Fedora uses it by default. Here it is switched off (see 2.1).
- **cgroup**: a box of processes that the kernel counts and limits together.
  systemd calls the boxes **slices** (boxes of boxes), **services** and
  **scopes**. The VM runs in `machine.slice`; system services in
  `system.slice`; your desktop apps in `app.slice` inside your user manager;
  the toolboxes in the inner `user.slice` of your user manager.
- **sysctl**: a kernel setting under `/proc/sys`. Files in `/etc/sysctl.d/`
  set them at every boot.
- **PSI** (pressure stall information): the share of time in which tasks had
  to wait, here for memory.

---

## 2. Why it is set up like this

### 2.1 zswap, not zram

zswap is a compressed cache **inside** the kernel's swap path, in front of a
real swap device. Its **shrinker** is part of the normal memory reclaim, so
cold pages move from the pool to the disk by themselves. Pages that do not
compress: on current kernels (6.18 and later; sophia runs 7.x) zswap keeps
them in the pool **uncompressed** (counted in `stored_incompressible_pages`,
[section 6](#6-reading-the-status)) and writes them on to disk like the
rest. Older kernels rejected them, so they went straight to disk. The pool's memory is
counted to the cgroup that owns the pages, which `systemd-oomd` relies on.

zram in front of a disk swap does the opposite: it fills fast RAM with the
**first** pages that went cold, and has no way to move them on to the disk
later. Newer, warmer pages then have to go to the disk instead ("LRU
inversion"). Fedora ships zram because its default install has no disk
swap; once a disk swap partition exists, zswap is the coherent choice. Long
version: <https://chrisdown.name/2026/03/24/zswap-vs-zram-when-to-use-what.html>.

The pool may grow to **20 %** of RAM (`max_pool_percent`, the kernel default):
about 18.7 GiB compressed, which holds roughly 45-60 GiB of pages at the
usual 2.5-3:1 ratio. It is not raised on purpose: with a 48 GiB VM, host +
pool + VM get tight before the shrinker catches up. Raise it only if
`pool_limit_hit` keeps growing (see [section 6](#6-reading-the-status)).

### 2.2 tmpfiles.d, not a kernel argument

zswap could be switched on with kernel arguments (`zswap.enabled=1
zswap.compressor=zstd`). They are read very early, in the initramfs, where
the `zstd` crypto module is not available. The kernel then falls back to
its built-in default (`lzo`) with only a line in `dmesg`. A file in
`/etc/tmpfiles.d/` is applied after the root filesystem is mounted, when
modules can load, so `zstd` works. The file sets the compressor first and
`enabled` last, so the first pool uses `zstd` from the start (changing the
compressor later creates a second pool that lives until the old one drains).

### 2.3 The encrypted swap partition

- **Plain dm-crypt with a random key**: `/etc/crypttab` opens `swap0` with a
  new key from `/dev/urandom` at every boot. There is nothing to type, and
  yesterday's swap contents are noise today. The `swap` option makes systemd
  run `mkswap` after opening it.
- **Cipher**: `aes-xts-plain64`, `size=512` (two 256-bit keys, as XTS needs),
  `sector-size=4096` (one swap page is one encryption sector). `discard`
  lets the NVMe reuse blocks that pages left.
- **Found by PARTUUID**, the partition's ID in the GPT partition table. The
  usual filesystem UUID cannot be used: the random key turns it into noise.
- **Size check**: because the `swap` option destroys whatever is on the
  partition at every boot, `apply` checks its exact size (137438953472
  bytes) every time, and refuses to touch a partition that carries a
  filesystem signature. If the PARTUUID ever pointed at another disk (after
  a clone or a repartition), `apply` stops.
- `/etc/fstab` has `nofail` (boot goes on if the disk is missing) and
  `discard`.

### 2.4 swappiness 100 and the other settings

| Setting | Value | Why |
|---|---|---|
| `vm.swappiness` | 100 | Treat "swap a page out" and "drop file cache that must be read again" as equally costly. With zswap most swap-ins come from RAM, so swapping a cold page is usually cheaper than dropping cache. Not the 180 used when **all** swap lived in RAM: pages written on to the NVMe are really expensive to bring back. Lower to 60 if the disk read-back grows (section 6). |
| `vm.page-cluster` | 0 | Read one page per swap fault. Read-ahead helps spinning disks only; on zswap and NVMe it just adds latency. |
| `vm.watermark_boost_factor` | 0 | No extra reclaim bursts after memory fragmentation events. |
| `vm.watermark_scale_factor` | 125 | Background reclaim (`kswapd`) wakes earlier (1.25 % of memory between the watermarks), so it works smoothly instead of in stalls. |

The `cpu` module's memory experiment `wsf-300` changes the last one at
runtime (to 300). While it is on, `plan` and `status` show that row as `exp`
(not `DIFF`) with a note, also inside a toolbox, and `apply` leaves that one
setting alone (it says `kept:`), so it does not end a measurement silently.
After `cst cpu experiment off wsf-300` the old value is back; `apply` sets
125 if it was something else.

### 2.5 `MemoryLow=48G` for the VM

The VM (libvirt `qemu:///system`) runs in `machine.slice`. With
swappiness 100, its 48 GiB of RAM would be a prime target for swapping.
`MemoryLow` is a **soft floor**: while other cgroups still have memory that
can be reclaimed, the kernel takes it from them first. When nothing else is
left, the floor is ignored, so it cannot starve the host. The cgroup
filesystem is mounted with `memory_recursiveprot`, so the protection reaches
the VM's own scope without any libvirt setting. Raise the value in
`units/machine.slice.d-50-memory-low.conf` if the VM grows.

The drop-in is `machine.slice.d/50-memory-low.conf`. The `cpu` module
has its own drop-in in the same folder, `50-cpu-setup.conf`, with
`CPUWeight=50`; the two set different things and do not clash. While a game
runs, the `cpu` module's game profile gives the game's cgroups
`MemoryLow=16G` (runtime only); it does not touch `machine.slice`'s memory
settings. `cst swap status` shows both floors in its `low` column.

**Not managed here** (done once, by hand): `freePageReporting='on'` on the
virtio memballoon of the `win11-devbox` VM, which lets the guest hand freed
memory back to the host.

---

## 3. Quick start

```bash
cst swap plan          # what apply would change; changes nothing
cst swap apply         # asks for sudo
cst swap status
```

`plan` and `status` work as your normal user, also inside a toolbox (there
they read the host's `/etc` at `/run/host/etc`). Two things need root:
reading `/etc/crypttab` (`?` in the output otherwise) and zswap's debug
counters. Add `--root` on the host for both (`cst swap status --root`); it
calls `sudo` by itself. `cst status` on the host asks for `sudo` anyway, so
the swap part shows everything there.

No reboot is needed: `apply` also sets everything live.

---

## 4. Commands

| Command | Root | What it does |
|---|---|---|
| `plan [--root]` | no (`--root`: yes) | Dry run: compares the files in `/etc` and the live settings with this folder, and says per item what `apply` would do. |
| `apply` | yes, host only | Writes what differs, applies it live, then checks the result (see below). Running it twice in a row changes nothing the second time. |
| `status [--root]` | no (`--root`: yes) | The `plan` view plus the live numbers: pool, traffic, debug counters, swap devices, a per-cgroup table, memory pressure ([section 6](#6-reading-the-status)). |
| `help` | no | The built-in help. |

The words in the first column of `plan` and `status`:

| Word | Meaning |
|---|---|
| `ok` | Nothing to do. |
| `adopt` | Up to date, but written by an older name of this tool: `apply` only renews the comment (section 5). |
| `dedupe` | The same `swap0` entry is there twice (for example, the old script ran again): `apply` keeps one (section 5). |
| `exp` | A `cpu` module experiment set this value on purpose: `apply` leaves it alone ([section 2.4](#24-swappiness-100-and-the-other-settings)). |
| `write`, `update`, `mask`, `remove` | `apply` would write, rewrite, mask or delete that file. |
| `start`, `restart`, `swapoff` | `apply` would activate `swap0`, re-activate it, or swap off `zram0`. |
| `DIFF` | The live value differs from the desired one; `apply` sets it. |
| `later` | Needs a reboot (for example, `swap0` is in use, so it keeps its old options). |
| `?` | Could not be read without root. |
| `REFUSE`, `CONFLICT` | `apply` would stop: wrong partition, or an entry for `swap0` that was written by hand. |

What `apply` does, in order:

1. **Checks** that zswap exists, that the `zstd` compressor loads, and the
   partition (exists, exact size, no filesystem signature).
2. **swap0**: the marked lines in `/etc/crypttab` (mode 0600) and
   `/etc/fstab`. A marked line is replaced **in place**; a second marked line
   is removed; with none, the line is appended. The file is replaced
   atomically (temporary file in the same folder, then a rename), then
   `restorecon` fixes the SELinux label. A copy of the file before the first
   change stays as `<file>.pre-swap-config.bak`. After any rewrite, even a
   renewed comment, `systemctl daemon-reload` runs: systemd builds the
   `swap0` units from these files and would otherwise show them as "changed
   on disk" until the next reboot. A reload restarts nothing. If an entry
   really changed and `swap0` is active: it is restarted when empty, else
   the new options apply at the next boot (a `WARNING` says so). If `swap0`
   is not active, it is opened and switched on.
3. **zswap**: `/etc/tmpfiles.d/zswap.conf`, then the same values live.
4. **zram**: `/etc/systemd/zram-generator.conf` becomes a symlink to
   `/dev/null` (zram-generator's documented way to disable the vendor file).
   If `zram0` is still in use as swap, it is swapped off only when free RAM
   (`MemAvailable`) covers its contents plus 1 GiB; otherwise it is gone after
   the next reboot. This comes after step 3 so the pages that leave zram can
   land in the zswap pool.
5. **sysctl**: deletes the old `99-zram-tuning.conf` if present, writes
   `/etc/sysctl.d/99-swap-tuning.conf`, applies it (without
   `vm.watermark_scale_factor` while the `cpu` experiment `wsf-300` is on,
   section 2.4).
6. **machine.slice**: writes the drop-in; `daemon-reload` when the file was
   rewritten (also for a renewed comment, for the same reason as in step 2).
7. **Verification**: the live settings against this folder, `swap0` active,
   `zram0` gone; `swapon --show`. Any `DIFF` makes `apply` end with an error.

Files are written only when they differ, always atomically.

---

## 5. Coming from `apply-swap-config.sh`

The old script marked its lines with `# managed-by: apply-swap-config.sh`,
and the one before it with `# managed-by: apply-zram-config.sh`. The new
marker is `# managed-by: swap-setup.sh (CST)`. Its own files started with
`# Managed by apply-swap-config.sh`.

- A crypttab/fstab line with an old marker and otherwise the same text is
  **adopted**: `apply` replaces the marker in place, nothing else. It does not
  count as a change: no restart of `swap0`, and no line is added twice. Only
  a `daemon-reload` runs, so systemd stops calling the `swap0` units
  "changed on disk" (it restarts nothing).
- A file whose settings are the same and only the comments differ is also
  adopted: rewritten with the new header (plus a `daemon-reload` for the
  `machine.slice` drop-in, for the same reason).
- On sophia, the first `cst swap plan` after the move shows `adopt` for all
  five items (crypttab needs `--root` to show it) and `ok` for the rest.
- The old script itself is not touched; do not run it again. It does not
  know the new marker, so it **appends a second `swap0` line** to
  `/etc/crypttab` and `/etc/fstab`, runs `daemon-reload`, and may restart
  `swap0` if it is empty. Until the next `cst swap apply`, systemd sees the
  entry twice. `plan` then shows `dedupe` for those lines, and `apply`
  removes the second copy (it keeps one, in place, with the new marker),
  runs `daemon-reload` and does not restart `swap0`, because the entry
  itself is the same. If the old script wrote different options, the
  lines show `update` instead and count as a real change.

---

## 6. Reading the status

`cst swap status` prints these parts.

**files** and **runtime**: the `plan` view (section 4).

**live settings vs desired**: each zswap parameter, sysctl and the VM's
`memory.low`, live and desired.

**zswap pool**:

- `pool in RAM: X of a Y limit`: the compressed pool (`Zswap` in
  `/proc/meminfo`) against `max_pool_percent` of RAM.
- When the pool is full, zswap **rejects** new pages (they go straight to
  disk) and pushes old ones to disk. It accepts new pages again only when
  the pool has shrunk below `accept_threshold_percent` (default 90 %) of the
  limit. That is the "takes new pages again below ..." line.
- `holds A compressed into B: ratio`: `Zswapped` (the pages, uncompressed)
  against `Zswap` (their compressed size). 2.5-4 : 1 is normal.

**swap traffic since boot** (`/proc/vmstat`, shown as sizes):

| Counter | Meaning |
|---|---|
| `zswpout` | pages that went into the pool |
| `zswpin` | pages that came back from the pool (fast) |
| `zswpwb` | pages the pool wrote on to the disk (writeback) |
| `pswpout` | pages written to the swap disk, by any way |
| `pswpin` | pages read back from the swap disk (slow) |

The derived shares:

- **written back** = `zswpwb / zswpout`: of the pages zswap took, how many it
  moved on to disk. A high value is fine when the pages are cold.
- **`pswpout = zswpwb`** means no page bypassed zswap. If `pswpout` is
  larger, the difference went to disk directly: zswap rejected it (for
  example, the pool was full, or it had no memory for the copy) or was off
  at the time. A page that does not compress is not a reason on current
  kernels: it is kept in the pool uncompressed (section 2.1).
- **read back** = `pswpin / pswpout`: of the pages written to disk, how many
  had to be read again. When nothing bypassed zswap this is the same as
  `pswpin / zswpwb`. **This is the number to watch.** Low (a few %) means
  the disk holds pages nobody needs. If it grows and you feel lag, pages are
  coming back from the NVMe instead of RAM: lower `vm.swappiness` to 60.

These counters run since boot, so one heavy day stays in them. Compare two
readings a few minutes apart to see what happens now.

**zswap debug counters** (root only; `needs root` otherwise):
`pool_limit_hit` counts how often the pool was full. If it grows steadily,
raise `max_pool_percent` a little (section 2.1). The `reject_*` lines count
pages zswap refused, by reason (pool full and writeback failed, compressed
to no gain, compression error, no memory). `stored_incompressible_pages`
counts pages in the pool that did not compress and are stored as they are
(section 2.1). `written_back_pages` is the same as `zswpwb`.

**swap devices**: `/proc/swaps`, with `dm-N` translated to `swap0`.

**per cgroup** (`memory.stat` of each cgroup; counters since the cgroup was
created): the whole system, `system.slice`, `machine.slice` (VM),
`builds.slice` (Docker, if the `cpu` module's `docker-slice` is installed),
and per user: `app.slice` (desktop apps, Steam) and the inner `user.slice`
(toolboxes).

| Column | Source | Meaning |
|---|---|---|
| `swap` | `memory.swap.current` | swap in use by the cgroup (pool and disk) |
| `zswapped`, `zswap` | `memory.stat` | what it has in the pool, uncompressed and compressed |
| `->pool`, `->disk`, `disk in` | `zswpout`, `zswpwb`, `pswpin` | its swap traffic |
| `wb%` | `zswpwb / zswpout` | written back to disk |
| `rb%` | `pswpin / pswpout` | read back from disk |
| `zswap.wb` | `memory.zswap.writeback` | 1 = the pool may write this cgroup's pages to disk |
| `low` | `memory.low` | its memory floor (48 GiB for the VM; 16 GiB for apps while a game runs) |
| `psi60` | `memory.pressure` | % of time its tasks waited for memory, last 60 s |

A cgroup with a high `rb%` and a noticeable `psi60` is the one that suffers
from disk swap.

**memory pressure**: `/proc/pressure/memory` for the whole system. `some` =
at least one task waited, `full` = all tasks waited.

If the disks themselves are slow, `cst btrfs report --since '<time>'` tells
you whether memory reclaim or the filesystem held programs up
([../btrfs/README.md, section 7](../btrfs/README.md#7-report-was-storage-the-bottleneck)).

---

## 7. Per-cgroup writeback: only during games

The kernel can switch zswap's writeback off per cgroup
(`memory.zswap.writeback`, `0` in a cgroup's folder; systemd's
`MemoryZSwapWriteback=no`). Pages of such a cgroup then stay in the pool (or
in RAM) and never go to the swap disk. This module does **not** change it:
outside games every cgroup keeps the kernel default `1`.

The **game profile** of the `cpu` module sets it to `0` on your `app.slice`
(desktop apps and the game) while a game runs, and puts it back afterwards
([../cpu/README.md, section 7.1](../cpu/README.md#71-what-it-does-while-a-game-runs)).
The reason is in the per-cgroup table of `status`: desktop apps had the
highest `readback` (about half of their pages sent to disk were read back),
so their disk swap-ins are the ones you feel. During a game, `zswap.wb`
shows `0` for `app.slice`.

---

## 8. How to undo

There is no `uninstall` command: removing swap while it holds tens of GiB is
a step to do on purpose, at a quiet moment. As root, on the host:

1. `swapoff /dev/mapper/swap0` (needs free RAM for everything in it; check
   `swapon --show` first), then `systemctl stop systemd-cryptsetup@swap0`.
2. Delete the line ending in `# managed-by: swap-setup.sh (CST)` from
   `/etc/crypttab` and from `/etc/fstab` (`<file>.pre-swap-config.bak` is the
   version before the first change).
3. `rm /etc/tmpfiles.d/zswap.conf /etc/sysctl.d/99-swap-tuning.conf
   /etc/systemd/system/machine.slice.d/50-memory-low.conf`
4. To get Fedora's zram back: `rm /etc/systemd/zram-generator.conf` (the
   `/dev/null` symlink).
5. `systemctl daemon-reload`, then reboot (zswap and the sysctls return to
   the kernel defaults at boot).

---

## 9. Files in this folder

| File | Installed as / used by |
|---|---|
| `tmpfiles/zswap.conf` | `/etc/tmpfiles.d/zswap.conf` |
| `sysctl/99-swap-tuning.conf` | `/etc/sysctl.d/99-swap-tuning.conf` |
| `units/machine.slice.d-50-memory-low.conf` | `/etc/systemd/system/machine.slice.d/50-memory-low.conf` |
| `swap-lib.bash` | sourced by `swap-setup.sh`: markers, file comparison, atomic writes |
| `swap-status` | the live part of `status` and `plan` (Python, read-only) |
| `tests/test_swap.py` | unit tests: `python3 -I -B -m unittest discover -s swap/tests -t .` from the repository folder |

The crypttab and fstab lines, the partition's PARTUUID and its size are set
at the top of `swap-setup.sh`.

---

## 10. Glossary

- **compression ratio**: uncompressed size divided by compressed size.
- **crypttab / fstab**: the files that tell systemd which encrypted devices to
  open and which filesystems and swap to use at boot.
- **dm-crypt**: Linux disk encryption. **Plain** mode has no header on disk;
  the key comes from the crypttab line (here: random at every boot).
- **drop-in**: a small file in `<unit>.d/` that adds settings to a systemd
  unit without editing it.
- **MemoryLow / memory.low**: a soft memory floor for a cgroup (section 2.5).
- **PARTUUID**: the ID of a partition in the GPT partition table.
- **pool**: zswap's compressed pages in RAM.
- **PSI**: pressure stall information; how long tasks waited for memory.
- **reclaim**: the kernel freeing memory: dropping file cache or swapping
  pages out. `kswapd` does it in the background; "direct reclaim" makes the
  program that needs memory wait.
- **shrinker**: the part of zswap that moves cold pages from the pool to disk.
- **swappiness**: how much the kernel prefers swapping pages out over
  dropping file cache (0-200).
- **writeback**: zswap moving a page from its pool to the swap disk.
- **zram**: a compressed swap device in RAM; off here (section 2.1).
- **zswap**: a compressed cache in RAM in front of a swap device.
