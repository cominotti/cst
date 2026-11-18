#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# bluetooth-setup.sh  (the `bluetooth` module of CST: run it as `cst bluetooth ...`)
#
# Bluetooth audio of this workstation: LE Audio (BAP/LC3) in BlueZ, and an
# adaptive LDAC quality for the headset (DEVICE in the settings) when it is
# connected over A2DP. LE Audio is kept OFF in PipeWire by default
# (LE_AUDIO=off): BlueZ stays ready for it, PipeWire offers only A2DP and HFP. Replaces the standalone apply-le-audio.sh; what
# that script wrote is recognised and adopted, without any restart. The
# rationale is in bluetooth/README.md.
#
# Commands:
#   plan [OPTIONS]         what apply would change (dry run). Works as your
#                          user, also in a toolbox. Exit 0 = nothing to do,
#                          10 = apply would write or restart something
#   apply [OPTIONS]        idempotent. Runs as your user and calls sudo by
#                          itself, only for the system parts:
#       * /etc/bluetooth/main.conf, section [General] (root):
#         Experimental = true (BlueZ's experimental plugins and interfaces:
#         BAP, BASS, VCP, MICP, CSIP, Device1.PreferredBearer) and
#         KernelExperimental = the ISO socket UUID only (an old "true" is
#         replaced). A timestamped backup is taken first
#       * ~/.config/wireplumber/wireplumber.conf.d/50-bluez-<MAC>.conf (your
#         user): the headset's LDAC quality
#       * ~/.config/wireplumber/wireplumber.conf.d/90-cst-le-audio-off.conf
#         (your user), with LE_AUDIO=off (the default): PipeWire registers
#         only the classic roles (A2DP, HFP) with BlueZ, for all devices.
#         With LE_AUDIO=on it is removed. Either change restarts WirePlumber
#       * restarts, only when something changed (or with --force):
#         bluetooth.service (asks first: Bluetooth keyboards, mice and
#         headsets drop for a few seconds) and your WirePlumber (audio stops
#         for a moment). Afterwards the headset's A2DP is reconnected if
#         only HFP came back (not while its microphone is in use)
#       * the preferred bearer, if one is set (--bearer, or BEARER)
#       * the LDAC rate watcher (your user), only with LDAC_RATE=48000 in the
#         settings: ~/.local/lib/cst/bt-ldac-watch and the user unit
#         ~/.config/systemd/user/cst-bt-ldac48.service, enabled and started.
#         It puts LDAC back to 48000 Hz when the headset itself set up
#         96000 Hz. With LDAC_RATE empty (the default) apply removes both
#       * verification at the end (the checks of status)
#   status [--device MAC] [--ldac-quality Q] [--le-audio on|off]
#                          read-only: the files, then ISO socket, the
#                          adapter's ISO feature, BAP in BlueZ, PipeWire's
#                          BAP LC3 endpoint, the headset's LDAC codec,
#                          quality and sample rate, the LDAC rate watcher
#                          (and its last failure, from its log).
#                          Works as your user, also in a toolbox.
#                          Exit 1 when a check failed
#   le-audio [on|off] [--yes] [--no-restart]
#                          turn LE Audio in PipeWire on or off for good:
#                          sets LE_AUDIO in ~/.config/cst/bluetooth.conf
#                          (creates it if needed), then runs apply (which
#                          restarts WirePlumber). Without on/off: shows the
#                          current setting (read-only)
#   revert [--check] [--yes] [--no-restart] [--device MAC]
#                          the two main.conf keys back to how the oldest
#                          backup has them (the old script's backups count),
#                          the WirePlumber rule, the LE Audio drop-in (so
#                          LE Audio is on again) and the LDAC rate watcher
#                          removed; restarts as apply.
#                          --check: only show what it would do (also
#                          works in a toolbox)
#   help
#
# Options of plan and apply:
#   --device MAC           the headset (default: DEVICE in the settings; none
#                          set: the headset's parts are skipped)
#   --ldac-quality Q       auto (adaptive, the default) | hq | sq | mq
#   --bearer B             preferred transport: le | bredr | last-used (only
#                          for connections the PC starts)
#   --le-audio on|off      LE Audio in PipeWire, for this run only (default:
#                          LE_AUDIO in the settings, "off"). To keep it on:
#                          le-audio on
#   --force                (apply) restart bluetoothd and WirePlumber even
#                          when nothing changed
#   --yes                  (apply, revert, le-audio) do not ask before
#                          restarting Bluetooth
#   --no-restart           (apply, revert, le-audio) write the files, restart nothing,
#                          print the commands instead; the restarts stay owed
#                          (plan lists them, the next apply makes them) until
#                          the service restarts or stops
#
# Settings: bluetooth/bluetooth.conf in CST (the defaults), then
# ~/.config/cst/bluetooth.conf (yours, wins); the options win over both.
# CST sets no headset: put DEVICE=<its MAC> in your file. Without one, plan,
# apply, status and revert skip the headset's LDAC rule, preferred bearer,
# LDAC rate watcher and checks. When your file has no DEVICE line and CST's
# LDAC rule for one headset is there (an older CST set that headset by
# default), that headset is used, and apply (or revert, before it removes
# the rule) saves it as DEVICE in your file.
# Not as root: apply and revert run as your user (with sudo they switch back
# to the user who called sudo, so no file in your home is owned by root).

# This file is a thin shim: the program is bluetooth/bt-setup, on the system
# Python (/usr/bin/python3 -I); its help is the header above.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
# CST_ARG0: the name this script was started with (the help and hints use
# it when CST_PROG is not set). CST_LC_CTYPE: the caller's LC_CTYPE ("" when
# unset, "=VALUE" when set), which Python changes at start under a C locale.
# The program takes both out of its environment again.
CST_ARG0="$0" CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" exec /usr/bin/python3 -I -B "$(dirname "${SELF}")/bluetooth/bt-setup" "$@"
