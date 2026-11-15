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
| `alerts` | CST desktop alerts kept for the next login in `/var/lib/cst-alerts/pending` (last 7 days) that you have not seen yet | `ERROR` for a critical alert, `WARN` for a normal one; `info` once you have seen it |
| `alerts` | `/var/lib/f44-alerts` (a link or folder left by the versions named f44-tools) | `WARN`, fix: `cst btrfs install` |
| `btrfs` | How full the chunks are: data and metadata, used of allocated, and metadata as a share of data (see [section 6](#6-the-btrfs-check-chunks-and-unallocated-space)) | `ok` (numbers only) |
| `btrfs` | Automatic data reclaim (`dynamic_reclaim`, `periodic_reclaim`) on | `WARN` when off on the root filesystem, fix: `cst btrfs install`; `info` on other btrfs filesystems or on kernels without these switches |
| `cpu` | While the game profile is on (`/run/cpu-setup/game.state` exists): the settings it made that no longer hold ("drift"), compared with what it recorded in `/run/cpu-setup/game.intended` right after `on` (see [section 8](#8-the-cpu-checks-game-profile-and-user-manager)) | `WARN`, fix: `sudo systemctl restart cpu-game.service` (`cst cpu game off && cst cpu game on` for a profile switched on by hand); `ok` when they all hold, or "game profile: off"; `info` when a profile switched on by an older version recorded nothing, or a unit could not be asked; nothing when the cpu module is not installed |
| `cpu` | While the game profile is off: its MemoryLow drop-ins `/run/systemd/system/*.d/zz-cst-game.conf` still there | `WARN`, fix: `sudo /usr/local/sbin/cpu-game-profile off` |
| `cpu` | Your user manager's cgroup tree (`/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service`): top-level slices other than `app.slice`, `session.slice`, `background.slice` and `user.slice`, with the CPU time each used (`cpu.stat`); `systemd-run --user` jobs (`run-*.scope`, `run-*.service`) straight in `app.slice` | `WARN` per unknown slice, with the name to use instead (`user-<name>.slice`; no fix line, it is a change in whatever starts the jobs); one `info` line for the jobs, busiest first; `ok` when there are none; nothing when there is no user manager to look at (CI, containers) |
| `vm` | A libvirt VM with `<cpu mode='host-passthrough'>` that does not disable CET and nested MBEC (`cet-ss`, `cet-ibt`, `vmx-mbec`, `vmx-exit-save-cet`, `vmx-entry-load-cet`) while this QEMU offers them (see [section 7](#7-the-vm-checks)) | `WARN`, fix: `cst cpu vm-pin plan`; `ok` when QEMU does not offer them; `info` when that could not be read |
| `vm` | The virtualization packages (QEMU, libvirt, UEFI firmware `edk2-ovmf`, `swtpm`, `libtpms`) differ between the rollback and the booted deployment | `WARN` when a VM exists and the rollback is not pinned (fix: `sudo ostree admin pin N`) or no VM backup, local or on the NAS, was made since the booted deployment was built (fix: `cst vm backup --nas` when the NAS backup is set up, else `cst vm backup`); `info` when both are in place; `ok` when nothing changed |
| `vm` | A disk of a VM that points to an overlay of a NAS backup (`<image>.cst-overlay-<time>`, in the running VM or in its definition): `cst vm backup --nas` could not merge it back | `WARN`, fix by case: still running on the overlay: `virsh blockcommit`; only the definition names a stale overlay: `virsh edit` (never `qemu-img commit`); shut off on a newer overlay: `qemu-img commit`; unclear: a command that only looks (see [section 7](#7-the-vm-checks)); `info` while that backup is still running |
| `vm` | The weekly NAS backup timer of each VM (`cst-vm-backup@VM.timer`) | `info`: on or off. Never `WARN` |
| `vm` | A VM whose disks or network card differ from what `cst vm tune` recommends (SATA/IDE disks instead of virtio-scsi, disk driver without `cache='none' io='native' discard='unmap' detect_zeroes='unmap'`, e1000/e1000e/rtl8139 card; see [section 7](#7-the-vm-checks)) | `info`, naming the first differences and `cst vm tune plan VM`; `ok` when it matches. Never `WARN` |
| `vm` | A VM image folder on btrfs (`/var/lib/libvirt/images`, `~/.local/share/libvirt/images`, the two GNOME Boxes folders) without the NOCOW mark (`+C`), or with images still copy-on-write | `info` (`cst vm nocow apply` for the mark; `cst vm nocow plan` for how to convert an image); `info` "not readable as your user" for root's folder, never a finding; `ok` when NOCOW and clean; nothing for folders that are not there (CI, containers). Never `WARN` |
| `btrfs` | Unallocated space per disk, estimated from sysfs | `WARN` below 50 GiB, `ERROR` below 5 GiB (the same levels as the hourly health check; no fix line: follow the recovery steps); `ERROR` also when metadata is almost full; `info` "needs root" when the layout gives no per-disk number |
| `btrfs` | Snapshots left writable (see [section 6.1](#61-snapshots-left-writable)): names in `/var/lib/btrfs-snapshot/core-unfinished` (an hourly run stopped between taking a snapshot and making it read-only), and writable entries named like CST's snapshots in `/var/lib/btrfs-snapshot/core-writable` that no run left | `WARN`; fix: `sudo systemctl start btrfs-snapshot.service` for the first, `sudo btrfs subvolume show <path>` (it only looks) for the second; `ok` when there are none; `info` when the lists cannot be read; nothing when snapshots were never set up |

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
  does not answer on a read-only connection) and read the world-readable
  index `/var/lib/cst/vm-backups.json` written by `cst vm backup`, the
  world-readable `/etc/btrfs-setup/snapshots.conf` (is the NAS backup set
  up?), the lock file `/run/cst-vm/nas-VM.lock` with `/proc/<pid>` (is a NAS
  backup running?) and `/etc/systemd/system/timers.target.wants` (is the
  weekly timer on?). Your user
  needs libvirt's permission for the system connection: membership of the
  `libvirt` group, or an equivalent polkit rule. `domcapabilities` is asked
  only when a VM lacks one of the disables. No `virsh`, no permission,
  libvirt not running, no VM: `info` or `ok`, never `WARN`. The hardware
  and image folder checks load `vm/cst-vm` of the same CST copy (no second
  copy of its rules) and read the folders' inode flags with an ioctl; they
  run no extra command.
- The `cpu` check reads `/run/cpu-setup/game.state`, `game.intended` and
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
  command.
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
- The levels (50 GiB warning, 5 GiB critical) are read from
  `btrfs/btrfs-health-check` (`UNALLOC_MIN_GIB`, `UNALLOC_CRIT_GIB`), or
  from the installed copy in `/usr/local/sbin`, so the two always agree.
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
`CPU_DISABLE_DEFAULT` in `cpu/vm-pin`.

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
  `/var/lib/cst/vm-backups.json` newer than the deployment's timestamp. The
  fix is `cst vm backup` (with the VM shut off).

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
when no game runs. Details, and why each cause happens: [cpu/README.md, section 7.5](../cpu/README.md#75-when-something-undoes-a-setting-drift).

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

## 9. Adding a check

Each check is a small function in `cst-doctor`, registered with
`@check("area")`. It gets the `Host` (how to run a query and where host
files are) and yields `Result(status, message, fix)`. Areas are printed in
the order their first check was registered. A check that crashes becomes
one `ERROR` line; the others still run. A check must never change anything:
only read files and run query commands. Modules can add their own checks
here later.

## 10. Tests

```
python3 -I -B -m unittest discover -s doctor/tests -t . -v
```

The tests use fixture outputs of `rpm-ostree status --json` and
`rpm-ostree db list` (`tests/fixtures/`), a fixture `/sys/fs/btrfs` tree
built per test, fixture `systemctl show` answers and game profile records,
a fixture cgroup tree of the user manager and leftover game drop-ins,
fixture lists of snapshots left writable,
and stub `rpm-ostree`, `systemctl`, `findmnt`, `stat`,
`virsh` and `flatpak-spawn` programs that log every call. They check the tricky
names, the fix command, the btrfs levels and layouts, the exit codes, the
toolbox path, the `cst status` section, and that only read-only queries were
made.
