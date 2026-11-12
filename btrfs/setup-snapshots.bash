# shellcheck shell=bash
#
# setup-snapshots.bash: `cst btrfs snapshots ...` (sourced by the frontend,
# runs as root; uses its helpers install_file/install_text/install_units/render/
# check_syntax/root_fs_uuid/install_pending_alerts and
# variables SRC, UNIT_DIR, PROG, target_user). Nothing here runs unless one of these
# explicit actions is called.

SNAP_SRC="${SRC}/snapshots"
SNAP_CONF=/etc/btrfs-setup/snapshots.conf
SNAP_PLAN=/etc/btrfs-setup/convert-plan
SNAP_STATE=/var/lib/btrfs-snapshot
SNAP_TOP=/var/mnt/btrfs-top
SNAP_DIR_NAME=snapshots
SNAP_SSH_CONFIG=/root/.ssh/config
SNAP_NAS_KEY=/etc/btrfs-setup/ssh/id_ed25519
SNAP_SSH_ALIAS=btrfs-setup-nas          # Host block restic's ssh uses (same name in btrfs-snapshot)
SNAP_RESTIC_PW=/etc/btrfs-setup/restic-password
SNAP_RUNTIME=/usr/local/sbin/btrfs-snapshot
# One snapshot per hour when the subvolume changed. /home: every hour 3 days,
# a day 1 week, a week 1 month, a month 3 months. ~/Workspace (its own
# subvolume): every hour 2 days, a day 1 week. btrfs-snapshot has the values.
SNAP_PRESERVE_TEXT="/home 72h 7d 4w 3m, ~/Workspace 48h 7d"
SNAP_UNITS=(btrfs-snapshot.service btrfs-snapshot.timer btrfs-snapshot-nas.service
            btrfs-snapshot-nas.timer btrfs-subvol-convert.service btrfs-bulk-boot.service)
SNAP_BOOT_FLAG=/etc/btrfs-setup/convert-at-boot
# Trees under $HOME made nested subvolumes: left out of /home's snapshots
# (big and rewritten often: caches, user Flatpak installs), or snapshotted on
# their own (~/Workspace, shorter retention). Everything else that can be
# downloaded again (IDE builds, SDKs, emulator disks, models, ISOs, Steam
# library, toolchain caches) lives in ~/Bulk, one more nested subvolume that
# btrfs-bulk fills (setup-bulk.bash): the old paths become symlinks into it.
# Personal data (game saves, LM Studio chats, toolbox containers) stays in /home.
#   copy   cloned with reflinks (no data written), then the names are switched;
#          absent but its parent there: created as an empty subvolume
#   aside  pure cache moved out of its parent before the parent is cloned,
#          then deleted (~/.cache/uv: 157 GiB that uv re-downloads)
# Chrome's Flatpak cache (~/.var/app/com.google.Chrome/cache) is not one of
# them any more: its HTTP cache goes to the sandbox's /tmp (RAM, 512 MiB cap)
# through chrome-flags.conf (btrfs/chrome-cache, run as you by setup); the
# rest (shader and font caches, and Chrome's Code Cache unless the flag moves
# it too) stays in /home snapshots. A subvolume made there by an earlier
# setup is left as it is (status points it out).
SNAP_EXCLUDE=(
    "aside .cache/uv"
    "copy .cache"
    # Whole dir: ostree hardlinks between repo/ and the checkouts.
    "copy .local/share/flatpak"
    "copy Workspace"
    # Normally created by btrfs-bulk (as you) just before; a plain ~/Bulk
    # folder is converted like the others.
    "copy Bulk"
)

# shellcheck source=btrfs/setup-bulk.bash
. "${SRC}/setup-bulk.bash"

CHROME_TOOL="${SRC}/chrome-cache"
# The toolchains module (toolchains-setup.sh): its program owns the cargo and
# uv settings and the pruning of toolchain caches; setup only calls it.
TOOLCHAINS_TOOL="$(dirname "${SRC}")/toolchains/cst-toolchains"

# How hints name the toolchains module: "cst toolchains" when this runs as
# "cst btrfs", else the module script's path.
snap_toolchains_prog() {
    case "${PROG}" in
        *" btrfs") echo "${PROG% btrfs} toolchains" ;;
        *) echo "$(dirname "${SRC}")/toolchains-setup.sh" ;;
    esac
}

# toolchains/cst-toolchains as the user, never as root: `config apply` (cargo
# build-dir, uv link-mode), `auto on --unless-off` (the hourly pruning).
snap_toolchains() {   # args...
    local home
    home="$(snap_home)" || return 1
    runuser -u "${target_user}" -- env CST_PROG="$(snap_toolchains_prog)" python3 -I "${TOOLCHAINS_TOOL}" "$@" --home "${home}"
}

# The `config plan` lines for the setup preview, indented, without its
# "Apply with:" hint: setup applies the settings itself after the confirmation.
snap_toolchains_config_preview() {
    snap_toolchains config plan --no-lean-debuginfo 2>&1 | grep -v '^Apply with:' | sed 's/^/    /' || true
}

# btrfs/chrome-cache (the repo copy, like btrfs-bulk) as the user, never as
# root: status|plan|apply|off for ~/.var/app/com.google.Chrome/config/chrome-flags.conf.
snap_chrome() {   # action
    local home
    home="$(snap_home)" || return 1
    runuser -u "${target_user}" -- python3 -I "${CHROME_TOOL}" "$1" --home "${home}"
}

snap_home() {
    [[ -n "${target_user}" && "${target_user}" != root ]] ||
        { echo "snapshots: run through sudo from your user account (needs SUDO_USER)" >&2; exit 1; }
    getent passwd "${target_user}" | cut -d: -f6
}

# Path of /var/home relative to the top level, e.g. "home".
snap_subvol() {
    findmnt -rn -o OPTIONS -M /var/home | tr ',' '\n' | sed -n 's|^subvol=/\{0,1\}||p' | head -n1
}

is_subvol() { [[ -d "$1" && "$(stat -c %i "$1")" == 256 ]]; }

# Update settings in snapshots.conf: KEY=VALUE arguments; everything else
# (including manual edits) is kept. Created with defaults the first time.
snap_conf_set() {
    local kv key val tmp
    install -d -m 0755 "$(dirname "${SNAP_CONF}")"
    if [[ ! -e "${SNAP_CONF}" ]]; then
        cat >"${SNAP_CONF}" <<'DEFAULTS'
# Managed by btrfs-setup.sh (CST); read by /usr/local/sbin/btrfs-snapshot.
# Manual edits of values are kept when btrfs-setup.sh updates other keys.
HOME_DIR=''
HOME_SUBVOL=home
SKIP_AT_OR_BELOW_GIB=250
RESUME_ABOVE_GIB=270
EMERGENCY_AT_OR_BELOW_GIB=150
NAS_ENABLED=0
NAS_HOST=''
NAS_PORT=2204
NAS_USER=''
NAS_PATH=''
NAS_RESTIC_HOST=''
NAS_STALE_DAYS=7
NAS_TIMEOUT=6h
NAS_KEEP_DAILY=14
NAS_KEEP_WEEKLY=8
NAS_KEEP_MONTHLY=12
DEFAULTS
        chmod 0644 "${SNAP_CONF}"
    fi
    for kv in "$@"; do
        key="${kv%%=*}"; val="${kv#*=}"
        printf -v val '%q' "${val}"
        tmp="$(mktemp)"
        K="${key}" L="${key}=${val}" awk 'BEGIN {k = ENVIRON["K"]; line = ENVIRON["L"]; done = 0}
            index($0, k "=") == 1 {print line; done = 1; next} {print}
            END {if (!done) print line}' "${SNAP_CONF}" >"${tmp}"
        install -m 0644 "${tmp}" "${SNAP_CONF}"
        rm -f "${tmp}"
    done
}

snap_install_runtime() {
    local f
    for f in btrfs-snapshot btrfs-snapshot-core btrfs-notify btrfs-bulk; do
        check_syntax "${SRC}/${f}" || { echo "${SRC}/${f}: syntax error, not installed" >&2; exit 1; }
        install_file "${SRC}/${f}" "/usr/local/sbin/${f}" 0755
    done
    # Alerts raised before the login (convert-boot) are shown at the next login.
    install_pending_alerts
    install_units "${SNAP_UNITS[@]}"
    systemctl daemon-reload
}

# State of one SNAP_EXCLUDE entry: done | todo | create | aside | absent | -.
snap_entry_state() {   # strategy path
    local strategy="$1" path="$2"
    if [[ "${strategy}" == aside ]]; then
        if [[ -d "${path}" ]] && ! is_subvol "${path}" && ! is_subvol "$(dirname "${path}")"; then
            echo aside
        else
            echo -
        fi
    elif is_subvol "${path}"; then echo "done"
    elif [[ -s "${SNAP_STATE}/skipped" ]] && cut -f1 "${SNAP_STATE}/skipped" | grep -qxF -- "${path}"; then
        printf 'skipped: %s\n' "$(P="${path}" awk -F '\t' '$1 == ENVIRON["P"] {print $2; exit}' "${SNAP_STATE}/skipped")"
    elif [[ -e "${path}" ]]; then echo todo
    elif [[ -d "$(dirname "${path}")" ]]; then echo create
    else echo absent; fi
}

snap_plan_summary() {   # home: prints the plan; sets SNAP_TODO
    local home="$1" entry strategy rel state
    SNAP_TODO=0
    echo "Directories that become nested subvolumes (left out of /home snapshots;"
    echo "your Workspace folder gets snapshots of its own; ~/Bulk is filled below):"
    for entry in "${SNAP_EXCLUDE[@]}"; do
        read -r strategy rel <<<"${entry}"
        state="$(snap_entry_state "${strategy}" "${home}/${rel}")"
        case "${state}" in
            done) state="done" ;;
            todo) state="to convert (reflink clone)"; SNAP_TODO=$((SNAP_TODO + 1)) ;;
            create) state="absent: created empty"; SNAP_TODO=$((SNAP_TODO + 1)) ;;
            aside) state="cache: deleted before converting its parent"; SNAP_TODO=$((SNAP_TODO + 1)) ;;
            absent) state="absent (created when its parent appears)" ;;
            -) continue ;;
        esac
        printf '  ~/%-62s %s\n' "${rel}" "${state}"
    done
    echo "  Directories in use now (your session, IDE, Steam...) are converted at the next normal"
    echo "  boot, before the login screen; the old copies are then deleted in the background."
}

# One command: plan, confirm once, then everything else; safe to re-run.
snap_setup() {
    local yes=0 home subvol bulk_rc
    [[ "${1:-}" == --yes ]] && yes=1
    home="$(snap_home)"
    # Checked before anything changes (snap_activate needs both later).
    subvol="$(snap_subvol || true)"
    [[ -n "${subvol}" && -n "$(root_fs_uuid || true)" ]] ||
        { echo "snapshots: /var/home is not a btrfs subvolume mount; nothing changed" >&2; exit 1; }
    snap_plan_summary "${home}"
    echo
    bulk_run plan
    echo
    snap_clean_targets_plan "${home}"
    echo
    echo "Also, through $(snap_toolchains_prog) (as you):"
    echo "      ~/.cargo/config.toml build-dir -> ~/.cache/cargo-build/{workspace-path-hash} and"
    echo "      ~/.config/uv/uv.toml link-mode = \"clone\" (hardlinks cannot cross subvolumes):"
    snap_toolchains_config_preview
    echo "      the hourly pruning of ~/.cache/cargo-build (cst-toolchains-prune.timer): folders"
    echo "      idle 48h+, then the least recently active while over 150 GiB; never one active in"
    echo "      the last 2h or in use ($(snap_toolchains_prog) prune plan shows what it would delete"
    echo "      now; stays off if you ran $(snap_toolchains_prog) auto off);"
    echo "      Chrome (Flatpak) HTTP cache in the sandbox's /tmp (RAM, emptied at reboot, at most"
    echo "      512 MiB) via chrome-flags.conf: $(snap_chrome plan 2>&1 || true)"
    echo "      hourly snapshots when something changed, keep ${SNAP_PRESERVE_TEXT}"
    echo "      (/home: every hour 3 days, a day 1 week, a week 1 month, a month 3 months;"
    echo "      ~/Workspace: every hour 2 days, a day 1 week); no new snapshot at or below"
    echo "      250 GiB free, emergency purge at or below 150 GiB (btrfs-snapshot-core; nothing"
    echo "      to install). The first snapshot is taken right away, or, if directories to"
    echo "      convert or items to move into ~/Bulk are busy now, after the next boot has"
    echo "      handled them (a snapshot taken before would keep their data in /home's history"
    echo "      for months). If a ~/Bulk item needs a look (its data still in /home), snapshots"
    echo "      start only if you say so; items you keep in /home (bulk keep ID) are fine."
    echo
    if (( ! yes )); then
        [[ -t 0 ]] || { echo "No terminal to confirm: re-run with --yes." >&2; exit 1; }
        read -r -p "Apply all of the above? [y/N] " ans
        [[ "${ans}" == [yYsS]* ]] || { echo "Nothing changed."; return 0; }
    fi

    # 1. Plan, settings, runtime, user configs.
    install -d -m 0755 "$(dirname "${SNAP_PLAN}")"
    {
        echo "# Managed by btrfs-setup.sh (CST): <aside|copy> <absolute path>"
        local entry strategy rel
        for entry in "${SNAP_EXCLUDE[@]}"; do
            read -r strategy rel <<<"${entry}"
            echo "${strategy} ${home}/${rel}"
        done
    } | install_text "${SNAP_PLAN}" 0644
    snap_conf_set HOME_DIR="${home}" HOME_SUBVOL="${subvol}"
    snap_install_runtime
    snap_user_configs "${home}"
    # 2. ~/Bulk first (a subvolume as you, then each item moved in and linked;
    # busy items at the next boot): what moves there is never cloned below.
    echo
    echo "Filling ~/Bulk..."
    bulk_rc=0
    bulk_apply || bulk_rc=$?
    # 3. Old cargo targets (before converting, so they are not cloned for nothing).
    snap_clean_targets_apply "${home}"
    # 4. Live conversion; what is busy now is done at the next boot, before login.
    /usr/local/sbin/btrfs-snapshot convert
    if [[ -e "${SNAP_BOOT_FLAG}" ]]; then
        systemctl enable btrfs-subvol-convert.service
    else
        systemctl disable btrfs-subvol-convert.service 2>/dev/null || true
    fi
    # 5. ~/Bulk not complete: said before any snapshot can be taken (the first
    # one would keep what is still at the old paths for months). Only 1 asks:
    # 4 = needs a look inside ~/Bulk only (no data of it in /home).
    (( bulk_rc == 0 )) || bulk_warn "${bulk_rc}"
    if (( bulk_rc == 1 )) && ! snap_start_anyway "${yes}"; then
        echo
        snap_status
        return 1
    fi
    # 6. Snapshots, starting now (or after the boot that finishes the moves).
    snap_activate
    echo
    snap_status
}

# Items need a look: start snapshots only if you say so (never with --yes).
snap_start_anyway() {   # yes
    local ans
    if (( $1 )) || [[ ! -t 0 ]]; then
        echo
        echo "Snapshots were NOT started (--yes never starts them over ~/Bulk items that need"
        echo "a look). Fix the items (or keep one in /home on purpose: '${PROG} bulk"
        echo "keep ID'), then run '${PROG} snapshots setup' again; or run it"
        echo "without --yes and answer y to start them anyway."
    else
        echo
        read -r -p "Start snapshots anyway (that data then stays in /home's snapshots)? [y/N] " ans
        [[ "${ans}" == [yYsS]* ]] && return 0
        echo "Snapshots were NOT started. Fix the items, then run '${PROG} snapshots setup' again."
    fi
    if systemctl is-enabled -q btrfs-snapshot.timer 2>/dev/null; then
        echo "(The hourly snapshots an earlier setup enabled keep running; stop them with"
        echo " '${PROG} snapshots disable'.)"
    fi
    return 1
}

# User-owned configs, written as the user; existing settings are never changed.
snap_user_configs() {   # home
    local home="$1" tc
    tc="$(snap_toolchains_prog)"
    # cargo build-dir and uv link-mode (the toolchains module writes them).
    # --no-lean-debuginfo: setup never turns on the opt-in lean debug info
    # (it rebuilds every dev cache once; the user applies it when builds are quiet).
    snap_toolchains config apply --no-lean-debuginfo ||
        echo "NOTE: the toolchain settings were not all written (see above); later: ${tc} config apply" >&2
    # Nothing ever deletes cargo's build folders: the user timer does (it
    # refuses, and says so, when build-dir is not the line written above).
    # --unless-off: after `auto off` it stays off (and says so).
    snap_toolchains auto on --unless-off ||
        echo "NOTE: the hourly pruning of toolchain caches was not turned on (see above); later: ${tc} auto on" >&2
    # Chrome's HTTP cache out of /home snapshots (only if its Flatpak is installed).
    snap_chrome apply || echo "NOTE: Chrome's chrome-flags.conf was left as it is (see above)" >&2
}

# NUL-separated cargo target dirs under ~/Workspace that still hold
# intermediate artifacts (old layout: .fingerprint inside target/; with
# build-dir only the final artifacts stay there).
snap_cargo_targets() {
    find "$1/Workspace" -xdev \( -path '*/.flatpak-builder' -prune \) -o \
        \( -type d -name target -exec sh -c 'for m in "$1"/debug/.fingerprint "$1"/release/.fingerprint "$1"/*/debug/.fingerprint "$1"/*/release/.fingerprint; do
              [ -e "$m" ] && exit 0; done; exit 1' _ '{}' \; -print0 -prune \) 2>/dev/null
}

# Sets SNAP_TARGETS (NUL-safe array) and prints them; one walk of ~/Workspace.
snap_clean_targets_plan() {
    local sizes total
    mapfile -d '' -t SNAP_TARGETS < <(snap_cargo_targets "$1")
    (( ${#SNAP_TARGETS[@]} )) || { echo "Old cargo target/ dirs: none."; return 0; }
    # Files vanishing during a running build make du exit non-zero.
    sizes="$(du -sx -- "${SNAP_TARGETS[@]}" 2>/dev/null || true)"
    total="$(awk '{s += $1} END {print s + 0}' <<<"${sizes}")"
    echo "Old cargo target/ dirs to delete (rebuilt into ~/.cache/cargo-build): ${#SNAP_TARGETS[@]}, $(( total / 1024 / 1024 )) GiB. Largest:"
    sort -rn <<<"${sizes}" | head -n 5 |
        while read -r size dir; do printf '  %7.1f GiB  %q\n' "$(awk -v s="${size}" 'BEGIN {print s / 2^20}')" "${dir}"; done
}

snap_clean_targets_apply() {
    (( ${#SNAP_TARGETS[@]} )) || return 0
    if pgrep -u "${target_user}" -f '(^|/)(cargo|rustc|rust-analyzer[^ ]*|clippy-driver|cargo-clippy)( |$)' >/dev/null ||
        pgrep -u "${target_user}" -f 'RustRover|rustrover' >/dev/null; then
        echo "Skipped deleting cargo target/ dirs: a Rust build, rust-analyzer or RustRover is running." >&2
        echo "Later: ${PROG} snapshots clean-targets --apply" >&2
        return 0
    fi
    printf '%s\0' "${SNAP_TARGETS[@]}" | xargs -0r runuser -u "${target_user}" -- rm -rf --one-file-system --
    echo "Deleted ${#SNAP_TARGETS[@]} cargo target/ dir(s)."
}

snap_clean_targets() {
    local home
    home="$(snap_home)"
    snap_clean_targets_plan "${home}"
    if [[ "${1:-}" == --apply ]]; then
        snap_clean_targets_apply
    else
        echo "Dry run. Delete with: ${PROG} snapshots clean-targets --apply"
    fi
}

# Mount the top level on demand, start the timer and the first snapshot.
snap_activate() {
    local subvol uuid opts unit
    subvol="$(snap_subvol)"
    uuid="$(root_fs_uuid)"
    [[ -n "${subvol}" && -n "${uuid}" ]] || { echo "cannot resolve the /home subvolume" >&2; exit 1; }
    # Same options as /var (btrfs mount options are per filesystem), minus the subvolume.
    opts="$(findmnt -rn -o OPTIONS -M /var | tr ',' '\n' |
        grep -vE '^(rw|ro|seclabel|subvol=.*|subvolid=.*)$' | paste -sd, - || true)"
    opts="subvolid=5${opts:+,${opts}}"
    unit="$(systemd-escape --path "${SNAP_TOP}")"
    install -d -m 0700 "${SNAP_TOP}"
    render "${SNAP_SRC}/top.mount.in" UUID="${uuid}" OPTIONS="${opts}" |
        install_text "${UNIT_DIR}/${unit}.mount" 0644
    install_text "${UNIT_DIR}/${unit}.automount" 0644 <"${SNAP_SRC}/top.automount.in"
    systemctl daemon-reload
    systemctl enable --now "${unit}.automount"
    install -d -m 0700 "${SNAP_TOP}/${SNAP_DIR_NAME}"
    # Start the health check's clock now (not reset by re-running setup).
    install -d -m 0755 "${SNAP_STATE}"
    [[ -e "${SNAP_STATE}/last-ok" ]] || date +%s >"${SNAP_STATE}/last-ok"
    systemctl enable --now btrfs-snapshot.timer
    # Busy directories (and busy ~/Bulk items) wait for the next boot: until
    # then the hourly run takes no snapshot (it would hold their data for months).
    if [[ -e "${SNAP_BOOT_FLAG}" || -e "${BULK_FLAG}" ]]; then
        echo "Snapshots start after the next boot, once the busy directories are converted"
        echo "and the busy items are in ~/Bulk."
        return 0
    fi
    # Nothing to wait for (no package to layer): the first snapshot now.
    echo "First snapshot started in the background (journalctl -u btrfs-snapshot)."
    systemctl start --no-block btrfs-snapshot.service || true
}

snap_disable() {
    local purge=0 unit f
    [[ "${1:-}" == --purge ]] && purge=1
    systemctl disable --now btrfs-snapshot.timer btrfs-snapshot-nas.timer btrfs-subvol-convert.service 2>/dev/null || true
    rm -f "${SNAP_BOOT_FLAG}"
    # ~/Bulk is not part of the snapshots: its boot step stays (it also
    # finishes interrupted moves).
    if [[ -e "${BULK_FLAG}" ]]; then
        echo "Note: ~/Bulk items wait for the next boot ($(tr '\n' ' ' <"${BULK_FLAG}")) and are still moved"
        echo "then; to cancel that: sudo rm ${BULK_FLAG}"
    fi
    # btrbk configs written by earlier versions (only ours).
    for f in /etc/btrbk/btrbk.conf /etc/btrbk/btrbk-emergency.conf /etc/btrbk/btrbk-nas.conf; do
        if grep -qs 'Managed by btrfs-snapshot' "${f}"; then rm -fv "${f}"; fi
    done
    echo "Hourly snapshots and the NAS backup are stopped; existing snapshots are kept."
    echo "Not touched: the pruning of ~/.cache/cargo-build (it follows the cargo build-dir setting,"
    echo "             which stays; '$(snap_toolchains_prog) auto off' removes it)."
    (( purge )) || { echo "Delete them all with: ${PROG} snapshots disable --purge"; return 0; }
    # A run in progress must end first (a NAS backup unmounts and unpins on stop).
    systemctl stop btrfs-snapshot.service btrfs-snapshot-nas.service 2>/dev/null || true
    if [[ -x "${SNAP_RUNTIME}" ]]; then
        # Only what btrfs-snapshot-core owns (name, read-only subvolume, Parent
        # UUID of /home or ~/Workspace); anything else in the directory stays.
        "${SNAP_RUNTIME}" purge || echo "WARNING: deleting the snapshots failed; see the messages above" >&2
    elif [[ -d "${SNAP_TOP}/${SNAP_DIR_NAME}" ]]; then
        echo "WARNING: ${SNAP_RUNTIME} is missing: snapshots in ${SNAP_TOP}/${SNAP_DIR_NAME} not deleted" >&2
    fi
    unit="$(systemd-escape --path "${SNAP_TOP}")"
    systemctl disable --now "${unit}.automount" 2>/dev/null || true
    systemctl stop "${unit}.mount" 2>/dev/null || true
    rm -fv "${UNIT_DIR:?}/${unit:?}.mount" "${UNIT_DIR:?}/${unit:?}.automount"
    systemctl daemon-reload
    echo "Snapshots purged and their configuration removed (the subvolume exclusions stay;"
    echo "the restic repository on the NAS and ${SNAP_RESTIC_PW} are kept)."
}

# The installed convert-plan lists paths SNAP_EXCLUDE no longer has (written
# by an older version, e.g. the Chrome cache): the hourly run would still
# convert them. `snapshots setup` rewrites it.
snap_plan_stale() {   # home
    local entry strategy rel line known
    [[ -r "${SNAP_PLAN}" ]] || return 0
    while IFS= read -r line; do
        [[ -z "${line}" || "${line}" == \#* ]] && continue
        known=0
        for entry in "${SNAP_EXCLUDE[@]}"; do
            read -r strategy rel <<<"${entry}"
            [[ "${line}" == "${strategy} $1/${rel}" ]] && known=1
        done
        (( known )) && continue
        echo "  ${SNAP_PLAN} is from an older version (it still plans: ${line#* })."
        echo "  Run '${PROG} snapshots setup' again to update it."
        return 0
    done <"${SNAP_PLAN}"
}

# Snapshot names in the directory (what btrfs-snapshot-core names; cheap, no
# btrfs calls: `snapshots list` shows the verified view).
snap_list() {
    find "${SNAP_TOP}/${SNAP_DIR_NAME}" -mindepth 1 -maxdepth 1 -regextype posix-extended \
        -regex '.*/(home|Workspace)\.[0-9]{8}T[0-9]{4}(_[0-9]+)?' -printf '%f\n' 2>/dev/null | sort
}

snap_status() {
    local home entry strategy rel state timer name
    local -a snaps
    home="$(snap_home)"
    timer="$(systemctl is-enabled btrfs-snapshot.timer 2>/dev/null)" || timer=off
    echo "hourly snapshots: ${timer}, keep ${SNAP_PRESERVE_TEXT} (btrfs-snapshot-core)"
    if [[ -d "${SNAP_TOP}/${SNAP_DIR_NAME}" ]]; then
        for name in home Workspace; do
            mapfile -t snaps < <(snap_list | grep "^${name}\.")
            (( ${#snaps[@]} )) || continue
            echo "  ${name}: ${#snaps[@]} snapshot(s); oldest ${snaps[0]#*.}; newest ${snaps[-1]#*.}"
        done
    fi
    if [[ -e "${SNAP_STATE}/emergency" ]]; then
        echo "  EMERGENCY purge by the space guard (free space at or below the emergency limit)"
    elif [[ -e "${SNAP_STATE}/paused" ]]; then
        echo "  PAUSED by the space guard (low free space)"
    fi
    [[ -r "${SNAP_STATE}/last-ok" ]] && echo "  last successful run: $(date -d "@$(cat "${SNAP_STATE}/last-ok")" '+%F %T')"
    if [[ -e "${SNAP_STATE}/alerted-blocked" ]] && (( $(date +%s) - $(stat -c %Y "${SNAP_STATE}/alerted-blocked") < 86400 )); then
        echo "  a safety check blocked snapshots or deletions in the last day:"
        echo "    journalctl -u btrfs-snapshot; ${PROG} snapshots list"
        echo "    (deletions held back by the circuit breaker: ${PROG} snapshots prune [--confirm])"
    fi
    [[ -s "${SNAP_STATE}/drop-pending" ]] && echo "  old trees still being deleted: $(grep -c . "${SNAP_STATE}/drop-pending") (no snapshot until done)"
    [[ -e "${SNAP_BOOT_FLAG}" ]] &&
        echo "  conversion of busy directories pending: at the next boot, before login (no snapshot until then)"
    [[ -e "${BULK_FLAG}" ]] &&
        echo "  items to move into ~/Bulk pending: at the next boot, before login (no snapshot until then)"
    echo "nested subvolumes:"
    for entry in "${SNAP_EXCLUDE[@]}"; do
        read -r strategy rel <<<"${entry}"
        state="$(snap_entry_state "${strategy}" "${home}/${rel}")"
        case "${state}" in
            done) state="ok" ;;
            todo) state="pending (busy)" ;;
            create) state="pending (create)" ;;
            aside) state="pending (delete cache)" ;;
            absent) state="absent" ;;
            -) continue ;;
        esac
        printf '  %-23s ~/%s\n' "${state}" "${rel}"
    done
    snap_plan_stale "${home}"
    echo "toolchain caches (~/.cache/cargo-build) and settings: see $(snap_toolchains_prog) status"
    snap_chrome status 2>&1 || true
    local NAS_ENABLED=0 NAS_HOST="" NAS_PORT="" NAS_USER="" NAS_PATH=""
    # shellcheck source=/dev/null
    [[ -r "${SNAP_CONF}" ]] && . "${SNAP_CONF}"
    if [[ "${NAS_ENABLED}" == 1 ]]; then
        echo "NAS backup (restic): sftp:${SNAP_SSH_ALIAS}:${NAS_PATH} (${NAS_USER}@${NAS_HOST}:${NAS_PORT}), timer $(systemctl is-enabled btrfs-snapshot-nas.timer 2>/dev/null || true)"
        command -v restic >/dev/null || echo "  restic not active yet (rpm-ostree): backups start after the next reboot"
        [[ -e "${SNAP_STATE}/nas-init-pending" ]] &&
            echo "  the repository is created by the first run, with ${SNAP_RESTIC_PW} (store a copy once it exists)"
        if [[ -r "${SNAP_STATE}/nas-last-ok" ]]; then
            echo "  last success: $(date -d "@$(cat "${SNAP_STATE}/nas-last-ok")" '+%F %T')"
        else
            echo "  last success: never"
        fi
        [[ -r "${SNAP_STATE}/nas-summary" ]] && sed 's/^/  latest /' "${SNAP_STATE}/nas-summary"
        [[ -r "${SNAP_STATE}/nas-last-prune" ]] && echo "  last prune:   $(date -d "@$(cat "${SNAP_STATE}/nas-last-prune")" '+%F')"
        [[ -r "${SNAP_STATE}/nas-last-check" ]] && echo "  last check:   $(date -d "@$(cat "${SNAP_STATE}/nas-last-check")" '+%F')"
        echo "  password: ${SNAP_RESTIC_PW} (without it the backups cannot be read: keep a copy in your password manager)"
    else
        echo "NAS backup: off"
    fi
    return 0
}

# Managed Host block in root's ssh config, under its own alias (root's other
# ssh use of the NAS is unaffected): address, port, user, key, timeouts,
# keepalives. restic's sftp backend runs plain ssh and picks it up.
snap_ssh_config() {   # host port user
    local tmp
    install -d -m 0700 /root/.ssh
    tmp="$(mktemp)"
    [[ -e "${SNAP_SSH_CONFIG}" ]] &&
        awk '/^# BEGIN btrfs-setup.sh NAS/ {skip = 1} !skip {print} /^# END btrfs-setup.sh NAS/ {skip = 0}' "${SNAP_SSH_CONFIG}" >"${tmp}"
    cat >>"${tmp}" <<EOF
# BEGIN btrfs-setup.sh NAS (managed; snapshots nas setup)
Host ${SNAP_SSH_ALIAS}
    HostName $1
    Port $2
    User $3
    IdentityFile ${SNAP_NAS_KEY}
    IdentitiesOnly yes
    BatchMode yes
    ConnectTimeout 10
    # Long phases without traffic (restic skipping unchanged files) must not
    # drop the session; a NAS silent for 3 minutes is given up on.
    ServerAliveInterval 30
    ServerAliveCountMax 6
    StrictHostKeyChecking yes
# END btrfs-setup.sh NAS
EOF
    install -m 0600 "${tmp}" "${SNAP_SSH_CONFIG}"
    rm -f "${tmp}"
}

# sftp batch from stdin through the managed Host block; quiet, bounded.
snap_sftp() { timeout 60 sftp -q -b - "${SNAP_SSH_ALIAS}" >/dev/null 2>&1; }

# The NAS accepts the key and the SFTP subsystem (what restic uses) can write
# into PATH, or, before the repository exists, into its parent (restic init
# creates PATH). Paths are validated: no spaces or quotes in the batch.
snap_nas_test() {   # path
    local dir="$1" tmp name rc
    printf 'cd %s\n' "${dir}" | snap_sftp || dir="$(dirname "$1")"
    tmp="$(mktemp)"
    name=".btrfs-setup-write-test.$$"
    # (btrfs-setup.sh runs with errexit: the status is taken, not left to fail.)
    rc=0
    printf 'cd %s\nput %s %s\nrm %s\n' "${dir}" "${tmp}" "${name}" "${name}" | snap_sftp || rc=$?
    rm -f "${tmp}"
    return "${rc}"
}

# restic with the repository at SNAP_REPO_PATH (set by the caller).
snap_restic() {
    RESTIC_REPOSITORY="sftp:${SNAP_SSH_ALIAS}:${SNAP_REPO_PATH}" RESTIC_PASSWORD_FILE="${SNAP_RESTIC_PW}" \
        RESTIC_CACHE_DIR=/var/cache/btrfs-snapshot-restic restic "$@"
}

# Layer restic if absent; true if it can be used now.
snap_restic_layer() {
    local staged
    command -v restic >/dev/null && return 0
    staged="$(rpm-ostree status --json 2>/dev/null | python3 -I -c 'import json, sys
d = [x for x in json.load(sys.stdin)["deployments"] if x.get("staged")]
print("yes" if d and "restic" in (d[0].get("requested-packages") or []) else "no")' 2>/dev/null || echo no)"
    if [[ "${staged}" != yes ]]; then
        echo "Layering restic (rpm-ostree install --apply-live)..."
        # --apply-live is refused while another deployment is staged: then
        # restic just joins that one (active after the next reboot).
        rpm-ostree install --idempotent --apply-live restic ||
            rpm-ostree install --idempotent restic ||
            echo "WARNING: rpm-ostree install restic failed; re-run 'snapshots nas setup' later" >&2
    fi
    command -v restic >/dev/null && return 0
    echo "restic becomes active after the next reboot; the NAS backup starts by itself then."
    return 1
}

# Random repository password, root-only. Sets SNAP_PW_NEW=1 if created now.
snap_restic_password() {
    local tmp
    SNAP_PW_NEW=0
    [[ -s "${SNAP_RESTIC_PW}" ]] && return 0
    install -d -m 0755 "$(dirname "${SNAP_RESTIC_PW}")"
    tmp="$(mktemp)"   # 0600 from the start
    head -c 33 /dev/urandom | base64 | tr -d '\n' >"${tmp}"
    install -m 0600 -o root -g root "${tmp}" "${SNAP_RESTIC_PW}"
    rm -f "${tmp}"
    SNAP_PW_NEW=1
}

# Open the repository, or create it if there is none. 0 ready, 1 failed.
# Sets SNAP_REPO_CREATED=1 if it was created now.
snap_restic_init() {
    local err rc
    rc=0
    SNAP_REPO_CREATED=0
    err="$(snap_restic cat config 2>&1 >/dev/null)" || rc=$?
    case "${rc}" in
        0) echo "Repository found: sftp:${SNAP_SSH_ALIAS}:${SNAP_REPO_PATH} (the password opens it)." ;;
        10)
            echo "Creating the restic repository sftp:${SNAP_SSH_ALIAS}:${SNAP_REPO_PATH}..."
            snap_restic init || { echo "nas setup: restic init failed" >&2; return 1; }
            SNAP_REPO_CREATED=1 ;;
        12)
            echo "nas setup: a repository exists at ${SNAP_REPO_PATH}, but ${SNAP_RESTIC_PW} does not open it." >&2
            echo "           Put the password you saved for it into that file (root, 0600) and run setup again." >&2
            return 1 ;;
        *) echo "nas setup: restic cannot open the repository (rc=${rc}): ${err:0:400}" >&2; return 1 ;;
    esac
}

# A repository already at PATH? Asked over SFTP (restic's config file), so
# it also works before restic is active.
snap_repo_exists() {   # path
    printf 'ls %s/config\n' "$1" | snap_sftp
}

# What the runtime recorded about the repository at the old place (which
# snapshots it holds, last prune/check, summary) does not apply to a new one.
snap_nas_forget_repo() {
    rm -f "${SNAP_STATE}"/nas-done-* "${SNAP_STATE}/nas-summary" "${SNAP_STATE}/nas-last-prune" \
        "${SNAP_STATE}/nas-last-check" "${SNAP_STATE}/nas-check-failures" "${SNAP_STATE}/nas-repo-id"
}

snap_nas() {
    local action="${1:-status}" host user port path key restic_now=0 repo_exists=0
    [[ $# -gt 0 ]] && shift
    local NAS_ENABLED=0 NAS_HOST="" NAS_PORT="" NAS_USER="" NAS_PATH="" NAS_RESTIC_HOST="" HOME_DIR=""
    # shellcheck source=/dev/null
    [[ -r "${SNAP_CONF}" ]] && . "${SNAP_CONF}"
    host="${NAS_HOST:-diskstation.local}"; port="${NAS_PORT:-2204}"; user="${NAS_USER:-${target_user}}"; path="${NAS_PATH}"
    key="${SNAP_NAS_KEY}"
    SNAP_REPO_PATH="${path}"
    case "${action}" in
        setup)
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --path) path="${2:?}"; shift 2 ;;
                    --host) host="${2:?}"; shift 2 ;;
                    --port) port="${2:?}"; shift 2 ;;
                    --user) user="${2:?}"; shift 2 ;;
                    *) echo "nas setup: unknown argument $1" >&2; exit 2 ;;
                esac
            done
            # These go into the ssh config and sftp batches: no spaces or quotes.
            [[ "${path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || { echo "nas setup: --path /<share>/<dir> is required (no spaces)" >&2; exit 2; }
            [[ "${host}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo "nas setup: --host must be a host name or IPv4 address" >&2; exit 2; }
            [[ "${user}" =~ ^[A-Za-z_][A-Za-z0-9._-]*$ ]] || { echo "nas setup: --user: letters, digits, '.', '_', '-' only" >&2; exit 2; }
            [[ "${port}" =~ ^[0-9]+$ ]] || { echo "nas setup: --port must be a number" >&2; exit 2; }
            [[ -n "${HOME_DIR}" ]] || { echo "nas setup: run '${PROG} snapshots setup' first" >&2; exit 1; }
            install -d -m 0700 "$(dirname "${key}")"
            [[ -e "${key}" ]] || ssh-keygen -q -t ed25519 -N "" -C "btrfs-setup@$(hostname -s)" -f "${key}"
            snap_ssh_config "${host}" "${port}" "${user}"
            # known_hosts names port 22 plainly, other ports as [host]:port.
            local kh_name="${host}" scan
            [[ "${port}" == 22 ]] || kh_name="[${host}]:${port}"
            if ! ssh-keygen -F "${kh_name}" -f /root/.ssh/known_hosts >/dev/null 2>&1; then
                scan="$(ssh-keyscan -p "${port}" -t ed25519 "${host}" 2>/dev/null)"
                [[ -n "${scan}" ]] || { echo "nas setup: no SSH answer from ${host}:${port}" >&2; exit 1; }
                printf '%s\n' "${scan}" >>/root/.ssh/known_hosts
                echo "Recorded the NAS host key (trust on first use):"
                ssh-keygen -l -F "${kh_name}" -f /root/.ssh/known_hosts | tail -n1
            fi
            snap_conf_set NAS_HOST="${host}" NAS_PORT="${port}" NAS_USER="${user}" NAS_PATH="${path}" \
                NAS_RESTIC_HOST="${NAS_RESTIC_HOST:-$(hostname -s)}"
            snap_install_runtime
            # Layered first, also while the NAS is not ready yet: it may only
            # become active after a reboot.
            if snap_restic_layer; then restic_now=1; fi
            if ! snap_nas_test "${path}"; then
                # Synology's SFTP server shows the shared folders at its root
                # (/<share>/<dir>), while an ssh shell sees /volumeN/<share>/<dir>.
                if [[ "${path}" =~ ^/volume[0-9]+(/.+)$ ]] && snap_nas_test "${BASH_REMATCH[1]}"; then
                    path="${BASH_REMATCH[1]}"
                    echo "Note: the NAS's SFTP server shows this folder as ${path} (without /volumeN); using that."
                    snap_conf_set NAS_PATH="${path}"
                else
                    echo
                    echo "On the NAS (DSM):"
                    echo "  - enable SFTP (Control Panel > File Services > FTP > SFTP) on port ${port};"
                    echo "  - enable 'User Home' (Control Panel > User & Group > Advanced) so ~/.ssh exists;"
                    echo "  - the home and ~/.ssh must not be writable by others (chmod 755 ~ ; chmod 700 ~/.ssh);"
                    echo "  - ${user} needs write access to ${path} (or to its parent; the repository directory"
                    echo "    is created). Give the path as SFTP shows it: /<share>/<dir>;"
                    echo "  - append this key to ~${user}/.ssh/authorized_keys (chmod 600):"
                    echo
                    cat "${key}.pub"
                    echo
                    echo "Then run the same '${PROG} snapshots nas setup' again: it tests and enables the backup."
                    return 0
                fi
            fi
            SNAP_REPO_PATH="${path}"
            install -d -m 0755 "${SNAP_STATE}"
            [[ "${path}" == "${NAS_PATH}" && "${host}" == "${NAS_HOST}" ]] || snap_nas_forget_repo
            # The password decides whether the backups can ever be read: find
            # out about the repository before making or announcing one. An
            # existing repository needs the password it was created with.
            if snap_repo_exists "${path}"; then repo_exists=1; fi
            if [[ ! -s "${SNAP_RESTIC_PW}" ]] && (( repo_exists )); then
                echo "nas setup: there is a restic repository at ${path} already, and no ${SNAP_RESTIC_PW}." >&2
                echo "           Put the password you saved for that repository into that file (owner root," >&2
                echo "           mode 0600) and run setup again. No new password was made: it could not open it." >&2
                exit 1
            fi
            snap_restic_password
            if (( restic_now )); then
                snap_restic_init || exit 1
                rm -f "${SNAP_STATE}/nas-init-pending"
                if (( SNAP_REPO_CREATED )); then
                    echo
                    echo "IMPORTANT: the new repository is encrypted with the password in ${SNAP_RESTIC_PW}."
                    echo "Without it the backups cannot be read (e.g. after losing this machine's disks)."
                    if (( SNAP_PW_NEW )); then
                        echo "Store a copy in your password manager now, as a new entry (it replaces no other);"
                    else
                        echo "It is the password already in that file: make sure your password manager has it;"
                    fi
                    echo "show it with: sudo cat ${SNAP_RESTIC_PW}"
                    echo
                fi
            elif (( repo_exists )); then
                rm -f "${SNAP_STATE}/nas-init-pending"
                echo "There is a repository at ${path}: the first run after the next reboot checks that"
                echo "${SNAP_RESTIC_PW} opens it (an alert says so if it does not)."
            else
                # Created by the first run once restic is active (only then,
                # and only because it was asked for here).
                touch "${SNAP_STATE}/nas-init-pending"
                echo
                echo "The password in ${SNAP_RESTIC_PW} is provisional: the first backup after the next"
                echo "reboot creates the repository with it (a notification says when). Then store a copy"
                echo "in your password manager as a new entry; without it the backups cannot be read."
                echo
            fi
            snap_nas enable
            ;;
        status)
            [[ -n "${path}" ]] || { echo "NAS backup not set up: ${PROG} snapshots nas setup --path ..."; return 0; }
            echo "NAS ${user}@${host}:${port}, repository sftp:${SNAP_SSH_ALIAS}:${path} (enabled=${NAS_ENABLED})"
            if snap_nas_test "${path}"; then
                echo "  connection ok; ${path} writable over SFTP"
            else
                echo "  NOT ready: key not accepted, SFTP off, or ${path} missing/not writable"
                return 1
            fi
            if ! command -v restic >/dev/null; then
                echo "  restic not active yet (after the next reboot)"
            elif [[ -s "${SNAP_RESTIC_PW}" ]]; then
                snap_restic snapshots --compact --latest 1 --host "${NAS_RESTIC_HOST:-$(hostname -s)}" \
                    --tag btrfs-setup 2>&1 | sed 's/^/  /'
            fi
            ;;
        enable)
            [[ -n "${path}" ]] || { echo "Run 'snapshots nas setup --path ...' first." >&2; exit 1; }
            snap_nas_test "${path}" ||
                { echo "The NAS is not ready (see 'snapshots nas status'): fix that first." >&2; exit 1; }
            [[ -s "${SNAP_RESTIC_PW}" ]] || { echo "No ${SNAP_RESTIC_PW}: run 'snapshots nas setup' again." >&2; exit 1; }
            snap_conf_set NAS_ENABLED=1
            snap_install_runtime
            install -d -m 0755 "${SNAP_STATE}"
            # The staleness clock starts when the backup is turned on (also
            # again after 'snapshots disable', which stops only the timer), not
            # at every re-run of setup: that would hide a backup that keeps failing.
            if [[ "${NAS_ENABLED}" != 1 || ! -s "${SNAP_STATE}/nas-enabled-at" ]] ||
                ! systemctl is-enabled -q btrfs-snapshot-nas.timer 2>/dev/null; then
                date +%s >"${SNAP_STATE}/nas-enabled-at"
            fi
            systemctl enable --now btrfs-snapshot-nas.timer
            echo "Daily NAS backup enabled (restic: keeps 14 daily, 8 weekly, 12 monthly)."
            if command -v restic >/dev/null; then
                echo "Start the first one now with: ${PROG} snapshots nas run"
            else
                echo "It starts after the next reboot, once restic is active."
            fi
            ;;
        disable)
            systemctl disable --now btrfs-snapshot-nas.timer 2>/dev/null || true
            snap_conf_set NAS_ENABLED=0
            echo "NAS backup disabled (the repository on the NAS and ${SNAP_RESTIC_PW} are kept)."
            ;;
        run)
            systemctl start --no-block btrfs-snapshot-nas.service
            echo "Started; follow with: journalctl -fu btrfs-snapshot-nas"
            ;;
        *) echo "nas: setup|status|disable|run" >&2; exit 2 ;;
    esac
}

# The normal retention by hand, including what the circuit breaker held back
# (e.g. after a shorter HOME_PRESERVE/WS_PRESERVE in snapshots.conf): a dry
# run first; --confirm deletes. Pinned snapshots, the 3 newest of each source
# and those of the last 24 h are always kept.
snap_prune() {
    local rc=0
    [[ -x "${SNAP_RUNTIME}" ]] || { echo "Not set up: ${PROG} snapshots setup" >&2; exit 1; }
    case "${1:-}" in
        --confirm) "${SNAP_RUNTIME}" prune --allow-mass-delete || rc=$? ;;
        "")
            "${SNAP_RUNTIME}" prune --dry-run --allow-mass-delete || rc=$?
            (( rc == 0 )) && echo "Dry run, nothing deleted. Delete the above with: ${PROG} snapshots prune --confirm" ;;
        *) echo "snapshots prune [--confirm]" >&2; exit 2 ;;
    esac
    return "${rc}"
}

snapshots_cmd() {
    local action="${1:-status}"
    [[ $# -gt 0 ]] && shift
    case "${action}" in
        setup) snap_setup "$@" ;;
        status) snap_status ;;
        list)
            [[ -x "${SNAP_RUNTIME}" ]] || { echo "Not set up: ${PROG} snapshots setup" >&2; exit 1; }
            "${SNAP_RUNTIME}" list "$@" ;;
        prune) snap_prune "$@" ;;
        clean-targets) snap_clean_targets "$@" ;;
        disable) snap_disable "$@" ;;
        nas) snap_nas "$@" ;;
        chrome-cache)
            case "${1:-status}" in
                status|apply|off) snap_chrome "${1:-status}" ;;
                *) echo "snapshots chrome-cache: status|apply|off" >&2; exit 2 ;;
            esac ;;
        *) echo "snapshots: setup|status|list [--json]|prune [--confirm]|clean-targets|disable|nas|chrome-cache" >&2; exit 2 ;;
    esac
}
