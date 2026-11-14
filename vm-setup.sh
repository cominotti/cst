#!/usr/bin/env bash
#
# vm-setup.sh  (the `vm` module of CST: run it as `cst vm ...`)
#
# Backups of the libvirt VM (win11-devbox) before risky changes, such as a
# Fedora major upgrade, which brings a new QEMU, libvirt, UEFI firmware and
# TPM emulator at once. The program is vm/cst-vm; the rationale, and how to
# restore, are in vm/README.md.
#
# Commands:
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
#                          disables, the last backup, and the virtualization
#                          packages that changed since it
#   help
#
# VM defaults to VM_NAME of /etc/cpu-setup/cpu-setup.conf (win11-devbox).
# backup runs on the host only; list and status also work in a toolbox
# (they run on the host through flatpak-spawn). Old backups are removed by
# hand (sudo rm -r the folder); the next backup drops them from the list.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SRC="$(dirname "${SELF}")/vm"
TOOL="${SRC}/cst-vm"

# Shown in the help and in hints: "cst vm" when run through the cst command
# (it sets CST_PROG), else the name this script was started with.
PROG="${CST_PROG:-$0}"

usage() {
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    echo "Usage: ${PROG} <command> [args]"
}
in_container() {
    case "${CST_VM_CONTAINER:-}" in 1) return 0 ;; 0) return 1 ;; esac   # override: tests
    [[ -e /run/.containerenv || -e /.dockerenv ]]
}

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift
orig_args=("${cmd}" "$@")

case "${cmd}" in
    help|-h|--help) usage; exit 0 ;;
    list|status)
        # Read-only, as any user: the index is world-readable and libvirt's
        # system connection answers the user's queries (libvirt group or polkit rule).
        if in_container; then
            if command -v flatpak-spawn >/dev/null 2>&1; then
                exec flatpak-spawn --host "--env=CST_PROG=${PROG}" python3 -I "${TOOL}" "${cmd}" "$@"
            fi
            # A container without a way to the host (CI, plain podman/docker):
            # answer from here, which degrades to info lines.
            echo "(cannot reach the host from this container: showing what is visible here)"
        fi
        CST_PROG="${PROG}" exec python3 -I "${TOOL}" "${cmd}" "$@"
        ;;
    backup) ;;
    *) echo "Unknown command: ${cmd}" >&2; usage >&2; exit 2 ;;
esac

# ---- root commands -------------------------------------------------------------
if in_container; then
    echo "'${PROG} ${orig_args[*]}' must run on the host, not inside a toolbox/container." >&2
    echo "In a host terminal: ${PROG} ${orig_args[*]}" >&2
    exit 1
fi
if [[ ${EUID} -ne 0 ]]; then
    # sudo resets the environment: CST_PROG is handed over explicitly.
    if [[ -n "${CST_PROG:-}" ]]; then
        exec sudo -- env "CST_PROG=${CST_PROG}" "${SELF}" "${orig_args[@]}"
    fi
    exec sudo -- "${SELF}" "${orig_args[@]}"
fi
CST_PROG="${PROG}" exec python3 -I "${TOOL}" "${cmd}" "$@"
