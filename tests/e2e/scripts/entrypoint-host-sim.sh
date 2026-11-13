#!/usr/bin/env bash
set -euo pipefail

# Default is host-simulation mode (remove container marker files so cst acts as on a real host).
# Set E2E_KEEP_DOCKERENV=1 to verify cst's in_container() guardrail in dedicated tests.
if [[ "${E2E_KEEP_DOCKERENV:-0}" != "1" ]]; then
    rm -f /.dockerenv /run/.containerenv
fi

# Docker mounts private cgroupns read-only unless --privileged; with CAP_SYS_ADMIN
# remounting it rw lets unprivileged systemd PID 1 + user@.service run cleanly.
if [[ -d /sys/fs/cgroup ]]; then
    mount -o remount,rw /sys/fs/cgroup 2>/dev/null || true
fi

if [[ "${E2E_LOOP_BTRFS:-0}" == "1" ]]; then
    /usr/local/sbin/e2e-setup-loop-btrfs.sh
fi

exec "$@"
