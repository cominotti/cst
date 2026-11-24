<!-- SPDX-License-Identifier: MPL-2.0 -->
# `cst swap`: zswap, encrypted swap and memory protection

This folder holds the parts of `swap-setup.sh`, the `swap` module of CST
(Cominotti Setup Tools). The script lives one level up, at the top of
the CST folder. You run it as `cst swap <command>` (how to
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
rest, except in a cgroup with zswap writeback off, where they stay in RAM
(`reject_compress_fail`, section 6). Older kernels rejected them, so they
went straight to disk. The pool's memory is counted to the cgroup that owns
the pages, which `systemd-oomd` relies on.

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
  partition at every boot, `apply` checks its exact size every time, and
  refuses to touch a partition that carries a filesystem signature. If the
  PARTUUID ever pointed at another disk (after a clone or a repartition),
  `apply` stops.
- **Which partition: `/etc/cst/swap.conf`** (`KEY=VALUE` lines, root's, mode
  0644). Nothing is configured by default: without it `plan` and `status`
  show `REFUSE` for `cst/swap.conf` (and no crypttab, fstab or partition
  rows), `cst doctor` warns, and `apply` stops before it changes anything.
  `cst doctor` reads the file as `cst swap` does: for one this user may
  not read it shows `info` (on the host, `cst swap status --root` reads
  it) rather than "not configured".
  A `/etc/cst/swap.conf` that is there but cannot be read is never treated
  as missing: as a user who may not read it (its mode or owner, or a folder
  above it the user may not enter) `plan` and `status` show `?` and ask for
  `--root`; one that is not a regular file, or that root cannot read, is
  `REFUSE`, and `apply` stops before it changes anything.
  On sophia it reads:

  ```
  SWAP_PARTUUID=612551b1-ca02-4514-86ad-7d8902e612dc   # ls -l /dev/disk/by-partuuid
  SWAP_SIZE_BYTES=137438953472                         # blockdev --getsize64 /dev/disk/by-partuuid/...
  SWAP_NAME=swap0                                      # optional: /dev/mapper/NAME (default swap0)
  ```

  A PARTUUID in lower case, as `/dev/disk/by-partuuid` names it; the size
  in bytes, exact. Any other line or value is a problem that `plan`, `status`
  and `cst doctor` name, and `apply` refuses. Only Python reads the file.
  Until 2026-11 these values were written in `swap/domain.py`; a machine
  set up then gets the file from its first `apply` (section 5).
- `/etc/fstab` has `nofail` (boot goes on if the disk is missing) and
  `discard`.

### 2.4 swappiness 100 and the other settings

| Setting | Value | Why |
|---|---|---|
| `vm.swappiness` | 100 | Treat "swap a page out" and "drop file cache that must be read again" as equally costly. With zswap most swap-ins come from RAM, so swapping a cold page is usually cheaper than dropping cache. Not the 180 used when **all** swap lived in RAM: pages written on to the NVMe are really expensive to bring back. Lower to 60 if the disk read-back grows (section 6). |
| `vm.page-cluster` | 0 | Read one page per swap fault. Read-ahead helps spinning disks only; on zswap and NVMe it just adds latency. |
| `vm.watermark_boost_factor` | 0 | No extra reclaim bursts after memory fragmentation events. |
| `vm.watermark_scale_factor` | 125 | Background reclaim (`kswapd`) wakes earlier (1.25 % of memory between the watermarks), so it works smoothly instead of in stalls. |
| `vm.min_free_kbytes` | 262144 | Emergency free memory reserve (256 MiB) so sudden allocation bursts on large-RAM hosts do not fall into synchronous direct reclaim (`allocstall`). |

Two more kernel reclaim knobs persist via `/etc/tmpfiles.d/cst-mm-responsiveness.conf` and apply live in `apply`:

| Knob | Value | Why |
|---|---|---|
| `/sys/kernel/mm/transparent_hugepage/defrag` | `defer+madvise` | Avoids synchronous page-fault compaction pauses on `MADV_HUGEPAGE` callers (browsers, VMs, games, toolchains) while keeping `kcompactd` active in the background. |
| `/sys/kernel/mm/lru_gen/min_ttl_ms` | 1000 | Sets a 1-second Multi-Gen LRU working-set protection floor so transient cache bursts cannot immediately evict newly faulted desktop pages. |

The `cpu` module's memory experiments `wsf-300` (changes
`vm.watermark_scale_factor` to 300) and `thp-defer` (changes THP `defrag` to
`defer`) change those two knobs at runtime. While either is on, `plan` and
`status` show its row as `exp` (not `DIFF`) with a note (one for each, when
both are on), also inside a toolbox,
and `apply` leaves that setting alone (it says `kept:`), so it does not end a
measurement silently. After `cst cpu experiment off NAME` the old value is
back; `apply` sets CST's default if it was something else (`minfree-256m` matches
the `vm.min_free_kbytes = 262144` default already).

### 2.5 `MemoryLow=48G` for the VM

The VM (libvirt `qemu:///system`) runs in `machine.slice`. With
swappiness 100, its 48 GiB of RAM would be a prime target for swapping.
`MemoryLow` is a **soft floor**: while other cgroups still have memory that
can be reclaimed, the kernel takes it from them first. When nothing else is
left, the floor is ignored, so it cannot starve the host. The cgroup
filesystem is mounted with `memory_recursiveprot`, so the protection reaches
the VM's own scope without any libvirt setting. Raise the value in
`units/machine.slice.d-10-swap-memory-low.conf` if the VM grows.
`apply`'s `daemon-reload` puts the new value into the running slice's
`memory.low` (no VM restart), and its verification checks it.

The drop-in is `machine.slice.d/10-swap-memory-low.conf`. The `cpu` module
has its own drop-in in the same folder, `10-cpu-setup.conf`, with
`CPUWeight=50`; the two set different things and do not clash. Both
names start with `10-` on purpose: systemd applies the drop-ins of a unit
in file-name order and the last one wins, and `systemctl set-property`
writes its values as `50-<Property>.conf` (`50-MemoryLow.conf`). A value
set that way (also with `--runtime`) still wins after a `daemon-reload`.
Until 2026-11 the file was `50-memory-low.conf`, which sorts after
`50-MemoryLow.conf` (upper case first), so every reload put the 48G back
over a value set at run time. `plan` shows that old file as `remove`, and
`apply` deletes it and its line in the install record, but only when CST
(or `apply-swap-config.sh`) wrote it: a file of that name with no
"Managed by" comment of theirs is left alone,
and only its line in the install record goes (as when the file is gone). While a game
runs, the `cpu` module's game profile gives the game's cgroups
`MemoryLow=16G` (runtime only); it does not touch `machine.slice`'s memory
settings. `cst swap status` shows both floors in its `low` column.

**A MemoryLow from another drop-in.** `plan`, `status` and `apply` ask
systemd (`systemctl show machine.slice`, on the host from a toolbox) which
`MemoryLow` it applies and from which drop-ins. When CST's drop-in is loaded
and a drop-in that systemd applies after it sets another value (a
`systemctl set-property [--runtime] machine.slice MemoryLow=...`, or a
`50-memory-low.conf` someone else wrote), the row is `over`: it shows
systemd's value and names that drop-in, and `apply` leaves it (it does not
fail on it, and writes nothing for it). That is the point of the `10-` name.
`DIFF` is left for a cgroup whose `memory.low` differs from the value
systemd reports as CST's. When systemd cannot be asked, the cgroup is
compared as before. The `cpu` module's game profile sets `AllowedCPUs`,
`CPUWeight` and `IOWeight` on `machine.slice` at run time, never
`MemoryLow` (its `zz-cst-game.conf` drop-ins are for the user's slices), so
it does not make this row `over`. `cst doctor` gives the same answer (an
`info` line naming the drop-in).

**Not managed here** (done once, by hand): `freePageReporting='on'` on the
virtio memballoon of the `win11-devbox` VM, which lets the guest hand freed
memory back to the host.

### 2.6 `MemoryMin=1G` for the active session (`uresourced`)

Fedora's `uresourced` sets `MemoryMin=250M` by default across `user.slice ->
user-<uid>.slice -> user@<uid>.service -> session.slice`, which is smaller than a
modern GNOME Shell / compositor working set. In Linux cgroup v2, effective
`memory.min` is capped by every ancestor slice along that chain, so raising
`session.slice` alone would be clamped at `user.slice` / `user-<uid>.slice`.

`units/uresourced.conf` installs `/etc/uresourced.conf` preserving Fedora's
defaults (`MaxMemoryMin=10%`, `IOWeight=500`, `CPUWeight=500`, `AppBoost`
weights, and no `MemoryLow` so `cpu/game-profile` owns soft protection) while
raising `[ActiveUser] MemoryMin=1G`. `MaxMemoryMin=10%` caps it: on a machine
with less than 10 GiB of RAM, uresourced gives 10% of `MemTotal` (in whole
pages) instead, and `plan`, `status` and `apply` compare
`user.slice memory.min` with that capped value (within one page).

`apply` writes `/etc/uresourced.conf` when it differs, whether or not
uresourced is installed (it takes effect once it is), after saving a one-time
backup of a pre-existing file that is not CST's as
`/etc/uresourced.conf.pre-swap-config.bak`. It restarts `uresourced.service`,
when active, if it rewrote the file **or** the live `user.slice memory.min`
differs from what the file asks (uresourced started before the file, or did not
apply it); `systemctl --user daemon-reload` or a new login propagates the
drop-in to a live `session.slice`. What the file asks is `[ActiveUser]
MemoryMin` for each user with a session in a seat's foreground (logind's
`/run/systemd/seats`, `ACTIVE_UID`; one user in front of two seats counts
once), capped in total by `[Global] MaxMemoryMin`, as uresourced computes it.
A greeter (the login screen, a uid below 1000) is no such user; an
`ACTIVE_UID` of other than ASCII digits (0-9) is no uid, so it counts as a user,
as a seat without one does. A cgroup value (`memory.min`, `memory.low`,
`memory.swap.current`) of other than ASCII digits is shown as it is and is no
match.
`user.slice memory.min` is inactive (`-`), not a difference, when uresourced
is not active, and when it reads 0 while no such user is in front of a seat (a
server, an ssh login, the login screen), since uresourced then protects nobody.

---

## 3. Quick start

On a new machine, first name the partition in `/etc/cst/swap.conf`
([section 2.3](#23-the-encrypted-swap-partition)); `apply` refuses without it.

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

`apply` writes its five files (`zswap.conf`, `cst-mm-responsiveness.conf`,
`99-swap-tuning.conf`, `uresourced.conf`, and `10-swap-memory-low.conf`) through
CST's installer and lists them in the install record
`/var/lib/cst/installed.json` (component `swap:core`), where `cst doctor` checks
them.

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
| `over` | `machine.slice`'s `MemoryLow` comes from a drop-in systemd applies after CST's: the row names it, `apply` leaves it ([section 2.5](#25-memorylow48g-for-the-vm)). |
| `write`, `update`, `mask`, `remove` | `apply` would write, rewrite, mask or delete that file. |
| `start`, `restart`, `swapoff` | `apply` would activate `swap0`, re-activate it, or swap off `zram0`. |
| `DIFF` | The live value differs from the desired one; `apply` sets it. |
| `MISSING` | This kernel has no such zswap parameter (a new kernel renamed or dropped it): `systemd-tmpfiles` skips it at boot without a word, and `apply` cannot set it. `apply` skips it (a `skipped:` line) and goes on; `cst doctor` warns about it too. |
| `later` | Needs a reboot (for example, `swap0` is in use, so it keeps its old options). |
| `?` | Could not be read without root (as root: `apply` stops before it writes anything). |
| `REFUSE`, `CONFLICT` | `apply` would stop: no partition configured or a problem in `/etc/cst/swap.conf` (section 2.3), wrong partition, or an entry for `swap0` that was written by hand. |

What `apply` does, in order:

1. **Checks** that a partition is configured (`/etc/cst/swap.conf`,
   section 2.3), that zswap exists, that the `zstd` compressor loads, and the
   partition (exists, exact size, no filesystem signature), that no `swap0`
   line was written by hand, and that `/etc/crypttab` and `/etc/fstab` can
   be read (a folder in place of one is refused): a refusal stops it before
   it writes anything. On a machine an older CST set up, it then writes
   `/etc/cst/swap.conf` (section 5).
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
6. **memory responsiveness**: writes
   `/etc/tmpfiles.d/cst-mm-responsiveness.conf`, then sets THP `defrag` and
   MGLRU `min_ttl_ms` live (a knob this kernel lacks is skipped; THP `defrag`
   is kept while the `cpu` experiment `thp-defer` is on).
7. **session memory floor (uresourced)**: backs up a pre-existing
   `/etc/uresourced.conf` that is not CST's, writes CST's, and restarts
   `uresourced.service` when active if the file changed or the live
   `user.slice memory.min` differs (section 2.6).
8. **machine.slice**: writes the drop-in, removes the one under its old name
   (`50-memory-low.conf`, section 2.5); `daemon-reload` when a file was
   rewritten or removed (also for a renewed comment, for the same reason as
   in step 2).
9. **Verification**: the live settings against this folder, `swap0` active,
   `zram0` gone; `swapon --show`. Any `DIFF` makes `apply` end with an error.

Files are written only when they differ, always atomically. Each step's lines
are printed while `apply` runs. A command that fails (`blockdev`,
`systemctl`, `sysctl`) stops `apply` there with that command's own message
and exit status (128+N when signal N killed it, as from bash, with a line
`cst swap: COMMAND was killed by signal N` where bash printed `Terminated`);
the steps before it stay done. A closed pipe (`cst swap status | head`) stops it
quietly. `/etc/fstab` and
`/etc/crypttab` are read and rewritten byte for byte: the lines that are not
CST's come back exactly as they were, also bytes that are not UTF-8.

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
  adopted: rewritten with the new header. The old script's
  `machine.slice` drop-in, `50-memory-low.conf`, is removed instead, and
  the drop-in is written under its new name (section 2.5), with a
  `daemon-reload`.
- On sophia, the first `cst swap plan` after the move showed `adopt` for
  the four other items (crypttab needs `--root` to show it) and `ok` for
  the rest.
- **The partition settings (2026-11).** CST used to have the partition
  in its code. A machine it set up then has CST's marked lines in
  `/etc/crypttab` and `/etc/fstab` and no `/etc/cst/swap.conf`. There,
  `plan --root` shows `write` for `cst/swap.conf` with the values `apply`
  will write: the name and PARTUUID from the marked crypttab line, the size
  that partition has now (trusted on this first use; from then on `apply`
  checks it every time). `apply` writes the file (`== settings`, after all its
  checks, also those for a crypttab or fstab line written by hand or a crypttab
  or fstab root cannot read, and before
  anything else), and the next `plan` shows `ok`. As your
  user (also in a toolbox) `/etc/crypttab` cannot be read: `plan` shows `?`
  and says so, from the marked `/etc/fstab` line. `apply` never writes over a
  `/etc/cst/swap.conf` that is already there, even one without a
  partition.
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
`memory.low`, live and desired. `-` for `machine.slice memory.low` means
systemd reports the slice not active (no VM running): a cgroup folder left
behind is not read as the slice's. When systemd cannot be asked, a missing
`memory.low` means the same. A slice systemd reports active whose cgroup has no
`memory.low` (the memory controller is not on for it) is `-` too, with
`slice active, no memory.low`: the floor is then not applied.

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
pages zswap did not take, by reason. Three of them do not mean what their
kernel names suggest:

- `reject_compress_fail` and `reject_compress_poor`: a page did not
  compress (`_poor`: the compressor said its output did not fit in a page;
  `_fail`: any other compressor result) while its cgroup had zswap
  writeback switched off. zswap normally stores such a page as it is, but
  only when it may write it on to the disk later; with writeback off it
  refuses the page, and the page stays in RAM. The `cpu` module's game
  profile switches writeback off for `app.slice` while a game runs (see
  [../cpu/README.md](../cpu/README.md)), so these counters grow during
  games. That is expected, not an error.
- `reject_reclaim_fail`: the zswap shrinker tried to write a page from the
  pool to the disk and could not. Most often the page was already in the
  swap cache (it is being read back), which is the kernel's normal sign to
  stop shrinking, because it reached pages that are in use. It does not mean
  the pool was full: that is `pool_limit_hit`.

The others: `reject_alloc_fail` / `reject_kmemcache_fail` (no memory for
the compressed copy or for the entry). `stored_incompressible_pages` counts
pages in the pool that did not compress and are stored as they are (section
2.1). `written_back_pages` is the same as `zswpwb`.

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

**diagnosis**: objective findings evaluated from the live memory probes
(`perf` memory axis: `vm.min_free_kbytes`, `allocstall` / `compact_stall`, THP
defrag, MGLRU `min_ttl_ms`, `zswap`, memory PSI, and `session.slice` floors; see
[../perf/README.md](../perf/README.md)).

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
3. `rm /etc/cst/swap.conf /etc/tmpfiles.d/zswap.conf /etc/tmpfiles.d/cst-mm-responsiveness.conf
   /etc/sysctl.d/99-swap-tuning.conf /etc/systemd/system/machine.slice.d/10-swap-memory-low.conf`
   and, if `/etc/uresourced.conf.pre-swap-config.bak` exists, restore it:
   `mv /etc/uresourced.conf.pre-swap-config.bak /etc/uresourced.conf` (else
   `rm /etc/uresourced.conf`). Restart `uresourced` if active (`systemctl restart uresourced`).
4. To get Fedora's zram back: `rm /etc/systemd/zram-generator.conf` (the
   `/dev/null` symlink).
5. `systemctl daemon-reload`, then reboot (zswap and the sysctls/sysfs knobs
   return to kernel defaults at boot).

---

## 9. Files in this folder

| File | Installed as / used by |
|---|---|
| `tmpfiles/zswap.conf` | `/etc/tmpfiles.d/zswap.conf` |
| `tmpfiles/cst-mm-responsiveness.conf` | `/etc/tmpfiles.d/cst-mm-responsiveness.conf` |
| `sysctl/99-swap-tuning.conf` | `/etc/sysctl.d/99-swap-tuning.conf` |
| `units/uresourced.conf` | `/etc/uresourced.conf` |
| `units/machine.slice.d-10-swap-memory-low.conf` | `/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf` |
| `domain.py` | pure swap domain model, the partition (`SwapHardwareSpec`, read from `/etc/cst/swap.conf`), parsing, and metrics |
| `ports.py` / `adapters.py` | Linux system ports (`SwapSystemPort`) and adapters over `blockdev`, `blkid`, `modprobe`, `sysctl`, `zramctl`, `swapon` |
| `application/` / `presenter.py` / `cli.py` | pure use cases: `plan` and `status` in `application/reports.py`, memory reclaim and `uresourced` helpers in `application/memory_tuning.py`, `apply` in `application/apply.py` (apply hands each event to a callback as it happens), the command line's words and where it stops (help, a wrong argument) in `application/command_line.py`; the report renderer and `run_command` (prints apply's steps as they run, turns a failed command into its message and exit status); and the CLI entrypoint (called by the thin `../swap-setup.sh` shim) |
| `wiring.py` | the composition root: the environment, the sudo re-run of `cli.py` (with `CST_PROG` and the caller's `LC_CTYPE`), and the use cases built over the real adapters. `CST_SWAP_PROC` and `CST_SWAP_SYS` move where `/proc` and `/sys` are read and written (the cgroup tree stays `/sys/fs/cgroup`): for the tier 1 E2E test only, whose container has a read-only `/proc/sys` and `/sys` of the host's kernel. When either is set, `plan`, `status` and `apply` say so on their first line (`test seam (...)`), so a stale export in a root shell cannot pass for the kernel's state; the line also says that `/etc` is the real one and that `apply`'s `sysctl`, `modprobe` and `zramctl` are the ones on `PATH` (the tier 1 test puts stand-ins there) |
| `tests/test_swap.py` | unit tests: `python3 -I -B -m unittest discover -s swap/tests -t .` from the repository folder runs this module's tests; all of CST goes through `python3 -I -B tests/run_unit.py` ([AGENTS.md, "Commands"](../AGENTS.md#commands)) |

The swap partition's PARTUUID, device name and expected size are in
`/etc/cst/swap.conf` (section 2.3), read by `parse_swap_settings` in
`swap/domain.py`.
Shared file/marker helpers and system ports live in `../cst_core/`.

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
