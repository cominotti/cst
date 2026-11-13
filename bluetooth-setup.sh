#!/usr/bin/env bash
#
# bluetooth-setup.sh  (the `bluetooth` module of CST: run it as `cst bluetooth ...`)
#
# Bluetooth audio of this workstation: LE Audio (BAP/LC3) in BlueZ, and an
# adaptive LDAC quality for the headset (by default the JBL Live Beam 4) when
# it is connected over A2DP. LE Audio is kept OFF in PipeWire by default
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
#   --device MAC           the headset (default: DEVICE in the settings)
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
#                          print the commands instead
#
# Settings: bluetooth/bluetooth.conf in CST (the defaults), then
# ~/.config/cst/bluetooth.conf (yours, wins); the options win over both.
# Not as root: apply and revert run as your user (with sudo they switch back
# to the user who called sudo, so no file in your home is owned by root).

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SRC="$(dirname "${SELF}")/bluetooth"
HELPER="${SRC}/bt-helper"
DEFAULT_SETTINGS="${SRC}/bluetooth.conf"
# The kernel's experimental "ISO socket" feature (also in bt-helper).
ISO_UUID="6fbaf188-05e0-496a-9885-d6ddfdb4e03e"
WP_MARKER="# managed-by: bluetooth-setup.sh (CST)"
OLD_WP_MARKER="Gerado por apply-le-audio.sh"
# A2DP Audio Sink: the one profile reconnected after a WirePlumber restart.
A2DP_SINK_UUID="0000110b-0000-1000-8000-00805f9b34fb"
# The LDAC rate watcher (bluetooth/bt-ldac-watch): installed as a copy, so
# the service does not depend on this working copy (also in bt-helper).
LW_UNIT="cst-bt-ldac48.service"
LW_SRC="${SRC}/bt-ldac-watch"

# Shown in the help and in hints: "cst bluetooth" when run through the cst
# command (it sets CST_PROG), else the name this script was started with.
PROG="${CST_PROG:-$0}"

usage() {
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    echo "Usage: ${PROG} <command> [options]"
}
in_container() { [[ -e /run/.containerenv || -e /.dockerenv ]]; }
die() { echo "${PROG}: ERROR: $*" >&2; exit 1; }
bad_usage() { echo "${PROG} ${cmd}: $*" >&2; exit 2; }
helper() { python3 -I "${HELPER}" "$@"; }

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift
orig_args=("${cmd}" "$@")

opt_device="" opt_quality="" opt_bearer="" bearer_given=0 opt_le="" le_set=""
FORCE=0 YES=0 NO_RESTART=0 CHECK=0
case "${cmd}" in
    help|-h|--help) usage; exit 0 ;;
    plan|apply|status|revert) ;;
    le-audio)
        # The value is positional: le-audio on|off. Without it: show the setting.
        if [[ $# -gt 0 && "$1" != --* ]]; then
            le_set="$1"
            shift
            [[ "${le_set}" == on || "${le_set}" == off ]] ||
                { echo "${PROG} le-audio: say on or off (got '${le_set}')" >&2; exit 2; }
        fi
        ;;
    *) echo "Unknown command: ${cmd}" >&2; usage >&2; exit 2 ;;
esac
# allowed OPTION: whether this command takes OPTION.
allowed() {
    case "${cmd}:$1" in
        le-audio:--device) return 1 ;;
        *:--device) return 0 ;;
        plan:--ldac-quality|apply:--ldac-quality|status:--ldac-quality) return 0 ;;
        plan:--bearer|apply:--bearer) return 0 ;;
        plan:--le-audio|apply:--le-audio|status:--le-audio) return 0 ;;
        apply:--force) return 0 ;;
        apply:--yes|revert:--yes|apply:--no-restart|revert:--no-restart|revert:--check) return 0 ;;
        le-audio:--yes|le-audio:--no-restart) return 0 ;;
    esac
    return 1
}
while [[ $# -gt 0 ]]; do
    opt="${1%%=*}"
    allowed "${opt}" || bad_usage "unknown option '$1' (see ${PROG} help)"
    case "$1" in
        --device|--ldac-quality|--bearer|--le-audio)
            [[ $# -ge 2 && "$2" != --* ]] || bad_usage "$1 needs a value"
            val="$2"
            shift
            ;;
        --*=*) val="${1#*=}" ;;
        *) val="" ;;
    esac
    case "${opt}" in
        --device) opt_device="${val}" ;;
        --ldac-quality) opt_quality="${val}" ;;
        --bearer) opt_bearer="${val}"; bearer_given=1 ;;
        --le-audio) opt_le="${val}" ;;
        --force) FORCE=1 ;;
        --yes) YES=1 ;;
        --no-restart) NO_RESTART=1 ;;
        --check) CHECK=1 ;;
    esac
    shift
done

# ---- who runs it -----------------------------------------------------------------
# The WirePlumber rule and restart belong to the user's session; only
# main.conf and bluetooth.service need root, and those calls use sudo. Run as
# root through sudo (also by `cst status`), it starts again as that user.
if [[ ${EUID} -eq 0 ]]; then
    user="${SUDO_USER:-}"
    [[ -n "${user}" && "${user}" != root ]] ||
        die "run it from your user account (needs SUDO_USER): the WirePlumber parts belong to your session; it calls sudo by itself for main.conf"
    uid="$(id -u "${user}")"
    env_args=("HOME=$(getent passwd "${user}" | cut -d: -f6)" "USER=${user}" "LOGNAME=${user}"
              "XDG_RUNTIME_DIR=/run/user/${uid}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus")
    [[ -n "${CST_PROG:-}" ]] && env_args+=("CST_PROG=${CST_PROG}")
    exec runuser -u "${user}" -- env -u XDG_CONFIG_HOME -u SUDO_USER "${env_args[@]}" "${SELF}" "${orig_args[@]}"
fi
# revert --check and le-audio without on/off only read, like plan.
if [[ "${cmd}" == apply || ( "${cmd}" == revert && ${CHECK} -eq 0 ) || ( "${cmd}" == le-audio && -n "${le_set}" ) ]] &&
    in_container; then
    echo "'${PROG} ${orig_args[*]}' must run on the host, not inside a toolbox/container." >&2
    exit 1
fi

# Inside a toolbox the host's /etc is at /run/host/etc and the host's tools
# are reached through flatpak-spawn (read-only use: apply never runs there).
ETC=/etc
USR_SHARE=/usr/share
if in_container; then ETC=/run/host/etc; USR_SHARE=/run/host/usr/share; fi
MAIN_CONF="${ETC}/bluetooth/main.conf"
hostcmd() {
    if in_container; then flatpak-spawn --host "$@"; else "$@"; fi
}

# Seconds that each short bluetoothctl call (bt_info and the bearer change)
# may take before it is stopped, so a stuck bluetoothd cannot hang the
# script. 0 calls bluetoothctl directly, without timeout: the tests use it to
# reach their bluetoothctl shell function (timeout only runs programs).
# The value is read as base 10, so a leading zero ("08") means 8 seconds:
# without that, bash would read it as octal inside (( )) and fail.
BT_TIMEOUT="${BT_TIMEOUT:-5}"
[[ "${BT_TIMEOUT}" =~ ^[0-9]{1,9}$ ]] || { echo "BT_TIMEOUT must be a whole number of seconds (got '${BT_TIMEOUT}')" >&2; exit 2; }
BT_TIMEOUT=$((10#${BT_TIMEOUT}))

# ---- settings ----------------------------------------------------------------------
USER_SETTINGS="${XDG_CONFIG_HOME:-${HOME}/.config}/cst/bluetooth.conf"
S_DEVICE="" S_LDAC_QUALITY="" S_BEARER="" S_LDAC_RATE="" S_LE_AUDIO=""
while IFS='=' read -r k v; do
    case "${k}" in
        DEVICE) S_DEVICE="${v}" ;;
        LDAC_QUALITY) S_LDAC_QUALITY="${v}" ;;
        BEARER) S_BEARER="${v}" ;;
        LDAC_RATE) S_LDAC_RATE="${v}" ;;
        LE_AUDIO) S_LE_AUDIO="${v}" ;;
    esac
done < <(helper settings "${DEFAULT_SETTINGS}" "${USER_SETTINGS}")
# The keys your own file sets (plan and status say where a setting came from).
USER_KEYS=" $(helper settings-keys "${USER_SETTINGS}" | tr '\n' ' ')"
user_sets() { [[ "${USER_KEYS}" == *" $1 "* ]]; }
device_from="settings"
DEVICE_MAC="${opt_device:-${S_DEVICE}}"
[[ -n "${opt_device}" ]] && device_from="--device"
LDAC_QUALITY="${opt_quality:-${S_LDAC_QUALITY:-auto}}"
BEARER="${S_BEARER}"
(( bearer_given )) && BEARER="${opt_bearer}"
LDAC_RATE="${S_LDAC_RATE}"
LE_AUDIO="${opt_le:-${S_LE_AUDIO:-off}}"
le_from="the CST default"
user_sets LE_AUDIO && le_from="${USER_SETTINGS/#"${HOME}"/\~}"
[[ -n "${opt_le}" ]] && le_from="--le-audio, this run only"
# le-audio on|off sets it; a broken LE_AUDIO in the settings must not stop that.
[[ -n "${le_set}" ]] && LE_AUDIO="${le_set}"

case "${BEARER}" in ""|le|bredr|last-used) ;; *) bad_usage "the bearer must be le, bredr or last-used (got '${BEARER}')" ;; esac
case "${LDAC_RATE}" in ""|48000) ;; *) bad_usage "LDAC_RATE must be empty (watcher off) or 48000 (got '${LDAC_RATE}' in the settings)" ;; esac
case "${LE_AUDIO}" in on|off) ;; *) bad_usage "LE Audio must be on or off (got '${LE_AUDIO}'; LE_AUDIO in the settings or --le-audio)" ;; esac
case "${LDAC_QUALITY}" in auto|hq|sq|mq) ;; *) bad_usage "the LDAC quality must be auto, hq, sq or mq (got '${LDAC_QUALITY}')" ;; esac
[[ "${DEVICE_MAC}" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] ||
    bad_usage "the device must be a MAC address like AA:BB:CC:DD:EE:FF (got '${DEVICE_MAC}'; set DEVICE in ${USER_SETTINGS} or use --device)"
DEVICE_MAC="${DEVICE_MAC^^}"
DEVICE_ID="${DEVICE_MAC//:/_}"
WP_CONF_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/wireplumber/wireplumber.conf.d"
WP_CONF="${WP_CONF_DIR}/50-bluez-${DEVICE_ID}.conf"
# The very first name of apply-le-audio.sh's rule: removed when found.
LEGACY_WP_CONF="${WP_CONF_DIR}/50-jbl-live-beam-4.conf"
# LE_AUDIO=off: PipeWire offers BlueZ only the classic roles (all devices).
LE_CONF="${WP_CONF_DIR}/90-cst-le-audio-off.conf"
LE_ON_HINT="${PROG} le-audio on"
LE_OFF_HINT="${PROG} le-audio off"
# Where WirePlumber looks for fragments, highest priority first (the same
# file name in a later folder is hidden by an earlier one).
WP_FRAGMENT_DIRS=("${WP_CONF_DIR}" "${ETC}/xdg/wireplumber/wireplumber.conf.d"
                  "${ETC}/wireplumber/wireplumber.conf.d" "${USR_SHARE}/wireplumber/wireplumber.conf.d")
# The unit's ExecStart names the program as %h/.local/lib/cst/bt-ldac-watch.
LW_PROG="${HOME}/.local/lib/cst/bt-ldac-watch"
LW_UNIT_FILE="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user/${LW_UNIT}"

TMP="$(mktemp)"
BACKUP=""
trap 'rm -f "${TMP}"' EXIT
# Only for the commands that change something (revert --check exits 10 on purpose).
if [[ "${cmd}" == apply || ( "${cmd}" == revert && ${CHECK} -eq 0 ) ]]; then
    trap 'echo "${PROG}: stopped at line ${LINENO}. Backup of main.conf: ${BACKUP:-none taken}. Undo: ${PROG} revert" >&2' ERR
fi

# ---- output ------------------------------------------------------------------------
# 52: the widest label, wireplumber.conf.d/50-bluez-<ID>.conf (50), still fits.
item() { printf '  %-9s %-52s %s\n' "$1" "$2" "${3:-}"; }
section() { printf '\n== %s\n' "$1"; }
FAILS=0
WARNS=0
check_ok() { item ok "$1" "${2:-}"; }
check_bad() { item BAD "$1" "${2:-}"; FAILS=$((FAILS + 1)); }
check_warn() { item warn "$1" "${2:-}"; WARNS=$((WARNS + 1)); }
restart_hint() { echo "sudo systemctl restart bluetooth && systemctl --user restart wireplumber"; }
# le_off_active: the LE Audio drop-in of CST is in place.
le_off_active() {
    [[ -f "${LE_CONF}" ]] && grep -qF -e "${WP_MARKER}" "${LE_CONF}"
}
wp_label() { echo "wireplumber.conf.d/$(basename "$1")"; }

# ---- facts (read-only) ---------------------------------------------------------------
wait_for() { # wait_for SECONDS CMD...
    local t="$1" i
    shift
    for ((i = 0; i < t * 2; i++)); do "$@" >/dev/null 2>&1 && return 0; sleep 0.5; done
    return 1
}

adapter_path() { # the first object with org.bluez.Adapter1
    hostcmd busctl --no-pager -j call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects 2>/dev/null |
        helper adapter-path
}

# bap_endpoint_state: whether your session's WirePlumber (through PipeWire)
# offers the running bluetoothd an LE Audio LC3 sink. Prints one word:
#   registered    the last BAPSink event of one of its bus connections
#   unregistered  its last event was the removal
#   no-record     the journal has no event of it (rotated away)
#   unknown       no bluetoothd or no WirePlumber found
# Only events of the session's WirePlumber count: test sessions that run a
# WirePlumber of their own (dbus-run-session) register and drop the same
# endpoint path, and fooled a plain "last event" check.
bap_endpoint_state() {
    local inv wp_pid senders events last
    inv="$(hostcmd systemctl show -p InvocationID --value bluetooth 2>/dev/null || true)"
    wp_pid="$(hostcmd systemctl --user show -p MainPID --value wireplumber 2>/dev/null || true)"
    if [[ -z "${inv}" || ! "${wp_pid}" =~ ^[1-9][0-9]*$ ]]; then echo unknown; return; fi
    senders="$(hostcmd busctl --system list --no-legend --unique 2>/dev/null | awk -v p="${wp_pid}" '$2 == p {print $1}')"
    if [[ -z "${senders}" ]]; then echo unknown; return; fi
    events="$(hostcmd journalctl "_SYSTEMD_INVOCATION_ID=${inv}" --no-pager -o cat 2>/dev/null |
        grep -E 'Endpoint (un)?registered:.*MediaEndpointLE/BAPSink/lc3' || true)"
    last="$(grep -F -f <(sed 's/^/sender=/; s/$/ /' <<<"${senders}") <<<"${events}" | tail -n1 || true)"
    case "${last}" in
        "Endpoint registered:"*) echo registered ;;
        "") echo no-record ;;
        *) echo unregistered ;;
    esac
}
bap_endpoint_registered() { [[ "$(bap_endpoint_state)" == registered ]]; }

# card_state: "PROFILE|A2DP_AVAILABLE|MIC_IN_USE" of the headset in PipeWire.
card_state() {
    local cards srcs outs
    command -v pactl >/dev/null 2>&1 || return 1
    cards="$(pactl -f json list cards 2>/dev/null)" || return 1
    srcs="$(pactl -f json list short sources 2>/dev/null)" || return 1
    outs="$(pactl -f json list source-outputs 2>/dev/null)" || return 1
    printf '[%s,%s,%s]' "${cards}" "${srcs}" "${outs}" | helper card-state "${DEVICE_ID}"
}

bt_info() {
    if (( BT_TIMEOUT > 0 )); then
        hostcmd timeout "${BT_TIMEOUT}" bluetoothctl info "${DEVICE_MAC}" </dev/null 2>/dev/null || true
    else
        hostcmd bluetoothctl info "${DEVICE_MAC}" </dev/null 2>/dev/null || true
    fi
}

# ---- what apply would do ----------------------------------------------------------------
# compute_plan: sets MC_CHANGED (main.conf, new text in TMP), WP_STATE,
# LEGACY (0/1), the LE_* of le_plan, BEARER_STATE (none|ok|set|unpaired|
# unknown) and the restart needs BT_RESTART / WP_RESTART.
compute_plan() {
    local rc=0 info
    [[ -f "${MAIN_CONF}" ]] || die "${MAIN_CONF} does not exist (is the bluez package installed?)"
    [[ -L "${MAIN_CONF}" ]] && die "${MAIN_CONF} is a symlink: refusing to edit it"
    helper general-apply "${MAIN_CONF}" >"${TMP}" || rc=$?
    case "${rc}" in
        0) MC_CHANGED=0 ;;
        10) MC_CHANGED=1 ;;
        *) die "could not process ${MAIN_CONF} (rc=${rc})" ;;
    esac
    if [[ -L "${WP_CONF}" ]]; then
        WP_STATE=conflict
    else
        WP_STATE="$(helper wp-state "${WP_CONF}" "${DEVICE_MAC}" "${LDAC_QUALITY}")"
    fi
    LEGACY=0
    [[ -f "${LEGACY_WP_CONF}" && "${LEGACY_WP_CONF}" != "${WP_CONF}" ]] && LEGACY=1
    le_plan
    BEARER_STATE=none
    if [[ -n "${BEARER}" ]]; then
        if ! command -v bluetoothctl >/dev/null 2>&1 && ! in_container; then
            BEARER_STATE=unknown
        else
            info="$(bt_info)"
            if [[ "${info}" != *"Paired: yes"* ]]; then
                BEARER_STATE=unpaired
            elif grep -Eq "^[[:space:]]*PreferredBearer: ${BEARER}[[:space:]]*$" <<<"${info}"; then
                BEARER_STATE=ok
            else
                BEARER_STATE="set"
            fi
        fi
    fi
    lw_plan
    BT_RESTART=0
    WP_RESTART=0
    (( MC_CHANGED || FORCE )) && BT_RESTART=1
    # A bluetoothd restart takes WirePlumber's endpoints with it: it is
    # restarted too. A comment-only renewal ("adopt") restarts nothing.
    if (( BT_RESTART || LEGACY || LE_CHANGE )) || [[ "${WP_STATE}" == missing || "${WP_STATE}" == change ]]; then
        WP_RESTART=1
    fi
}

# le_plan: the LE Audio drop-in. LE_STATE: with LE_AUDIO=off as wp-state
# (ok|adopt|change|missing|conflict); with LE_AUDIO=on: absent, remove (ours,
# apply removes it) or conflict (not ours: left alone). LE_CHANGE=1 when apply
# writes or removes it (WirePlumber is restarted then; not for "adopt").
le_plan() {
    LE_CHANGE=0
    if [[ "${LE_AUDIO}" == off ]]; then
        if [[ -L "${LE_CONF}" ]]; then
            LE_STATE=conflict
        else
            LE_STATE="$(helper le-state "${LE_CONF}")"
        fi
        [[ "${LE_STATE}" == missing || "${LE_STATE}" == change ]] && LE_CHANGE=1
    elif [[ ! -e "${LE_CONF}" && ! -L "${LE_CONF}" ]]; then
        LE_STATE=absent
    elif [[ ! -L "${LE_CONF}" ]] && grep -qF -e "${WP_MARKER}" "${LE_CONF}"; then
        LE_STATE=remove
        LE_CHANGE=1
    else
        LE_STATE=conflict
    fi
    return 0
}

# lw_plan: the LDAC rate watcher. Sets LW_WANT (LDAC_RATE set), LW_UNIT_ST
# and LW_PROG_ST (ok|change|missing|conflict), LW_ENABLED and LW_ACTIVE (as
# systemctl --user prints them; empty when unknown) and LW_CHANGE (apply would
# write, start, restart or remove something).
lw_plan() {
    LW_WANT=0
    [[ -n "${LDAC_RATE}" ]] && LW_WANT=1
    if [[ -L "${LW_UNIT_FILE}" ]]; then
        LW_UNIT_ST=conflict
    else
        LW_UNIT_ST="$(helper unit-state "${LW_UNIT_FILE}" "${DEVICE_MAC}" "${LDAC_RATE:-48000}")"
    fi
    if [[ -L "${LW_PROG}" ]]; then
        LW_PROG_ST=conflict
    elif [[ ! -e "${LW_PROG}" ]]; then
        LW_PROG_ST=missing
    elif cmp -s "${LW_SRC}" "${LW_PROG}"; then
        LW_PROG_ST=ok
    elif grep -qF -e "${WP_MARKER}" "${LW_PROG}"; then
        LW_PROG_ST=change
    else
        LW_PROG_ST=conflict
    fi
    LW_ENABLED="$(hostcmd systemctl --user is-enabled "${LW_UNIT}" 2>/dev/null || true)"
    LW_ACTIVE="$(hostcmd systemctl --user is-active "${LW_UNIT}" 2>/dev/null || true)"
    LW_CHANGE=0
    if (( LW_WANT )); then
        [[ "${LW_UNIT_ST}" == ok && "${LW_PROG_ST}" == ok && "${LW_ENABLED}" == enabled && "${LW_ACTIVE}" == active ]] ||
            LW_CHANGE=1
    elif lw_ours_present; then
        LW_CHANGE=1
    fi
}
# lw_ours_present: a unit or program of ours is installed.
lw_ours_present() {
    [[ "${LW_UNIT_ST}" == ok || "${LW_UNIT_ST}" == change || "${LW_PROG_ST}" == ok || "${LW_PROG_ST}" == change ]]
}
lw_unit_label() {
    echo "systemd/user/${LW_UNIT}"
}
lw_prog_label() {
    echo "${LW_PROG/#"${HOME}"/\~}"
}

wp_note() {
    case "$1" in
        ok) echo "up to date (LDAC ${LDAC_QUALITY} for ${DEVICE_MAC})" ;;
        adopt) echo "up to date; written by apply-le-audio.sh or with older comments: apply only renews the comments, no restart" ;;
        change) echo "differs (apply writes LDAC ${LDAC_QUALITY}; the old file is kept as .bak)" ;;
        missing) echo "missing" ;;
        conflict) echo "not written by CST or the old script (or a symlink): apply refuses; move it away by hand" ;;
    esac
}
wp_word() {
    case "$1" in
        ok|adopt) echo "$1" ;;
        change) echo update ;;
        missing) echo write ;;
        conflict) echo CONFLICT ;;
    esac
}

files_report() {
    local key st val
    section "files"
    while IFS=$'\t' read -r key st val; do
        if [[ "${st}" == ok ]]; then
            item ok "main.conf [General] ${key}" "${val}"
        elif [[ "${key}" == KernelExperimental ]]; then
            item update "main.conf [General] ${key}" "${val} -> ${ISO_UUID} (ISO socket)"
        else
            item update "main.conf [General] ${key}" "${val} -> true"
        fi
    done < <(helper general-show "${MAIN_CONF}")
    item "$(wp_word "${WP_STATE}")" "$(wp_label "${WP_CONF}")" "$(wp_note "${WP_STATE}")"
    (( LEGACY )) && item remove "$(wp_label "${LEGACY_WP_CONF}")" "older name of the same rule (apply-le-audio.sh)"
    le_report
    le_others_report
    case "${BEARER_STATE}" in
        none) item ok "preferred bearer" "left alone (no BEARER set)" ;;
        ok) item ok "preferred bearer" "already ${BEARER}" ;;
        set) item set "preferred bearer" "-> ${BEARER} (only for connections the PC starts)" ;;
        unpaired) item skip "preferred bearer" "${DEVICE_MAC} is not paired: apply leaves it" ;;
        unknown) item "?" "preferred bearer" "bluetoothctl not found" ;;
    esac
    if [[ "${BEARER}" == le && "${LE_AUDIO}" == off ]]; then
        item warn "preferred bearer" "BEARER=le while LE Audio is off: connections the PC starts get no audio profile; use bredr"
    fi
    lw_report
    return 0
}

# le_report: the LE Audio drop-in's line of plan and status.
le_report() {
    local label
    label="$(wp_label "${LE_CONF}")"
    case "${LE_AUDIO}:${LE_STATE}" in
        off:ok) item ok "${label}" "LE Audio off (${le_from}): PipeWire offers only A2DP and HFP" ;;
        off:adopt) item adopt "${label}" "LE Audio off; older comments: apply only renews them, no restart" ;;
        off:change) item update "${label}" "differs (apply rewrites it: LE Audio off; restarts WirePlumber)" ;;
        off:missing) item write "${label}" "LE Audio off (${le_from}): PipeWire will offer only A2DP and HFP" ;;
        off:conflict) item CONFLICT "${label}" "not written by CST (or a symlink): apply refuses; move it away by hand" ;;
        on:absent) item ok "LE Audio" "on (${le_from}): PipeWire offers the bap-* profiles too" ;;
        on:remove) item remove "${label}" "LE Audio on (${le_from}): apply removes it and restarts WirePlumber" ;;
        on:conflict) item CONFLICT "${label}" "not written by CST: left alone (it may keep LE Audio off)" ;;
    esac
    return 0
}

# le_others_report: with LE Audio off, the other WirePlumber files that set
# monitor.bluez.properties. One read before the CST file loses those settings
# (the CST file replaces the whole section); one read after it can add the LE
# Audio roles back (status then checks the roles WirePlumber really uses).
le_others_report() {
    local where path
    [[ "${LE_AUDIO}" == off ]] || return 0
    while IFS='|' read -r where path; do
        [[ -n "${path}" ]] || continue
        if [[ "${where}" == before ]]; then
            item warn "${path/#"${HOME}"/\~}" "also sets monitor.bluez.properties: while LE Audio is off the CST file replaces that section, so its settings there do not apply"
        else
            item warn "${path/#"${HOME}"/\~}" "sets monitor.bluez.properties after the CST file: it can bring the LE Audio roles back (status checks them)"
        fi
    done < <(helper wp-others "$(basename "${LE_CONF}")" "${WP_FRAGMENT_DIRS[@]}")
    return 0
}

# wp_roles: STATE|VALUE of bluez5.roles as WirePlumber resolves all its
# config files now (bt-helper wp-roles, through libwireplumber, read-only; on
# the host, where the library is). Fails when it could not be read.
wp_roles() {
    hostcmd env XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}" python3 -I "${HELPER}" wp-roles 2>/dev/null
}

# le_roles_check: whether the roles WirePlumber will offer BlueZ match
# LE_AUDIO. Text checks of the CST file alone missed a file WirePlumber read
# differently from what was meant (README, "LE Audio: off by default").
le_roles_check() {
    local r st val label="WirePlumber bluez5.roles"
    if ! r="$(wp_roles)"; then
        check_warn "${label}" "could not be read (libwireplumber-0.5 not found on the host)"
        return 0
    fi
    IFS='|' read -r st val <<<"${r}"
    case "${LE_AUDIO}:${st}" in
        off:classic) check_ok "${label}" "${val} (no LE Audio roles)" ;;
        off:le) check_bad "${label}" "${val}: LE Audio roles in it although LE Audio is off; another WirePlumber file adds them (see the files above)" ;;
        off:default)
            if le_off_active; then
                check_bad "${label}" "not set: WirePlumber does not use the CST file, so PipeWire's defaults (with LE Audio) apply"
            else
                item note "${label}" "not set yet (PipeWire's defaults, with LE Audio): ${PROG} apply writes the CST file"
            fi
            ;;
        on:classic) check_warn "${label}" "${val}: no LE Audio roles although LE Audio is on (a WirePlumber file other than CST's sets them)" ;;
        *) check_ok "${label}" "${val:-not set: the PipeWire defaults} (LE Audio roles included)" ;;
    esac
}

# lw_report: the watcher's lines of plan and status.
lw_report() {
    local svc="${LW_UNIT} (your session)"
    if (( ! LW_WANT )); then
        if lw_ours_present; then
            item remove "LDAC rate watcher" "LDAC_RATE is empty: apply stops and removes ${LW_UNIT} and its program"
        else
            item ok "LDAC rate watcher" "off (LDAC_RATE empty; 48000 turns it on)"
        fi
        [[ "${LW_UNIT_ST}" == conflict ]] && item CONFLICT "$(lw_unit_label)" "not written by CST: left alone"
        [[ "${LW_PROG_ST}" == conflict ]] && item CONFLICT "$(lw_prog_label)" "not written by CST: left alone"
        return 0
    fi
    if user_sets LDAC_RATE; then
        item note "LDAC rate watcher" "on because ${USER_SETTINGS/#"${HOME}"/\~} sets LDAC_RATE=${LDAC_RATE} (CST default: off)"
        item note "" "CST keeps it off until it is checked again on PipeWire 1.6.11; to turn it off: remove LDAC_RATE from that file, then ${PROG} apply"
    fi
    # plan only: status (and apply) report the last failure among the live checks.
    if [[ "${cmd}" == plan ]] && lw_last_failure; then
        item warn "LDAC rate watcher" "its last attempt failed at ${LW_FAIL_WHEN}: ${LW_FAIL_REASON}${LW_FAIL_DETAIL:+ (${LW_FAIL_DETAIL})}"
    fi
    case "${LW_UNIT_ST}" in
        ok) item ok "$(lw_unit_label)" "up to date (LDAC to ${LDAC_RATE} Hz)" ;;
        change) item update "$(lw_unit_label)" "differs (apply rewrites it)" ;;
        missing) item write "$(lw_unit_label)" "the LDAC rate watcher (LDAC_RATE=${LDAC_RATE})" ;;
        conflict) item CONFLICT "$(lw_unit_label)" "not written by CST (or a symlink): apply refuses; move it away by hand" ;;
    esac
    case "${LW_PROG_ST}" in
        ok) item ok "$(lw_prog_label)" "same as ${LW_SRC##*/} in CST" ;;
        change) item update "$(lw_prog_label)" "older copy (apply installs the one in CST)" ;;
        missing) item write "$(lw_prog_label)" "copy of bluetooth/bt-ldac-watch" ;;
        conflict) item CONFLICT "$(lw_prog_label)" "not written by CST (or a symlink): apply refuses; move it away by hand" ;;
    esac
    if [[ "${LW_ENABLED}" == enabled && "${LW_ACTIVE}" == active ]]; then
        if [[ "${LW_UNIT_ST}" == ok && "${LW_PROG_ST}" == ok ]]; then
            item ok "${svc}" "enabled, active"
        else
            item restart "${svc}" "apply restarts it with the new files"
        fi
    else
        item enable "${svc}" "now ${LW_ENABLED:-not installed}, ${LW_ACTIVE:-inactive}: apply enables and starts it"
    fi
}

plan() {
    compute_plan
    echo "Dry run: nothing is changed. ok = nothing to do; adopt = apply only renews comments, restarts nothing;"
    echo "write/update/remove/set/restart = apply would change that; CONFLICT = apply stops; ? = could not be read;"
    echo "warn/note = for your information."
    header
    files_report
    section "restarts"
    if (( BT_RESTART )); then
        item restart "bluetooth.service" "main.conf changes: apply asks first (Bluetooth keyboards, mice and headsets drop for seconds)"
    else
        item ok "bluetooth.service" "no restart needed"
    fi
    if (( WP_RESTART )); then
        item restart "wireplumber (your session)" "audio stops for a moment; A2DP is restored afterwards"
        (( LE_CHANGE )) && item "" "" "(the LE Audio change takes effect only with this restart)"
    else
        item ok "wireplumber (your session)" "no restart needed"
    fi
    if (( MC_CHANGED )); then
        section "main.conf, as apply would write it"
        diff -u "${MAIN_CONF}" "${TMP}" | sed 's/^/    /' || true
    fi
    if [[ "${WP_STATE}" == change ]]; then
        section "$(wp_label "${WP_CONF}"), as apply would write it"
        diff -u "${WP_CONF}" <(helper wp-render "${DEVICE_MAC}" "${LDAC_QUALITY}") | sed 's/^/    /' || true
    fi
    if [[ "${LE_AUDIO}" == off && "${LE_STATE}" == change ]]; then
        section "$(wp_label "${LE_CONF}"), as apply would write it"
        diff -u "${LE_CONF}" <(helper le-render) | sed 's/^/    /' || true
    fi
    if [[ "${LW_WANT}" == 1 && "${LW_UNIT_ST}" == change ]]; then
        section "$(lw_unit_label), as apply would write it"
        diff -u "${LW_UNIT_FILE}" <(helper ldac-unit "${DEVICE_MAC}" "${LDAC_RATE}") | sed 's/^/    /' || true
    fi
    echo
    if (( MC_CHANGED || BT_RESTART || WP_RESTART || LEGACY || LW_CHANGE || LE_CHANGE )) ||
        [[ "${WP_STATE}" != ok || "${BEARER_STATE}" == set || "${LE_STATE}" == adopt ]]; then
        echo "Apply with: ${PROG} apply"
        return 10
    fi
    echo "Nothing to do."
    return 0
}

header() {
    echo "Headset ${DEVICE_MAC} (${device_from}), LDAC quality ${LDAC_QUALITY}, LE Audio ${LE_AUDIO} (${le_from}); host /etc in ${ETC}"
}

# ---- the live checks (status, and the end of apply) -------------------------------------
verify() {
    local adapter feats uuids dump q prc=0 prof rule codec quality rate
    section "services"
    if hostcmd systemctl is-active --quiet bluetooth; then check_ok bluetooth.service active; else check_bad bluetooth.service "not active"; fi
    if hostcmd systemctl --user is-active --quiet wireplumber; then
        check_ok "wireplumber (your session)" active
    else
        check_bad "wireplumber (your session)" "not active"
    fi
    # The watcher's state as lw_plan read it just before.
    if (( LW_WANT )); then
        if [[ "${LW_ACTIVE}" == active ]]; then
            check_ok "${LW_UNIT}" "active: LDAC back to ${LDAC_RATE} Hz when the headset sets up another rate"
        else
            check_warn "${LW_UNIT}" "${LW_ACTIVE:-not running} (LDAC_RATE=${LDAC_RATE}): ${PROG} apply; logs: journalctl --user -u ${LW_UNIT}"
        fi
    elif [[ "${LW_ACTIVE}" == active ]]; then
        check_warn "${LW_UNIT}" "running although LDAC_RATE is empty: ${PROG} apply stops and removes it"
    fi
    if (( LW_WANT )) || lw_ours_present; then lw_failure_check; fi

    section "LE Audio"
    if [[ "${LE_AUDIO}" == off ]]; then
        if [[ "${LE_STATE}" == ok || "${LE_STATE}" == adopt ]]; then
            check_ok "LE Audio" "off (${le_from}; PipeWire offers only classic profiles)"
        else
            check_warn "LE Audio" "off in the settings (${le_from}), but its drop-in is not in place yet: ${PROG} apply"
        fi
        item "" "" "turn on with: ${LE_ON_HINT}"
    else
        check_ok "LE Audio" "on (${le_from})"
        item "" "" "turn off with: ${LE_OFF_HINT}"
    fi
    le_roles_check
    adapter="$(adapter_path || true)"
    if [[ -z "${adapter}" ]]; then
        check_bad "adapter" "no Bluetooth adapter in org.bluez"
    else
        # The ISO feature as bluetoothd switched it on for the adapter.
        feats="$(hostcmd busctl get-property org.bluez "${adapter}" org.bluez.Adapter1 ExperimentalFeatures 2>/dev/null || true)"
        if [[ "${feats,,}" == *"${ISO_UUID}"* ]]; then
            check_ok "adapter ISO feature" "on for ${adapter}"
        else
            check_bad "adapter ISO feature" "${ISO_UUID} not on for ${adapter} (KernelExperimental not in effect? restart bluetooth)"
        fi
        # AF_BLUETOOTH=31, SOCK_SEQPACKET=5, BTPROTO_ISO=8. The kernel flag is
        # global and lasts until reboot, hence also the adapter check above.
        if hostcmd /usr/bin/python3 -I -c 'import socket; socket.socket(31, 5, 8).close()' 2>/dev/null; then
            check_ok "kernel ISO socket" "opens"
        else
            check_bad "kernel ISO socket" "not available"
        fi
        # BlueZ's BAP plugin publishes PACS: Sink PAC 0x2bc9, Source PAC 0x2bcb.
        uuids="$(hostcmd busctl get-property org.bluez "${adapter}" org.bluez.Media1 SupportedUUIDs 2>/dev/null || true)"
        if [[ "${uuids,,}" == *00002bc9* && "${uuids,,}" == *00002bcb* ]]; then
            check_ok "BAP in BlueZ" "Sink and Source PAC in Media1"
        else
            check_bad "BAP in BlueZ" "not in Media1 (Experimental not in effect? restart bluetooth)"
        fi
    fi
    if [[ "${LE_AUDIO}" == off ]]; then
        case "$(bap_endpoint_state)" in
            registered)
                if le_off_active; then
                    check_warn "PipeWire BAP LC3 endpoint" "still registered: LE Audio off takes effect after systemctl --user restart wireplumber"
                else
                    check_warn "PipeWire BAP LC3 endpoint" "still registered: LE Audio off takes effect after ${PROG} apply"
                fi
                ;;
            unregistered|no-record) check_ok "PipeWire BAP LC3 endpoint" "not registered (as wanted with LE Audio off)" ;;
            *) check_warn "PipeWire BAP LC3 endpoint" "not confirmed (no bluetoothd or WirePlumber found)" ;;
        esac
    else
        case "$(bap_endpoint_state)" in
            registered) check_ok "PipeWire BAP LC3 endpoint" "registered by your WirePlumber with the running bluetoothd" ;;
            no-record) check_warn "PipeWire BAP LC3 endpoint" "not confirmed: the journal no longer has your WirePlumber's registration" ;;
            unregistered) check_warn "PipeWire BAP LC3 endpoint" "your WirePlumber removed it (journalctl -u bluetooth | grep BAPSink)" ;;
            *) check_warn "PipeWire BAP LC3 endpoint" "not confirmed (no bluetoothd or WirePlumber found)" ;;
        esac
    fi
    bearer_check "$(bt_info)"

    section "headset ${DEVICE_MAC} in PipeWire"
    dump="$(hostcmd timeout 10 pw-dump 2>/dev/null || true)"
    if [[ -z "${dump}" ]]; then
        check_warn "pw-dump" "no answer: PipeWire stuck? (systemctl --user restart pipewire wireplumber)"
        return 0
    fi
    q="$(helper pw-parse "${DEVICE_ID}" <<<"${dump}")" || prc=$?
    case "${prc}" in
        0)
            IFS='|' read -r prof rule codec quality rate <<<"${q}"
            check_ok "headset" "profile ${prof}, rule ldac.quality=${rule}, node codec ${codec}, quality ${quality}, rate ${rate} Hz"
            [[ "${LE_AUDIO}" == off && "${prof}" == bap* ]] &&
                check_warn "headset profile" "${prof} although LE Audio is off: restart WirePlumber (systemctl --user restart wireplumber)"
            [[ "${rule}" == "${LDAC_QUALITY}" ]] ||
                check_warn "LDAC rule" "not loaded on the device (want ${LDAC_QUALITY}; reconnect the headset)"
            if [[ "${codec}" == ldac && "${quality}" != "${LDAC_QUALITY}" ]]; then
                check_warn "LDAC quality" "LDAC is on at ${quality} (want ${LDAC_QUALITY}; reconnect the headset)"
            fi
            # Above 48 kHz: set up by the headset (README, "LDAC sample rate").
            if [[ "${codec}" == ldac && "${rate}" =~ ^[0-9]+$ ]] && (( rate > 48000 )); then
                if (( LW_WANT )) && [[ "${LW_ACTIVE}" == active ]]; then
                    check_warn "LDAC sample rate" "${rate} Hz: the watcher switches it to ${LDAC_RATE} within seconds, unless it gave up (journalctl --user -u ${LW_UNIT})"
                elif (( LW_WANT )); then
                    check_warn "LDAC sample rate" "${rate} Hz, set up by the headset; the watcher (LDAC_RATE=${LDAC_RATE}) is not running"
                else
                    check_warn "LDAC sample rate" "${rate} Hz, set up by the headset: fewer bits per sample at the same bitrate; LDAC_RATE=48000 turns on the watcher (see bluetooth/README.md)"
                fi
            fi
            ;;
        4) check_warn "headset" "not in PipeWire now (connect it to check LDAC/BAP)" ;;
        *) check_warn "pw-dump" "could not read its output (rc=${prc})" ;;
    esac
    return 0
}

# bearer_check INFO: with LE Audio off, a headset whose PreferredBearer is le
# (bluetoothctl info INFO) connects over LE when the PC starts the connection,
# and gets no audio profile there.
bearer_check() {
    local pb
    [[ "${LE_AUDIO}" == off ]] || return 0
    pb="$(sed -n 's/^[[:space:]]*PreferredBearer:[[:space:]]*\([^[:space:]]*\).*/\1/p' <<<"$1" | head -n1)"
    [[ "${pb}" == le ]] || return 0
    check_warn "preferred bearer" "${DEVICE_MAC} prefers LE while LE Audio is off: ${PROG} apply --bearer bredr (or BEARER=bredr in the settings)"
}

# lw_last_failure: 0 when the watcher's last outcome in its log (read-only)
# was a failure; sets LW_FAIL_WHEN, LW_FAIL_REASON and LW_FAIL_DETAIL.
lw_last_failure() {
    local f
    f="$(hostcmd journalctl --user -u "${LW_UNIT}" -o short-iso --no-pager -n 2000 2>/dev/null | helper lw-failure)" || return 1
    IFS='|' read -r LW_FAIL_WHEN LW_FAIL_REASON LW_FAIL_DETAIL <<<"${f}"
}

# lw_failure_check: a warning with the last failure, and how to turn it off.
lw_failure_check() {
    lw_last_failure || return 0
    check_warn "LDAC rate watcher" "its last attempt failed at ${LW_FAIL_WHEN}: ${LW_FAIL_REASON}${LW_FAIL_DETAIL:+ (${LW_FAIL_DETAIL})}"
    if user_sets LDAC_RATE; then
        item "" "" "to turn it off: remove LDAC_RATE from ${USER_SETTINGS/#"${HOME}"/\~}, then ${PROG} apply"
    else
        item "" "" "to turn it off: make LDAC_RATE empty in the settings, then ${PROG} apply"
    fi
}

status() {
    compute_plan
    header
    files_report
    verify
    echo
    if (( FAILS )); then
        echo "${FAILS} check(s) failed, ${WARNS} warning(s)."
        return 1
    fi
    echo "All checks passed$( (( WARNS )) && echo ", ${WARNS} warning(s)")."
    if (( MC_CHANGED || LEGACY || LW_CHANGE || LE_CHANGE )) || [[ "${WP_STATE}" != ok && "${WP_STATE}" != adopt ]] ||
        [[ "${LE_STATE}" == conflict ]]; then
        echo "Some files or services differ from what apply sets up: ${PROG} plan"
    elif [[ "${WP_STATE}" == adopt || "${LE_STATE}" == adopt ]]; then
        echo "The WirePlumber rule has older comments: ${PROG} apply renews them (no restart)."
    fi
    return 0
}

# ---- changes (apply, revert) ------------------------------------------------------------
preconditions() {
    local bin
    for bin in bluetoothctl busctl systemctl python3 journalctl timeout sudo; do
        command -v "${bin}" >/dev/null 2>&1 || die "command not found: ${bin}"
    done
    systemctl --user show-environment >/dev/null 2>&1 ||
        die "no systemd --user session (XDG_RUNTIME_DIR/D-Bus): run it in your desktop or login session"
}

# install_main_conf NEWFILE: backup, then the new main.conf atomically (root).
# BACKUP is set only once the copy exists (the ERR message names it).
install_main_conf() {
    local bak
    local -a z_opt=()
    bak="${MAIN_CONF}.bak-$(date +%Y%m%d-%H%M%S)"
    if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; then
        sudo cp -p --preserve=context "${MAIN_CONF}" "${bak}"
        z_opt=(-Z)
    else
        sudo cp -p "${MAIN_CONF}" "${bak}"
    fi
    BACKUP="${bak}"
    echo "backup:    ${BACKUP}"
    sudo install -m 0644 -o root -g root "${z_opt[@]}" "$1" "${MAIN_CONF}.new"
    sudo mv -f "${MAIN_CONF}.new" "${MAIN_CONF}"
    echo "written:   ${MAIN_CONF}"
}

# write_wp: the rule file, as the user, atomically. A previous version with
# other settings is kept as .bak (WirePlumber only reads *.conf).
write_wp() {
    local tmp
    mkdir -p "${WP_CONF_DIR}"
    if [[ "${WP_STATE}" == change ]]; then
        cp -p "${WP_CONF}" "${WP_CONF}.bak"
        echo "backup:    ${WP_CONF}.bak"
    fi
    tmp="$(mktemp -p "${WP_CONF_DIR}" ".$(basename "${WP_CONF}").XXXXXX")"
    helper wp-render "${DEVICE_MAC}" "${LDAC_QUALITY}" >"${tmp}"
    chmod 0644 "${tmp}"
    mv -f "${tmp}" "${WP_CONF}"
}

# The restarts run in an `||` list (see do_restarts), where bash ignores
# set -e and the ERR trap: each failure is checked and returned by hand.
restart_bluetooth() {
    echo "restart:   bluetooth.service (Bluetooth connections drop for a few seconds)"
    sudo systemctl restart bluetooth || {
        echo "${PROG}: ERROR: bluetooth.service was not restarted. To finish: $(restart_hint)" >&2
        return 1
    }
    wait_for 15 busctl get-property org.bluez "$(adapter_path || echo /org/bluez/hci0)" org.bluez.Adapter1 Powered ||
        echo "WARNING: the adapter did not answer within 15 s after the restart" >&2
}

restart_wireplumber() {
    echo "restart:   wireplumber (your session; audio stops for a moment)"
    systemctl --user restart wireplumber || {
        echo "${PROG}: ERROR: wireplumber was not restarted. To finish: systemctl --user restart wireplumber" >&2
        return 1
    }
    sleep 1 # let the old WirePlumber unregister before checking
    # With LE Audio off (the drop-in in place) there is no BAP endpoint to wait for.
    if ! le_off_active; then
        wait_for 15 bap_endpoint_registered ||
            echo "WARNING: WirePlumber did not register the BAP endpoint within 15 s" >&2
    fi
    restore_a2dp
}

# After a WirePlumber restart the headset's A2DP channel closes and only HFP
# comes back by itself: it sits in headset-head-unit (mSBC) without the
# a2dp-sink profile. Reconnect only the Audio Sink (no unpairing) and switch
# back to A2DP, unless someone uses the headset's microphone (a call).
restore_a2dp() {
    local info st prof a2dp mic
    info="$(bt_info)"
    grep -Eq '^[[:space:]]*BREDR\.Connected: yes' <<<"${info}" || return 0
    # Connected over LE too: with LE Audio on, LE may carry the audio; with
    # it off, A2DP is the only music profile, so it is restored anyway.
    if ! le_off_active; then grep -Eq '^[[:space:]]*LE\.Connected: yes' <<<"${info}" && return 0; fi
    wait_for 10 card_state || { echo "WARNING: the headset did not show up in PipeWire after the restart" >&2; return 0; }
    IFS='|' read -r prof a2dp mic <<<"$(card_state)"
    [[ "${prof}" == a2dp-sink* ]] && return 0
    if [[ "${mic}" == 1 ]]; then
        echo "WARNING: the headset is in ${prof} with its microphone in use; A2DP not restored (switch the profile after the call)" >&2
        return 0
    fi
    if [[ "${a2dp}" != 1 ]]; then
        echo "reconnect: the headset's A2DP profile (Audio Sink)"
        timeout 20 bluetoothctl connect "${DEVICE_MAC}" "${A2DP_SINK_UUID}" >/dev/null 2>&1 || true
        for ((st = 0; st < 20; st++)); do
            IFS='|' read -r prof a2dp mic <<<"$(card_state || echo '||')"
            [[ "${a2dp}" == 1 ]] && break
            sleep 0.5
        done
        [[ "${a2dp}" == 1 ]] || { echo "WARNING: A2DP did not come back; switch the headset off and on" >&2; return 0; }
    fi
    if [[ "${prof}" != a2dp-sink* ]] && ! pactl set-card-profile "bluez_card.${DEVICE_ID}" a2dp-sink 2>/dev/null; then
        echo "WARNING: could not switch the profile to a2dp-sink" >&2
        return 0
    fi
    echo "restored:  the headset is back on A2DP"
}

# confirm_bt_restart: 0 = go ahead. Asks on a terminal unless --yes.
confirm_bt_restart() {
    local a
    (( YES )) && return 0
    if [[ -t 0 ]]; then
        read -rp "Restart Bluetooth now? Bluetooth keyboards, mice and headsets will disconnect. [y/N] " a
        [[ "${a}" =~ ^[yY]$ ]] && return 0
        echo "Restart cancelled. To finish later: $(restart_hint)"
    else
        echo "No terminal to ask on: use --yes to restart. To finish later: $(restart_hint)"
    fi
    return 1
}

# do_restarts BT WP: the restarts, or the commands for later.
# 0 = done (or nothing to do), 1 = cancelled at the question, 2 = a restart
# failed (WirePlumber is not restarted after a failed bluetoothd restart).
do_restarts() {
    local bt="$1" wp="$2"
    (( bt || wp )) || return 0
    if (( NO_RESTART )); then
        if (( bt )); then
            echo "Not restarted (--no-restart). To finish: $(restart_hint)"
        else
            echo "Not restarted (--no-restart). To finish: systemctl --user restart wireplumber"
        fi
        return 0
    fi
    if (( bt )); then
        confirm_bt_restart || return 1
        restart_bluetooth || return 2
    fi
    restart_wireplumber || return 2
}

apply() {
    local out info rc=0 pending
    preconditions
    compute_plan
    [[ "${WP_STATE}" != conflict ]] ||
        die "${WP_CONF} was not written by CST or apply-le-audio.sh (or is a symlink): move it away, then run apply again."
    [[ "${LE_AUDIO}" != off || "${LE_STATE}" != conflict ]] ||
        die "${LE_CONF} was not written by CST (or is a symlink): move it away, then run apply again."
    if (( LW_WANT )); then
        [[ "${LW_UNIT_ST}" != conflict ]] || die "${LW_UNIT_FILE} was not written by CST (or is a symlink): move it away, then run apply again."
        [[ "${LW_PROG_ST}" != conflict ]] || die "${LW_PROG} was not written by CST (or is a symlink): move it away, then run apply again."
    fi

    echo "== BlueZ: ${MAIN_CONF}"
    if (( MC_CHANGED )); then
        install_main_conf "${TMP}"
        echo "           Experimental = true, KernelExperimental = ${ISO_UUID}"
    else
        echo "unchanged: ${MAIN_CONF} (Experimental and KernelExperimental already set in [General])"
    fi

    echo "== WirePlumber: ${WP_CONF}"
    if (( LEGACY )); then
        rm -f "${LEGACY_WP_CONF}"
        echo "removed:   ${LEGACY_WP_CONF} (older name of the same rule)"
    fi
    case "${WP_STATE}" in
        ok) echo "unchanged: ${WP_CONF}" ;;
        adopt) write_wp; echo "adopted:   ${WP_CONF} (comments renewed, settings unchanged: no restart)" ;;
        *) write_wp; echo "written:   ${WP_CONF} (LDAC ${LDAC_QUALITY})" ;;
    esac
    apply_le

    echo "== restarts"
    if (( BT_RESTART || WP_RESTART )); then
        do_restarts "${BT_RESTART}" "${WP_RESTART}" || rc=$?
        if (( rc == 1 )); then
            # Cancelled at the question: the files are written. The watcher
            # does not need the restarts, so it is still set up (or removed);
            # the bearer (an experimental BlueZ setting) and the checks wait.
            apply_watcher
            pending="the restarts above"
            [[ "${BEARER_STATE}" != set ]] || pending+=", the preferred bearer"
            echo
            echo "Not done yet: ${pending} and the checks."
            echo "After restarting, run: ${PROG} apply"
            return 0
        fi
        (( rc == 0 )) || die "a restart failed (see above). Backup of main.conf: ${BACKUP:-none taken}. Undo: ${PROG} revert"
    else
        echo "none needed"
    fi

    apply_watcher

    if [[ "${BEARER_STATE}" == set ]]; then
        echo "== preferred bearer"
        if (( BT_TIMEOUT > 0 )); then
            out="$(timeout "${BT_TIMEOUT}" bluetoothctl bearer "${DEVICE_MAC}" "${BEARER}" </dev/null 2>&1 || true)"
        else
            out="$(bluetoothctl bearer "${DEVICE_MAC}" "${BEARER}" </dev/null 2>&1 || true)"
        fi
        info="$(bt_info)"
        if grep -Eq "^[[:space:]]*PreferredBearer: ${BEARER}[[:space:]]*$" <<<"${info}"; then
            echo "set:       PreferredBearer = ${BEARER} (for connections the PC starts; disconnect and reconnect)"
        else
            echo "WARNING: could not set the bearer: ${out:-no output}" >&2
        fi
    elif [[ "${BEARER_STATE}" == unpaired ]]; then
        echo "WARNING: ${DEVICE_MAC} is not paired; bearer not changed" >&2
    fi

    # The states compute_plan read are from before the changes above: read the
    # drop-in and the watcher again, so verify reports what is there now.
    le_plan
    lw_plan
    verify
    echo
    (( FAILS == 0 )) || die "verification found problems (see above)."
    if (( WARNS )); then
        echo "Done, with ${WARNS} warning(s). Details: ${PROG} status"
    else
        if [[ "${LE_AUDIO}" == off ]]; then
            echo "Done. LE Audio is off in PipeWire: the headset plays over a2dp-sink, with LDAC (${LDAC_QUALITY})."
            echo "Turn LE Audio on: ${LE_ON_HINT}"
        else
            echo "Done. With the headset on LE: bap-* profiles (LC3). On BR/EDR: a2dp-sink, with LDAC (${LDAC_QUALITY})."
            echo "Choose the transport: ${PROG} apply --bearer le|bredr   (for connections the PC starts)"
        fi
    fi
}

# apply_le: the LE Audio drop-in written (LE_AUDIO=off) or removed (on), as
# the user, atomically. Its restart is WirePlumber's (WP_RESTART).
apply_le() {
    local tmp
    echo "== LE Audio: ${LE_CONF}"
    case "${LE_AUDIO}:${LE_STATE}" in
        off:ok) echo "unchanged: LE Audio off" ;;
        off:*)
            mkdir -p "${WP_CONF_DIR}"
            tmp="$(mktemp -p "${WP_CONF_DIR}" ".$(basename "${LE_CONF}").XXXXXX")"
            helper le-render >"${tmp}"
            chmod 0644 "${tmp}"
            mv -f "${tmp}" "${LE_CONF}"
            if [[ "${LE_STATE}" == adopt ]]; then
                echo "adopted:   ${LE_CONF} (comments renewed, no restart)"
            else
                echo "written:   ${LE_CONF} (LE Audio off: PipeWire offers only A2DP and HFP)"
            fi
            ;;
        on:remove) rm -f "${LE_CONF}"; echo "removed:   ${LE_CONF} (LE Audio on)" ;;
        on:absent) echo "unchanged: LE Audio on (no drop-in)" ;;
        on:conflict) echo "WARNING: ${LE_CONF} was not written by CST: left alone (it may keep LE Audio off)" >&2 ;;
    esac
}

# apply_watcher: the LDAC rate watcher installed (LDAC_RATE set) or removed.
# Runs after the restarts, so that it does not act while bluetoothd or
# WirePlumber restart; when the restart is cancelled, apply runs it anyway.
apply_watcher() {
    echo "== LDAC rate watcher: ${LW_UNIT}"
    if (( LW_WANT )); then
        lw_install
    else
        lw_remove
    fi
}

# lw_install: the watcher's program and unit (as the user, atomically), then
# enabled and started; restarted when it ran with older files.
lw_install() {
    local tmp changed=0
    if [[ "${LW_PROG_ST}" != ok ]]; then
        python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "${LW_SRC}"
        mkdir -p "$(dirname "${LW_PROG}")"
        tmp="$(mktemp -p "$(dirname "${LW_PROG}")" ".bt-ldac-watch.XXXXXX")"
        cp "${LW_SRC}" "${tmp}"
        chmod 0755 "${tmp}"
        mv -f "${tmp}" "${LW_PROG}"
        echo "written:   ${LW_PROG}"
        changed=1
    fi
    if [[ "${LW_UNIT_ST}" != ok ]]; then
        mkdir -p "$(dirname "${LW_UNIT_FILE}")"
        tmp="$(mktemp -p "$(dirname "${LW_UNIT_FILE}")" ".${LW_UNIT}.XXXXXX")"
        helper ldac-unit "${DEVICE_MAC}" "${LDAC_RATE}" >"${tmp}"
        chmod 0644 "${tmp}"
        mv -f "${tmp}" "${LW_UNIT_FILE}"
        echo "written:   ${LW_UNIT_FILE} (LDAC to ${LDAC_RATE} Hz)"
        changed=1
        systemctl --user daemon-reload
    fi
    if [[ "${LW_ENABLED}" != enabled ]]; then
        systemctl --user enable "${LW_UNIT}"
        echo "enabled:   ${LW_UNIT} (starts with your session)"
    fi
    if [[ "${LW_ACTIVE}" != active ]]; then
        systemctl --user start "${LW_UNIT}"
        echo "started:   ${LW_UNIT} (logs: journalctl --user -u ${LW_UNIT})"
    elif (( changed )); then
        systemctl --user restart "${LW_UNIT}"
        echo "restarted: ${LW_UNIT} (new files)"
    else
        echo "unchanged: ${LW_UNIT} enabled and active"
    fi
    LW_ACTIVE="$(systemctl --user is-active "${LW_UNIT}" 2>/dev/null || true)"
}

# lw_remove: the watcher stopped, disabled and removed, if CST installed it
# (apply with LDAC_RATE empty, and revert). With CHECK=1 only says so.
lw_remove() {
    if ! lw_ours_present; then
        echo "unchanged: not installed"
        [[ "${LW_UNIT_ST}" == conflict ]] && echo "WARNING: ${LW_UNIT_FILE} was not written by CST: left alone" >&2
        [[ "${LW_PROG_ST}" == conflict ]] && echo "WARNING: ${LW_PROG} was not written by CST: left alone" >&2
        return 0
    fi
    if (( CHECK )); then
        echo "would stop, disable and remove ${LW_UNIT} and ${LW_PROG}"
        return 0
    fi
    if [[ "${LW_UNIT_ST}" == ok || "${LW_UNIT_ST}" == change ]]; then
        systemctl --user disable --now "${LW_UNIT}" 2>/dev/null || true
        rm -f "${LW_UNIT_FILE}"
        echo "removed:   ${LW_UNIT_FILE} (stopped and disabled)"
        systemctl --user daemon-reload
    fi
    if [[ "${LW_PROG_ST}" == ok || "${LW_PROG_ST}" == change ]]; then
        rm -f "${LW_PROG}"
        rmdir "$(dirname "${LW_PROG}")" 2>/dev/null || true
        echo "removed:   ${LW_PROG}"
    fi
    LW_ACTIVE=""
}

# oldest_backup: the oldest main.conf backup (by time), ours or the old
# script's (both named main.conf.bak-*): the state before any of this.
oldest_backup() {
    compgen -G "${MAIN_CONF}.bak-*" >/dev/null || return 1
    # Oldest modification time first (cp -p kept the original's); name as tie-break.
    stat -c '%Y %n' -- "${MAIN_CONF}".bak-* | sort -k1,1n -k2 | head -n1 | cut -d' ' -f2-
}

revert() {
    local backup rc=0 rrc=0 mc=0 wp=0 lw=0 le=0
    (( CHECK )) || preconditions
    [[ -f "${MAIN_CONF}" && ! -L "${MAIN_CONF}" ]] || die "${MAIN_CONF} is missing or a symlink"
    echo "== BlueZ: ${MAIN_CONF}"
    if backup="$(oldest_backup)"; then
        helper general-revert "${MAIN_CONF}" "${backup}" >"${TMP}" || rc=$?
        case "${rc}" in
            0) echo "unchanged: Experimental and KernelExperimental are already as in ${backup}" ;;
            10)
                mc=1
                if (( CHECK )); then
                    echo "would set the two keys as in ${backup}:"
                    diff -u "${MAIN_CONF}" "${TMP}" | sed 's/^/    /' || true
                else
                    install_main_conf "${TMP}"
                    echo "           the two keys as in ${backup}"
                fi
                ;;
            *) die "could not process ${MAIN_CONF} (rc=${rc})" ;;
        esac
    else
        echo "WARNING: no backup ${MAIN_CONF}.bak-* found; main.conf not changed" >&2
    fi

    echo "== WirePlumber: ${WP_CONF}"
    if [[ ! -e "${WP_CONF}" ]]; then
        echo "unchanged: the rule does not exist"
    elif [[ -L "${WP_CONF}" ]] || ! grep -qF -e "${WP_MARKER}" -e "${OLD_WP_MARKER}" "${WP_CONF}"; then
        echo "WARNING: ${WP_CONF} was not written by CST or apply-le-audio.sh: left alone" >&2
    else
        wp=1
        if (( CHECK )); then echo "would remove ${WP_CONF}"; else rm -f "${WP_CONF}"; echo "removed:   ${WP_CONF}"; fi
    fi
    echo "== LE Audio: ${LE_CONF}"
    if [[ ! -e "${LE_CONF}" && ! -L "${LE_CONF}" ]]; then
        echo "unchanged: the drop-in does not exist (LE Audio on)"
    elif [[ -L "${LE_CONF}" ]] || ! grep -qF -e "${WP_MARKER}" "${LE_CONF}"; then
        echo "WARNING: ${LE_CONF} was not written by CST: left alone" >&2
    else
        le=1
        if (( CHECK )); then
            echo "would remove ${LE_CONF} (LE Audio on again)"
        else
            rm -f "${LE_CONF}"
            echo "removed:   ${LE_CONF} (LE Audio on again; LE_AUDIO=off in the settings writes it again at the next apply)"
        fi
    fi
    echo "== LDAC rate watcher: ${LW_UNIT}"
    lw_plan
    lw_ours_present && lw=1
    lw_remove
    if (( CHECK )); then
        (( mc )) && echo "would restart bluetooth.service (after asking) and wireplumber"
        (( !mc && (wp || le) )) && echo "would restart wireplumber"
        (( mc || wp || lw || le )) && return 10
        return 0
    fi
    echo "== restarts"
    (( mc || wp || le )) || { echo "none needed"; return 0; }
    do_restarts "${mc}" 1 || rrc=$?
    # Cancelled at the question: the commands for later were printed.
    (( rrc != 2 )) || die "a restart failed (see above). Backup of main.conf: ${BACKUP:-none taken}"
    return 0
}

# le_audio_cmd: le-audio on|off writes LE_AUDIO into your settings file
# (through a symlink, atomically, other lines kept), then runs apply. Without
# on/off it only shows the setting and the CST file's state.
le_audio_cmd() {
    local label target tmp rc=0 args=(apply)
    label="${USER_SETTINGS/#"${HOME}"/\~}"
    if [[ -z "${le_set}" ]]; then
        le_plan
        echo "LE Audio in PipeWire: ${LE_AUDIO} (${le_from})"
        le_report
        if [[ "${LE_AUDIO}" == off ]]; then echo "Turn it on: ${LE_ON_HINT}"; else echo "Turn it off: ${LE_OFF_HINT}"; fi
        return 0
    fi
    mkdir -p "$(dirname "${USER_SETTINGS}")"
    target="$(readlink -f -- "${USER_SETTINGS}")"
    helper settings-set "${target}" LE_AUDIO "${le_set}" >"${TMP}" || rc=$?
    case "${rc}" in
        0) echo "unchanged: ${label} already has LE_AUDIO=${le_set}" ;;
        10)
            tmp="$(mktemp -p "$(dirname "${target}")" ".$(basename "${target}").XXXXXX")"
            cat "${TMP}" >"${tmp}"
            if [[ -e "${target}" ]]; then chmod --reference="${target}" "${tmp}"; else chmod 0644 "${tmp}"; fi
            mv -f "${tmp}" "${target}"
            echo "written:   ${label}: LE_AUDIO=${le_set}"
            ;;
        *) die "could not update ${label} (rc=${rc})" ;;
    esac
    (( YES )) && args+=(--yes)
    (( NO_RESTART )) && args+=(--no-restart)
    echo "Now: ${PROG} ${args[*]}"
    rm -f "${TMP}"
    exec "${SELF}" "${args[@]}"
}

case "${cmd}" in
    plan) plan ;;
    status) status ;;
    apply) apply ;;
    revert) revert ;;
    le-audio) le_audio_cmd ;;
esac
