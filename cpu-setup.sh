#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# cpu-setup.sh  (the `cpu` module of CST: run it as `cst cpu ...`)
#
# Frontend for CPU/scheduler responsiveness on this workstation (Core Ultra 9
# 285K: P-cores 0-7, E-cores 8-23, no SMT): games stay smooth while builds,
# tests, Docker and the Windows VM run. The kernel scheduler stays stock
# (EEVDF, default preemption); what this does is placement, cgroup priority
# and memory protection around it. Components live in ./cpu/ as real files.
#
# Commands:
#   install                (root) idempotent:
#       * game profile, switched on/off by GameMode when a game starts/ends
#         (cpu-game.service; polkit lets your session start/stop only that
#         unit; a 30 s watchdog switches it off if the game died): VM and
#         Docker builds to CPUs 12-23 with a low weight, toolboxes idle-class,
#         and so are the other boxes of your user manager next to the game
#         (background.slice, `systemd-run --user` jobs and their own slices),
#         P-cores EPP performance and C-state latency ceiling (<= GAME_CSTATE_MAX_LATENCY_US),
#         active game GPU boost (render-offload DRM discovery + connected
#         display GPU runtime PM on + AMD DPM 3D_FULL_SCREEN), the game's
#         memory protected along the whole cgroup chain; runtime-only, exact revert
#       * GPU runtime PM & CPU C-state baseline (cst-cpu-power.service +
#         65-cst-gpu-pm.rules): keeps power/control=on on connected display GPUs
#         across hotplug (GPU_PM_MODE='always') and caps baseline deep C-states
#         (CPU_CSTATE_MAX_LATENCY_US='200')
#       * baseline weights: VM (machine.slice) 50, toolboxes 30 (like
#         background.slice), so they never tie with the desktop
#       * toolboxes and their host commands (flatpak-spawn --host) always on
#         the E-cores (cgroup AllowedCPUs; cpuset delegated to the user manager,
#         next login): Claude Code, shells, cargo/go/gradle/maven size their
#         parallelism from that set, no per-toolchain config needed. Side
#         effect: host commands of Flatpak apps with org.freedesktop.Flatpak
#         access (here OBS, Remmina, RustConn, Resources, TestCenter) also
#         run on the E-cores. Not covered outside games: `systemd-run --user`
#         jobs (app.slice, or a slice of their own unless it is named
#         user-NAME.slice) and Docker without docker-slice (dockerd is a
#         system service)
#       * HWP dynamic boost (/sys/.../hwp_dynamic_boost=1) and /etc/modules-load.d/cst-ntsync.conf
#         (loads ntsync at boot and via modprobe for Wine/Proton NT sync)
#       * GameMode: ~/.config/gamemode.ini (only triggers the profile and
#         disables the split-lock penalty during games), your user in the
#         'gamemode' group (copied from /usr/lib/group; next login)
#       * cpu-watch (frame-pacing telemetry for A/B comparisons)
#       * ~/.local/bin/cst -> the cst command of this folder (for your user,
#         if nothing else is there), so the hints of installed tools work
#   status                 (root) the whole picture
#   uninstall              (root) removes what install added
#   game on|off|state      (root) switch the game profile by hand
#   vm-pin plan|apply|status   (root) the VM permanently on 12 vCPUs pinned to
#                          12-23, and with <cpu mode='host-passthrough'> the
#                          CPU features of VM_CPU_DISABLE in cpu-setup.conf
#                          kept off (default: CET and nested MBEC, which make
#                          Windows bootloop under QEMU 11; '' = leave them)
#                          (diff first; apply asks; effective at the VM's
#                          next start, which is left to you)
#   docker-slice install|status|uninstall   (root) Docker containers born in
#                          builds.slice (weight 20, E-cores only; idle + 12-23
#                          during games):
#                          daemon.json "cgroup-parent"; restarting dockerd (stops
#                          containers) is left to you
#   experiment list|on NAME|off NAME   (root) runtime-only memory A/B knobs
#                          (thp-defer, wsf-300, minfree-256m); persist the
#                          winners in swap/sysctl/ (`cst swap apply`)
#   power [status|plan|apply|revert]   (`status`/`plan`: user & toolbox;
#                          `apply`/`revert`: root on host) manage connected display GPU
#                          runtime PM and CPU deep C-state latency ceilings
#   watch -l LABEL [--game NAME] [--seconds N] [--mangohud DIR] [-- CMD]
#                          (user) record game wait/memory stalls/frequencies
#   report [RUN...]        (user) compare recorded runs
#   quiet [--for SECONDS] [--wait MAX] [--json] [--cpu PCT] [--cpu-pressure PCT]
#         [--io-pressure PCT] [--memory-pressure PCT] [--write-mbps MB]
#                          (user) is the machine quiet enough for a benchmark?
#                          exit 0 quiet, 1 not (each test with its value and
#                          limit); --wait 2h waits for a quiet window:
#                          `cst cpu quiet --wait 2h && make bench`
#   help
#
# Root commands must run on the host, not inside toolbox; watch/report/quiet
# and read-only `power status|plan` work in both (cpu-watch runs on the host).

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
# The commands are cpu/cpu-setup (the cpu.setup package): it prints the help
# above, execs cpu-watch for watch, report and quiet (on the host from a
# toolbox), and runs this script again through sudo for the root commands.
# CST_SELF and CST_ARG0: this script and the name it was started with;
# CST_LC_CTYPE: the caller's LC_CTYPE, which Python changes at start under a
# C locale.
CST_SELF="${SELF}" CST_ARG0="$0" CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" \
    exec /usr/bin/python3 -I -B "$(dirname "${SELF}")/cpu/cpu-setup" "$@"
