<!-- SPDX-License-Identifier: MPL-2.0 -->
# `cst vm`: backups, virtual hardware and NOCOW for the libvirt VM

This folder holds `cst-vm`, the program behind `cst vm` (the module script
is `vm-setup.sh` at the top of CST). It backs up a libvirt VM (by default
`win11-devbox`): its disks, its UEFI variables, its TPM state and its
definition. The default is a weekly backup **to the NAS** with restic, taken
while the VM keeps running (section 10); a local copy with the VM shut off is
the quick way back before a risky change (sections 3 to 6). It also shows whether the VM is ready for the QEMU it runs on,
gives it the recommended virtual hardware (`tune`: virtio-scsi disks with
`cache='none'`, virtio network card; section 8) and keeps the VM image
folders without copy-on-write (`nocow`; section 9).
How to set up `cst`: [../README.md](../README.md#the-cst-command).

> **In short**
>
> - **The default backup goes to the NAS**: `cst vm backup --nas`, also while
>   the VM runs, and every week by itself after `cst vm backup-timer on`.
>   It keeps 4 weekly and 3 monthly copies; `--keep-tag NAME` keeps one until
>   you remove it. It needs the NAS backup of `/home` set up first
>   (`cst btrfs snapshots nas setup`): both share one restic repository.
>   Section 10.
> - **Before every Fedora major upgrade**: shut the VM down, then run
>   `cst vm backup` (a local copy, for a quick rollback), and/or
>   `cst vm backup --nas --keep-tag pre-f46`. After the upgrade, **keep the old deployment pinned**
>   (`sudo ostree admin pin N`) until the VM boots on the new one.
>   `cst doctor` reminds you of both.
> - `cst vm backup` makes reflink copies by default. They are instant on
>   btrfs and take almost no space, because they **share disk blocks** with
>   the VM's files. They protect against changes to the images (a repair
>   tool, a Windows update, a new QEMU writing to them). They do **not**
>   protect against losing the disks or the filesystem. For that, use
>   `--full` (real copies) or a copy on another machine.
> - It never restores anything by itself. It prints the restore commands.
> - `cst vm tune plan` shows how the VM's disks and network card differ from
>   the recommended ones. For Windows, the virtio drivers must be installed
>   in the guest **first** (section 8.2); without `--guest-has-virtio` only
>   the safe disk-cache settings are applied.
> - `cst vm nocow apply` marks the VM image folders NOCOW (`+C`). Images
>   already there are never converted; `cst vm nocow plan` prints the
>   commands for you.

---

## 1. Commands

```
cst vm backup [VM] --nas [--keep-tag NAME] [--no-quiesce]
                              # (root, calls sudo) to the NAS, running or shut off (section 10)
cst vm backup-timer on|off [VM]   # (root, calls sudo) the weekly --nas backup (section 10.5)
cst vm backup-timer status [VM]   # whether it is on, up to date, the next run
cst vm backup [VM] [--full]   # (root, calls sudo) a local copy; the VM must be shut off
cst vm list [VM]              # the backups, local and NAS (as your user, also in a toolbox)
cst vm status [VM]            # state, CPU mode and the CET/MBEC fix, hardware vs tune,
                              # last backup, virtualization packages changed since it,
                              # NOCOW state of the VM image folders
cst vm tune plan [VM] [--guest-has-virtio]    # the recommended hardware, as a diff (section 8)
cst vm tune apply [VM] [--guest-has-virtio]   # (root, calls sudo) the same, asks, virsh define
cst vm nocow plan [--root]    # the VM image folders and their images (section 9)
cst vm nocow apply            # +C on those folders (sudo only for the system one)
cst vm help
```

`VM` defaults to `VM_NAME` in `/etc/cpu-setup/cpu-setup.conf`
(`win11-devbox`). `backup`, `backup-timer on|off`, `tune apply`,
`nocow apply` and `nocow plan --root` must run on the host: in a toolbox
they stop and show the command to type in a host terminal. `list`,
`status`, `backup-timer status`, `tune plan` and `nocow plan` run on the
host by themselves (through `flatpak-spawn --host`). `list` without a VM, or
with an empty one (`cst vm list ""`), lists the backups of every VM.
Settings: the optional `/etc/cst/vm.conf` (sections 8 and 10.7).

## 2. The incident that started it (Fedora 45)

After the upgrade from Fedora 44 to 45 (QEMU 10.2.2 -> 11.1.3, libvirt
12.0 -> 12.6, new `edk2-ovmf` UEFI firmware and `libtpms`), Windows in
`win11-devbox` no longer booted. It looped in Automatic Repair (WinRE).

The cause: the VM uses `<cpu mode='host-passthrough'>`, so the guest sees
the host CPU's features as far as QEMU passes them on. QEMU 11 started to
pass Intel CET (`cet-ss`, `cet-ibt`) and nested MBEC (`vmx-mbec`,
`vmx-exit-save-cet`, `vmx-entry-load-cet`), and Windows with
Hyper-V/VBS/WSL2 bootloops with them. Upstream reports:
[QEMU issue 3215](https://gitlab.com/qemu-project/qemu/-/issues/3215) and the
[Proxmox forum thread](https://forum.proxmox.com/threads/windows-11-bootloop-with-kernel-7-0-works-with-kernel-6-17.185608/).
The fix: five `<feature policy='disable' .../>` lines in the definition.
`cst cpu vm-pin` now keeps them there
([cpu/README.md, section 8.1](../cpu/README.md#81-cet-and-nested-mbec-why-vm-pin-switches-them-off)).

Two lessons, which this module and `cst doctor` turn into habits:

1. While Windows loops in repair, its repair tools write to the disk image.
   A copy taken **before** the upgrade is the safe way back.
2. The old deployment (Fedora 44, with QEMU 10) is the other way back: boot
   it from the GRUB menu and the VM starts as before. Silverblue keeps only
   a few deployments, so **pin** it until the VM works on the new one.

## 3. What a local backup contains

Sections 3 to 6 are about the local copy (`cst vm backup`, without
`--nas`). The NAS backup is section 10.

The paths come from the VM's definition
(`virsh dumpxml --inactive --security-info VM`). Nothing is hardcoded:

| Part | From | Copied how |
|---|---|---|
| Every disk with `type='file'` and `device='disk'` | `<disk><source file=...>` | reflink (default) or full copy |
| UEFI variables (Secure Boot keys, boot entries) | `<os><nvram>` | plain copy (small) |
| Software TPM state (BitLocker and Windows Hello keys live there) | `/var/lib/libvirt/swtpm/<domain UUID>` when the VM has `<tpm><backend type='emulator'>` | plain copy (small) |
| The definition | `domain.xml`, mode 0600 (it may hold graphics passwords) | written |
| A manifest | `manifest.json`: time, VM, UUID, mode, each file with its allocated and apparent size, the versions of `qemu-kvm`, `qemu-kvm-core`, `qemu-system-x86-core`, `libvirt-daemon`, `edk2-ovmf`, `swtpm`, `libtpms` ("not installed" for names that are not), the booted rpm-ostree deployment (version, checksum, timestamp) | written |

CD-ROM drives are skipped. The backup refuses, and copies nothing, when a
disk is a block device, a network disk or a pool volume, or a qcow2 overlay
with a backing file. Back those up by hand. Messages name a disk by its target
(`sda`); a disk whose definition has no target name is an `unnamed disk`,
with its source path when it has one.

A disk, UEFI variables file or TPM folder that is a symbolic link is copied
through the link: the backup holds the data of the file it points to, under
that file's own path, and the restore commands write back to that file. A
note says so for each such file, in a local copy and in a NAS backup alike
(`--nas` reads them through the link the same way).

Where: `/var/lib/libvirt/cst-backups/VM/YYYYmmddTHHMMSSZ/` (UTC time; the
folders are mode 0700, owned by root). Each source keeps its full path under
`files/`, for example
`files/var/lib/libvirt/images/win11-devbox.qcow2`, so restoring is one
`cp` per file.

The backup also writes `/var/lib/cst/vm-backups.json` (mode 0644): a list
of the backups with the VM, time, path, mode, package versions and
deployment, and **no secrets**. `cst vm list`, `cst vm status` and
`cst doctor` read it as your user. It is replaced atomically and written to
disk (fsync) at once, so a power loss right after a backup cannot hide it.
Its `"version"` (format 1; an index without it is read as before) tells the
readers which format they read: an index of a newer `cst vm` (after you went
back to an older CST) is left as it is, and `backup`, `backup --nas`, `list` and
`status` stop with exit 1 and a message that names the format, before anything
is copied, instead of dropping what they do not know when they write it back.
`cst doctor` warns about it too. The same holds for an index that is there but
cannot be read as one (not JSON, or no `"backups"` list, or a read error other
than a missing file): read as empty, the next backup would have written an index
with only itself. The message names the file; move it aside (`mv
/var/lib/cst/vm-backups.json /var/lib/cst/vm-backups.json.bad`) to start a new
index. If the index changes that way while a backup runs (a newer `cst vm`
rewrote it, or it was damaged), the backup is finished and kept (the folder,
or the restic snapshot), but it is not added to the index: the backup ends with
exit 1 and a message that names it and says how to restore it by hand, after
its notes (a TPM state not found, ...) and its restore lines, as a backup that
is indexed prints them. For a NAS backup the message also says that the next
NAS backups may name the snapshot as one no record names (section 10.6), and
wait with their retention until it is kept as a backup (`restic tag --add
cst-adopted ID`) or forgotten. An entry this `cst vm` does not understand (no `vm`, a NAS entry
without its snapshot, a local one without its folder, a known key of another
type) is not listed or pruned, and is written back as it is after the others.

The VM must be **shut off** (`virsh domstate` says `shut off`). Only then
are the disk, the UEFI variables and the TPM state consistent with each
other. Shut it down from Windows, or with
`virsh -c qemu:///system shutdown win11-devbox`.

- **Saved is not shut off.** After a managed save (virt-manager's "Save",
  or `virsh managedsave`), `domstate` also says `shut off`, but Windows did
  not shut down: its disk only makes sense together with the saved memory.
  The backup refuses then. Start the VM and shut Windows down, or drop the
  saved memory with `virsh -c qemu:///system managedsave-remove VM`.
- **Started during the copy.** Nothing stops the VM from starting while a
  long `--full` copy runs. After copying, the backup checks the state again
  and checks that no source file changed. If either did, it removes the
  incomplete backup and refuses.

## 4. Reflink or full

| | `cst vm backup` (reflink) | `cst vm backup --full` |
|---|---|---|
| Time | about a second | minutes (the whole image is read and written) |
| Space at first | almost none | the allocated size of the disks |
| Space later | grows as the VM changes blocks (the old blocks stay for the backup) | stays the same |
| Protects against | changes to the image: repair tools, updates, a new QEMU | the same, and damage to the original's blocks |
| Does not protect against | a dead disk pair, a broken filesystem, an accidental `rm` of both | a dead disk pair, a broken filesystem (it is on the same disks) |
| Needs | the backup folder on the same btrfs filesystem as the disks | free space: the allocated size plus a margin (1 GiB or 5 %, the larger); it refuses otherwise |

Reflink copies use `cp --reflink=always -a`; if the filesystem cannot
clone (not btrfs, or another filesystem), the backup stops, removes the
incomplete folder and tells you to use `--full`. Full copies use
`cp --reflink=never --sparse=auto -a`: the holes of the image stay holes, and
the data is written in long pieces. (`--sparse=always` would also turn every
run of zero blocks inside the data into a hole. On a real 92 GiB image that
gave 1.05 million extents instead of 0.6 million, and a guest writing into
those holes allocates new extents, even on a NOCOW file.) A disk marked NOCOW (`chattr +C`) gets a NOCOW copy, because btrfs
clones only between files with the same flag (section 9.1).

## 5. Restore

The backup prints the exact commands. They look like this (shut the VM down
first; each `cp` replaces the current file):

```
B=/var/lib/libvirt/cst-backups/win11-devbox/20261114T120000Z
sudo cp --reflink=auto -a $B/files/var/lib/libvirt/images/win11-devbox.qcow2 /var/lib/libvirt/images/win11-devbox.qcow2
sudo cp --reflink=auto -a $B/files/var/lib/libvirt/qemu/nvram/win11-devbox_VARS.qcow2 /var/lib/libvirt/qemu/nvram/win11-devbox_VARS.qcow2
sudo rm -rf /var/lib/libvirt/swtpm/<UUID> && sudo cp -a $B/files/var/lib/libvirt/swtpm/<UUID> /var/lib/libvirt/swtpm/
sudo virsh -c qemu:///system define $B/domain.xml
```

Restore all parts together: a disk from one time with a TPM state from
another can make BitLocker ask for its recovery key. If the problem was the
new QEMU (as in section 2), restoring is not enough: boot the old deployment
or fix the definition first (`cst cpu vm-pin plan`).

## 6. Removing old backups

Nothing removes backups by itself. Remove one by hand:

```
sudo rm -r /var/lib/libvirt/cst-backups/win11-devbox/20261114T120000Z
```

`cst vm list` then shows it as "(folder gone)" where it can tell, and the
next `cst vm backup` drops it from the index. A reflink backup frees its
space only when it is removed, so do not keep many old ones.

## 7. The status

```
win11-devbox: shut off
  cpu mode host-passthrough: disabled cet-ss cet-ibt vmx-mbec vmx-exit-save-cet vmx-entry-load-cet
  hardware: disks and network card as tune recommends
  last backup: win11-devbox     20261121T043512Z  nas      deployment 45.20261107.0  qemu-kvm-11.1.3-1.fc45.x86_64  restic 3f9c1a2b (quiesced)
  virtualization packages: unchanged since the last backup
  weekly NAS backup timer: on (cst vm backup-timer status win11-devbox)
VM image folders (NOCOW):
  /var/lib/libvirt/images  [libvirt qemu:///system: virt-manager, virsh]
    NOCOW mark (+C): not readable as your user (root-only folder): cst vm nocow plan --root
  ...
```

When a package changed since the last backup, it lists them: back up again
once the VM works on the new versions.

## 8. `cst vm tune`: the recommended virtual hardware

```
cst vm tune plan [VM] [--guest-has-virtio]    # as your user, also in a toolbox; changes nothing
cst vm tune apply [VM] [--guest-has-virtio]   # (root, calls sudo) asks, then virsh define
```

`tune` compares the VM's definition with the hardware CST recommends, and
shows the difference as a diff. It changes only what differs:

| Item | Recommended | Knob in `/etc/cst/vm.conf` |
|---|---|---|
| Disks (`device='disk'`, file or block) on `sata` or `ide` | `bus='scsi'` on a `virtio-scsi` controller. If the VM has no virtio-scsi controller, one is added. The disk keeps its name (`sda`). Its old SATA address is replaced by an address on the virtio-scsi controller. An IDE disk named `hdX` gets the first free `sdX` name | `VM_TUNE_DISK_BUS` |
| The disk driver (file and block disks) | `cache='none' io='native' discard='unmap' detect_zeroes='unmap'`. The other attributes (`type`, `error_policy`, ...) stay. The four go together: QEMU accepts `io='native'` only with `cache='none'` (or `directsync`), and `detect_zeroes='unmap'` only with `discard='unmap'` | `VM_TUNE_DISK_DRIVER` |
| Network cards `e1000`, `e1000e`, `rtl8139` | `virtio`. **The MAC address never changes**: Windows and DHCP see the same card address | `VM_TUNE_NIC` |
| `<memballoon>` | Left as it is. If it is missing, tune only reports it | none |
| CPU features (CET/MBEC) and pinning | Not tune's: `cst cpu vm-pin` owns them. tune only shows their state and points to `cst cpu vm-pin plan` | none |

Left alone: CD-ROM and floppy drives (they stay on SATA), disks already on
`virtio` (virtio-blk), network and pool disks (only reported), network cards
without a `<model>`, and `hostdev`/`vhostuser`/`vdpa` cards. Every other
interface type with an emulated card (`network`, `bridge`, `direct`, `user`,
`ethernet`, `null`, `vds`, and the socket types `mcast`, `server`,
`client`, `udp`) is tuned. A converted disk never takes a SCSI unit that another disk, CD-ROM
or SCSI host device (`<hostdev type='scsi'>`) already uses.

Each knob is `yes` (default) or `no`. The file is optional; CST does not
create it:

```
# /etc/cst/vm.conf
VM_NOCOW=yes
VM_TUNE_DISK_BUS=yes
VM_TUNE_DISK_DRIVER=yes
VM_TUNE_NIC=yes
```

How `apply` works (as `cst cpu vm-pin`): it reads the definition with
`virsh dumpxml --inactive --security-info` (with the graphics passwords, so
they are kept), shows the diff, asks `[y/N]` (only in a terminal; Ctrl-D is a no), saves the
previous definition in
`/var/lib/libvirt/cst-backups/VM/definitions/<UTC time>.before-tune.xml`
(mode 0600, folders 0700), and runs `virsh define`. The change takes effect
at the VM's **next start**. It prints how to put the old definition back
(`sudo virsh -c qemu:///system define <that file>`). `plan` reads the
definition without the passwords, so its output can be shared.

### 8.1 Why these settings

- **virtio-scsi** instead of emulated SATA: a paravirtual disk controller,
  with less CPU work per request than an emulated AHCI chip. It also passes
  TRIM (`discard`) through.
- **`cache='none'`**: QEMU opens the image with `O_DIRECT`, so the host's
  page cache does not keep a second copy of what Windows already caches.
  On btrfs, Linux 6.14/6.15 and later fall back to buffered writes for
  `O_DIRECT` on files that have checksums, and later also on profiles that
  keep two copies (RAID1). So `cache='none'` is safe here (no torn writes
  between the RAID1 copies); the gain is mostly on reads: no double caching.
- **`io='native'`**: Linux native AIO; it needs `cache='none'`.
- **`discard='unmap'` and `detect_zeroes='unmap'`**: blocks that Windows
  frees (TRIM) or fills with zeros are given back to the host, so the qcow2
  image does not keep growing.
- **virtio network card**: paravirtual, less CPU per packet than e1000e.

### 8.2 Windows guests: drivers first

Windows has no virtio drivers of its own. If its boot disk moves to
virtio-scsi before the `vioscsi` driver is installed and set to start at
boot, Windows stops with **INACCESSIBLE_BOOT_DEVICE**. So:

- tune decides the guest's system from the libosinfo id that virt-manager,
  virt-install and Boxes write into `<metadata>`
  (`http://microsoft.com/win/11`). Without one, Hyper-V enlightenments in
  `<features>` mean Windows.
- For a **Windows guest, or one not known to be Linux**, without
  `--guest-has-virtio`, tune applies only the driver attributes (cache, io,
  discard, detect_zeroes). They are host-side settings and safe without
  guest drivers. The bus and network card changes are shown as "held back",
  with the steps below.
- With `--guest-has-virtio`, it applies them too, but **only with the VM
  shut off**. CST cannot look inside the guest (no guest agent), so the flag
  is your confirmation.
- **Linux guests** have virtio in the kernel: no flag needed, and the
  definition may be changed while the VM runs (it takes effect at the next
  start). One caveat: an initramfs built only with the drivers in use
  ("hostonly") may lack `virtio_scsi`. Check with `lsinitrd | grep virtio`
  in the guest first.

The procedure (how `win11-devbox` was moved, 2026-11-14):

1. Download the virtio-win ISO (Fedora's virtio-win project) and check its
   sha256. Attach it to the running VM as a CD-ROM.
2. In an administrator PowerShell in Windows (D: is the ISO). Do **not**
   use the `.msi`: it is not signed. The `.inf` files are WHQL-signed;
   check the catalog first:

   ```
   Get-AuthenticodeSignature D:\vioscsi\w11\amd64\vioscsi.cat
   pnputil /add-driver D:\vioscsi\w11\amd64\vioscsi.inf /install
   pnputil /add-driver D:\NetKVM\w11\amd64\netkvm.inf /install
   pnputil /add-driver D:\Balloon\w11\amd64\balloon.inf /install
   ```

3. Hot-add a virtio-scsi controller, so that Windows finds a device for
   `vioscsi`, binds it and marks it as a boot driver:

   ```
   echo "<controller type='scsi' model='virtio-scsi'/>" > ctrl.xml
   virsh -c qemu:///system attach-device win11-devbox ctrl.xml --live
   ```

   It needs a free PCIe root port (virt-manager creates spare ones). In
   Windows, `sc qc vioscsi` must then show `START_TYPE : 0 BOOT_START`.
4. Shut Windows down **fully**, then
   `cst vm tune apply win11-devbox --guest-has-virtio`. With Fast Startup
   (on by default), the Start menu's "Shut down" hibernates the Windows
   kernel, and resuming it on a different boot controller can fail. Use
   `shutdown /s /t 0` in an admin prompt, or hold Shift while clicking
   "Shut down". tune also refuses the bus change after a managed save
   (virt-manager's "Save"): start the VM, shut Windows down fully, then
   apply.
5. Start it. Windows finds the new network card with the same MAC.

Back up first (`cst vm backup`): the restore commands put the old
definition back too.

### 8.3 Defaults for new VMs

libvirt has **no global setting** for the hardware of new VMs. Each tool
picks it when it creates the VM:

- **virt-manager and virt-install** ask libosinfo what the guest system
  supports. For Linux guests they pick virtio disks and network cards. For
  Windows they fall back to SATA and e1000e, because Windows has no virtio
  drivers out of the box.
- **GNOME Boxes** does not let you choose.

So `cst vm tune` is the fix **after** the VM is created. To get the
recommended hardware at creation time, pass it to virt-install:

```
virt-install ... \
  --controller type=scsi,model=virtio-scsi \
  --disk path=/var/lib/libvirt/images/NAME.qcow2,size=100,bus=scsi,cache=none,io=native,discard=unmap,detect_zeroes=unmap \
  --network network=default,model=virtio
```

For Windows, also attach the virtio-win ISO as a second CD-ROM, and load
`vioscsi` from it in Windows Setup ("Load driver"), or the installer will
not see the disk.

`cst doctor` lists VMs whose hardware differs from this, as `info` lines.

## 9. `cst vm nocow`: VM image folders without copy-on-write

```
cst vm nocow plan [--root]   # changes nothing; --root reads the root-only system folder through sudo
cst vm nocow apply           # sets the NOCOW mark (+C) on the folders; never converts an image
```

The folders (only the ones that exist; the others show as "not present").
A folder that is a symbolic link (images moved elsewhere) is shown with
its target and left alone: CST never creates or marks a path through a
link. Set the mark on the real folder by hand if it is on btrfs
(`chattr +C FOLDER`).

| Folder | Used by |
|---|---|
| `/var/lib/libvirt/images` | libvirt's system connection (`qemu:///system`): virt-manager, virsh, `win11-devbox`. Owned by root, mode 0711 |
| `~/.local/share/libvirt/images` | libvirt's session connection (`qemu:///session`) |
| `~/.local/share/gnome-boxes/images` | GNOME Boxes from the RPM |
| `~/.var/app/org.gnome.Boxes/data/gnome-boxes/images` | GNOME Boxes from Flathub |

**Why.** A VM disk image is a big file that the guest rewrites in place
all the time. With copy-on-write every rewrite goes to a new place on disk,
so the image becomes very fragmented and slow. (NOCOW also takes away the
checksum reason for the `O_DIRECT` fallback of section 8.1; on RAID1, newer
kernels may still buffer those writes because the profile keeps two
copies.)

**What it costs.** As in
[btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md#why-copy-on-write-stays-on-except-for-the-emulator-disks) and the Docker
volumes of [btrfs/README.md, section 4.10](../btrfs/README.md): NOCOW turns
off checksums and compression for those files. On RAID1, btrfs then cannot
tell a damaged copy from a good one, and scrub can neither check nor
repair them. The VM's own backups (`cst vm backup`, and a copy elsewhere)
are what protects the image. To keep the folders copy-on-write, set
`VM_NOCOW=no` in `/etc/cst/vm.conf`.

**What `apply` does.**

- `chattr +C` on a folder affects **only files created in it later**. The
  files already there keep their state.
- User folders: set as your user.
- The system folder: `sudo chattr +C /var/lib/libvirt/images`, then
  `sudo lsattr -d` to confirm it. If it is missing and libvirt is installed
  (`/var/lib/libvirt` exists), it is first created like libvirt does it:
  `sudo install -d -o root -g root -m 0711` and `sudo restorecon`.
- It runs on the host only (in a toolbox it stops and shows the command).
  It asks `[y/N]` first.

**Reading the system folder.** As your user, the flags of
`/var/lib/libvirt/images` (root, 0711) and of its images (qemu, 0600)
cannot be read. Then `plan`, `status` and `cst doctor` say "not readable
as your user", **never** "copy-on-write". `cst vm nocow plan --root` reads
it through sudo.

**Existing images are never converted.** `plan` lists each image still
copy-on-write, with its size and the commands to convert it. Run them
yourself, with every VM using the image shut off, and about the image's
allocated size free:

```
F=/var/lib/libvirt/images/win11-devbox.qcow2
sudo mv $F $F.cow-old
sudo install -m 0600 /dev/null $F      # new empty file, private until cp -a sets owner and mode
sudo chattr +C $F                      # only works while the file is empty
sudo cp -a --reflink=never --sparse=auto $F.cow-old $F   # not --sparse=always: see section 4
sudo cmp $F.cow-old $F && echo same
# start the VM; after a good boot:
sudo rm $F.cow-old
```

`--reflink=never` matters: btrfs cannot clone between a NOCOW and a
copy-on-write file, and a real copy is what makes the new file NOCOW.
`cp -a` copies the owner (qemu), the mode and the SELinux label. `plan`
also lists `*.cow-old` leftovers.

### 9.1 NOCOW and the reflink backups

btrfs cannot clone (reflink) between a NOCOW file and a copy-on-write one:
`cp --reflink=always` fails with "Invalid argument". `cst vm backup`
handles this: for a NOCOW disk it first creates an empty NOCOW file as the
destination, then `cp --reflink=always` fills it. The tests check this on a
real btrfs, including that the clone fails without that step.

Two side effects to know:

- **"COW once" after each reflink backup.** Right after the backup, the
  image and the backup share their blocks. btrfs never overwrites a shared
  block in place, even in a NOCOW file. So the first write to each shared
  block goes to a new place (copy-on-write once), and after that the block
  is NOCOW again. The image fragments a little after each backup. Remove old
  backups to stop sharing. The same happens after every btrfs snapshot that
  holds the image: the user folders under `~` are in the `/home` snapshots
  ([../btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md)), so a Boxes or session
  image there gets this one copy-on-write write per block after each
  snapshot.
- **Restoring an old copy-on-write backup** onto a NOCOW image (for example
  a backup made before the conversion): `cp --reflink=auto` cannot clone
  across the two kinds, so it falls back to a full copy. That is slower but
  correct, and the image stays NOCOW.

## 10. The NAS backup (the default)

```
cst vm backup [VM] --nas [--keep-tag NAME] [--no-quiesce]   # (root, calls sudo)
cst vm backup-timer on [VM]                                 # every week, by itself
```

The local copy of sections 3 to 6 lives on the same disks as the VM. If
both disks of the RAID1 die, or the filesystem breaks, it is gone with the
VM. The NAS backup is the copy on **another machine**. It is the default
strategy:

- **weekly**, by a systemd timer (section 10.5);
- with the VM **running**: you do not have to shut Windows down;
- kept **4 weeks and 3 months** (4 weekly and 3 monthly restic snapshots);
- one you mark with `--keep-tag NAME` (for example before an upgrade) is
  kept until you remove it by hand.

The local `cst vm backup` stays: it is the quick way back (a reflink copy in
a second, a restore without the network) right before a risky change.

### 10.1 What it needs

The NAS backup of `/home`, set up with `cst btrfs snapshots nas setup`
([btrfs/SNAPSHOTS.md, section 7](../btrfs/SNAPSHOTS.md#7-optional-backup-on-the-nas-restic)).
The VM backup uses the **same restic repository**, the same SSH settings
(the `btrfs-setup-nas` entry of root's `~/.ssh/config`), the same password
file (`/etc/btrfs-setup/restic-password`, read by restic itself: CST never
copies or prints it) and the same cache folder. It reads `NAS_ENABLED`,
`NAS_PATH` and `NAS_RESTIC_HOST` from `/etc/btrfs-setup/snapshots.conf`.
Without them it refuses and points to the setup.

Before it touches the VM, it checks that restic can open the repository
(NAS reachable, password right; `restic cat config --no-lock`, so a lock
held by the `/home` job's prune does not stop it). An unreachable NAS
therefore never leaves the VM on a snapshot.

### 10.2 How a running VM is backed up

A disk that is being written cannot be copied as it is: the copy would mix
blocks from before and after each write. So, for a running VM:

1. **A short-lived snapshot.** `virsh snapshot-create-as VM --disk-only
   --atomic --no-metadata` puts a new, empty **overlay** file in front of
   each disk:
   `/var/lib/libvirt/images/win11-devbox.qcow2.cst-overlay-<UTC time>`.
   From that moment the guest writes into the overlay, and the disk image
   itself no longer changes. It is a normal file again, which restic can
   read at its normal path. CD-ROMs are left out (`snapshot=no`).
   `--no-metadata`: libvirt keeps no snapshot record, because the overlay
   only lives for the length of the backup.
2. **restic reads the disk image**, the UEFI variables, the TPM folder, the
   definition and a manifest (section 10.3).
3. **The overlay is merged back.** `virsh blockcommit VM sda --active
   --pivot --wait` copies what the guest wrote during the backup into the
   disk image and switches the VM back to it, without stopping the guest.
   Then CST checks with `virsh domblklist` that the running VM **and** its
   saved definition use the disk image again, and only then deletes the
   overlay file. (`virsh blockcommit` also has `--delete`; CST does not rely
   on it and removes the file itself, after the check.)

Step 3 always runs, also when restic fails, the NAS goes away, or the
backup is stopped (Ctrl-C, a closed terminal, `systemctl stop`, the time
limit): the program catches the stop, ends restic, and merges first. Some
details keep a stop from cutting the merge short:

- After the first stop, further ones are ignored, and every command of the
  merge runs in its own session. A second Ctrl-C or a terminal that closes
  cannot reach `virsh blockcommit` (virsh cancels the merge on Ctrl-C).
- Output errors are ignored: a closed terminal or a pipe whose reader went
  away (`| head`) does not stop the merge or the alert.
- `virsh snapshot-create-as` is never killed by a stop: the stop waits
  until virsh answers, so the program always knows whether the VM runs on
  the overlay. If virsh does not answer within 10 minutes, the program
  watches `virsh domblklist` for 10 more minutes. Once every disk shows its
  overlay, the backup goes on (marked crash-consistent). If they do not
  all show it, libvirt might still switch later: nothing is deleted, and
  you get `DISK NOT MERGED BACK` with the commands to look, plus a critical
  alert. That alert names every overlay file there is also when a stop
  came while virsh was waited for (the refusal then gives the stop as the
  backup's own failure), and when the merge back of another disk failed.
- A snapshot that switched only some disks (which `--atomic` should never
  let happen) is no backup: the overlay files of the disks not switched
  are removed, the switched disks merged back, and the backup fails, with
  no second attempt. The files of the disks a settled answer did not
  switch are removed before a stop that came while virsh was waited for
  ends the read.
- The merge has its own time limit: 4 hours for all disks together
  (`virsh blockcommit --timeout`). If it hits that limit, virsh cancels the
  merge cleanly: the disk stays on the overlay, with no job half done, and
  the usual failure follows. The systemd unit allows a stop to take that
  long, plus a held snapshot call (20 minutes) and a 30-minute margin
  (`KillMode=mixed`, `TimeoutStopSec=295min`), so systemd never kills a
  merge halfway. The unit has no overall start time
  limit (`TimeoutStartSec=infinity`), because each step has its own (restic:
  `VM_NAS_TIMEOUT_HOURS`).

**NOCOW stays as it is.** The disk image is never copied, moved or
converted; the overlay is created in the same folder, so it gets the
folder's NOCOW mark (`+C`) too. The merge writes into the image in place.

**If the merge fails**, CST changes nothing more. It prints
`DISK NOT MERGED BACK`, the exact commands to finish by hand, and sends a
desktop alert (critical). `cst doctor` then warns about the disk until it is
back on its image (section 10.8). Nothing is lost: the overlay holds the
latest writes, the image everything before.

**If the VM is paused or asleep at merge time** (paused by you, by
virt-manager, by a full disk, or a Windows guest in sleep: `pmsuspended`),
QEMU still runs, so the merge runs as usual.

**If the VM is shut down during the backup**, the merge cannot run (it needs
QEMU running). The backup stops with an error, an alert and the commands to
merge offline (`qemu-img commit`, then `virsh edit`; section 10.8). The data
restic read is still a good backup, but it is not recorded: the next backup
names it as a snapshot no record names (section 10.6), to keep or forget.

A VM that is **shut off** (and not saved: see the managed-save note in
section 3) is read directly, without a snapshot. CST checks the state again
afterwards, and that no file changed while restic read it. If the VM was
started in between, that restic snapshot is forgotten and the backup fails.
The same for a snapshot restic could not finish (some files not read). When
restic cannot forget it (the repository locked, the NAS gone), the message
says so and names the snapshot to forget by hand (`restic forget ID`, with
the variables of section 10.9).

Such a **torn** snapshot is no backup, but the retention (section 10.6) would
count it as one and could forget a good backup in its place. So, before the
forget, its ID goes into `/var/lib/cst/vm-nas-torn.json` (0644, with the
VM, the repository, the host, the time and what tore it; a stop waits until
the record is written, so a stop then or while `restic forget` waits for
the repository's lock leaves it there), and it leaves the record once
restic forgot it. A stop that ends restic after restic saved its snapshot
(it printed the snapshot's ID, but had not ended on its own) still gets the
ID from restic's last lines: the snapshot counts as torn and is recorded
(what tore it: "stopped"), and the stop is raised right after the record.
After a stop nothing is forgotten (the next NAS backup forgets it), so the
stop stays short for the merge back. Once restic has answered with a
snapshot, a stop waits (the stops are held: the staged files' removal, the
check of a shut-off VM after the read) until a torn snapshot is recorded,
or, for a backup, until that check is done; so does a stop that comes when
restic has just ended on its own. When the snapshot is a backup (the check
of a shut-off VM passed, or the read of a running VM's snapshot), it is
indexed after the merge back, and then the stop ends the run with "restic
snapshot ID of VM is a backup: indexed (the retention runs at the next
backup)": no retention and no listing then, nothing forgotten. A stop that
comes after the merge back of a good read (while the retention, a listing or
the index's write runs) ends the run the same way: the snapshot is indexed
first, with the same words, and nothing more is forgotten or listed. If the
index cannot be written then (a full disk, say), the stop says so instead,
names the snapshot as a backup and gives the commands that keep or forget
it. If the merge
back fails, the snapshot is not indexed, as without a stop (above). After a
stop that ended restic, a snapshot whose check could not be done at all (an
error of libvirt or of a file read, not a change it found) is recorded as
torn (what tore it: "check failed"), and the next NAS backup forgets it;
never at once. Without such a stop, the error ends the run and the next
backup names the snapshot (section 10.6, "Snapshots no record names").
The one instant not covered: restic ended between saving its snapshot and
printing its ID (or the program was killed before the record): the next
backup names such a snapshot (section 10.6, "Snapshots no record names").
When the forget fails, the message
adds "recorded in /var/lib/cst/vm-nas-torn.json: the next NAS backup of VM
forgets it before its retention". If the record cannot be written either,
the message says so (a stop's message too): then forget it by hand before
the next backup. The next NAS backup of that VM forgets the recorded snapshots first, and only
those restic still lists with the tags `cst-vm` and `vm:<name>`, under
the host recorded with them (restic lists a snapshot only under the host it
was made on, so a changed `NAS_RESTIC_HOST` or hostname hides none; one
listed snapshot per recorded ID, forgotten by its listed ID): never by the
recorded ID alone. A recorded ID that restic no longer lists was forgotten
already (by hand, or by the run that recorded it when it could not update
the record): it is dropped from the record. One that restic lists without
both tags is never forgotten: it stays recorded, and the retention waits.
The record is shared by every VM
and changed under its own lock (`/run/cst-vm/torn-record.lock`, one minute
at most), so the backups of two VMs never lose each other's entries.

**Paused**, suspended or other states: refused. Resume the VM or shut it down.

### 10.3 Consistent, or crash-consistent

At the instant of the snapshot, Windows may have data in its memory that is
not on the disk yet. There are two levels:

- **quiesced**: CST asks the **QEMU guest agent** inside Windows to freeze
  the file systems for that instant (`--quiesce`). On Windows the agent uses
  VSS, the Volume Shadow Copy Service, so programs that support it (SQL
  Server, Exchange, ...) flush their data first. This is the normal case on
  `win11-devbox`.
- **crash-consistent**: without the agent the disk is captured as it would
  be after a power cut. Windows (NTFS has a journal) and most programs
  recover from that, but an open database might need its own repair.

CST tries `--quiesce` when the definition has the agent channel and libvirt
shows it **connected**. If the freeze fails (the agent stopped answering),
it takes the snapshot without it. The output says which one you got
(`quiesced` or `crash-consistent`, and why), and the manifest and the index
record it (`consistency`). `--no-quiesce` skips the attempt. A shut-off VM
is recorded as `shut-off` (fully consistent).

#### Installing the guest agent in Windows

1. The definition needs the agent's channel. virt-manager adds it for new
   VMs; else add it with `sudo virsh -c qemu:///system edit win11-devbox`,
   inside `<devices>`:

   ```xml
   <channel type='unix'>
     <target type='virtio' name='org.qemu.guest_agent.0'/>
   </channel>
   ```

   It takes effect at the next start of the VM. (A virtio-serial controller
   is added by libvirt when needed.)
2. Windows needs the **vioserial** driver (the channel is a virtio-serial
   port). With the virtio-win ISO attached (section 8.2), in an admin
   PowerShell (`D:` is the ISO):

   ```
   pnputil /add-driver D:\vioserial\w11\amd64\vioser.inf /install
   ```

3. Install the agent: `D:\guest-agent\qemu-ga-x86_64.msi`. Like the other
   `.msi` files on the ISO it is **not signed**; check the ISO's sha256
   against the one published on fedorapeople.org before you run it. It
   installs the agent as a Windows service, started automatically, together
   with its VSS provider (what `--quiesce` uses to freeze the file systems).
4. Check from the host:

   ```
   sudo virsh -c qemu:///system qemu-agent-command win11-devbox '{"execute":"guest-ping"}'
   sudo virsh -c qemu:///system dumpxml win11-devbox | grep -A1 guest_agent   # state='connected'
   sudo virsh -c qemu:///system qemu-agent-command win11-devbox '{"execute":"guest-info"}' | grep -o guest-fsfreeze-freeze
   ```

### 10.4 What is stored, with which tags

One restic snapshot per backup, with the host name of the `/home` backup
(`NAS_RESTIC_HOST`) and these **tags**:

| Tag | Meaning |
|---|---|
| `cst-vm` | a VM backup made by CST |
| `vm:<name>` | which VM, e.g. `vm:win11-devbox` |
| `cst-keep` and `NAME` | only with `--keep-tag NAME`: never forgotten by the retention |
| `cst-adopted` | only added by you (`restic tag --add cst-adopted ID`): a snapshot no record names that you keep as a backup (section 10.6); the retention counts it, `cst vm list` does not show it |

The paths inside the snapshot are the real ones:

- each disk image, e.g. `/var/lib/libvirt/images/win11-devbox.qcow2`;
- the UEFI variables, e.g. `/var/lib/libvirt/qemu/nvram/win11-devbox_VARS.qcow2`;
- the TPM folder, `/var/lib/libvirt/swtpm/<domain UUID>`;
- `/var/lib/cst/vm-nas/<name>/domain.xml` (the definition) and
  `manifest.json` (what was read, the consistency, the versions of QEMU,
  libvirt, UEFI firmware and TPM emulator, the booted deployment). They are
  written there (0600, in a 0700 folder) only for restic to read, and
  removed after the run.

**The definition includes its secrets** (`virsh dumpxml --security-info`:
for example a SPICE/VNC password). It is needed as it is to `virsh define`
the VM again after a restore, the local backup stores the same, and the
repository is encrypted with a password that only root on this machine (and
your password manager) has. It is never printed, and the index has no
secrets.

The UEFI variables and the TPM state are read while the VM runs. They change
rarely (a boot entry, a BitLocker key update), so a copy taken a few
seconds after the disk snapshot matches it in practice. They have no
snapshot, though: if QEMU or the TPM emulator writes them while restic reads
them, that copy can be torn. CST compares them before and after the read
and prints a note when they changed (the next backup reads them again). If
you want them exactly in step, shut the VM down and back it up shut off.

**Deduplication.** restic cuts each file into pieces by content and stores
each piece once in the whole repository. A disk image changes in a few
places a week, so a weekly backup stores only those parts again: the first
backup uploads the whole image (its allocated size, compressed), later ones
usually a few GiB. Because the paths never change, restic also finds the
previous snapshot of the VM to compare with.

### 10.5 The weekly timer

```
cst vm backup-timer on [VM]       # (root) installs and starts it
cst vm backup-timer status [VM]   # on or off, up to date, next run, last NAS backup
cst vm backup-timer off [VM]      # (root) stops it; removes the files when no VM uses them
```

Both write and remove these files through CST's installer, which lists them in
the install record `/var/lib/cst/installed.json` (component `vm:backup-timer`,
marked complete once the timer is enabled; `off` takes them out again), where
`cst doctor` checks them.

`off` also makes systemd forget that VM's last run
(`systemctl clean --what=state`, which removes
`/var/lib/systemd/timers/stamp-cst-vm-backup@<VM>.timer`), before the files go.

`on` installs:

- `/usr/local/sbin/cst-vm`: this program, as `install` writes every CST
  Python program: one file (a zipapp) holding `vm/cst-vm` and the `vm`,
  `cpu` and `cst_core` packages it imports, so it runs without the CST
  folder. After updating CST, run `backup-timer on` again;
  `backup-timer status` says when the copy is older (also a plain-script
  copy installed by an older CST: `on` replaces it);
- `/etc/systemd/system/cst-vm-backup@.service`: runs
  `cst-vm backup <VM> --nas` with low priority (`Nice=19`,
  `IOSchedulingClass=idle`, `CPUWeight=20`, `IOWeight=20`; the IO class only
  works with the BFQ or Kyber IO schedulers and the weight with BFQ or iocost;
  these NVMe disks use `none`:
  see [cpu/README.md, section 6.3](../cpu/README.md#63-baseline-always-active-even-without-a-game)).
  Its journal lines carry the name `cst-vm` (`SyslogIdentifier=`), so
  `journalctl -t cst-vm` finds them; a service written by an older CST shows
  as `OLDER` in `backup-timer status`;
- `/etc/systemd/system/cst-vm-backup@.timer`, and enables
  `cst-vm-backup@<VM>.timer`.

A masked unit (`systemctl mask` leaves a link to `/dev/null` in
`/etc/systemd/system`: the service, the timer, or the timer of one VM,
`cst-vm-backup@<VM>.timer`) stays masked: `on` refuses, changes nothing and
says how to unmask it; `status` shows it as `masked`; `off` leaves the link
where it is.

When: **Saturday 04:30**, up to 30 minutes later (`RandomizedDelaySec`).
That keeps it away from the other jobs: the `/home` NAS backup starts every
day between 00:00 and 01:00, and the btrfs scrub runs on Sunday at 02:00.
If the machine was off at that time, it runs at the next boot
(`Persistent=true`). If the `/home` job is still using the repository,
restic waits up to 30 minutes for its lock (`--retry-lock 30m`).

A failed run makes the unit `failed` (`cst doctor` lists failed units) and
sends a desktop alert; the next week tries again. A settings line that is not a
plain `KEY=VALUE` (section 10.7) is such a failure: the alert names the file and
the line. Run one now:
`sudo systemctl start cst-vm-backup@win11-devbox.service`; its log:
`journalctl -u cst-vm-backup@win11-devbox.service`.

### 10.6 Retention, keep tags, and where space is freed

After each good backup:

```
restic forget --host <NAS_RESTIC_HOST> --tag cst-vm,vm:<name> --group-by host \
    --keep-weekly 4 --keep-monthly 3 --keep-tag cst-keep
```

- It only looks at snapshots with **both** tags `cst-vm` and `vm:<name>`:
  the `/home` snapshots (tag `btrfs-setup`) and anything you made by hand
  without those tags are never touched. The `/home` job's own `forget` looks
  only at `btrfs-setup`, so it never touches the VM backups either.
- `--group-by host`: all backups of one VM are one group, also if its disks
  change.
- A backup made with `--keep-tag NAME` carries `cst-keep` and is kept until
  you remove it: `restic forget <ID>` (the ID from `restic snapshots --tag
  cst-vm`), or `restic tag --remove cst-keep <ID>` to let the retention have
  it.
- `forget` only removes the snapshot records. The space is freed by
  `restic prune`, which the `/home` NAS job runs for the whole repository,
  at most every `NAS_PRUNE_EVERY_DAYS` (7). The VM backup never prunes.

**Torn snapshots first.** When the record of torn snapshots (section 10.2)
names one of this VM in this repository, the backup forgets it before the
retention. If that does not work (restic forget fails, `restic snapshots`
cannot be read, a recorded ID names several listed snapshots or one listed
without both tags, or the record cannot be read or written), the run
**skips the retention** and says so
("retention skipped: WHY; the repository keeps more until the next backup"):
the repository keeps more, never less. The backup itself is good, so the run
still ends with exit status 0, and the next backup tries again. A record of
a newer CST, or one that cannot be read as the record (not JSON, no `torn`
list), makes `backup --nas` refuse before anything is done: use the newer
CST's `cst vm`, or forget the snapshots it names by hand and move it aside
(`mv /var/lib/cst/vm-nas-torn.json /var/lib/cst/vm-nas-torn.json.bad`).
`cst doctor` shows a row while the record names a snapshot, and one for a
record it cannot use (it reads only the file, no restic).

**How an entry leaves the record.** Only a NAS backup of the same VM to the
same repository takes an entry out: it forgets the snapshot, or drops the
entry when restic no longer lists the snapshot (forgotten by hand with
`restic forget ID`, or by the run that recorded it when that run could not
update the record). A VM that gets no more NAS backups (deleted, renamed,
its timer off) or a repository it no longer uses (`NAS_PATH` changed)
keeps its entries, and `cst doctor` keeps warning about them. Then forget
each of its snapshots by hand (section 10.9) and take its entries out of
the file by hand, as root with an editor. Remove the whole file only when
it names no other entry: an absent record is an empty one, so removing
one that names another VM's torn snapshot would let that VM's retention
count it as a backup.

**Snapshots no record names.** Before the retention, the backup also lists
the VM's snapshots in the repository (`restic snapshots` of the tags
`cst-vm` and `vm:<name>`, made on this host) and compares them with the
index and the record of torn snapshots. A snapshot that neither names (by
its ID, or by the ID it had before `restic tag` changed it), that is not
this run's own and that has no `cst-adopted` tag may be torn: a kill
(SIGKILL, power loss) after restic saved it and before its record, restic
ending between saving it and printing its ID, a good read whose merge back
failed (section 10.2), or a snapshot made by hand with both tags. The
retention would count it as a backup and could forget a good one in its
place, so the run names each in its output and in one critical alert, and
**skips the retention**:
"retention skipped: restic lists 1 snapshot of VM that neither the index nor
the record of torn snapshots names (each may be torn): ID (TIME); look at
each (restic ls ID), then forget it (restic forget ID) or keep it as a backup
(restic tag --add cst-adopted ID), ...". Nothing is forgotten on its own,
and the backup itself is good: the run still ends with exit status 0. Look
at the snapshot with the repository's variables (section 10.9), then either
forget it or tag it `cst-adopted`; the next backup is quiet again and runs
the retention, which counts an adopted snapshot as a backup (it is not added
to the index, so `cst vm list` does not show it). The check only starts
from the oldest snapshot the index names: a snapshot older than it (made by
an older CST, or before the index was moved aside) is left alone, and with
no NAS backup of the VM in the index (its first backup, or the index moved
aside) nothing is compared and restic is not asked. So a snapshot no record
names that is older than every indexed backup is not seen. When the index or
the record cannot be read, or `restic snapshots` fails, the retention waits
too, with the reason.

The weekly timer runs the installed copy `/usr/local/sbin/cst-vm`
(section 10.5). A copy installed before this record existed does not read
it and runs the retention anyway, so after updating CST run
`cst vm backup-timer on VM` again (`cst vm backup-timer status VM` and
the `install` row of `cst doctor` show an older copy).

The index `/var/lib/cst/vm-backups.json` gets one entry per NAS backup
(`mode` `nas`, the restic snapshot ID, the consistency, the keep tag, the
versions and the deployment); entries whose snapshot was forgotten leave it.
An entry stays when its snapshot's tags were changed by hand (`restic tag`,
for example `restic tag --remove cst-keep ID` above): restic then lists the
snapshot under a new ID, with the entry's ID as its `original`; the entry
keeps the ID it had (`restic restore` takes either).
When `restic snapshots` cannot be read as a list of snapshots with their ids
(another shape, an id that is not text), no entry leaves: the index keeps them
all until a later backup reads the list.
`cst vm list` and `cst vm status` show them, and `cst doctor` counts them as
backups (its "No VM backup since the booted deployment" warning clears).

### 10.7 Settings

In `/etc/cst/vm.conf` (all optional):

| Setting | Default | Meaning |
|---|---|---|
| `VM_NAS_KEEP_WEEKLY` | 4 | weekly restic snapshots kept |
| `VM_NAS_KEEP_MONTHLY` | 3 | monthly restic snapshots kept |
| `VM_NAS_TIMEOUT_HOURS` | 6 | restic is stopped after this long (the overlay is merged back) |

A value that is not a whole number in ASCII digits (`bad`, `²`, digits of
another script) uses the default.

`cst-vm` reads these files with CST's one settings reader
(`cst_core.domain.text.parse_shell_assignments`); the last line of a key wins,
and a byte that is not UTF-8 (in a comment, say) no longer stops the program.
`cpu-setup.conf` and `snapshots.conf`, which bash also sources, are read exactly
as bash reads them (`NAS_ENABLED=1 # on` is `1`; in `snapshots.conf` also a
value an older CST wrote with backslashes, `a\ b`). `vm.conf`, which only
Python reads, also allows blanks around `=` (`VM_NOCOW = 0`; spaces and tabs:
a no-break space is not a blank, so before the key or around `=` it makes
the line a problem, and in a value it stays part of the value). A line that is
not a plain `KEY=VALUE` (blanks around `=` in a bash file, a value with `$`,
a backtick or a backslash outside single quotes, an unclosed quote) sets
nothing and is named with its line number: `status` and the `plan`s print it
and go on; `backup`, `tune apply`, `nocow apply` and `backup-timer on`
refuse to start until it is fixed (a default never stands in for it);
`backup-timer off` prints it and still turns the timer off. A `backup` or
`backup-timer` of a VM named on the command line reads nothing from
`cpu-setup.conf` (only `VM_NAME`, when no VM is named), so a line there does
not stop it.

The commands that only read (`status`, `list`, the `plan`s, `backup-timer
status`, `help`) end quietly when their reader goes away (`cst vm status |
head -1`). A backup never stops because its terminal or pipe went away: what
it cannot print is dropped, and it finishes, or cleans up, as usual.

### 10.8 If a backup left an overlay

`cst doctor` warns when a disk of a VM points to a `.cst-overlay-` file and
no backup is running. The failed backup printed the commands for its case;
the doctor's warning names the case too. There are three cases, and the fix
is different for each. **Look first**:

```
sudo virsh -c qemu:///system domstate win11-devbox
sudo virsh -c qemu:///system domblklist win11-devbox --details               # what QEMU uses now
sudo virsh -c qemu:///system domblklist win11-devbox --details --inactive    # what the definition names
sudo virsh -c qemu:///system blockjob win11-devbox sda --info                # a block job on sda?
```

**1. The VM runs (or is paused) on the overlay.** The first listing shows
the overlay. The VM works, but the overlay keeps growing and the next
backup refuses. Merge it:

```
sudo virsh -c qemu:///system blockjob win11-devbox sda --info     # a merge still running? let it finish
# a commit "ready" at 100 % that nothing waits for (virsh was killed): finish it
sudo virsh -c qemu:///system blockjob win11-devbox sda --pivot
# no block job at all: start the merge again
sudo virsh -c qemu:///system blockcommit win11-devbox sda --active --pivot --wait --verbose
sudo virsh -c qemu:///system domblklist win11-devbox --details               # sda: the .qcow2 again
sudo virsh -c qemu:///system domblklist win11-devbox --details --inactive    # here too
sudo rm /var/lib/libvirt/images/win11-devbox.qcow2.cst-overlay-<time>        # only then
```

**2. The VM runs on the image, and only the definition names the overlay.**
The merge worked, but libvirt did not update the saved definition. The
overlay is **stale** now: the guest has written to the image since then.
**Do not `qemu-img commit` it**: that would overwrite newer data in the
image with old data. Do not start the VM from that definition either. Point
the definition back, then remove the overlay:

```
sudo virsh -c qemu:///system edit win11-devbox
#   in the <disk> with <target dev='sda'>: <source file='/var/lib/libvirt/images/win11-devbox.qcow2'/>,
#   and delete its <backingStore> element if there is one
sudo virsh -c qemu:///system domblklist win11-devbox --details --inactive    # sda: the .qcow2
sudo rm /var/lib/libvirt/images/win11-devbox.qcow2.cst-overlay-<time>
```

`virsh edit` on a running VM changes only the saved definition, which is
what is needed here.

**3. The VM is shut off on the overlay.** It was shut down before the merge,
so the overlay holds the guest's last writes. Merge it offline, then point
the definition back:

```
sudo qemu-img commit -p /var/lib/libvirt/images/win11-devbox.qcow2.cst-overlay-<time>
sudo virsh -c qemu:///system edit win11-devbox
#   as in case 2: <source file='...win11-devbox.qcow2'/>, no <backingStore>
sudo virsh -c qemu:///system domblklist win11-devbox --details --inactive
sudo rm /var/lib/libvirt/images/win11-devbox.qcow2.cst-overlay-<time>
```

**A shut-off VM: case 3, or not sure.** The backup's own message and alert
name the case: a VM shut down during the backup is case 3. If you only have
`cst doctor`'s warning, it compares the files' times
(`sudo ls -l --time-style=full-iso` on the image and the overlay):

- **The overlay is newer:** case 3. During a backup the image does not
  change, and after a merge the guest writes only to the image. So the
  newer overlay holds the last writes, and `qemu-img commit` cannot
  overwrite anything newer.
- **The image is newer:** this does **not** tell. It happens after a merge
  that left the definition behind (case 2, the overlay is stale). It also
  happens after a merge that was cut short, for example by a power cut,
  because the merge writes the image while the overlay is still needed.
  The doctor then gives no fix that changes anything, only the `ls` command.
  Find the backup's message or alert (`journalctl -u
  cst-vm-backup@VM.service` for the timer). Do not remove or commit the
  overlay until you know which case it is.

### 10.9 Restore from the NAS

Commands checked against restic 0.19. Run them as root on the host (restic
uses root's SSH settings). Restore **all parts of one snapshot together**:
a disk from one time with a TPM state from another can make BitLocker ask
for its recovery key.

```
sudo -i
export RESTIC_REPOSITORY="sftp:btrfs-setup-nas:$(. /etc/btrfs-setup/snapshots.conf; echo "$NAS_PATH")"
export RESTIC_PASSWORD_FILE=/etc/btrfs-setup/restic-password
export RESTIC_CACHE_DIR=/var/cache/btrfs-snapshot-restic
restic snapshots --tag cst-vm,vm:win11-devbox      # pick an ID (the time is UTC)
ID=3f9c1a2b
```

1. A **NOCOW folder** to restore into, on the same filesystem as the images
   (files created in it get the mark):

   ```
   install -d -m 0700 /var/lib/libvirt/images/restore-$ID
   chattr +C /var/lib/libvirt/images/restore-$ID
   ```

2. Restore. `--sparse` writes runs of zeros as holes, so the image takes its
   allocated size, not its full virtual size:

   ```
   restic restore $ID --target /var/lib/libvirt/images/restore-$ID --sparse
   ```

   The files land under their full paths, for example
   `/var/lib/libvirt/images/restore-$ID/var/lib/libvirt/images/win11-devbox.qcow2`.
   One file only: add `--include /var/lib/libvirt/images/win11-devbox.qcow2`.
   (`restic dump $ID /path > file` also works, but writes every zero.)
3. Shut the VM down (`virsh -c qemu:///system domstate win11-devbox` says
   `shut off`), keep the current files aside, and move the restored ones in
   place (same filesystem: `mv` is instant and keeps the NOCOW mark):

   ```
   R=/var/lib/libvirt/images/restore-$ID
   mv /var/lib/libvirt/images/win11-devbox.qcow2 /var/lib/libvirt/images/win11-devbox.qcow2.before-restore
   mv $R/var/lib/libvirt/images/win11-devbox.qcow2 /var/lib/libvirt/images/
   cp -a $R/var/lib/libvirt/qemu/nvram/win11-devbox_VARS.qcow2 /var/lib/libvirt/qemu/nvram/
   rm -rf /var/lib/libvirt/swtpm/<UUID> && cp -a $R/var/lib/libvirt/swtpm/<UUID> /var/lib/libvirt/swtpm/
   virsh -c qemu:///system define $R/var/lib/cst/vm-nas/win11-devbox/domain.xml
   ```

4. Start the VM. Once it works, remove the `.before-restore` file and the
   restore folder.

On a new machine: install restic, recreate the SSH access and the password
file as in [btrfs/SNAPSHOTS.md, section 6](../btrfs/SNAPSHOTS.md#6-how-to-restore-a-file-or-a-folder)
("If this computer is lost"), then follow the steps above with
`--host <old host name>` on `restic snapshots`.

## 11. Follow-ups (not done yet)

- `cst vm prune --keep N`: removing old local backups automatically.

## 12. Tests

```
python3 -I -B -m unittest discover -s vm/tests -t . -v
```

This runs the vm tests only. The whole suite, the way CI runs it, is
`python3 -I -B tests/run_unit.py` ([AGENTS.md, "Commands"](../AGENTS.md#commands)).

`virsh`, `cp`, `rpm` and `rpm-ostree` are stubs that log their calls; the
backup folder, the index and the swtpm folder are scratch paths. The tests
check the paths taken from the definition, the refusals (running VM, block
disks, qcow2 overlays, no space for `--full`, reflink not possible), the
modes of the folders and files, that the index holds no secrets, that
removed backups leave the index, and `list` and `status`.

For the NAS backup (`test_vm_nas.py`; `ResticBackupStopTest`: a stop while
restic runs keeps the snapshot's ID from restic's lines, kept or still in
the pipe): virsh and restic are stateful fakes
(the VM's disk sources live in a JSON file, so `domblklist` shows what the
snapshot and the merge did). The tests check the snapshot's arguments, the
quiesce attempt and its fallback (agent missing, disconnected, failing,
`--no-quiesce`), restic's arguments, paths, tags and environment, that
restic reads the image while the VM is on the overlay, that the merge always
runs (also when restic fails) and is checked in both definitions before the
overlay is removed, a failed merge (loud, alert, nothing removed), a VM shut
down during the backup, the shut-off path and its re-check, the refused
states, a left-over overlay, an unusable repository (no snapshot taken),
the keep tag, `forget`'s arguments, the index entries (an entry whose snapshot
restic lists under a new ID after `restic tag` stays: `SnapshotNamesTest`), the timer's units
(the stop budget covers the merge's limit), and that the NAS constants match
`btrfs/btrfs-snapshot`. They also check what a manual run can meet: a closed
terminal during restic (on a pseudo-terminal) and a pipe without a reader
still merge and alert, a SIGTERM while the alert is sent cuts neither the
alert nor the refusal short, a second Ctrl-C does not reach the merging virsh
(the fake cancels on SIGINT as the real one does), a VM paused at merge time
is merged, a `snapshot-create-as` that does not answer (snapshot in place
later: the backup goes on; never in place: the files are kept and an alert
is sent), a settled snapshot that switched only some disks (refused, the
others' files removed, no second attempt: `NasMergeBackAndStrayTest`), the
files of the disks a settled answer did not switch removed before a stop
held through the call (`NasHeldStopDropTest`), a stop held through a quiesced call that timed out or failed with
the snapshot in place (the guest is thawed before the stop ends the read),
the recovery text per case (a definition still on a stale overlay:
`virsh edit`, never `qemu-img commit`; no commands for a disk the merge back
put back, unless no other disk is left to name: then the look-first
commands), and the note for UEFI variables
written during the read. The torn record (`TornRecordTest`, its path a
scratch file through `CST_VM_TORN`): a torn snapshot restic does not forget
is recorded (once, 0644, atomically), one it forgets leaves no entry, a
record that cannot be written or updated is said, a stop while forget waits
or while the record waits for its lock leaves the entry, a stop after a
record that failed names the snapshot left, a stop that ends restic after
it printed its snapshot leaves that snapshot recorded and forgets nothing
(or names it when the record fails), and a stop while the check of a
shut-off VM started during the read waits for virsh is raised once its
snapshot is recorded (`NasStopHeldFromAnswerTest` adds the staged files'
removal and the check of a VM still shut off, in-process; a stop after a
read whose check passed, live or shut off, carried or held, indexes the
snapshot after the merge back and names it in the stop, with no listing,
forget or retention, and indexes nothing when the merge back fails; the
index refusing it, or failing to be written, puts its words in the stop; a check that could not be
done records the snapshot as torn only after a stop that ended restic, the
check's error kept in the stop's context; `NasStopAfterTheMergeBackTest`: a
stop after a good read's merge back, in the retention, the index's listing
or its write, indexes the snapshot before it ends the run, with nothing
listed or forgotten after it; `ResticBackupAnsweredTest`
the call that starts the hold, once restic exited with its snapshot); the next run forgets a recorded snapshot restic lists with
both tags by its listed ID (under the host it recorded), skips the retention
when that forget fails, a listing fails, an ID is ambiguous or its listed
snapshot lacks a tag, drops one restic no longer lists without forgetting
it, keeps the entries of other VMs, repositories and
newer formats, refuses a newer or unreadable record before anything, and
the record's lock gives up after its timeout. The check of the snapshots no
record names (`NasStraySnapshotTest`, `ResticInstantTest`): a newer one is
named (its short ID and time, the commands with its full ID) with one alert,
and the retention waits; one older than every indexed backup, the run's own,
an adopted one, a recorded torn one and one listed under a new ID after
`restic tag` (its original indexed) are not; no NAS entry of the VM in the
index means no listing at all; an unreadable time names the stray but is no
baseline; times are compared as instants across a change of the offset; a
listing that fails or an index or record that cannot be read makes the
retention wait unnamed. Tier 1's `test_203` runs it against the real restic
(its `time`, the `original` a real `restic tag` sets, the `--host`
filter): the stray named and not counted, then quiet after it is adopted or
forgotten, and no listing with the index moved aside. Tier 1's `test_200` stops a
real restic read once restic printed its snapshot (the snapshot is recorded
and named, and the next retry forgets it), `test_204` stops it once restic
ended on its own (exit 0: the snapshot is indexed, the record stays empty,
and the next retry names nothing and runs the retention), `test_221` stops
the retention's restic forget after a good read (the snapshot is indexed
first, nothing is forgotten, and the next retry names nothing and runs the
retention), and `test_151` runs the
torn-snapshot retry, the retention after it and `forget_torn` against the
real restic over SFTP to its NAS container (restic's own `snapshots --json`,
a short recorded ID, the record and its lock as root). No E2E test runs a
whole NAS backup: neither tier has libvirt with a running guest.

A model of the NAS backup's protocol, `vm/tests/quint/nas.qnt` (a Quint spec
that TLC checks over every state of its instances: `invowk cmd test quint`),
adds the runs nobody wrote a test for: a stop, a kill, the guest shutting
down or pausing, or libvirt answering late, between any two steps. Its
properties: each disk is merged back or the alert names its overlay
(`mergedOrAlerted`); an overlay file is never removed while in use or
holding writes (`noOverlayLost`); a stale definition is never named for
`qemu-img commit` (`staleNamed`); an unreachable NAS leaves no overlay
(`unreachableNoOverlay`); a leftover overlay is refused, never stacked
(`noStacking`); a torn snapshot is recorded or named (`tornHandledFull`:
the gaps left are a kill before its record, and restic ended between
saving its snapshot and printing its summary); the index holds no torn
snapshot and loses an entry only on a listing it read (`indexClean`,
`indexInRepo`); the retention never lets a torn snapshot take a good
one's place and keeps the kept ones (`goodKeptFull`, `keepKept`); only a
consistent read is indexed (`consistentRead`), also without `--atomic`
(`nas_partial`); a retention never runs while a snapshot no record names
is newer than the oldest indexed one (`strayNotCounted`), and never lets a
torn snapshot take a good one's place but one the check cannot see (older
than every indexed backup) or the user adopted (`goodKeptAll`); a run a
stop ended after a checked read, or after a good read's merge back, all
merged back, has its snapshot in the index (`checkedIndexed`); six
instances, `nas_strays` the one with four runs where the user adopts or
forgets a named snapshot. Its traces are replayed against the real code
(`vm/tests/test_vm_nas_replay.py`, in the unit suite), so a change of order
in `nas.py` fails the unit tests until the spec follows it. What it models,
its gaps and its numbers:
[docs/dev-tools.md, "Model checking with Quint"](../docs/dev-tools.md#model-checking-with-quint-optional).

For `tune`: the transform of a copy of the real `win11-devbox` definition
(no diff), of the same VM before its migration (SATA, plain driver,
e1000e), with an existing non-virtio SCSI controller, several disks (units,
IDE rename, block disks tuned, network disks and CD-ROMs left alone), the
driver attributes merged, the knobs, the MAC kept, a second pass changing
nothing, Windows without the flag, and `apply` (refused while Windows runs,
defined from the `--security-info` dump, old definition 0600, no secrets
printed). For `nocow`: the probe with fake inode flags (unreadable is never
"copy-on-write"), the conversion commands, `apply` with sudo for the system
folder (creation like libvirt), and, on a real btrfs under `~/.cache`
(skipped elsewhere), that a reflink backup of a NOCOW image works and that
the clone fails without the pre-created NOCOW destination. `PackageTest`
in `vm/tests/test_vm.py` covers the settings, the parsing, the dispatch, the
virsh wrapper and the timer units in-process.

The Docker E2E tests ([tests/e2e/README.md](../tests/e2e/README.md), tier 2)
run the commands for real. `test_20`: `tune plan`, `apply` and a second run
against the real `virtqemud`, then the restore of the saved definition.
`test_21`: `nocow plan` and `apply` on a real btrfs, with the system folder
created through sudo, and a second run as root and as the user.

## 13. Python module layout

The Python code inside `vm/` (standard library only at run time):

- `vm-setup.sh` (at the top of CST): its header comment is the help of
  `cst vm`; the rest is a shim that runs `cst_core/route.py vm`. The router
  (`ROUTING` in `cst_core/domain/dispatch.py`, tests in
  `tests/test_module_routing.py`) prints the help or a usage error, and
  execs `cst-vm`: as the user for `list`, `status`, `backup-timer status`,
  `tune plan` and `nocow plan` without `--root` (from a toolbox on the host through
  flatpak-spawn), on the host only for the rest, which first runs the
  script again through sudo (`nocow apply` stays the user's).
- `vm/cst-vm`: the entry point (its docstring is the help); it only hands
  argv to `vm/cli/` (`main`). Installed as `/usr/local/sbin/cst-vm` (a zipapp, by
  `backup-timer on`).
- `vm/domain/`: the rules, no I/O. `tune.py`: the hardware `tune` recommends
  (`tune_domain`, `guest_kind`) and the lines that report it (`tune_line`,
  `cpu_status_line`, `cpu_owner_line`, and the plan's, `tune_plan_lines`). `__init__.py`: what a domain definition is
  made of (`domain_parts`), the lines that describe an image folder
  (`nocow_lines`), the NAS backup's names and retention (`valid_keep`,
  `overlay_kind`, `forget_args`), the timer units (`render_service`,
  `render_timer`), the command line (`parse_args`, which returns a `VmArgs`),
  and the records the other modules pass around (`DomainParts`, `LiveDisk`,
  `ImageDir`, `ImageProbe`; `cst doctor` uses the last two too). `backup.py`: the
  records of the backups (an index entry, the manifests, restic's summary, a
  fingerprint, the state of one NAS backup run: `NasRun`), the NAS backup's pure
  parts (`snapshot_args`, `nas_manifest`, `nas_index_entry`, `nas_index`,
  `nas_done_lines`, `restic_failure_why`), and the lines `status` and
  `backup-timer status` print about them (`package_change_lines`,
  `timer_status_lines`).
- `vm/adapters/`: what reaches the host. `__init__.py`: `virsh`, `rpm`, `rpm-ostree`
  (with `LC_ALL=C`, through cst_core's
  `SubprocessCommandRunner`; virsh through its `SubprocessVirshAdapter`), the settings
  files, and `fault_words`, cst-vm's words for a command that is missing or does not
  answer (systemctl runs through cst_core's `SystemctlAdapter` with them, in the C
  locale, 120 s at most); `VmCommands` (libvirt, the packages, the deployment) and
  `VmConfFiles` (the settings) are the ports' adapters there. `backup.py`: the
  backup index, the record of torn NAS snapshots (`read_torn`, `record_torn`,
  `drop_torn`, under its own lock), fingerprints and sizes, the stop signals,
  the per-VM lock and the alert; `BackupFiles`, `StopSignals` and `TerminalLines` are the ports'
  adapters there. `restic.py`: restic (`Restic`: a command, and `restic backup
  --json` with its progress), generic: it takes the repository's variables at
  each call and names no VM, so the /home NAS backup can use it as it is.
  `timer.py`: the weekly timer's files. `images.py`: the NOCOW flag
  and the image folders (`probe_dir`), and setting the mark (`mark_dir`; the
  system folder through `sudo` when not root); `ImageFolders` gives them to `vm/cli` through `wiring.images` (no use case is typed against them, so no port). `nocow apply` has no
  application layer: it marks folders one by one, nothing to roll back.
- `vm/wiring.py`: the composition root: the environment of one run becomes a
  `VmSettings` (`vm/domain`: its `VmPaths`, `CST_PROG`, the test overrides
  `CST_VM_*`, which move each path for the tests only, and the snapshot and
  merge timeouts). `cst doctor` builds the same settings for its VM checks.
  It also builds the use cases and the ports over the adapters (`local_backup`,
  `nas_backup`, `timer_port`, `systemd`, `settings_port`, ...) and hands `vm/cli`
  the image folders (`images`: an adapter, no port): vm is in
  `HEXAGONAL_PACKAGES`, so only `wiring.py` and `vm/adapters/` import an adapter.
- `vm/application/`: the use cases that change the machine in several
  steps, typed against the ports of `vm/ports.py`. `backup.py`: the local
  backup (`backup_local`) and the checks every backup makes first, over
  `VmCommandsPort`, `BackupStorePort` (the backups' files and the index,
  `vm/adapters/backup.py`'s `BackupFiles`), cst_core's `ClockPort` and a
  `ProgressPort` (the lines it prints as it goes). `nas.py`: the NAS backup
  (`backup_nas`: the snapshot, restic, the merge back, the torn snapshots of
  earlier runs, the retention), moved
  from the cli, over the same ports plus `SettingsPort` (vm.conf and
  the NAS settings), `ResticPort` (`vm/adapters/restic.py`'s `Restic`, generic:
  the /home NAS backup can use it), `SignalsPort` (the stop signals: caught,
  held during the snapshot call, ignored during the merge back and what
  follows it: the unclear snapshot's problems, the alert and the building
  of the refusal), the per-VM lock and the critical alert. `__init__.py`: `backup-timer on` and
  `off` (`TimerFilesPort`: the timer's files through the installer and the
  links that say which timer is on, its adapter `vm/adapters/timer.py`'s
  `TimerFiles`; systemd through cst_core's `SystemdPort`), and `tune` (plan,
  then apply: `VmCommandsPort`, libvirt as `vm.adapters.VmCommands` reaches it:
  virsh, and `virsh define` of a text; and `DefinitionStorePort`, the copy of
  the definition kept before tune, `vm/adapters/backup.py`'s `DefinitionFiles`).
- `vm/cli/`: the commands, one module per command group: `backup.py` (the
  local copy), `nas.py` (the NAS backup), `timer.py` (`backup-timer`),
  `status.py` (`list`, `status`), `tune.py`, `nocow.py`; `main` is in
  `__init__.py`. They parse, take their ports from `vm/wiring.py`, run a use
  case (or read the ports, for list, status and nocow) and print lines that
  are mostly built in `vm/domain`: no I/O of their own.

`cst doctor` imports `vm.domain.tune` (tune rules), the records of `vm.domain`,
`vm.adapters` (knobs) and `vm.adapters.images` (the image folder probe) from
the same CST copy.
