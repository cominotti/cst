#!/usr/bin/env bash
#
# toolchains-setup.sh  (the `toolchains` module of CST: run it as `cst toolchains ...`)
#
# The caches of the developer toolchains and the toolchain settings CST
# writes. Targets: rust:build, cargo's intermediate build files in
# ~/.cache/cargo-build (one folder per workspace path, never deleted by
# cargo; they once grew to 356 GiB in 3 days), and python:uv (`uv cache
# prune`). The program is toolchains/cst-toolchains; the rationale is in
# toolchains/README.md.
#
# Commands (all as your user; never type sudo):
#   status [--brief]       read-only: per target what and where (through the
#                          ~/Bulk links), size, policy, last prune; the hourly
#                          timer; the installed rustup toolchains and which
#                          ~/Workspace projects pin them (a report: never
#                          removed); the toolchain settings below
#   prune plan [TARGET...] dry run: what prune apply would delete, and why
#   prune apply [TARGET...]
#                          delete now. TARGET is LANG:CACHE (rust:build) or
#                          LANG (rust); none = every target enabled in the
#                          settings. Deletes cargo build folders idle 48h or
#                          that no workspace under ~/Workspace claims any
#                          more, and only the incremental part of folders
#                          idle 6h. Never touches a folder used in the last
#                          2h or in use (open files, cargo's lock, a running
#                          cargo test on it)
#   auto on [--unless-off] the user timer cst-toolchains-prune.timer (hourly,
#                          random delay, idle IO priority) runs `prune apply`;
#                          the program is copied to ~/.local/lib/cst/.
#                          --unless-off: not after `auto off` (snapshots
#                          setup uses it)
#   auto off               stops and removes it, and remembers that:
#                          `cst btrfs snapshots setup` then leaves it off
#                          (until `auto on`)
#   config plan | apply [--lean-debuginfo | --no-lean-debuginfo]
#                          cargo's build-dir line in ~/.cargo/config.toml and
#                          uv's link-mode = "clone" in ~/.config/uv/uv.toml.
#                          A setting already there is never changed. Also run
#                          by `cst btrfs snapshots setup`. Opt-in (only with
#                          RUST_LEAN_DEBUGINFO=yes or --lean-debuginfo; never
#                          by snapshots setup): lean debug info for dev builds,
#                          which makes every workspace rebuild once
#   help
#
# Settings: toolchains/toolchains.conf in CST (the defaults), then
# ~/.config/cst/toolchains.conf (yours, wins), e.g. RUST_BUILD_MAX_IDLE=48h,
# RUST_BUILD_MAX_TOTAL=150G, RUST_BUILD_MIN_KEEP=2h, RUST_BUILD_ORPHANS=yes,
# RUST_BUILD_INCREMENTAL_IDLE=6h, PYTHON_UV_ENABLED=yes, RUST_LEAN_DEBUGINFO=no.
# Started as root through sudo (also by `cst status`), it runs again as the
# user who called sudo. Inside a toolbox the program runs itself on the host.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SRC="$(dirname "${SELF}")/toolchains"
TOOL="${SRC}/cst-toolchains"

# Shown in the help and in hints: "cst toolchains" when run through the cst
# command (it sets CST_PROG), else the name this script was started with.
PROG="${CST_PROG:-$0}"

usage() {
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    echo "Usage: ${PROG} <command> [args]"
}
die() { echo "${PROG}: ERROR: $*" >&2; exit 1; }

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift
case "${cmd}" in
    help|-h|--help) usage; exit 0 ;;
    status|prune|auto|config) ;;
    *) echo "Unknown command: ${cmd}" >&2; usage >&2; exit 2 ;;
esac

# The caches, the timer and the settings are the user's: as root (sudo, or
# `cst status` after asking sudo once) it starts again as that user.
if [[ ${EUID} -eq 0 ]]; then
    user="${SUDO_USER:-}"
    [[ -n "${user}" && "${user}" != root ]] ||
        die "run it from your user account (needs SUDO_USER): toolchain caches and settings belong to your user"
    uid="$(id -u "${user}")"
    env_args=("HOME=$(getent passwd "${user}" | cut -d: -f6)" "USER=${user}" "LOGNAME=${user}"
              "XDG_RUNTIME_DIR=/run/user/${uid}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus")
    [[ -n "${CST_PROG:-}" ]] && env_args+=("CST_PROG=${CST_PROG}")
    exec runuser -u "${user}" -- env -u XDG_CONFIG_HOME -u SUDO_USER "${env_args[@]}" "${SELF}" "${cmd}" "$@"
fi

# The program checks the rest of the command line (exit 2 on a mistake).
CST_PROG="${PROG}" exec python3 -I "${TOOL}" "${cmd}" "$@"
