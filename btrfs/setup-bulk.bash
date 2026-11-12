# shellcheck shell=bash
#
# setup-bulk.bash: `cst btrfs bulk ...` and the ~/Bulk step of
# `snapshots setup` (sourced by setup-snapshots.bash; runs as root; uses
# snap_home/snap_install_runtime from there and the frontend's install_text,
# SRC, PROG and target_user). The work itself is btrfs/btrfs-bulk (Python): every
# change in the home is made by a child process running as the user.

BULK_TOOL="${SRC}/btrfs-bulk"
BULK_CONF=/etc/btrfs-setup/bulk.conf
BULK_FLAG=/etc/btrfs-setup/bulk-at-boot
BULK_UNIT=btrfs-bulk-boot.service

# The repo copy (like docker-gc): matches these sources before reinstalling.
bulk_run() {   # plan|apply|status | keep|unkeep ID... | steam plan|apply [--app ID]... [--copy]
    # shellcheck disable=SC2154  # target_user: set by btrfs-setup.sh
    python3 -I "${BULK_TOOL}" "$@" --user "${target_user}"
}

# Before any run that can leave a journal (bulk apply, bulk steam apply):
# install the current btrfs-bulk as /usr/local/sbin/btrfs-bulk (the boot
# step runs that copy, and only a copy as new as the journal can finish or
# undo an interrupted move), remember the user, enable the boot unit.
bulk_prepare() {
    snap_home >/dev/null   # refuses without SUDO_USER
    snap_install_runtime
    printf '# Managed by btrfs-setup.sh (CST): whose ~/Bulk btrfs-bulk-boot.service handles.\nBULK_USER=%s\n' \
        "${target_user}" | install_text "${BULK_CONF}" 0644
    # Gated by the flag file or a journal: does nothing at boot otherwise.
    systemctl enable "${BULK_UNIT}" >/dev/null 2>&1 || true
}

# Install what the boot step needs, remember the user, then apply.
# Returns btrfs-bulk's status: 0 all in place (kept items count as in
# place), 1 something needs a look, 4 only something inside ~/Bulk needs a
# look (no data of it in /home), 3 only busy items wait for the next boot.
bulk_apply() {
    local rc=0
    bulk_prepare
    bulk_run apply || rc=$?
    return "${rc}"
}

# `bulk steam plan|apply ...`: apply first installs the runtime (bulk_prepare),
# so that the boot step can recover an interrupted game move.
bulk_steam() {
    local rc=0
    if [[ "${1:-}" == apply ]]; then bulk_prepare; else snap_home >/dev/null; fi
    bulk_run steam "$@" || rc=$?
    return "${rc}"
}

# What `snapshots setup` prints when ~/Bulk is not complete. The first
# snapshot would keep every byte still at the old paths for months.
bulk_warn() {   # btrfs-bulk's status
    echo
    echo "WARNING: some ~/Bulk items are not in place yet (see the lines above)."
    if [[ -e "${BULK_FLAG}" ]]; then
        echo "  Busy ones are moved at the next boot, before login; snapshots wait for that"
        echo "  (no snapshot keeps their data). To do it without a reboot: close the programs"
        echo "  that use them (IDE, emulator, Steam, LM Studio...) and run:"
        echo "    ${PROG} bulk apply"
    fi
    case "${1:-1}" in
        3) return 0 ;;
        4)
            echo "  An item marked NEEDS A LOOK is inside ~/Bulk only (no data of it in /home):"
            echo "  snapshots do not wait for it. Fix it as '${PROG} bulk status' says."
            return 0 ;;
    esac
    echo "  Items marked NEEDS A LOOK are not handled automatically: once snapshots run,"
    echo "  their data would stay in /home snapshots for months. Fix each one as"
    echo "  '${PROG} bulk status' says (move the data, or remove the odd"
    echo "  symlink/file), then run '${PROG} bulk apply' again. To keep an"
    echo "  item in /home on purpose: '${PROG} bulk keep ID' (no warning then)."
}

bulk_status() {
    bulk_run status
    [[ -e "${BULK_FLAG}" ]] &&
        echo "  items waiting for the next boot: $(tr '\n' ' ' <"${BULK_FLAG}")(btrfs-bulk-boot.service)"
    return 0
}

bulk_cmd() {
    local action="${1:-status}" rc=0
    [[ $# -gt 0 ]] && shift
    case "${action}" in
        plan) snap_home >/dev/null; bulk_run plan; echo; echo "Dry run, nothing changed. Do it with: ${PROG} bulk apply" ;;
        apply)
            bulk_apply || rc=$?
            echo
            bulk_status
            return "${rc}" ;;
        status) snap_home >/dev/null; bulk_status ;;
        # Kept in /home by choice (one id per line in /etc/btrfs-setup/bulk-keep).
        keep|unkeep) snap_home >/dev/null; bulk_run "${action}" "$@" || rc=$?; return "${rc}" ;;
        # Steam closed: games from the main Steam library into ~/Bulk/steam.
        # 0 in place, 1 needs a look, 2 usage, 5 Steam is running (nothing done).
        steam) bulk_steam "$@" || rc=$?; return "${rc}" ;;
        *) echo "bulk: plan|apply|status|keep ID...|unkeep ID...|steam plan|apply [--app ID]... [--copy]" >&2; exit 2 ;;
    esac
}
