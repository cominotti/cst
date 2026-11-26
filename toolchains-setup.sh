#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
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
#   config plan | apply [--lean-debuginfo | --no-lean-debuginfo] [--sccache | --no-sccache]
#                          cargo's build-dir line in ~/.cargo/config.toml and
#                          uv's link-mode = "clone" in ~/.config/uv/uv.toml.
#                          A setting already there is never changed. Also run
#                          by `cst btrfs snapshots setup`. Opt-in (only with
#                          RUST_LEAN_DEBUGINFO=yes or --lean-debuginfo; never
#                          by snapshots setup): lean debug info for dev builds,
#                          which makes every workspace rebuild once
#                          Opt-in (RUST_SCCACHE=yes or --sccache): rustc through
#                          sccache via ~/.local/libexec/cst/rustc-wrapper as
#                          [build] rustc-wrapper; --no-sccache removes CST's line
#   help
#
# Settings: toolchains/toolchains.conf in CST (the defaults), then
# ~/.config/cst/toolchains.conf (yours, wins), e.g. RUST_BUILD_MAX_IDLE=48h,
# RUST_BUILD_MAX_TOTAL=150G, RUST_BUILD_MIN_KEEP=2h, RUST_BUILD_ORPHANS=yes,
# RUST_BUILD_INCREMENTAL_IDLE=6h, PYTHON_UV_ENABLED=yes, RUST_LEAN_DEBUGINFO=no,
# RUST_INCREMENTAL=auto, RUST_SCCACHE=no, RUST_SCCACHE_CACHE_SIZE=25G,
# RUST_SCCACHE_WORKSPACE=no.
# Started as root through sudo (also by `cst status`), it runs again as the
# user who called sudo. Inside a toolbox the program runs itself on the host.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
# The routing (every command changes only the caller's files: as root, the
# runuser re-run as SUDO_USER) is ROUTING in cst_core/domain/dispatch.py, run by
# cst_core/route.py, which prints the help above. CST_SELF and CST_ARG0: this
# script and the name it was started with; CST_LC_CTYPE: the caller's
# LC_CTYPE, which Python changes at start under a C locale.
CST_SELF="${SELF}" CST_ARG0="$0" CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" \
    exec /usr/bin/python3 -I -B "$(dirname "${SELF}")/cst_core/route.py" toolchains "$@"
