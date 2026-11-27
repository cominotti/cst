#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# desktop-setup.sh  (the `desktop` module of CST: run it as `cst desktop ...`)
#
# Reduce desktop background writes on Fedora Workstation/Silverblue:
#   status                              read-only: show GNOME localsearch and browser profile sync status
#   gnome localsearch <subcmd> [opts]   restrict or disable GNOME localsearch-3 indexing (as your user)
#     status                            read-only: current gsettings and localsearch user units
#     plan [--mode restrict|disable]    dry run showing exact changes (changes nothing)
#     apply [--mode restrict|disable]   restrict (default: standard XDG dirs) or disable + mask localsearch-3
#     revert                            unmask units and restore pre-CST gsettings
#   psd <subcmd> [opts]                 overlayfs browser profile sync over RAID1 (no COPR, keeps Flatpak isolation)
#     status [--user NAME]              read-only: tmpfs, overlay mounts, recovery snapshots, and units
#     plan [OPTIONS]                    dry run showing profiles, tmpfs size cap, and actions
#     apply [OPTIONS]                   (root, host) install systemd units, mount idle profiles, enable hourly timer
#     revert [--user NAME]              (root, host) merge + unmount overlays, disable units, remove installed files
#     start | stop | sync [--user NAME] (root, host) systemd service/timer lifecycle actions
#     recover [--discard] [--user NAME] (root, host) replay or discard unmerged crash-recovery snapshots
#   help                                show this help
#
# Settings: ~/.config/cst/desktop.conf (KEY=value lines):
#   PSD_PROFILES=auto                   comma-separated profile IDs or `auto` (chrome-flatpak, chrome, chromium-flatpak,
#                                       firefox, firefox-legacy; optional: fastmail-flatpak, discord-flatpak)
#   PSD_TMPFS_SIZE=4G                   RAM upperdir tmpfs size cap (clamped to <= 25% of host RAM, max 32G)
#
# Started inside a toolbox, `status` and `plan` run on the host automatically while mutating `psd` actions refuse.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
CST_SELF="${SELF}" CST_ARG0="$0" CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" \
    exec /usr/bin/python3 -I -B "$(dirname "${SELF}")/cst_core/route.py" desktop "$@"
