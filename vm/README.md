# `cst vm`: backups, virtual hardware and NOCOW for the libvirt VM

This folder holds `cst-vm`, the program behind `cst vm` (the module script
is `vm-setup.sh` at the top of CST). It makes a copy of a libvirt VM (by
default `win11-devbox`): its disks, its UEFI variables, its TPM state and
its definition. It also shows whether the VM is ready for the QEMU it runs on,
gives it the recommended virtual hardware (`tune`: virtio-scsi disks with
`cache='none'`, virtio network card; section 8) and keeps the VM image
folders without copy-on-write (`nocow`; section 9).
How to set up `cst`: [../README.md](../README.md#the-cst-command).

> **In short**
>
> - **Before every Fedora major upgrade**: shut the VM down, then run
>   `cst vm backup`. After the upgrade, **keep the old deployment pinned**
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
cst vm backup [VM] [--full]   # (root, calls sudo) the VM must be shut off
cst vm list [VM]              # the backups (as your user, also in a toolbox)
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
(`win11-devbox`). `backup`, `tune apply`, `nocow apply` and
`nocow plan --root` must run on the host: in a toolbox they stop and show
the command to type in a host terminal. `list`, `status`, `tune plan` and
`nocow plan` run on the host by themselves (through `flatpak-spawn --host`).
Settings: the optional `/etc/cst/vm.conf` (section 8).

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

## 3. What a backup contains

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
with a backing file. Back those up by hand.

A disk, UEFI variables file or TPM folder that is a symbolic link is copied
through the link: the backup holds the data of the file it points to, under
that file's own path, and the restore commands write back to that file.

Where: `/var/lib/libvirt/cst-backups/VM/YYYYmmddTHHMMSSZ/` (UTC time; the
folders are mode 0700, owned by root). Each source keeps its full path under
`files/`, for example
`files/var/lib/libvirt/images/win11-devbox.qcow2`, so restoring is one
`cp` per file.

The backup also writes `/var/lib/cst/vm-backups.json` (mode 0644): a list
of the backups with the VM, time, path, mode, package versions and
deployment, and **no secrets**. `cst vm list`, `cst vm status` and
`cst doctor` read it as your user.

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
`cp --reflink=never --sparse=always -a`, so empty parts of the image stay
empty. A disk marked NOCOW (`chattr +C`) gets a NOCOW copy, because btrfs
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
  last backup: win11-devbox     20261114T120000Z  reflink  deployment 45.20261107.0  qemu-kvm-11.1.3-1.fc45.x86_64  /var/lib/libvirt/cst-backups/...
  virtualization packages: unchanged since the last backup
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
they are kept), shows the diff, asks `[y/N]` (only in a terminal), saves the
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
sudo cp -a --reflink=never --sparse=always $F.cow-old $F
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

## 10. Follow-ups (not done yet)

- A copy on the NAS (restic, like the `/home` snapshots in
  [btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md)): the only protection against
  losing both disks.
- `cst vm prune --keep N`: removing old backups automatically.

## 11. Tests

```
python3 -I -B -m unittest discover -s vm/tests -t . -v
```

`virsh`, `cp`, `rpm` and `rpm-ostree` are stubs that log their calls; the
backup folder, the index and the swtpm folder are scratch paths. The tests
check the paths taken from the definition, the refusals (running VM, block
disks, qcow2 overlays, no space for `--full`, reflink not possible), the
modes of the folders and files, that the index holds no secrets, that
removed backups leave the index, and `list` and `status`.

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
the clone fails without the pre-created NOCOW destination.
