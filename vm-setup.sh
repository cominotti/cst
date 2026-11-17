#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# vm-setup.sh  (the `vm` module of CST: run it as `cst vm ...`)
#
# Backups of the libvirt VM (win11-devbox) before risky changes, such as a
# Fedora major upgrade, which brings a new QEMU, libvirt, UEFI firmware and
# TPM emulator at once. The program is vm/cst-vm; the rationale, and how to
# restore, are in vm/README.md.
#
# Commands:
#   backup [VM] --nas [--keep-tag NAME] [--no-quiesce]
#                          (root) the default backup: to the NAS with restic,
#                          into the repository of the /home NAS backup
#                          (`cst btrfs snapshots nas setup` first). A running
#                          VM keeps running: a short-lived external snapshot
#                          holds its writes while restic reads the disks,
#                          then virsh blockcommit merges it back. Quiesced
#                          through the QEMU guest agent when it answers, else
#                          crash-consistent (the output and the index say
#                          which). Keeps 4 weekly + 3 monthly restic snapshots
#                          of the VM (VM_NAS_KEEP_WEEKLY, VM_NAS_KEEP_MONTHLY);
#                          --keep-tag NAME: kept until you forget it by hand
#   backup-timer on|off|status [VM]
#                          on/off (root): the weekly backup --nas (Saturday
#                          04:30, systemd timer cst-vm-backup@VM.timer);
#                          status (user): on or off, up to date, next run
#   backup [VM] [--full]   (root) the VM must be shut off. Copies every file
#                          disk, the UEFI variables, the TPM state, the
#                          definition (XML, 0600: it holds secrets) and a
#                          manifest (package versions, booted deployment) to
#                          /var/lib/libvirt/cst-backups/VM/<UTC time>/, and
#                          lists it in /var/lib/cst/vm-backups.json.
#                          Default: reflink copies, instant on btrfs. They
#                          share blocks with the VM's files: they protect
#                          against changes to the images (a repair tool, an
#                          upgrade), NOT against losing the disks or the
#                          filesystem. --full: real copies, after a check of
#                          the free space. Prints the restore commands;
#                          never restores by itself
#   list [VM]              (user) the backups, oldest first
#   status [VM]            (user) the VM's state, CPU mode and CET/MBEC
#                          disables, whether its hardware matches `tune`, the
#                          last backup, the virtualization packages that
#                          changed since it, and the NOCOW state of the VM
#                          image folders
#   tune plan [VM] [--guest-has-virtio]
#                          (user) the definition changes that give the VM the
#                          recommended hardware: disks on virtio-scsi with
#                          cache='none' io='native' discard='unmap'
#                          detect_zeroes='unmap', network card virtio (same
#                          MAC). Windows (or a guest not known to be Linux):
#                          bus and network card only with --guest-has-virtio,
#                          after the virtio drivers are in the guest (the plan
#                          prints how). Changes nothing
#   tune apply [VM] [--guest-has-virtio]
#                          (root) the same, asks, saves the old definition
#                          (0600) and runs virsh define: effective at the next
#                          start
#   nocow plan [--root]    (user) the VM image folders (libvirt system and
#                          session, GNOME Boxes RPM and Flatpak): NOCOW mark
#                          (+C) and the images still copy-on-write, with the
#                          commands to convert each. --root: through sudo,
#                          to read the root-only /var/lib/libvirt/images
#   nocow apply            (user, sudo for the system folder) sets +C on the
#                          folders; never converts an existing image
#   help
#
# VM defaults to VM_NAME of /etc/cpu-setup/cpu-setup.conf (win11-devbox).
# Settings: /etc/cst/vm.conf (optional): VM_NOCOW, VM_TUNE_DISK_BUS,
# VM_TUNE_DISK_DRIVER, VM_TUNE_NIC, each yes (default) or no;
# VM_NAS_KEEP_WEEKLY (4), VM_NAS_KEEP_MONTHLY (3), VM_NAS_TIMEOUT_HOURS (6).
# backup, backup-timer on|off, tune apply and nocow apply run on the host
# only; list, status, backup-timer status, tune plan and nocow plan also work
# in a toolbox (they run on the host through flatpak-spawn). Old backups are removed by hand (sudo rm -r the
# folder); the next backup drops them from the list.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
# The routing (read, user or root commands; flatpak-spawn from a toolbox,
# the sudo re-run) is ROUTING in cst_core/domain/dispatch.py, run by
# cst_core/route.py, which prints the help above. CST_SELF and CST_ARG0: this
# script and the name it was started with; CST_LC_CTYPE: the caller's
# LC_CTYPE, which Python changes at start under a C locale.
CST_SELF="${SELF}" CST_ARG0="$0" CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" \
    exec /usr/bin/python3 -I -B "$(dirname "${SELF}")/cst_core/route.py" vm "$@"
