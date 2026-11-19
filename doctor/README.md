<!-- SPDX-License-Identifier: MPL-2.0 -->
# `cst doctor`: a read-only check-up

This folder holds `cst-doctor`, the program behind `cst doctor`. It is not a
module with its own setup script: it installs nothing and has nothing to
undo, so `cst` runs it directly (`cst doctor`), and `cst status` runs its
short form as its last section. How to set up `cst`:
[../README.md](../README.md#the-cst-command).

> **In short**
>
> - `cst doctor` **only reads**. It never changes a file, a package or a
>   service, and it never asks for `sudo`.
> - It prints one line per check, grouped by area: `ok`, `info`, `WARN` or
>   `ERROR`. When there is a command that fixes a `WARN` or `ERROR`, the
>   line under it, `fix:`, is that command alone, ready to paste. You decide
>   whether to run it. Some lines have no fix, for example "not checked
>   (...)" when a query failed.
> - It ends with a summary line. Exit status: **0** when everything is ok
>   (or only `info`), **1** when there is at least one `WARN` or `ERROR`,
>   **2** when the command line was wrong.
> - It runs as your user, also inside a toolbox (there it asks the host).
>   In a toolbox the fix line reads `fix (on the host):`: the commands only
>   work on the host, so run them in a host terminal.

---

## 1. Quick start

```
cst doctor            # every check, grouped by area
cst doctor --brief    # only the summary and the problems (what cst status shows)
cst doctor help
```

## 2. What it checks

| Area | Check | Level when it finds something |
|---|---|---|
| `rpm-ostree` | Layered packages requested **with a version** (`containerd-2.3.4-1.fc44` instead of `containerd`), in the booted deployment and in the one of the next boot | `WARN` for the deployment of the next boot (see [section 3](#3-why-pinned-layered-packages-are-a-problem)) |
| `rpm-ostree` | Local RPM files layered (`rpm-ostree install ./file.rpm`) | `WARN`: upgrades never update them |
| `rpm-ostree` | Base packages replaced by another version (`rpm-ostree override replace`) | `WARN`: they stay at that version through every upgrade |
| `rpm-ostree` | Base packages removed (`rpm-ostree override remove`) | `info`: usually on purpose |
| `rpm-ostree` | A deployment waiting for a reboot: a staged one (and how old it is), or one left first by `rpm-ostree rollback` | `info` |
| `services` | Failed systemd units of the system and of your user | `WARN`; transient units of `systemd-run` (`run-*.scope`, `run-*.service`) only `info` (low priority) |
| `alerts` | CST desktop alerts kept for the next login in `/var/lib/cst-alerts/pending` (last 7 days) that you have not seen yet | `ERROR` for a critical alert, `WARN` for a normal one, fix: `cst-pending-alerts --list` (or, when `/usr/local/bin/cst-pending-alerts` is missing and no login would show it, `cst btrfs install`); `info` once you have seen it |
| `alerts` | Installed units whose programs alert through `/usr/local/sbin/btrfs-notify` (`btrfs-bulk-boot`, `btrfs-health-check`, `btrfs-snapshot`, `btrfs-snapshot-nas`, `btrfs-stall-alert`, `docker-volumes-nocow`), while it is missing: their alerts reach no desktop, only the journal (`btrfs-bulk-boot`'s, `btrfs-snapshot-nas`'s and `docker-volumes-nocow`'s are also kept in the pending folder, so the row above shows them; so are `btrfs-snapshot`'s, except those of its conversion step, which is bash) | `WARN`, fix: `cst btrfs install`; nothing when none of them is installed |
| `alerts` | `/var/lib/f44-alerts` (a link or folder left by the versions named f44-tools) | `WARN`, fix: `cst btrfs install` |
| `btrfs` | How full the chunks are: data and metadata, used of allocated, and metadata as a share of data (see [section 6](#6-the-btrfs-check-chunks-and-unallocated-space)) | `ok` (numbers only) |
| `btrfs` | The weekly scrub's speed limit: `SCRUB_LIMIT` in `/etc/btrfs-setup/snapshots.conf` (read with bash's rules, as `cst btrfs install` reads it), the limit the drop-in of `btrfs-scrub-weekly.service` sets, and `scrub_speed_max` of each btrfs device in sysfs now ([btrfs/README.md, 4.3](../btrfs/README.md#43-weekly-scrub)) | `info` with the limit and the value now; `WARN` when the setting is not a plain size (install refuses; one per bad `SCRUB_LIMIT` line, with its line number as bash counts it, and, when CST's Python programs find a problem on that line too, what the settings files row below says of it) or differs from the service (fix: `cst btrfs install`); nothing when the scrub unit is not installed and the setting is fine |
| `btrfs` | Automatic data reclaim (`dynamic_reclaim`, `periodic_reclaim`) on | `WARN` when off on the root filesystem, fix: `cst btrfs install`; `info` on other btrfs filesystems or on kernels without these switches |
| `cpu`, `btrfs`, `vm` | Lines of `/etc/cpu-setup/cpu-setup.conf`, `/etc/btrfs-setup/snapshots.conf` and `/etc/cst/vm.conf` that are not plain `KEY=VALUE` assignments as CST's one reader reads them (bash's rules for the first two: older CST versions sourced both in bash, and `btrfs-snapshot` reads `snapshots.conf` with the same rules; `vm.conf` also allows blanks around `=`): blanks around `=`, a `$`, backtick or backslash outside single quotes, an unclosed quote, text after the value (in `snapshots.conf`, a value an older CST wrote with backslashes, `a\ b` or `$'...'`, is read as bash reads it and is no problem: [btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md#how-cst-writes-snapshotsconf)) | `WARN` per line, with its line number: CST's Python programs take nothing from it (for `cpu-setup.conf` the warning says the game profile does not switch on; for `snapshots.conf`, that `btrfs-snapshot` ignores it too; a bad `SCRUB_LIMIT` line there gets one warning, the scrub row's, which says this too; the two rows match a line by its place in the file, as bash ends a line only at a newline and these readers at a carriage return too), and the cst commands that change the machine with that file (`cst cpu vm-pin apply`, `cst vm backup`, ...) refuse to run. The game profile does not switch on over such a line (and its `tick` changes nothing); `btrfs-snapshot` (and `cst btrfs snapshots status`) ignore it with a warning in the journal and do not refuse: the snapshot runs without it. Nothing when the files are fine or absent |
| `cpu` | While the game profile is on (`/run/cpu-setup/game.state` exists): the settings it made that no longer hold ("drift"), compared with what it recorded in `/run/cpu-setup/game.intended` right after `on` (see [section 8](#8-the-cpu-checks-game-profile-and-user-manager)) | `WARN`, fix: `sudo systemctl restart cpu-game.service` (`cst cpu game off && cst cpu game on` for a profile switched on by hand); `ok` when they all hold, or "game profile: off"; `info` when a profile switched on by an older version recorded nothing, when `game.intended` is of a newer format than this CST reads, or its first line names the format with no version number ("drift not checked"; systemd is not asked), or a unit could not be asked; nothing when the cpu module is not installed |
| `cpu` | While the game profile is off: its MemoryLow drop-ins `/run/systemd/system/*.d/zz-cst-game.conf` still there | `WARN`, fix: `sudo /usr/local/sbin/cpu-game-profile off` |
| `cpu` | Your user manager's cgroup tree (`/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service`): top-level slices other than `app.slice`, `session.slice`, `background.slice` and `user.slice`, with the CPU time each used (`cpu.stat`); `systemd-run --user` jobs (`run-*.scope`, `run-*.service`) straight in `app.slice` | `WARN` per unknown slice, with the name to use instead (`user-<name>.slice`; no fix line, it is a change in whatever starts the jobs); one `info` line for the jobs, busiest first; `ok` when there are none; nothing when there is no user manager to look at (CI, containers) |
| `swap` | Each zswap parameter that `/etc/tmpfiles.d/zswap.conf` (installed by `cst swap apply`) sets and this kernel does not have in `/sys/module/zswap/parameters` (a new kernel can rename or drop one; `systemd-tmpfiles` then skips it at boot without a word) | `WARN` per parameter (no fix line: it needs another kernel or a change in CST); one `WARN` when the kernel has no zswap at all; `ok` when they are all there; nothing when the file is not installed |
| `swap` | The swap partition in `/etc/cst/swap.conf` (`SWAP_PARTUUID`, `SWAP_SIZE_BYTES`, `SWAP_NAME`; [swap/README.md, section 2.3](../swap/README.md#23-the-encrypted-swap-partition)), read as `cst swap` reads it (the same resolution, `resolve_settings` in `swap/domain.py`) | `ok` with the partition; `WARN` per problem line or value, and for a file that is not a regular file (a folder, a link that points nowhere), that root cannot read, or whose read fails for another reason than a refusal (an I/O error: `cst swap apply` refuses); `info` when this user may not read it (its mode, or a folder above it the user may not enter: on the host, `cst swap status --root` reads it), never "not configured"; `WARN` with the fix `cst swap apply` when the file is missing on a machine an older CST set up (its marked `/etc/fstab` line, and `/etc/crypttab` with its marked line, or one this user may not read: apply writes the file from it); `WARN` "not configured" when the file has no partition, when swap is installed without it, or when an older CST's marked `/etc/fstab` line has no marked `/etc/crypttab` line to write it from (none there, or one naming a partition that is not here: `cst swap apply` refuses); nothing where swap is not set up |
| `swap` | `machine.slice`'s `MemoryLow` as systemd applies it (`systemctl show`), read as `cst swap status` reads it | `info` naming the drop-in when one that systemd applies after CST's `10-swap-memory-low.conf` sets another value (status shows it as `over`; `cst swap` leaves it); `ok` when CST's value applies; nothing when CST's drop-in is not installed or systemd cannot be asked |
| `vm` | A libvirt VM with `<cpu mode='host-passthrough'>` that does not disable CET and nested MBEC (`cet-ss`, `cet-ibt`, `vmx-mbec`, `vmx-exit-save-cet`, `vmx-entry-load-cet`) while this QEMU offers them (see [section 7](#7-the-vm-checks)) | `WARN`, fix: `cst cpu vm-pin plan`; `ok` when QEMU does not offer them; `info` when that could not be read |
| `vm` | The virtualization packages (QEMU, libvirt, UEFI firmware `edk2-ovmf`, `swtpm`, `libtpms`) differ between the rollback and the booted deployment | `WARN` when a VM exists and the rollback is not pinned (fix: `sudo ostree admin pin N`) or no VM backup, local or on the NAS, was made since the booted deployment was built (fix: `cst vm backup --nas` when the NAS backup is set up, else `cst vm backup`); `info` when both are in place; `ok` when nothing changed |
| `vm` | A disk of a VM that points to an overlay of a NAS backup (`<image>.cst-overlay-<time>`, in the running VM or in its definition): `cst vm backup --nas` could not merge it back | `WARN`, fix by case: still running on the overlay: `virsh blockcommit`; only the definition names a stale overlay: `virsh edit` (never `qemu-img commit`); shut off on a newer overlay: `qemu-img commit`; unclear: a command that only looks (see [section 7](#7-the-vm-checks)); `info` while that backup is still running |
| `vm` | `/var/lib/cst/vm-backups.json` of a newer format than this checkout's `cst vm` reads (its `"version"`; written by a newer CST) | `WARN` (no fix line: use the `cst vm` of that CST); the virtualization packages row then says `last backup: unknown` |
| `vm` | `/var/lib/cst/vm-backups.json` there but not readable as an index (not JSON, a byte that is not UTF-8 included, as `cst vm` reads it; no `"backups"` list; a read error) | `WARN` naming the reason: `cst vm` refuses to back up or list with it; move it aside to start a new index. The virtualization packages row then counts no backup from it (`last backup: never`) |
| `vm` | The weekly NAS backup timer of each VM (`cst-vm-backup@VM.timer`) | `info`: on or off. Never `WARN` |
| `vm` | A VM's weekly NAS backup timer is on, but `/usr/local/sbin/btrfs-notify` (which a failed backup alerts through) is missing, as `cst btrfs uninstall` leaves it when snapshots are not set up: a failed backup then reaches no desktop, only the journal and the pending alerts (the `alerts` rows) | `WARN`, fix: `cst btrfs install` (it installs `btrfs-notify` with the alerts kept for the next login); nothing when no timer is on |
| `install` | What CST installed, from its install records (`/var/lib/cst/installed.json` for root's files, `~/.local/state/cst/installed.json` for yours; see [section 9](#9-the-install-check)): per recorded file, missing; changed on disk since CST wrote it (its sha256 differs from the record's); a component installed only in part (its setup command started writing and did not finish: the record says `pending`); a unit whose CST program (a word under `/usr/local/` or `%h/` in its command lines: `ExecStart=`, `ExecStartPre=`, `ExecStop=` and the like, not `ExecSearchPath=` or `ExecPaths=`) is missing or not executable; a record written by a newer CST or that is no record | `WARN`, fix: the command that installs the component again (`cst btrfs install`, `cst btrfs docker-gc enable`, ...) |
| `install` | A recorded file older than this checkout (what CST installed differs from what this checkout installs now, a template rendered with the values it was installed with); a mode other than the one CST installed; a file not readable as your user (the record is then compared with this checkout only); a record you may not read | `info`, one line per component for the older files, naming the command that refreshes them. A file a module renders itself (the sccache wrapper) and generated files (units of `cst vm` and `cst toolchains`, the LDAC watcher's unit, the files of `cst btrfs degraded-boot`, the scrub drop-in, the stall-report launcher, cpu's hwp tmpfiles file and toolbox drop-ins) are only checked against the record |
| `install` | Without a record (an install from before it): each component's installed files compared with this checkout | `info` per component with files that differ, naming the command that installs and records them; nothing for a component that is not installed, or when this checkout cannot build a file to compare with |
| `install` | Every recorded file as CST installed it | `ok`, with the number of files per record |
| `vm` | A VM whose disks or network card differ from what `cst vm tune` recommends (SATA/IDE disks instead of virtio-scsi, disk driver without `cache='none' io='native' discard='unmap' detect_zeroes='unmap'`, e1000/e1000e/rtl8139 card; see [section 7](#7-the-vm-checks)) | `info`, naming the first differences and `cst vm tune plan VM`; `ok` when it matches. Never `WARN` |
| `vm` | A VM image folder on btrfs (`/var/lib/libvirt/images`, `~/.local/share/libvirt/images`, the two GNOME Boxes folders) without the NOCOW mark (`+C`), or with images still copy-on-write | `info` (`cst vm nocow apply` for the mark; `cst vm nocow plan` for how to convert an image); `info` "not readable as your user" for root's folder, never a finding; `ok` when NOCOW and clean; nothing for folders that are not there (CI, containers). Never `WARN` |
| `btrfs` | Unallocated space per disk, estimated from sysfs | `WARN` below 50 GiB, `ERROR` below 5 GiB (the same levels as the hourly health check: those of the installed `btrfs-health-check`, the one that alerts (its `btrfs/health_check/domain.py` inside the zipapp, or the text of a bash version), else of this checkout's `btrfs/health_check/domain.py`; no fix line: follow the recovery steps); `ERROR` also when metadata is almost full; `info` "needs root" when the layout gives no per-disk number |
| `btrfs` | Snapshots left writable (see [section 6.1](#61-snapshots-left-writable)): names in `/var/lib/btrfs-snapshot/core-unfinished` (an hourly run stopped between taking a snapshot and making it read-only), and writable entries named like CST's snapshots in `/var/lib/btrfs-snapshot/core-writable` that no run left | `WARN`; fix: `sudo systemctl start btrfs-snapshot.service` for the first, `sudo btrfs subvolume show <path>` (it only looks) for the second; `ok` when there are none; `info` when the lists cannot be read; nothing when snapshots were never set up |
| `btrfs` | The boot-time NOCOW migration of Docker volumes postponed (see [section 6.2](#62-docker-volumes-nocow-migration-postponed)): the record `/var/lib/docker-volumes-nocow/postponed` that `docker-volumes-nocow` keeps of the boots it had to wait at | `WARN` from two boots on, with the reason of the last one; fix: `journalctl -b -u docker-volumes-nocow` (it only looks), or for a `/etc/docker/daemon.json` that cannot be read `sudo dockerd --validate --config-file /etc/docker/daemon.json` (it only checks the file); `info` for one boot, or when the record cannot be read; nothing when there is no record |

"Deployment" is the Silverblue word for one bootable version of the system.
The **booted** one is running now; a **staged** one was prepared by an
update and becomes the system at the next boot. After `rpm-ostree rollback`
the next boot is an older deployment that is not staged: `cst doctor` calls
it **pending (next boot)**. Either way, the next boot is the first
deployment that `rpm-ostree status` lists, and it is the one that
`rpm-ostree install` and `uninstall` change.

Not checked: whether a pinned version is still in the Fedora repositories.
That needs the repository metadata, which only a slow, networked query has;
`cst doctor` stays fast and offline.

## 3. Why pinned layered packages are a problem

On Silverblue, extra packages are **layered** on top of the system image
(`rpm-ostree install NAME`). rpm-ostree remembers exactly what you typed and
asks for it again at every update.

- If you typed a **name** (`containerd`), every update takes the newest
  `containerd` of the repositories. This is what you want.
- If you typed a **full version** (`containerd-2.3.4-1.fc44`), rpm-ostree
  asks for **that exact version** at every update. The package never gets
  security fixes. And Fedora keeps only the newest version in its update
  repositories: once `2.3.4-1.fc44` is gone, every update fails with
  "package not found", and the system stops updating.

This happens easily by copy and paste, for example from the output of
`rpm -q` or from an error message. The rule here: **layer by name only**.

The fix is one transaction that drops the pinned requests and asks for the
same packages by name:

```
sudo rpm-ostree uninstall containerd-2.3.4-1.fc44 pigz-2.8-8.fc44 --install=containerd --install=pigz
```

`cst doctor` builds that command for you, from the requests of the
deployment of the next boot (the staged or pending one if there is one,
else the booted one). Then reboot. Some of the packages may only be there as dependencies of
another one (for example `containerd` for `moby-engine`): you may leave
their `--install=` out, and rpm-ostree still brings them in.

How it recognises a pinned request: it reads the deployment's package list
(`rpm-ostree db list CHECKSUM`) and compares. A request that is the exact
name of an installed package is fine, even with digits and dashes in it
(`python3.13`, `gtk4`, `java-21-openjdk`). A request that is the
name-version-release (with or without epoch and architecture) of an
installed package is pinned, and the fix uses that package's real name. Only
when the package list cannot be read does it fall back to the form
`name-version-release.fcNN` and says so. A request such as `vim` that
installs a package with another name (`vim-enhanced`) is a name, not a pin.

The architecture is kept when it matters. A request that names one
(`wine-core-9.0-1.fc44.i686`), or that matches only a package of another
architecture than the machine's, comes back as `--install=wine-core.i686`.
A bare `--install=wine-core` would install the `x86_64` package instead and
break a 32-bit setup.

A request that looks pinned but matches no installed package is reported
on its own: it is probably an **inactive** request, for example because the
base image now has that package (another build). The fix drops it without
an `--install=` for it, because rpm-ostree refuses to layer a package the
base already has, and that refusal would cancel the whole transaction.

If the booted deployment has pinned requests but the next boot's one has
none (you already fixed it), you get an `info` line: reboot to use it.
Packages that came in only as dependencies are not requested by it at all,
which is fine.

## 4. How to read the output

```
cst doctor: read-only check-up (nothing is changed)

== rpm-ostree
  WARN   booted deployment 44.20261028.0: 13 of 21 layered packages are pinned to a version (...): containerd-2.3.4-1.fc44, ...
         fix: sudo rpm-ostree uninstall containerd-2.3.4-1.fc44 ... --install=containerd ...
  ok     booted deployment 44.20261028.0: no local RPM files layered

== services
  info   user: 1 transient unit(s) failed (systemd-run, low priority): run-p1964432-i22912428.scope; to clear: systemctl --user reset-failed run-p1964432-i22912428.scope

== alerts
  ok     no pending alerts

doctor: needs attention: 0 ERROR, 1 WARN, 1 info, 3 ok
```

- `ok`: nothing to do. `info`: worth knowing, nothing to do (it may show a
  command, if you want to tidy up). `WARN`: should be fixed; `ERROR`: fix
  soon.
- Every `fix:` is a suggestion: read it before you run it. It holds only
  the command; remarks are in the line above. For a failed unit, the line
  above names its log (`journalctl -b -u UNIT`): read it first, because the
  fix (`reset-failed`) only clears the mark, it does not fix the cause.
- Lines without a `fix:` have no single command that fixes them, for
  example "not checked (...)" or a check that failed itself (`ERROR`).
- A line "not checked (...)" means the query itself failed (for example
  `rpm-ostree` did not answer). It counts as a `WARN`.
- In `cst status` (and with `--brief`) you see the summary line and only the
  `WARN` and `ERROR` lines, each with its area in brackets.

## 5. Where it reads, and as which user

- On the host it runs the queries directly. Inside a toolbox it runs them on
  the host through `flatpak-spawn --host` (`rpm-ostree status --json`,
  `rpm-ostree db list`, `systemctl [--user] list-units --state=failed`) and
  reads host files under `/run/host` (`/run/ostree-booted`,
  `/var/lib/cst-alerts`, `/var/lib/f44-alerts`).
- The fixes it prints do not work inside the toolbox (there is no
  `rpm-ostree`, and `journalctl` there does not see the host's system log).
  So in a toolbox the output starts with a note, and each fix line reads
  `fix (on the host):`. Run them in a host terminal; a command without
  `sudo` also works with `flatpak-spawn --host` in front of it.
- The `vm` checks ask libvirt's system connection (`virsh -r -c
  qemu:///system list --all --name`, `dumpxml --inactive` and
  `domblklist VM --details`, never with `--security-info`, and `domstate VM` only when a
  disk points to a backup overlay; `virsh -c qemu:///system domcapabilities`, which libvirt
  does not answer on a read-only connection), always in the C locale
  (`LC_ALL=C`: virsh translates "running" and "shut off"), and read the world-readable
  index `/var/lib/cst/vm-backups.json` written by `cst vm backup`, the
  world-readable `/etc/btrfs-setup/snapshots.conf` (is the NAS backup set
  up?), the lock file `/run/cst-vm/nas-VM.lock` with `/proc/<pid>` (is a NAS
  backup running?) and `/etc/systemd/system/timers.target.wants` (is the
  weekly timer on?). Your user
  needs libvirt's permission for the system connection: membership of the
  `libvirt` group, or an equivalent polkit rule. `domcapabilities` is asked
  only when a VM lacks one of the disables. No `virsh`, no permission,
  libvirt not running, no VM: `info` or `ok`, never `WARN`. The hardware
  and image folder checks import the `vm` package of the same CST copy (no
  second copy of its rules) and read the folders' inode flags with an ioctl; they
  run no extra command.
- The `cpu` check reads `/run/cpu-setup/game.state`, `game.intended` (with
  the game profile's own reader: a last line cut short is skipped) and
  `manual` (the by-hand mark, which picks the fix command), and `TARGET_USER` in `/etc/cpu-setup/cpu-setup.conf` (all world-readable),
  and asks `systemctl show -p LoadState -p PROP... UNIT` once per unit, of
  the system and of your user manager (`systemctl --user show`; as root,
  `systemctl --user -M TARGET_USER@ show`). When the profile is for another
  user than the one running the doctor, the user manager's units are skipped
  (`info`). A `cpuset.cpus` file the profile wrote directly is read under
  `/sys/fs/cgroup`. While the profile is off it lists
  `/run/systemd/system/*.d/zz-cst-game.conf`. The check of the user
  manager's boxes reads only files: the folder names and `cpu.stat` under
  `/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service` (your uid,
  or `TARGET_USER`'s when run as root; under `/run/host` in a toolbox).
- The `btrfs` check reads `/sys/fs/btrfs` (under `/run/host` in a toolbox),
  asks `findmnt -t btrfs` on the host to name each filesystem by its mount
  point, and `stat -f` on that mount point for the filesystem's size. It
  never runs a `btrfs` command: those need root.
- The snapshots check reads `/var/lib/btrfs-snapshot/core-unfinished` and
  `core-writable` (world-readable, written by the hourly snapshot run) and
  `TOP`/`SNAP_DIR_NAME` from `/etc/btrfs-setup/snapshots.conf` for the path in
  its fix line. It never opens the snapshot folder (root-only) and runs no
  command. The names of those two files come from the `btrfs.snapshot`
  package of the same CST copy (what btrfs-snapshot-core writes).
- The Docker NOCOW check reads `/var/lib/docker-volumes-nocow/postponed`
  (world-readable, written by `docker-volumes-nocow` at boot). It never opens
  Docker's folders (root-only) and runs no command.
- Which alerts you have already seen comes from your record in
  `~/.local/state/cst-alerts/delivered` (written by `cst-pending-alerts` at
  login; the older `~/.local/state/f44-alerts` is read when the new one does
  not exist). `cst doctor` only reads it.
- Started as root (`cst status` asks `sudo` once for the modules that need
  it), `cst` runs the doctor as the user who called `sudo`, so it checks
  your user's units. Run as root without `sudo`, the user units are skipped.

## 6. The btrfs check: chunks and unallocated space

btrfs reserves disk space in pieces of about 1 GiB, the **chunks**: data
chunks for file contents, metadata chunks for the index of all files. Space
that is in no chunk yet is **unallocated**. Metadata can only grow into
unallocated space, so when that runs out btrfs fails with "No space left"
although `df` still shows free space inside data chunks. Why, and how to
recover: [btrfs/README.md, "When btrfs says 'No space left' but df shows free space"](../btrfs/README.md#when-btrfs-says-no-space-left-but-df-shows-free-space).

```
== btrfs
  ok     btrfs /var (RAID1): data chunks 729.4 GiB used of 930.0 GiB allocated (78%), metadata chunks 13.6 GiB used of 20.0 GiB allocated (68%), metadata is 1.9% of data
  ok     btrfs /var: automatic data reclaim on (dynamic, periodic): 0 chunk(s) reclaimed since mount
  ok     btrfs /var: unallocated per device /dev/mapper/luks-... 585.9 GiB, /dev/mapper/luks-... 585.9 GiB (warning below 50 GiB, critical below 5 GiB; ...)
```

- One block of lines per mounted btrfs filesystem, named by its mount point
  (`/var` for the root filesystem on Silverblue, where `/` itself is not the
  btrfs mount).
- **Unallocated per device** is computed from sysfs, without root: the size
  of each disk minus one copy of every chunk. That is exact for RAID1 over
  two disks (each disk holds one copy of every chunk), for RAID1C3/C4 over
  three/four disks, and for one disk with `single` or `dup`, as long as each
  btrfs device fills its whole block device. The check confirms that with
  `stat -f` on the mount point (the filesystem's size, as `df` sees it): when
  the btrfs devices are smaller than their disks (shrunk with
  `btrfs filesystem resize`), or the size cannot be read, the number is only
  an upper bound. It then still warns when even that bound is below a level
  (shown as "at most"), but never says `ok`: above the levels it shows
  `info: per-device unallocated needs root` instead. For other layouts, or with a disk missing, it
  shows the total over all disks (exact) and `info: per-device unallocated
  needs root: sudo btrfs filesystem usage -T /var`; it still warns when even
  the average per disk is below 50 GiB, because then at least one disk is.
  The hourly `btrfs-health-check` (root) always reads the exact numbers.
- The levels (50 GiB warning, 5 GiB critical) are the health check's own:
  `UNALLOC_MIN_GIB` and `UNALLOC_CRIT_GIB` in `btrfs/health_check/domain.py`.
  Doctor reads them from the installed `/usr/local/sbin/btrfs-health-check`
  first, since that copy is the one that alerts: from that module inside the
  zipapp, or from the text of a bash version installed before the port. Then
  from the checkout's `btrfs/health_check/domain.py`, and last from its own
  copy of that module.
- There is no `fix:` for low unallocated space: the way out is several steps
  (free space first, then a careful balance), not one command.

### 6.1 Snapshots left writable

The hourly snapshot run takes each snapshot writable, drops its cache
folders (`CACHEDIR.TAG`) and then makes it read-only
([btrfs/SNAPSHOTS.md, section 3](../btrfs/SNAPSHOTS.md#cache-folders-cachedirtag-dropped-from-every-new-snapshot)).
The snapshot folder is root-only, so this check does not open it. It reads
two lists the run keeps in `/var/lib/btrfs-snapshot` (world-readable):

- `core-unfinished`: snapshots taken and not yet read-only. A name stays
  there only when a run was stopped in between; the next hourly run
  finishes it. `WARN` until then, with the command that starts a run now.
  If the warning stays, the run's journal says why (usually: the listed
  entry is not a snapshot of that source, and it is left alone).
- `core-writable`: writable entries named like CST's snapshots that the
  last run saw. One that is not in `core-unfinished` was not left by a CST
  run (made by hand, for example). CST never deletes or counts it. `WARN`,
  with a command that only shows it.

```
== btrfs
  WARN   snapshots: Workspace.20261115T0400 left writable by an interrupted run (cache folders perhaps not dropped yet); the next hourly run finishes it (if this stays: journalctl -u btrfs-snapshot)
         fix: sudo systemctl start btrfs-snapshot.service
```

### 6.2 Docker volumes: NOCOW migration postponed

At every boot, before Docker starts, `docker-volumes-nocow.service` rewrites
Docker volumes that still hold copy-on-write data
([btrfs/README.md, section 4.10](../btrfs/README.md#410-docker-volumes-without-copy-on-write-nocow)).
It waits, and tries again at the next boot, when it cannot be sure nothing
uses the volumes: Docker is running, systemd gives no clear state for
`docker.service`, or another `dockerd` uses the same data folder. It waits too
when `/etc/docker/daemon.json` cannot be read (not JSON, for example): then
which volumes folder Docker uses is unknown. Such a boot
is not counted as a failed attempt and sends no alert, so a migration could
wait forever without a word. The helper records it instead, in
`/var/lib/docker-volumes-nocow/postponed`: the reason of the last boot, its
time, and how many boots were held back (once per boot). The first boot
that is not held back removes the record, and so does a boot that finds no
volumes folder (Docker removed, or its `data-root` moved).

One boot is `info`: it can happen once (Docker started by hand early, for
example). From two boots on it is a `WARN`: something holds the migration
back at every boot. The fix line only shows that boot's journal; for a
`daemon.json` that cannot be read the warning says to fix the file instead, and
the fix line is `sudo dockerd --validate --config-file /etc/docker/daemon.json`,
which checks it as dockerd reads it and changes nothing. The reasons
and what to do are in
[btrfs/README.md, section 13](../btrfs/README.md#docker-volumes-migration-postponed-at-n-boots-in-a-row).

```
== btrfs
  WARN   Docker volumes NOCOW: the boot-time migration was postponed at 3 boot(s) in a row, last 2026-11-15 08:02: Docker is running; the volumes keep copy-on-write data (and new ones may too) until a boot where nothing uses them
         fix: journalctl -b -u docker-volumes-nocow
```

## 7. The vm checks

**CET/MBEC on host-passthrough.** QEMU 11 (Fedora 45) passes Intel CET and
nested MBEC to a guest with `<cpu mode='host-passthrough'>`, and Windows 11
with Hyper-V/VBS/WSL2 then loops in Automatic Repair
([QEMU issue 3215](https://gitlab.com/qemu-project/qemu/-/issues/3215);
the full story: [vm/README.md](../vm/README.md)). `cst cpu vm-pin` adds the
`<feature policy='disable' .../>` lines.

"This QEMU offers them" is read from `virsh domcapabilities`: the
`<feature>` names of its `host-model` CPU. libvirt builds that list from what
the installed QEMU and KVM can give a guest on this CPU, so it is one query
that already accounts for the QEMU version, the kernel and the processor
(a QEMU version number alone would not know whether the CPU has CET). Only
the names that are both offered and in the list of `cst cpu vm-pin` count.
The list is `VM_CPU_DISABLE` in `/etc/cpu-setup/cpu-setup.conf` when that
line exists (empty switches the check off, on purpose), else
`CPU_DISABLE_DEFAULT` in `cpu/domain.py` (the list `cst cpu vm-pin` uses).

**Virtualization packages changed.** A Fedora upgrade brings a new QEMU,
libvirt, UEFI firmware and TPM emulator at once, and the guest sees new
virtual hardware. The way back is the **rollback** deployment (the one that
`ostree admin status` marks `(rollback)`: the next one after the booted
deployment). The check compares the package lists of the two
(`rpm-ostree db list`), for names starting with `qemu-kvm`,
`qemu-system-x86`, `libvirt-daemon`, `edk2-ovmf`, `swtpm` and `libtpms`.
When they differ and libvirt has at least one VM, it wants two things:

- the rollback deployment **pinned**, so that the next upgrades do not
  remove it. The fix is `sudo ostree admin pin N`, where N is the
  deployment's position in the list of `rpm-ostree status` (counting from 0,
  a staged deployment included), which is the index `ostree admin pin` takes:
  `ostree admin status` lists the same deployments in the same order.
  Unpin it later with `sudo ostree admin pin --unpin N`, once the VMs work on
  the new deployment;
- a **VM backup** made after the booted deployment was built: an entry in
  `/var/lib/cst/vm-backups.json` newer than the deployment's timestamp (only an
  entry `cst vm list` shows counts: one it keeps without understanding it, such
  as a NAS entry without its snapshot, does not). The fix is `cst vm backup`
  (with the VM shut off).

With both in place the line is `info`. A NAS backup (`mode` `nas` in the
index) counts like a local one. When the NAS backup of `/home` is set up
(`NAS_ENABLED=1` and `NAS_PATH` in `/etc/btrfs-setup/snapshots.conf`, a
world-readable file), the fix is `cst vm backup --nas`, which works with the
VM running; else `cst vm backup` with the VM shut off. Best practice: before
every Fedora major upgrade, back the VM up (`cst vm backup --nas --keep-tag
NAME`, and/or a local `cst vm backup` with it shut off); after it, keep the
old deployment pinned until the VM boots on the new one.

**A disk left on a backup overlay.** `cst vm backup --nas` puts a
short-lived overlay file (`<image>.cst-overlay-<UTC time>`) in front of each
disk of a running VM and merges it back at the end
([vm/README.md, section 10.2](../vm/README.md#102-how-a-running-vm-is-backed-up)).
When a disk still points to such a file, in the running VM
(`virsh -r domblklist VM --details`) or in its definition, the merge did not
finish: `WARN`. The fix depends on the case, which the doctor works out from
`virsh -r domstate` and the two listings:

- **Running (or paused) on the overlay:** the `virsh blockcommit` command.
  The text also covers a commit left "ready" at 100 %: `virsh blockjob VM
  DISK --pivot`.
- **Running on the image, but the definition names the overlay:** the overlay
  is stale. The fix is `virsh edit VM` to point the definition back, and the
  text says not to `qemu-img commit` it, which would overwrite newer data.
- **Shut off:** the doctor compares the times of the image and the overlay.
  If the overlay is newer, it holds the last writes and the fix is
  `qemu-img commit`. If the image is newer, the doctor cannot tell. A merge
  cut short also writes the image while the overlay is still needed, so
  the fix only looks (the `ls` command that shows the times), and the text
  says to check the backup's own message first. The same applies when the
  times cannot be read.
- **The state or the live listing cannot be read:** the fix only looks
  (`virsh domblklist`); nothing is called stale.

A disk whose definition has no `<target dev=...>` (libvirt refuses such a
definition, so this is only a safeguard) is shown as disk `?` and does not
stop the check for the other disks. While the VM runs, the doctor cannot
match that disk to the live listing, so its fix only looks
(`virsh domblklist`).

After the fix, check that both `domblklist` listings show the image again,
and only then remove the overlay file
([vm/README.md, section 10.8](../vm/README.md#108-if-a-backup-left-an-overlay)).
While the backup still runs, the line is `info` instead. "Still runs" means
its lock file `/run/cst-vm/nas-VM.lock` names a live process whose command
line is `cst-vm`, so a reused pid does not count.

**Weekly NAS backup timer.** One `info` line per VM: whether
`cst-vm-backup@VM.timer` is enabled (a link in
`/etc/systemd/system/timers.target.wants`). `cst vm backup-timer on VM`
turns it on.

**Hardware differs from `cst vm tune`.** For each VM, the definition
(the same `dumpxml --inactive` as above, asked once) goes through the
transform of `cst vm tune` as if the guest had its virtio drivers. Any
change it would make is listed in one `info` line, with
`cst vm tune plan VM`. For Windows the line adds that the drivers go in
first (the plan prints the steps). The knobs of `/etc/cst/vm.conf`
(`VM_TUNE_DISK_BUS`, `VM_TUNE_DISK_DRIVER`, `VM_TUNE_NIC`) are honoured.
Details: [vm/README.md, section 8](../vm/README.md#8-cst-vm-tune-the-recommended-virtual-hardware).

**VM image folders without NOCOW.** For each VM image folder that exists
on btrfs: no `+C` mark, or images in it without `+C`, is an `info` line.
The system folder `/var/lib/libvirt/images` is root's (mode 0711), so as
your user its flags cannot be read: the line then says so and points to
`cst vm nocow plan --root`; it never claims the folder is copy-on-write.
A folder that is a symbolic link is not checked. With `VM_NOCOW=no` in
`/etc/cst/vm.conf` the check only prints one `info` line saying the
folders are not managed. Details:
[vm/README.md, section 9](../vm/README.md#9-cst-vm-nocow-vm-image-folders-without-copy-on-write).

## 8. The cpu checks: game profile and user manager

### 8.1 Game profile drift

While a game runs, `cst cpu`'s game profile changes systemd properties with
`systemctl set-property --runtime` (CPU weights and CPU sets of the VM, the
toolboxes and Docker, and `MemoryLow` along the game's chain of boxes).
Those changes can be undone before the game ends: a `daemon-reload` reads
every drop-in again, and uresourced (a Fedora service) keeps
`50-MemoryLow.conf` files with `MemoryLow=0` that win over the profile's.
The profile's watchdog sets them again within 30 s. The check compares, for
each setting, the value the profile read back right after setting it with
the value systemd reports now, and says `WARN` for each one that differs:

```
== cpu
  WARN   game profile: ON, but 2 of its settings no longer hold: machine.slice CPUWeight is 50 (the profile set 10); user.slice MemoryLow is 0 (the profile set 17179869184). ...
         fix: sudo systemctl restart cpu-game.service
```

Right after a reload this can show for up to 30 s, until the watchdog's
next run; run the doctor again before acting. If it stays, the fix switches
the profile off (the exact revert) and on again. For a profile switched on
by hand (`/run/cpu-setup/manual` exists) the fix is
`cst cpu game off && cst cpu game on` instead: `off` removes the by-hand
mark, so after a plain restart the watchdog would switch the profile off
when no game runs. A setting whose record is missing from `game.state` is
one the watchdog does not set again (`off` could not undo it): the check
still warns about it, and the same fix, which writes fresh records, is the
right one. Details, and why each cause happens: [cpu/README.md, section 7.5](../cpu/README.md#75-when-something-undoes-a-setting-drift).

A profile switched on by a version of `cpu-game-profile` older than this
check recorded no values: the check says so (`info`) and compares nothing
until the profile is switched off and on again.

### 8.2 MemoryLow drop-ins left behind

While it is on, the profile also writes
`/run/systemd/system/<unit>.d/zz-cst-game.conf` for the system `user.slice`,
`user-<uid>.slice` and `user@<uid>.service`. Such a file wins over
uresourced's `50-MemoryLow.conf` at every `daemon-reload`
([cpu/README.md, section 7.5](../cpu/README.md#75-when-something-undoes-a-setting-drift)).
`off` and the watchdog remove them. One that is still there while the
profile is off keeps the game's memory protection until the next reboot, so
the check says `WARN`. The fix runs `off` once more: it removes the files
also without a `game.state`, and reloads systemd. (`cst cpu game off` would
not: with the profile already off, stopping `cpu-game.service` runs nothing.)

### 8.3 Boxes of the user manager outside CST's rules

The game profile handles the boxes it knows: `app.slice` (the game), the
toolboxes' `user.slice`, and, during games, every other top-level slice and
the `systemd-run` jobs in `app.slice`
([cpu/README.md, section 7.1](../cpu/README.md#71-what-it-does-while-a-game-runs)).
Outside games, a top-level slice of the user manager that CST does not know
runs at the default weight on all CPUs, beside the desktop. That is usually
a project that starts its jobs with `systemd-run --user --slice=NAME.slice`.
The check reads the user manager's cgroup folder (files only, no command):

```
== cpu
  WARN   user manager: lushtext.slice is a slice of its own next to app.slice, where the game runs (69,000 CPU-seconds (19.2 CPU-hours) since it was created). ... Name it user-lushtext.slice so it nests under the toolboxes' user.slice, or start it with --slice=user-lushtext.slice
  info   user manager: 1 systemd-run job(s) run straight in app.slice, next to the game, ...: run-u7.service (12 CPU-seconds). ...
```

The CPU time counts from when systemd made the box: a slice that emptied
and came back starts again at zero. `background.slice` (GNOME's file
indexer) and `session.slice` (gnome-shell) are known and not reported. The
user manager looked at is your own, or, when the doctor runs as root, the
`TARGET_USER` of `/etc/cpu-setup/cpu-setup.conf`.

## 9. The install check

`cst btrfs install`, `cst cpu install`, `cst vm backup-timer on` and the other
commands that install CST's files list each file they write in an **install
record**: `/var/lib/cst/installed.json` for the files root owns,
`~/.local/state/cst/installed.json` for the files in your home. A line per
file: where it came from in CST, its component (the part of a module one
command installs: `btrfs:core`, `btrfs:docker-gc`, ...), its sha256, mode and
owner, a template's values, and the commit of the CST folder that installed
it. A component is "pending" from the first write until its command finishes.

The `install` area compares three things: the record, the files as they are,
and what this CST folder would install now.

- A file the record lists that is gone, or whose bytes differ from the
  record's: something other than CST changed it. `WARN`, with the command
  that installs the component again.
- A component still "pending": its command was interrupted (an error, a
  Ctrl-C) between its first write and its end. `WARN`, same fix.
- A unit whose program is missing (a unit of one component running a program
  of another that was removed, for example). `WARN`.
- The record's bytes differ from what this folder installs now: the
  installed copy is **older** (or newer) than this checkout, the normal
  state after a `git pull` until the next install. `info`, once per component,
  naming the command that brings it up to date. The programs that timers run
  are the installed copies, not this folder's.
- No record at all (CST installed the files before it kept one): each
  component's files are compared with this checkout directly, as `info`.
  The next install of that component records them.

A file root may read only (the polkit rule's folder is
`drwxr-x--- root:polkitd`) is checked against the record only: `info`
"not readable as your user".

## 10. Adding a check and internal layout

The code is split into four layers under `doctor/`:

- `doctor/domain.py`: pure domain model, rules, parsers and formatting (no
  filesystem I/O or subprocess calls).
- `doctor/ports.py`: port protocols (`DoctorHostPort`, `CheckFn`, `VmToolsPort`, `InstalledFilesPort`).
- `doctor/adapters.py`: `Host` adapter (direct or `flatpak-spawn --host` /
  `/run/host`, as cst_core's `HostExecutionContext` detects, unless
  `CST_DOCTOR_CONTAINER` says; its commands go through cst_core's `SubprocessCommandRunner`
  with `on_host`, and virsh through cst_core's `SubprocessVirshAdapter` over
  the same runner (`Host.virsh`), in the doctor's own words for a command
  that is missing or does not answer; it also opens, lists and stats the
  host's files for the checks, and knows where the CST checkout is) and `VmTools` (the knobs and
  the image folder probe of `vm.adapters.images`), and `InstalledFiles` (the
  install records and the files this checkout would install).
- `doctor/application/`: the checks, one module per check area
  (`rpm_ostree.py`, `services.py`, `alerts.py`, `btrfs.py`, `cpu.py`,
  `swap.py`, `vm.py`, `install.py`), each with its own probes.
  `registry.py` holds `AREAS` (the area modules in their output order),
  `CHECKS` (their checks, in that order), `wired_checks` and `run_checks`.
  `shared.py` holds what several areas use: the `@check` decorator, the settings-file readers, and `DoctorFacts`: what
  several checks ask (rpm-ostree with every deployment, through cst_core's
  one query `cst_core.domain.ostree`, the libvirt domains, each VM's
  definition), asked once per `Host` and kept here, not on the host port.
  No direct I/O: files and commands go through `DoctorHostPort`
  (`tests/test_architecture.py` checks it).
- `doctor/presenter.py`: `render`, the report as text.
- `doctor/cli.py`: the command line: arguments, the VM checks wired with the
  VM tools, the report.
- `doctor/wiring.py`: the composition root: the environment, the signals, the
  `Host` (with the checkout the doctor runs from), `VmTools` and
  `InstalledFiles` (`installed_files()`).
- `doctor/cst-doctor`: the entry point: only the bootstrap and a `main()` that
  runs `cli.py`.

Each check is a small function in its area's module under
`doctor/application/`, registered with `@check(CHECKS, "area")` in that
module's `CHECKS` list. It gets a `DoctorHostPort` (how to run a query and
where host files are) and yields `Result(status, message, fix)`. Areas are
printed in the order of `AREAS` in `doctor/application/registry.py`, and the
checks of an area in the order its module registers them. A new area gets
its own module and a place in `AREAS`. A host file is named by a
`HostPath` (`/etc/cst/vm.conf`) and read through `host.path(...)`, which
gives the `ViewPath` this process can open (`/run/host/...` in a toolbox);
the host's file methods take a `ViewPath` only, so ty catches a path that
was not mapped or was mapped twice (`view_join` and `host_join` join on each
side). A check that crashes becomes one `ERROR` line; the others still run. A check must never change anything:
only read files and run query commands. Modules can add their own checks
here later.

All `doctor/` modules (including `doctor/tests/test_doctor.py`), like every
Python file, are under ruff's annotation rules ([docs/dev-tools.md](../docs/dev-tools.md),
"ANN applies to every file"): every function has type annotations, so a new check reads
`def check_name(host: DoctorHostPort) -> Iterator[Result]:`, and
`invowk cmd test types` (ty and ruff's ANN rules) fails until it does.

## 11. Tests

```
python3 -I -B -m unittest discover -s doctor/tests -t . -v
```

This runs the doctor tests only. The whole suite, the way CI runs it, is
`python3 -I -B tests/run_unit.py` ([AGENTS.md, "Commands"](../AGENTS.md#commands)).

The tests use fixture outputs of `rpm-ostree status --json` and
`rpm-ostree db list` (`tests/fixtures/`), a fixture `/sys/fs/btrfs` tree
built per test, fixture `systemctl show` answers and game profile records,
a fixture cgroup tree of the user manager and leftover game drop-ins,
fixture lists of snapshots left writable, fixture records of postponed
Docker NOCOW migrations,
and stub `rpm-ostree`, `systemctl`, `findmnt`, `stat`,
`virsh` and `flatpak-spawn` programs that log every call. They check the tricky
names, the fix command, the btrfs levels and layouts, the exit codes, the
toolbox path, the `cst status` section, and that only read-only queries were
made. The doctor runs its commands with the environment it was given (its
`PATH` finds them), so the stubs reach every command; a test checks that no
command a run starts, nor the one `flatpak-spawn --host` would run, resolves
outside the stub folder.
