#!/usr/bin/env bash
#
# btrfs-setup.sh  (the `btrfs` module of CST: run it as `cst btrfs ...`)
#
# Frontend for everything about storage on this workstation: btrfs RAID1 on
# dm-crypt over two NVMe, mounted `degraded` so it keeps running after a disk
# failure. Components live in ./btrfs/ as real files; this script installs,
# inspects and removes them, and forwards to the user-level tools.
#
# Commands:
#   install [--no-below]   (root) monitoring + tuning, idempotent:
#       * Docker volumes NOCOW (+C, disposable test data: in-place writes, no
#         CoW fragmentation, no checksums/compression); existing volumes are
#         rewritten once at the next boot, before Docker starts
#       * weekly fstrim: Fedora's own fstrim.timer, only made sure it is enabled
#         (discard=async trims btrfs continuously; this covers /boot and the
#         ESP, and is a safety net for btrfs)
#       * health check (hourly) + weekly scrub, alerting on: missing member or
#         mixed chunk profiles, device error counters, fs forced read-only,
#         kernel btrfs/IO errors, md RAID degraded, NVMe SMART warnings/wear,
#         scrub errors/age, low unallocated space, automatic-reclaim failures,
#         past degraded boots (if the degraded-boot component is installed)
#       * stall recording: btrfs-stall-monitord (always on, read-only) + below
#         (rpm-ostree layered), queried with `report`
#       * automatic block-group reclaim (dynamic + periodic, data)
#       * writeback limits vm.dirty_bytes 2 GiB / background 512 MiB
#       * desktop alerts (btrfs-notify) for every component; one raised while
#         no graphical session can show it (at boot, nobody logged in) is kept
#         in /var/lib/cst-alerts/pending (7 days) and shown at the next login
#         (cst-pending-alerts, a global systemd user unit)
#       * ~/.local/bin/cst -> the cst command of this folder (for your user,
#         if nothing else is there), so the hints of the alerts work as typed
#       * moves what versions named f44-tools left (/var/lib/f44-alerts and
#         its pending alerts, f44-pending-alerts and its user unit, below's
#         10-f44-tools.conf) to the new names
#     It never touches the initramfs or the boot path.
#   status                 (root) everything at a glance: services, last check,
#                          scrub, space and reclaim, mount-option coherence,
#                          degraded-boot summary, alerts raised while nobody
#                          was logged in (and whether you saw them),
#                          last hour of stalls
#   uninstall              (root) remove what `install` installed (keeps the
#                          recorded data and the layered below package)
#   test-alert             (root) send a test desktop notification
#   degraded-boot [install|status|files-only|uninstall]
#                          (root) unattended degraded boot when one NVMe is
#                          gone: udev rule + units in the initramfs. Separate
#                          on purpose: it changes the boot path.
#   crypt-perf [status|enable|disable]
#                          (root, asks the LUKS passphrase of each member)
#                          dm-crypt no_read_workqueue/no_write_workqueue on
#                          the btrfs members: decrypt/encrypt inline instead
#                          of through kcryptd queues (measured here: dm-crypt
#                          adds ~0.5 ms over the NVMe await). Applied live and
#                          stored in the LUKS2 header (--persistent), so it
#                          also holds in the initramfs; no crypttab change.
#   snapshots setup [--yes]  (root) one command, run once, no dedicated reboot:
#                          shows the plan and asks once; then fills ~/Bulk
#                          (see `bulk`), turns caches and user Flatpak installs
#                          into nested subvolumes left out of /home snapshots,
#                          and ~/Workspace into one snapshotted on its own
#                          (busy ones: at the next normal boot, before login),
#                          deletes old cargo target/ dirs, sets cargo build-dir
#                          + uv clone mode and turns on the hourly pruning of
#                          cargo's build folders (both through `cst toolchains`;
#                          the pruning stays off after `cst toolchains auto
#                          off`), puts Chrome's (Flatpak) HTTP cache
#                          in its sandbox /tmp (RAM, at most 512 MiB; flags in
#                          chrome-flags.conf, written as you) and starts
#                          hourly read-only snapshots right away, or after
#                          the next boot when busy
#                          directories wait for it (btrfs-snapshot-core, in
#                          this repo: no package to layer; /home 72h 7d 4w 3m,
#                          ~/Workspace 48h 7d; space guard: none at <=250 GiB
#                          free, emergency purge at <=150 GiB).
#   snapshots status | list [--json] | prune [--confirm] | clean-targets [--apply]
#             | disable [--purge]
#                          list: every snapshot with its keep/delete reason;
#                          prune: the retention by hand, also what the circuit
#                          breaker held back (e.g. after a shorter retention in
#                          snapshots.conf); dry run unless --confirm;
#                          --purge deletes only the snapshots the core owns.
#   snapshots nas setup --path DIR [--host H] [--port P] [--user U]
#                          optional daily backup to the NAS with restic over
#                          SFTP (port 2204 by default): layers restic
#                          (rpm-ostree), prints the key to put on the NAS;
#                          run it again to test, create the repository with a
#                          new random password in /etc/btrfs-setup/restic-password
#                          (keep a copy!) and enable it. A repository already
#                          there needs its saved password in that file first.
#   snapshots nas status | disable | run
#   snapshots chrome-cache [status|apply|off]
#                          Chrome's flags only (also part of setup and status);
#                          off removes the two flag lines and leaves a comment
#                          that keeps setup from adding them again.
#   bulk plan | apply | status | keep ID... | unkeep ID...
#                          (root) ~/Bulk: one folder for everything that can be
#                          downloaded again (IDE builds, ~/Programs, Android SDK
#                          and emulator disks, LM Studio models, ISOs, a Steam
#                          library, toolchain caches): its own subvolume with a
#                          CACHEDIR.TAG, so no snapshots and no NAS backup. Old
#                          paths become symlinks into it (moved by reflink, the
#                          emulator disks copied into ~/Bulk/emulators, the
#                          only NOCOW part; SDK and toolchains in
#                          ~/Bulk/toolchains, copy-on-write); idempotent;
#                          busy items move at the next boot, before login; odd
#                          states are reported, never touched. plan = dry run.
#                          keep ID = leave that item in /home on purpose (listed
#                          in /etc/btrfs-setup/bulk-keep; unkeep undoes it).
#                          Also the first step of `snapshots setup`.
#   bulk steam plan | apply [--app ID]... [--copy]
#                          (root) with Steam closed, move the installed games of
#                          the Flatpak's main Steam library into ~/Bulk/steam
#                          (reflink, checked; NOCOW game data cannot be
#                          reflinked: with --copy it is copied byte by byte,
#                          at idle priority, and compared; tools such as the Steam Linux
#                          Runtime stay unless named with --app); registers
#                          that library in both libraryfolders.vdf (backed up
#                          first). Refuses anything unexpected; exit 5 while
#                          Steam runs. Make it the default library by hand.
#   docker-gc enable       (root) weekly, conservative Docker cleanup: ephemeral
#                          containers (generated name or Testcontainers; never
#                          compose/k3d/named ones) stopped 7+ days, orphaned
#                          anonymous volumes (never named ones), images unused
#                          14+ days (use recorded from docker events, so the
#                          first timed removal is 14 days after enabling) and
#                          build cache unused 14+ days.
#   docker-gc status | run [--dry-run] | disable
#   docker-gc cleanup-now  one-off: lists every unused image not seen in use
#                          and removes it all after one confirmation.
#   report [args]          (user) btrfs-stall-report: was storage the
#                          bottleneck in a time window, and why
#   watch  [args]          (user) run a command and save the report of its
#                          window, labelled (A/B comparisons)
#   help
#
# Without a command it only prints this help and changes nothing; `install`
# must be asked for by name. The old flags --status, --uninstall, --test-alert
# and --no-below (= install --no-below) still work. Root commands must run on
# the host, not inside toolbox; report/watch work in both.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SRC="$(dirname "${SELF}")/btrfs"
UNIT_DIR="/etc/systemd/system"
SYSCTL_FILE="/etc/sysctl.d/90-btrfs-writeback.conf"
RECLAIM_FILE="/etc/tmpfiles.d/btrfs-automatic-reclaim.conf"
BELOW_DROPIN="${UNIT_DIR}/below.service.d/10-cst.conf"
DATA_DIR="/var/log/btrfs-stall-monitor"
DEGRADED="${SRC}/btrfs-degraded-boot-setup.sh"
# Alerts no desktop session could show (written by btrfs-notify, root only),
# and their reader, run as the user at each graphical login.
ALERTS_DIR=/var/lib/cst-alerts
USER_UNIT_DIR=/etc/systemd/user
PENDING_UNIT=cst-pending-alerts.service
PENDING_READER=/usr/local/bin/cst-pending-alerts
# The same parts under the names of versions called f44-tools: install moves
# or removes them, uninstall removes them too.
OLD_ALERTS_DIR=/var/lib/f44-alerts
OLD_PENDING_UNIT=f44-pending-alerts.service
OLD_PENDING_READER=/usr/local/bin/f44-pending-alerts
OLD_BELOW_DROPIN="${UNIT_DIR}/below.service.d/10-f44-tools.conf"
# source (relative to btrfs/) -> destination
declare -A BINARIES=(
    [btrfs-health-check]=/usr/local/sbin/btrfs-health-check
    [btrfs-notify]=/usr/local/sbin/btrfs-notify
    [btrfs-stall-monitord]=/usr/local/sbin/btrfs-stall-monitord
    [btrfs-stall-report]=/usr/local/bin/btrfs-stall-report
    [docker-volumes-nocow]=/usr/local/sbin/docker-volumes-nocow
)
UNITS=(btrfs-health-check.service btrfs-health-check.timer btrfs-scrub-weekly.service
       btrfs-scrub-weekly.timer btrfs-stall-monitor.service btrfs-stall-alert.service
       docker-volumes-nocow.service)
TIMERS=(btrfs-health-check.timer btrfs-scrub-weekly.timer)
# Superseded units from earlier versions; removed on install.
OLD_UNITS=(btrfs-scrub-monthly.service btrfs-scrub-monthly.timer)
# Mount options every btrfs mount of the root fs is expected to carry.
EXPECTED_OPTS=(compress=zstd:1 flushoncommit noatime discard=async degraded space_cache=v2 ssd)

# Shown in the help and in hints: "cst btrfs" when run through the cst
# command (it sets CST_PROG), else the name this script was started with.
PROG="${CST_PROG:-$0}"

usage() {
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    echo "Usage: ${PROG} <command> [args]"
}
in_container() { [[ -e /run/.containerenv || -e /.dockerenv ]]; }

# ---- command line ------------------------------------------------------------
orig_args=("$@")
# No command = help, like cpu-setup.sh: running the bare script to see what it
# does must not install anything.
cmd="${1:-help}"
[[ $# -gt 0 ]] && shift
case "${cmd}" in
    --status) cmd=status ;;
    --uninstall) cmd=uninstall ;;
    --test-alert) cmd=test-alert ;;
    --no-below) cmd=install; set -- --no-below "$@" ;;
    -h|--help) cmd=help ;;
esac

case "${cmd}" in
    help)
        usage
        exit 0
        ;;
    report)
        # The repo copy, so it matches these sources even before reinstalling.
        if in_container; then
            exec flatpak-spawn --host python3 -I "${SRC}/btrfs-stall-report" "$@"
        fi
        exec python3 -I "${SRC}/btrfs-stall-report" "$@"
        ;;
    watch)
        exec bash "${SRC}/btrfs-stall-watch" "$@"
        ;;
    install|status|uninstall|test-alert|degraded-boot|crypt-perf|snapshots|bulk|docker-gc)
        ;;
    *)
        echo "Unknown command: ${cmd}" >&2
        usage >&2
        exit 2
        ;;
esac

# ---- root commands -------------------------------------------------------------
if in_container; then
    echo "'${cmd}' must run on the host, not inside a toolbox/container (needs the host's systemd, rpm-ostree, sudo)." >&2
    exit 1
fi
if [[ ${EUID} -ne 0 ]]; then
    # sudo resets the environment: CST_PROG is handed over explicitly.
    if [[ -n "${CST_PROG:-}" ]]; then
        exec sudo -- env "CST_PROG=${CST_PROG}" "${SELF}" "${orig_args[@]}"
    fi
    exec sudo -- "${SELF}" "${orig_args[@]}"
fi

target_user="${SUDO_USER:-}"
shim=""
if [[ -n "${target_user}" && "${target_user}" != root ]]; then
    shim="$(getent passwd "${target_user}" | cut -d: -f6)/.local/bin/btrfs-stall-report"
fi

root_fs_uuid() {
    findmnt -rn -o UUID --target /sysroot 2>/dev/null | head -n1
}

# install_file SRC DEST MODE [OWNER]: copy only when the content changed.
install_file() {
    local src="$1" dest="$2" mode="$3" owner="${4:-root}"
    if [[ -f "${dest}" ]] && cmp -s "${src}" "${dest}"; then
        echo "unchanged: ${dest}"
        return
    fi
    if [[ "${owner}" == root ]]; then
        install -D -m "${mode}" -o root -g root "${src}" "${dest}"
        restorecon "${dest}" 2>/dev/null || true
    else
        # Into the user's home as the user: no root-owned dirs, no following
        # symlinks with root privileges.
        runuser -u "${owner}" -- install -D -m "${mode}" "${src}" "${dest}"
    fi
    echo "written:   ${dest}"
}

# install_text DEST MODE [OWNER] < content
install_text() {
    local tmp
    tmp="$(mktemp)"
    cat >"${tmp}"
    chmod 0644 "${tmp}"
    install_file "${tmp}" "$@"
    rm -f "${tmp}"
}

# render SRC KEY=VALUE...: SRC with every @KEY@ replaced by VALUE, literally
# (quoted replacement: no escaping of '|', '&' or '\' in values needed).
render() {
    local text kv
    text="$(<"$1")"
    shift
    for kv in "$@"; do
        text="${text//"@${kv%%=*}@"/"${kv#*=}"}"
    done
    printf '%s\n' "${text}"
}

# install_units NAME...: unit files from btrfs/units into UNIT_DIR.
install_units() {
    local u
    for u in "$@"; do
        install_file "${SRC}/units/${u}" "${UNIT_DIR}/${u}" 0644
    done
}

# check_syntax FILE: bash -n or a Python parse, by shebang; fails on errors.
check_syntax() {
    if head -n1 "$1" | grep -q python; then
        python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$1"
    else
        bash -n "$1"
    fi
}

# sysval FILE: its content, or "?" when unreadable.
sysval() { cat "$1" 2>/dev/null || echo '?'; }

# ---- alerts kept for the next login ---------------------------------------------------
# Shared with `snapshots setup` (setup-snapshots.bash), which also alerts
# through btrfs-notify. Callers install the current btrfs-notify first: it
# writes to ALERTS_DIR, the folder the reader installed here reads.
install_pending_alerts() {
    check_syntax "${SRC}/cst-pending-alerts"
    migrate_old_alerts
    install_file "${SRC}/cst-pending-alerts" "${PENDING_READER}" 0755
    install_file "${SRC}/units/user/${PENDING_UNIT}" "${USER_UNIT_DIR}/${PENDING_UNIT}" 0644
    # Written only by root (btrfs-notify); every user may read it.
    install -d -m 0755 -o root -g root "${ALERTS_DIR}" "${ALERTS_DIR}/pending"
    restorecon -R "${ALERTS_DIR}" 2>/dev/null || true
    systemctl --global enable "${PENDING_UNIT}"
    # A user manager already running sees the new unit only after a reload
    # (otherwise from the next login on; no harm either way).
    if [[ -n "${target_user}" && "${target_user}" != root ]]; then
        systemctl --user -M "${target_user}@" daemon-reload 2>/dev/null || true
    fi
}

# remove_old_reader: the reader and user unit of versions named f44-tools.
# disable needs the unit file; an enable link left without it is removed by
# hand. Each user's record (~/.local/state/f44-alerts) is moved by the new
# reader at that user's next login.
remove_old_reader() {
    if [[ -e "${USER_UNIT_DIR}/${OLD_PENDING_UNIT}" ]]; then
        systemctl --global disable "${OLD_PENDING_UNIT}" 2>/dev/null || true
    fi
    rm -fv "${USER_UNIT_DIR}"/*.wants/"${OLD_PENDING_UNIT}" \
        "${USER_UNIT_DIR}/${OLD_PENDING_UNIT}" "${OLD_PENDING_READER}"
}

# migrate_old_alerts: idempotent. Alerts still waiting in the old folder move
# to the new one with their names and times (the users' records of what they
# have seen refer to both), under the old folder's lock, so an old
# btrfs-notify still running cannot write there meanwhile. A file of the same
# name already in the new folder is the newer version of that alert and wins.
# Still under the lock, the old folder is replaced by a link to the new one:
# an old btrfs-notify that was waiting for the lock then writes its alert into
# the new folder, instead of into a folder that is about to be deleted. The
# link is removed by the next install (by then no btrfs-notify from before
# this one can still be running).
migrate_old_alerts() {
    local f
    remove_old_reader
    if [[ -L "${OLD_ALERTS_DIR}" ]]; then
        rm -f "${OLD_ALERTS_DIR}"
        echo "removed:   ${OLD_ALERTS_DIR} (link left by the previous install)"
    fi
    [[ -d "${OLD_ALERTS_DIR}" ]] || return 0
    install -d -m 0755 -o root -g root "${ALERTS_DIR}" "${ALERTS_DIR}/pending"
    if (
        exec 9>"${OLD_ALERTS_DIR}/.lock"
        flock -w 30 9 || true
        rc=0
        for f in "${OLD_ALERTS_DIR}"/pending/*.alert; do
            [[ -f "${f}" && ! -L "${f}" ]] || continue
            [[ -e "${ALERTS_DIR}/pending/${f##*/}" ]] || mv -- "${f}" "${ALERTS_DIR}/pending/" || rc=1
        done
        (( rc == 0 )) || exit 1
        # -T: fails, rather than linking inside, if the folder came back.
        rm -rf "${OLD_ALERTS_DIR}" && ln -sT "${ALERTS_DIR}" "${OLD_ALERTS_DIR}"
    ); then
        echo "moved:     ${OLD_ALERTS_DIR} -> ${ALERTS_DIR} (alerts kept for the next login; the old name is a link until the next install)"
    else
        echo "WARNING: could not finish moving ${OLD_ALERTS_DIR} to ${ALERTS_DIR}; what is left stays there (run install again)" >&2
    fi
    restorecon -R "${ALERTS_DIR}" 2>/dev/null || true
}

remove_pending_alerts() {
    systemctl --global disable "${PENDING_UNIT}" 2>/dev/null || true
    rm -fv "${USER_UNIT_DIR}/${PENDING_UNIT}" "${PENDING_READER}"
    remove_old_reader
    rm -rf "${ALERTS_DIR}" "${OLD_ALERTS_DIR}"
}

# status_pending_alerts: alerts kept for a login, and whether the user who ran
# sudo has seen it. The reader runs as that user: it reads their record in
# their home with their rights, not root's.
status_pending_alerts() {
    [[ -e "${USER_UNIT_DIR}/${PENDING_UNIT}" ]] || echo "  shown at login: NOT installed (${PROG} install)"
    [[ -d "${OLD_ALERTS_DIR}" && ! -L "${OLD_ALERTS_DIR}" ]] &&
        echo "  ${OLD_ALERTS_DIR} (a version named f44-tools) is still there: ${PROG} install moves it"
    if [[ -n "${target_user}" && "${target_user}" != root ]]; then
        runuser -u "${target_user}" -- bash "${SRC}/cst-pending-alerts" --list
    else
        bash "${SRC}/cst-pending-alerts" --list
    fi 2>&1 | sed 's/^/  /'
}

shim_content() {
    cat <<'EOF'
#!/bin/sh
# Installed by btrfs-setup.sh (CST). Lets the report run from the host
# and from inside toolbox/distrobox containers alike.
if [ -e /run/.containerenv ] || [ -e /.dockerenv ]; then
    exec flatpak-spawn --host /usr/local/bin/btrfs-stall-report "$@"
fi
exec /usr/local/bin/btrfs-stall-report "$@"
EOF
}

# ---- status ----------------------------------------------------------------------
section() { printf '\n== %s\n' "$1"; }

status_space() {
    local uuid="$1" mnt=/var alloc kind
    alloc="/sys/fs/btrfs/${uuid}/allocation"
    btrfs device usage -b "${mnt}" 2>/dev/null | awk '
        /, ID:/ {dev = $1; sub(/,$/, "", dev)}
        /Device size:/ {size = $3}
        /^[[:space:]]+Unallocated:/ {printf "  %-12s size %5.0f GiB, unallocated %5.0f GiB (%2.0f%%)\n", dev, size / 2^30, $2 / 2^30, size ? $2 * 100 / size : 0}'
    for kind in data metadata; do
        [[ -r "${alloc}/${kind}/bytes_used" ]] || continue
        awk -v k="${kind}" -v u="$(cat "${alloc}/${kind}/bytes_used")" -v t="$(cat "${alloc}/${kind}/total_bytes")" \
            'BEGIN {printf "  %-9s %7.1f of %7.1f GiB allocated in chunks used (%.0f%%)\n", k, u / 2^30, t / 2^30, t ? u * 100 / t : 0}'
    done
    printf '  automatic reclaim (data): dynamic=%s periodic=%s, reclaimed %s block group(s), errors %s%s\n' \
        "$(sysval "${alloc}/data/dynamic_reclaim")" "$(sysval "${alloc}/data/periodic_reclaim")" \
        "$(sysval "${alloc}/data/reclaim_count")" "$(sysval "${alloc}/data/reclaim_errors")" \
        "$([[ -e "${RECLAIM_FILE}" ]] || echo "  (NOT configured: run install)")"
}

status_mounts() {
    local mnt opts o missing want have
    for mnt in /sysroot /var /var/home; do
        opts="$(findmnt -rn -o OPTIONS -M "${mnt}" 2>/dev/null | head -n1)"
        [[ -n "${opts}" ]] || continue
        missing=()
        for o in "${EXPECTED_OPTS[@]}"; do
            [[ ",${opts}," == *",${o},"* ]] || missing+=("${o}")
        done
        if (( ${#missing[@]} )); then
            printf '  %-10s MISSING: %s\n' "${mnt}" "${missing[*]}"
        else
            printf '  %-10s ok (%s)\n' "${mnt}" "${EXPECTED_OPTS[*]}"
        fi
    done
    for o in dirty_bytes dirty_background_bytes; do
        want="$(awk -v k="vm.${o}" '$1 == k {print $3}' "${SYSCTL_FILE}" 2>/dev/null)"
        have="$(cat "/proc/sys/vm/${o}")"
        if [[ -z "${want}" ]]; then
            printf '  vm.%-22s %s (not managed: run install)\n' "${o}" "${have}"
        elif [[ "${want}" == "${have}" ]]; then
            printf '  vm.%-22s %s ok\n' "${o}" "${have}"
        else
            printf '  vm.%-22s %s DRIFT (expected %s; run install)\n' "${o}" "${have}" "${want}"
        fi
    done
}

status() (
    # A read-only overview must keep going on a partial or fresh setup:
    # empty greps, missing files and unmounted paths are normal here.
    set +e +o pipefail
    local uuid u data
    uuid="$(root_fs_uuid)"
    echo "kernel $(uname -r), root btrfs ${uuid:-?}"
    section "services and timers"
    systemctl --no-pager list-timers "${TIMERS[@]}" fstrim.timer 2>/dev/null | sed -n '1,4p' | sed 's/^/  /'
    for u in btrfs-stall-monitor.service below.service; do
        printf '  %-28s %s\n' "${u}" "$(systemctl is-active "${u}" 2>/dev/null || true)"
    done
    section "last health check"
    journalctl -u btrfs-health-check.service -o cat --no-pager 2>/dev/null |
        grep -E 'btrfs-health-check:' | tail -n 3 | sed 's/^/  /'
    section "last scrub"
    btrfs scrub status /var 2>/dev/null | grep -E 'Scrub started|Status|Duration|Error summary' | sed 's/^/  /'
    section "last fstrim (Fedora's fstrim.timer)"
    journalctl -u fstrim.service -o short-iso --no-pager -n 20 2>/dev/null |
        grep -E 'trimmed|Failed|failed' | tail -n 4 | sed 's/^/  /'
    section "docker volumes (NOCOW)"
    python3 -I "${SRC}/docker-volumes-nocow" status 2>&1 | sed 's/^/  /'
    section "docker-gc"
    if systemctl -q is-enabled docker-gc.timer 2>/dev/null; then
        journalctl -u docker-gc.service -o cat --no-pager 2>/dev/null | grep -E '^<[0-9]>docker-gc: removed|docker-gc: removed' |
            tail -n 1 | sed 's/^<[0-9]>//; s/^/  last: /'
        echo "  details: ${PROG} docker-gc status"
    else
        echo "  off (${PROG} docker-gc enable)"
    fi
    section "space and automatic reclaim"
    [[ -n "${uuid}" ]] && status_space "${uuid}"
    section "mount options and writeback limits"
    status_mounts
    section "degraded boot"
    if [[ -x "${DEGRADED}" ]]; then
        "${DEGRADED}" --status 2>/dev/null | sed -n '/==== summary/,$p' | sed '1d; s/^/  /'
    fi
    section "alerts raised while nobody was logged in (last 7 days)"
    status_pending_alerts
    section "snapshots"
    (
        # shellcheck source=btrfs/setup-snapshots.bash
        . "${SRC}/setup-snapshots.bash"
        snap_status 2>&1 | sed 's/^/  /'
    )
    section "Bulk (re-downloadable data: no snapshots, no NAS backup)"
    if [[ -n "${target_user}" && "${target_user}" != root ]]; then
        (
            # shellcheck source=btrfs/setup-snapshots.bash
            . "${SRC}/setup-snapshots.bash"
            bulk_status 2>&1 | sed 's/^/  /'
        )
    else
        echo "  run through sudo from your user account to see your ~/Bulk"
    fi
    section "stalls, last hour (report --since '1 hour ago' --no-below)"
    python3 -I "${SRC}/btrfs-stall-report" --since '1 hour ago' --no-below 2>&1 | grep -E '^(recorded|verdict)' |
        sed 's/^/  /'
    data="$(du -sh "${DATA_DIR}" 2>/dev/null | cut -f1)"
    echo
    echo "data: stall log ${data:-?}, below store $(du -sh /var/log/below/store 2>/dev/null | cut -f1)"
    echo "features: $(ls "/sys/fs/btrfs/${uuid}/features" 2>/dev/null | tr '\n' ' ')"
)

# ---- crypt-perf --------------------------------------------------------------------
# dm-crypt devices under the root btrfs: "name dm-N backing-device"
crypt_members() {
    local uuid dev name
    uuid="$(root_fs_uuid)"
    for dev in /sys/fs/btrfs/"${uuid}"/devices/*; do
        dev="$(basename "${dev}")"
        [[ -r "/sys/block/${dev}/dm/uuid" ]] && grep -q '^CRYPT-' "/sys/block/${dev}/dm/uuid" || continue
        name="$(cat "/sys/block/${dev}/dm/name")"
        echo "${name} ${dev} /dev/$(basename "$(ls -d /sys/block/"${dev}"/slaves/* | head -n1)")"
    done
}

# Active dm-crypt optional flags of a mapping, e.g. "allow_discards no_read_workqueue"
crypt_active_flags() {
    dmsetup table "$1" | awk '{for (i = 1; i <= NF; i++) if ($i ~ /^(allow_discards|same_cpu_crypt|submit_from_crypt_cpus|no_read_workqueue|no_write_workqueue|high_priority)$/) print $i}'
}

crypt_header_flags() {
    cryptsetup luksDump "$1" 2>/dev/null | awk -F: '/^Flags:/ {gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2}'
}

crypt_perf() {
    local action="${1:-status}" name dev backing flags opts f uuid ndev members n=0 ok=0
    case "${action}" in status|enable|disable) ;; *) echo "crypt-perf: status|enable|disable" >&2; exit 2 ;; esac
    [[ $# -le 1 ]] || { echo "crypt-perf: unexpected argument '$2'" >&2; exit 2; }
    uuid="$(root_fs_uuid)"
    [[ -n "${uuid}" ]] || { echo "crypt-perf: root btrfs not found" >&2; exit 1; }
    members="$(crypt_members)"
    ndev="$(ls "/sys/fs/btrfs/${uuid}/devices" | wc -l)"
    if [[ -z "${members}" ]]; then
        echo "crypt-perf: no dm-crypt member under the root btrfs" >&2
        exit 1
    fi
    if [[ "${action}" != status ]]; then
        if [[ "$(wc -l <<<"${members}")" -ne "${ndev}" ]] || [[ -n "$(cat "/sys/fs/btrfs/${uuid}/devinfo/"*/missing 2>/dev/null | grep -v '^0$')" ]]; then
            echo "crypt-perf: not every btrfs member is present and encrypted (degraded?): refusing, so no member is left without the flags" >&2
            exit 1
        fi
        [[ -t 0 ]] || { echo "crypt-perf: needs a terminal for the LUKS passphrases" >&2; exit 1; }
        echo "Each disk asks for its LUKS passphrase. Applying the new table suspends the device for an instant;"
        echo "btrfs may pause writers briefly (pending data is synced first). Best done while the machine is idle."
        echo
    fi
    # The member list is read from fd 3: cryptsetup must get the terminal on stdin.
    while read -r -u3 name dev backing; do
        n=$((n + 1))
        flags="$(crypt_active_flags "${name}" | xargs)"
        printf '%s (%s on %s)\n  active:     %s\n  persistent: %s\n' "${name}" "${dev}" "${backing}" \
            "${flags:-none}" "$(crypt_header_flags "${backing}")"
        [[ "${action}" == status ]] && continue
        # refresh resets every flag it is not given: carry the other ones over.
        opts=()
        for f in ${flags}; do
            case "${f}" in
                allow_discards) opts+=(--allow-discards) ;;
                same_cpu_crypt) opts+=(--perf-same_cpu_crypt) ;;
                submit_from_crypt_cpus) opts+=(--perf-submit_from_crypt_cpus) ;;
                high_priority) opts+=(--perf-high_priority) ;;
            esac
        done
        if [[ "${action}" == enable ]]; then
            opts+=(--perf-no_read_workqueue --perf-no_write_workqueue)
        fi
        sync
        echo "  -> cryptsetup refresh ${opts[*]} --persistent ${name}"
        if cryptsetup refresh "${opts[@]}" --persistent "${name}" </dev/tty; then
            ok=$((ok + 1))
            printf '  now active:     %s\n  now persistent: %s\n' "$(crypt_active_flags "${name}" | xargs)" \
                "$(crypt_header_flags "${backing}")"
        else
            echo "  FAILED on ${name}: unchanged (a wrong passphrase leaves the device as it was)" >&2
        fi
    done 3<<<"${members}"
    if [[ "${action}" != status ]]; then
        echo
        echo "Applied to ${ok} of ${n} member(s)."
        (( ok == n )) || { echo "Re-run 'crypt-perf ${action}' to finish (it is idempotent)." >&2; exit 1; }
        echo "Compare dm-crypt vs NVMe await on a similar workload before/after: btrfs-stall-report --since '<time>' -v"
    fi
}

# ---- uninstall -------------------------------------------------------------------
# ---- docker-gc: weekly, conservative Docker cleanup (explicit opt-in) ---------------------
DOCKER_GC_UNITS=(docker-gc-recorder.service docker-gc.service docker-gc.timer)
docker_gc() {
    local action="${1:-status}" bin=/usr/local/sbin/docker-gc
    [[ $# -gt 0 ]] && shift
    case "${action}" in
        enable)
            check_syntax "${SRC}/docker-gc" || { echo "${SRC}/docker-gc: syntax error" >&2; exit 1; }
            install_file "${SRC}/docker-gc" "${bin}" 0755
            install_units "${DOCKER_GC_UNITS[@]}"
            systemctl daemon-reload
            systemctl enable docker-gc-recorder.service docker-gc.timer
            systemctl start docker-gc.timer
            if systemctl -q is-active docker.service; then
                systemctl restart docker-gc-recorder.service
            fi
            echo "docker-gc enabled: image use is recorded from now on; weekly runs (Monday ~11:00)."
            echo "Timed image removal starts 14 days from now. Preview: ${PROG} docker-gc status"
            echo "To clear the current backlog once (asks first): ${PROG} docker-gc cleanup-now"
            ;;
        disable)
            systemctl disable --now "${DOCKER_GC_UNITS[@]}" 2>/dev/null || true
            echo "docker-gc disabled (recorded state kept in /var/lib/docker-gc)."
            ;;
        status)
            printf 'timer %s, recorder %s\n' "$(systemctl is-enabled docker-gc.timer 2>/dev/null || echo off)" \
                "$(systemctl is-active docker-gc-recorder.service 2>/dev/null || true)"
            python3 -I "${SRC}/docker-gc" status
            ;;
        run)
            if [[ "${1:-}" == --dry-run ]]; then
                python3 -I "${SRC}/docker-gc" run --dry-run
            else
                [[ -x "${bin}" ]] || { echo "Enable first: ${PROG} docker-gc enable" >&2; exit 1; }
                systemctl start docker-gc.service
                journalctl -u docker-gc.service -n 40 -o cat --no-pager
            fi
            ;;
        cleanup-now)
            [[ -t 0 ]] || { echo "cleanup-now asks for confirmation: run it in a terminal." >&2; exit 1; }
            python3 -I "${SRC}/docker-gc" cleanup-now
            ;;
        *) echo "docker-gc: enable|status|run [--dry-run]|cleanup-now|disable" >&2; exit 2 ;;
    esac
}

uninstall() {
    local unit
    # One by one: a missing unit must not stop the others from being stopped.
    for unit in "${TIMERS[@]}" btrfs-stall-monitor.service; do
        systemctl disable --now "${unit}" 2>/dev/null || true
    done
    systemctl stop btrfs-stall-alert.service 2>/dev/null || true
    # New Docker volumes copy-on-write again; existing NOCOW files stay as they are.
    systemctl disable docker-volumes-nocow.service 2>/dev/null || true
    [[ -x /usr/local/sbin/docker-volumes-nocow ]] && /usr/local/sbin/docker-volumes-nocow unmark
    rm -f /etc/btrfs-setup/docker-nocow-pending
    systemctl reset-failed "${UNITS[@]}" 2>/dev/null || true
    # This script enabled below's recorder; the package itself stays layered.
    systemctl disable --now below.service 2>/dev/null || true
    for unit in "${UNITS[@]}" "${OLD_UNITS[@]}"; do
        rm -fv "${UNIT_DIR}/${unit}"
    done
    for dest in "${BINARIES[@]}"; do
        rm -fv "${dest}"
    done
    # The snapshot component (left alone here) still alerts through btrfs-notify,
    # and its alerts at boot still need the login-time reader (brought to the
    # current names, so that reader and btrfs-notify use the same folder).
    if [[ -x /usr/local/sbin/btrfs-snapshot ]]; then
        install -m 0755 "${SRC}/btrfs-notify" /usr/local/sbin/btrfs-notify
        install_pending_alerts
        echo "kept:      /usr/local/sbin/btrfs-notify, ${PENDING_READER}, ${USER_UNIT_DIR}/${PENDING_UNIT}"
        echo "           and ${ALERTS_DIR} (used by the snapshot component)"
    else
        remove_pending_alerts
    fi
    rm -fv "${BELOW_DROPIN}" "${OLD_BELOW_DROPIN}"
    if grep -qs 'Managed by btrfs-setup.sh' "${RECLAIM_FILE}"; then
        rm -fv "${RECLAIM_FILE}"
        echo "Automatic reclaim stays enabled in sysfs until the next reboot."
    elif [[ -e "${RECLAIM_FILE}" ]]; then
        echo "Kept ${RECLAIM_FILE}: not created by this script."
    fi
    rmdir "$(dirname "${BELOW_DROPIN}")" 2>/dev/null || true
    rm -rf /var/lib/btrfs-health-check
    if [[ -n "${shim}" ]] && grep -q 'btrfs-setup.sh' "${shim}" 2>/dev/null; then
        rm -fv "${shim}"
    fi
    systemctl daemon-reload
    if [[ -e "${SYSCTL_FILE}" ]]; then
        rm -fv "${SYSCTL_FILE}"
        # Restore the tuned profile values (dirty_ratio) at runtime.
        systemctl try-restart tuned.service || true
    fi
    echo "Not touched: snapshots and the NAS backup ('${PROG} snapshots disable [--purge]' stops them / deletes"
    echo "             the local snapshots; the restic repository on the NAS, its password in"
    echo "             /etc/btrfs-setup/restic-password and the layered 'restic' package stay)."
    echo "Not touched: docker-gc ('${PROG} docker-gc disable' stops it)."
    echo "Not touched: toolchain caches and settings (the toolchains module; 'cst toolchains auto off' stops the pruning)."
    echo "Not touched: ~/Bulk and its symlinks (plain folders and links; see btrfs/README.md to undo one)."
    echo "Not touched: the degraded-boot component ('${PROG} degraded-boot uninstall' removes it)."
    echo "Kept: ${DATA_DIR} and /var/log/below (recorded data), the layered 'below' package."
    echo "Kept: ~/.local/bin/cst (the cst command; the other modules use it too)."
    echo "To remove those too: sudo rm -rf ${DATA_DIR} /var/log/below; sudo rpm-ostree uninstall below"
}

# ---- install ---------------------------------------------------------------------
install_all() {
    local with_below=1 f u uuid arg below_pending staged_has_below
    for arg in "$@"; do
        case "${arg}" in
            --no-below) with_below=0 ;;
            *) echo "install: unknown argument '${arg}' (only --no-below)" >&2; exit 2 ;;
        esac
    done

    # Preflight: every component must at least parse.
    for f in "${!BINARIES[@]}" btrfs-stall-watch cst-pending-alerts; do
        check_syntax "${SRC}/${f}"
    done
    for u in "${UNITS[@]}" "user/${PENDING_UNIT}"; do
        [[ -r "${SRC}/units/${u}" ]] || { echo "missing ${SRC}/units/${u}" >&2; exit 1; }
    done
    uuid="$(root_fs_uuid)"
    [[ "${uuid}" =~ ^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$ ]] ||
        { echo "unexpected root fs UUID '${uuid}'" >&2; exit 1; }

    # Files.
    for f in "${!BINARIES[@]}"; do
        install_file "${SRC}/${f}" "${BINARIES[${f}]}" 0755
    done
    install_units "${UNITS[@]}"
    install_pending_alerts
    install_file "${SRC}/sysctl/90-btrfs-writeback.conf" "${SYSCTL_FILE}" 0644
    # Always re-load: cheap, and corrects runtime drift (e.g. a tuned profile switch).
    sysctl -q -p "${SYSCTL_FILE}"
    render "${SRC}/tmpfiles/btrfs-automatic-reclaim.conf.in" UUID="${uuid}" | install_text "${RECLAIM_FILE}" 0644
    systemd-tmpfiles --create "${RECLAIM_FILE}"
    if [[ -n "${shim}" ]]; then
        shim_content | install_text "${shim}" 0755 "${target_user}"
    fi
    # shellcheck source=cst-link.bash
    source "$(dirname "${SELF}")/cst-link.bash"
    link_cst_command "${target_user}"
    for u in "${OLD_UNITS[@]}"; do
        if [[ -e "${UNIT_DIR}/${u}" ]]; then
            systemctl disable --now "${u}" 2>/dev/null || true
            rm -fv "${UNIT_DIR}/${u}"
        fi
    done

    # below.
    below_pending=0
    if (( with_below )); then
        if ! rpm -q below >/dev/null 2>&1; then
            staged_has_below="$(rpm-ostree status --json 2>/dev/null | python3 -I -c 'import json, sys
d = [x for x in json.load(sys.stdin)["deployments"] if x.get("staged")]
print("yes" if d and "below" in d[0].get("requested-packages", []) else "no")' 2>/dev/null || echo no)"
            if [[ "${staged_has_below}" != yes ]]; then
                echo "Layering 'below' (rpm-ostree install --apply-live)..."
                rpm-ostree install --idempotent --apply-live below || true
            fi
            rpm -q below >/dev/null 2>&1 || below_pending=1
        fi
        install_file "${SRC}/units/below.service.d/10-cst.conf" "${BELOW_DROPIN}" 0644
    fi
    # The drop-in under its name from versions called f44-tools: renamed (kept
    # with --no-below, as before), or removed once the new one is there.
    if [[ -e "${OLD_BELOW_DROPIN}" ]]; then
        if [[ -e "${BELOW_DROPIN}" ]]; then
            rm -fv "${OLD_BELOW_DROPIN}"
        else
            mv -v "${OLD_BELOW_DROPIN}" "${BELOW_DROPIN}"
        fi
    fi

    # Services.
    systemctl daemon-reload
    systemctl enable --now "${TIMERS[@]}"
    # Fedora's unit, enabled by default; only made sure of (left alone by uninstall).
    systemctl enable --now fstrim.timer
    # Docker volumes NOCOW: new ones at once; existing ones rewritten at the
    # next boot, before Docker starts (no container can be writing then).
    systemctl enable docker-volumes-nocow.service
    if ! /usr/local/sbin/docker-volumes-nocow mark; then
        install -d -m 0755 /etc/btrfs-setup
        touch /etc/btrfs-setup/docker-nocow-pending   # (mark resets the attempt counter)
    fi
    systemctl enable btrfs-stall-monitor.service
    systemctl restart btrfs-stall-monitor.service
    if (( with_below )) && rpm -q below >/dev/null 2>&1; then
        systemctl enable below.service
        systemctl restart below.service
    fi
    # Run the health check once now, so a broken install shows up immediately.
    systemctl start btrfs-health-check.service || true
    sleep 2

    echo
    echo "health check: $(journalctl -u btrfs-health-check.service -o cat --no-pager 2>/dev/null |
        grep 'btrfs-health-check:' | tail -n 1)"
    if systemctl is-active -q btrfs-stall-monitor.service; then
        echo "stall monitor: running ($(journalctl -u btrfs-stall-monitor.service -n 20 -o cat --no-pager |
            grep -m1 -o 'psi_trigger=[a-z]*, kmsg=[a-z]*, stacks=[a-z]*' || echo 'starting'))"
    else
        echo "ERROR: btrfs-stall-monitor.service is not running; see journalctl -u btrfs-stall-monitor" >&2
        exit 1
    fi
    echo "automatic reclaim: dynamic=$(sysval "/sys/fs/btrfs/${uuid}/allocation/data/dynamic_reclaim")," \
        "periodic=$(sysval "/sys/fs/btrfs/${uuid}/allocation/data/periodic_reclaim")"
    if rpm -q below >/dev/null 2>&1; then
        echo "below: $(systemctl is-active below.service 2>/dev/null || true)"
    elif (( below_pending )); then
        echo "below: staged for the next boot (cannot be applied live while another deployment is staged)."
        echo "       Reboot, then re-run this script to enable it."
    fi
    echo
    echo "Overview:            ${PROG} status"
    echo "Stalls (as ${target_user:-your user}, also in toolbox): btrfs-stall-report --since '5 min ago'"
    echo "Notification test:   ${PROG} test-alert"
}

case "${cmd}" in
    install) install_all "$@" ;;
    status) status ;;
    uninstall) uninstall ;;
    test-alert)
        [[ -x /usr/local/sbin/btrfs-notify ]] || { echo "Install first." >&2; exit 1; }
        # Without a desktop session (e.g. over ssh) it is kept and shown at the next login.
        exec /usr/local/sbin/btrfs-notify -a btrfs-setup "Disk health: test alert" \
            "Notifications from btrfs-health-check and btrfs-stall-monitor are working."
        ;;
    crypt-perf) crypt_perf "$@" ;;
    docker-gc) docker_gc "$@" ;;
    snapshots)
        # shellcheck source=btrfs/setup-snapshots.bash
        . "${SRC}/setup-snapshots.bash"
        snapshots_cmd "$@"
        ;;
    bulk)
        # shellcheck source=btrfs/setup-snapshots.bash
        . "${SRC}/setup-snapshots.bash"
        bulk_cmd "$@"
        ;;
    degraded-boot)
        case "${1:-install}" in
            install) shift $(( $# > 0 ? 1 : 0 )); exec "${DEGRADED}" "$@" ;;
            status|uninstall|files-only) exec "${DEGRADED}" "--$1" ;;
            *) exec "${DEGRADED}" "$@" ;;
        esac
        ;;
esac
