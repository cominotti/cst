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
| `vm` | A libvirt VM with `<cpu mode='host-passthrough'>` that does not disable CET and nested MBEC (`cet-ss`, `cet-ibt`, `vmx-mbec`, `vmx-exit-save-cet`, `vmx-entry-load-cet`) while this QEMU offers them (see [section 7](#7-the-vm-checks)) | `WARN`, fix: `cst cpu vm-pin plan`; `ok` when QEMU does not offer them; `info` when that could not be read |
| `vm` | The virtualization packages (QEMU, libvirt, UEFI firmware `edk2-ovmf`, `swtpm`, `libtpms`) differ between the rollback and the booted deployment | `WARN` when a VM exists and the rollback is not pinned (fix: `sudo ostree admin pin N`) or no `cst vm backup` was made since the booted deployment was built (fix: `cst vm backup`); `info` when both are in place; `ok` when nothing changed |
| `vm` | A VM whose disks or network card differ from what `cst vm tune` recommends (SATA/IDE disks instead of virtio-scsi, disk driver without `cache='none' io='native' discard='unmap' detect_zeroes='unmap'`, e1000/e1000e/rtl8139 card; see [section 7](#7-the-vm-checks)) | `info`, naming the first differences and `cst vm tune plan VM`; `ok` when it matches. Never `WARN` |
| `vm` | A VM image folder on btrfs (`/var/lib/libvirt/images`, `~/.local/share/libvirt/images`, the two GNOME Boxes folders) without the NOCOW mark (`+C`), or with images still copy-on-write | `info` (`cst vm nocow apply` for the mark; `cst vm nocow plan` for how to convert an image); `info` "not readable as your user" for root's folder, never a finding; `ok` when NOCOW and clean; nothing for folders that are not there (CI, containers). Never `WARN` |
| `btrfs` | Unallocated space per disk, estimated from sysfs | `WARN` below 50 GiB, `ERROR` below 5 GiB (the same levels as the hourly health check; no fix line: follow the recovery steps); `ERROR` also when metadata is almost full; `info` "needs root" when the layout gives no per-disk number |

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
  qemu:///system list --all --name` and `dumpxml --inactive`, never with
  `--security-info`; `virsh -c qemu:///system domcapabilities`, which libvirt
  does not answer on a read-only connection) and read the world-readable
  index `/var/lib/cst/vm-backups.json` written by `cst vm backup`. Your user
  needs libvirt's permission for the system connection: membership of the
  `libvirt` group, or an equivalent polkit rule. `domcapabilities` is asked
  only when a VM lacks one of the disables. No `virsh`, no permission,
  libvirt not running, no VM: `info` or `ok`, never `WARN`. The hardware
  and image folder checks load `vm/cst-vm` of the same CST copy (no second
  copy of its rules) and read the folders' inode flags with an ioctl; they
  run no extra command.
- The `btrfs` check reads `/sys/fs/btrfs` (under `/run/host` in a toolbox),
  asks `findmnt -t btrfs` on the host to name each filesystem by its mount
  point, and `stat -f` on that mount point for the filesystem's size. It
  never runs a `btrfs` command: those need root.
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

With both in place the line is `info`. Best practice: before every Fedora
major upgrade, shut the VM down and run `cst vm backup`; after it, keep the
old deployment pinned until the VM boots on the new one.

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

## 8. Adding a check

Each check is a small function in `cst-doctor`, registered with
`@check("area")`. It gets the `Host` (how to run a query and where host
files are) and yields `Result(status, message, fix)`. Areas are printed in
the order their first check was registered. A check that crashes becomes
one `ERROR` line; the others still run. A check must never change anything:
only read files and run query commands. Modules can add their own checks
here later.

## 9. Tests

```
python3 -I -B -m unittest discover -s doctor/tests -t . -v
```

The tests use fixture outputs of `rpm-ostree status --json` and
`rpm-ostree db list` (`tests/fixtures/`), a fixture `/sys/fs/btrfs` tree
built per test, and stub `rpm-ostree`, `systemctl`, `findmnt`, `stat`,
`virsh` and `flatpak-spawn` programs that log every call. They check the tricky
names, the fix command, the btrfs levels and layouts, the exit codes, the
toolbox path, the `cst status` section, and that only read-only queries were
made.
