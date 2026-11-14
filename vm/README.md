# `cst vm`: backups of the Windows VM before risky upgrades

This folder holds `cst-vm`, the program behind `cst vm` (the module script
is `vm-setup.sh` at the top of CST). It makes a copy of a libvirt VM (by
default `win11-devbox`): its disks, its UEFI variables, its TPM state and
its definition. It also shows whether the VM is ready for the QEMU it runs on.
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

---

## 1. Commands

```
cst vm backup [VM] [--full]   # (root, calls sudo) the VM must be shut off
cst vm list [VM]              # the backups (as your user, also in a toolbox)
cst vm status [VM]            # state, CPU mode and the CET/MBEC fix, last backup,
                              # and the virtualization packages changed since it
cst vm help
```

`VM` defaults to `VM_NAME` in `/etc/cpu-setup/cpu-setup.conf`
(`win11-devbox`). `backup` must run on the host: in a toolbox it stops and
shows the command to type in a host terminal. `list` and `status` run on the
host by themselves (through `flatpak-spawn --host`).

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
clones only between files with the same flag.

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
  last backup: win11-devbox     20261114T120000Z  reflink  deployment 45.20261107.0  qemu-kvm-11.1.3-1.fc45.x86_64  /var/lib/libvirt/cst-backups/...
  virtualization packages: unchanged since the last backup
```

When a package changed since the last backup, it lists them: back up again
once the VM works on the new versions.

## 8. Follow-ups (not done yet)

- A copy on the NAS (restic, like the `/home` snapshots in
  [btrfs/SNAPSHOTS.md](../btrfs/SNAPSHOTS.md)): the only protection against
  losing both disks.
- `cst vm prune --keep N`: removing old backups automatically.

## 9. Tests

```
python3 -I -B -m unittest discover -s vm/tests -t . -v
```

`virsh`, `cp`, `rpm` and `rpm-ostree` are stubs that log their calls; the
backup folder, the index and the swtpm folder are scratch paths. The tests
check the paths taken from the definition, the refusals (running VM, block
disks, qcow2 overlays, no space for `--full`, reflink not possible), the
modes of the folders and files, that the index holds no secrets, that
removed backups leave the index, and `list` and `status`.
