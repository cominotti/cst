#!/usr/bin/env bash
#
# btrfs-degraded-boot-setup.sh
#
# Purpose:
#   Let the machine boot unattended (degraded) when one member of the btrfs
#   RAID1 root filesystem is gone, without ever mounting degraded on a healthy
#   boot. Modeled on mdadm-last-resort@.{timer,service}.
#
# The problem:
#   /usr/lib/udev/rules.d/64-btrfs.rules sets SYSTEMD_READY=0 on every member
#   of a multi-device btrfs until *all* members are present. With one NVMe dead
#   the by-uuid .device unit never becomes active and sysroot.mount waits
#   forever; `degraded` in rootflags never gets a chance. On top of that,
#   dracut's initqueue "finished" hooks wait for every rd.luks.uuid mapping
#   (90-crypt.sh) and every rd.md.uuid array, and sysroot.mount is ordered
#   after dracut-initqueue, so the initrd would still end in "Could not boot".
#
# The mechanism ("btrfs last resort", only for the root filesystem UUID):
#   * /etc/udev/rules.d/65-btrfs-last-resort.rules runs after the untouched
#     upstream 64-btrfs.rules:
#       - fs incomplete (ID_BTRFS_READY=0): RUN systemctl --no-block start
#         btrfs-last-resort@<uuid>.timer (SYSTEMD_WANTS would be ignored, the
#         .device is inactive). Starting a running timer is a no-op, so the
#         45 s count starts when the FIRST member appears, i.e. after the first
#         LUKS passphrase was accepted, not at initrd start.
#       - fs incomplete and /run/btrfs-allow-degraded/<uuid> exists:
#         SYSTEMD_READY=1, overriding 64's "0", so the fs mounts `degraded`.
#       - fs complete: stop the timer.
#   * btrfs-last-resort@.service (when the timer elapses) only declares the fs
#     degraded if it is still incomplete after:
#       1. no passphrase prompt pending, no systemd-cryptsetup@ unit
#          activating (errors count as pending), no queued cryptsetup start
#          job (bounded: 120 s);
#       2. every rd.luks.uuid device whose mapping is missing either has no
#          source device, or is present but unreadable: its cryptsetup unit
#          logged device/header errors, or failed 3 times without ever asking
#          for a passphrase. A present disk that asked for a passphrase or
#          rejected one (typos, cancelled prompt) never degrades: its
#          systemd-cryptsetup@ unit is restarted so the prompt comes back.
#          (Manual escape hatch for a disk with a readable header but broken
#          keyslots: add btrfs_last_resort.force=1 once in the boot menu.)
#       3. udevadm settle + btrfs device scan + `btrfs device ready`, twice.
#     Then it creates the flag, logs at crit (journal, kmsg, plymouth), and in
#     the initrd disables dracut's 90-crypt.sh wait and force-runs rd.md.uuid
#     arrays with dracut's own mdraid_start, then re-triggers udev.
#   * /run survives switch-root (systemd moves it), so the flag reaches the real
#     root, where the same rule lets /var and /home mount too.
#     btrfs-degraded-boot-record.service then writes a persistent marker
#     /var/lib/btrfs-degraded-boot/degraded-<uuid>; btrfs-health-check alerts
#     until a scrub started after that boot finishes clean with RAID1 restored.
#   * The rule, units and helper live under /etc and are added to the
#     initramfs with `rpm-ostree initramfs-etc --track` (the initramfs overlay
#     only takes /etc; client-side dracut regeneration stays disabled).
#
# Safety model:
#   * Does not touch /etc/fstab, /etc/crypttab, kernel arguments or the btrfs
#     filesystem. Only adds files under /etc and tracks them in initramfs-etc.
#     The upstream 64-btrfs.rules is not shadowed.
#   * Refuses to run unless the root fs is multi-device btrfs on an rpm-ostree
#     system with `degraded` in the next-boot rootflags, and unless upstream
#     64-btrfs.rules (booted and next boot) still has the ID_BTRFS_READY /
#     SYSTEMD_READY=0 semantics this rule builds on.
#   * After writing, the udev self-test must show every root fs member ready;
#     otherwise the new rule is removed and no rpm-ostree change is made.
#   * A new deployment is only created when the tracked file set changes, or
#     the next-boot initramfs overlay content differs from /etc.
#
# Usage (normally through the frontend ../btrfs-setup.sh; this file can also
# be run directly with the same flags):
#   cst btrfs degraded-boot              install / update (idempotent)
#   cst btrfs degraded-boot --status     show what is installed / tracked
#   cst btrfs degraded-boot --files-only write /etc files + self-test only,
#                                             leave the initramfs alone (testing)
#   cst btrfs degraded-boot --uninstall  remove everything, untrack from initramfs

set -Eeuo pipefail

UPSTREAM_RULE="/usr/lib/udev/rules.d/64-btrfs.rules"
CONF_DIR="/etc/btrfs-degraded-boot"
HELPER="${CONF_DIR}/btrfs-last-resort"
SYNCED_SUM_FILE="${CONF_DIR}/initramfs-synced.sha256"
RULE="/etc/udev/rules.d/65-btrfs-last-resort.rules"
UNIT_DIR="/etc/systemd/system"
SERVICE="${UNIT_DIR}/btrfs-last-resort@.service"
TIMER="${UNIT_DIR}/btrfs-last-resort@.timer"
RECORD_UNIT="btrfs-degraded-boot-record.service"
FLAG_DIR="/run/btrfs-allow-degraded"
STATE_DIR="/var/lib/btrfs-degraded-boot"
# Files that must be in the initramfs overlay.
TRACKED=("${RULE}" "${SERVICE}" "${TIMER}" "${HELPER}")
# Left behind by the first version of this script (full shadow of upstream).
OLD_RULE="/etc/udev/rules.d/64-btrfs.rules"
OLD_FILES=("${CONF_DIR}/upstream-64-btrfs.rules.sha256")
OURS_MARK="Generated by btrfs-degraded-boot-setup.sh"

export PATH="/usr/sbin:/usr/bin"

if [[ ${EUID} -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

die() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }

write_if_changed() {
    local dest="$1" mode="$2" tmp
    tmp="$(mktemp)"
    cat >"${tmp}"
    if [[ -f "${dest}" ]] && cmp -s "${tmp}" "${dest}"; then
        rm -f "${tmp}"
        echo "unchanged: ${dest}"
        return
    fi
    install -D -m "${mode}" -o root -g root "${tmp}" "${dest}"
    rm -f "${tmp}"
    restorecon "${dest}" 2>/dev/null || true
    echo "written:   ${dest}"
}

# Queries over `rpm-ostree status --json`. Deployment 0 is the one the next
# boot uses (staged/pending if any, else booted).
ostree_query() {
    rpm-ostree status --json | python3 -c '
import json, sys
d = json.load(sys.stdin)["deployments"]
what = sys.argv[1]
if what == "tracked":
    print("\n".join(d[0].get("initramfs-etc") or []))
elif what == "booted-tracked":
    print("\n".join(next((x.get("initramfs-etc") or [] for x in d if x.get("booted")), [])))
elif what == "next-root":
    x = d[0]
    print("/ostree/deploy/%s/deploy/%s.%d" % (x["osname"], x["checksum"], x["serial"]))
elif what == "next-staged":
    print("yes" if d[0].get("staged") else "no")
elif what == "summary":
    for x in d:
        flags = [k for k in ("booted", "staged", "pinned") if x.get(k)]
        print("  %-14s %-13s %s initramfs-etc=%s" % (x.get("version"), ",".join(flags) or "-",
              x["checksum"][:12], x.get("initramfs-etc") or []))
' "$1"
}

# Path of the initramfs-etc overlay the next boot will load, or nothing.
next_overlay() {
    local next entry opts path initrds
    if [[ "$(ostree_query next-staged)" == "yes" ]]; then
        python3 - <<'PY'
import re
d = open("/run/ostree/staged-deployment", "rb").read()
i = d.find(b"overlay-initrds")
m = re.search(rb"[0-9a-f]{64}", d[i:]) if i >= 0 else None
if m:
    print("/run/ostree/staged-initrds/" + m.group().decode())
PY
        return
    fi
    next="$(realpath "$(ostree_query next-root)")"
    for entry in /boot/loader/entries/*.conf; do
        opts="$(awk '$1 == "options" {sub(/^options[ \t]+/, ""); print}' "${entry}")"
        path="$(grep -o 'ostree=[^ ]*' <<<"${opts}" | cut -d= -f2)"
        [[ -n "${path}" && "$(realpath "${path}" 2>/dev/null)" == "${next}" ]] || continue
        initrds="$(awk '$1 == "initrd" {for (i = 2; i <= NF; i++) print $i}' "${entry}")"
        grep -m1 'initramfs-overlays' <<<"${initrds}" | sed 's|^|/boot|'
        return
    done
}

# 0 = the next-boot overlay contains exactly the tracked files with the
# current /etc content; 1 = differs; 2 = overlay not locatable.
overlay_in_sync() {
    local ov tmpd f rc=0
    ov="$(next_overlay)"
    [[ -n "${ov}" && -r "${ov}" ]] || return 2
    tmpd="$(mktemp -d)"
    (cd "${tmpd}" && { zcat -f "${ov}" | cpio -idm --quiet --no-absolute-filenames; }) || { rm -rf "${tmpd}"; return 2; }
    for f in "${TRACKED[@]}"; do
        cmp -s "${tmpd}${f}" "${f}" || rc=1
    done
    if [[ "$(cd "${tmpd}" && find . -type f | sed 's|^\.||' | sort)" != "$(printf '%s\n' "${TRACKED[@]}" | sort)" ]]; then
        rc=1
    fi
    rm -rf "${tmpd}"
    return "${rc}"
}

root_fs_uuid() {
    findmnt -rn -o UUID --target /sysroot 2>/dev/null | head -n1
}

tracked_sums() {
    sha256sum "${TRACKED[@]}"
}

upstream_ok() {
    grep -qF 'IMPORT{builtin}="btrfs ready $devnode"' "$1" &&
        grep -qF 'ENV{ID_BTRFS_READY}=="0", ENV{SYSTEMD_READY}="0"' "$1"
}

old_rule_is_ours() {
    [[ -f "${OLD_RULE}" ]] && grep -qF "${OURS_MARK}" "${OLD_RULE}"
}

status() {
    local uuid f rc
    uuid="$(root_fs_uuid)"
    echo "root fs uuid: ${uuid:-?}"
    for f in "${TRACKED[@]}" "${UNIT_DIR}/${RECORD_UNIT}" "${SYNCED_SUM_FILE}"; do
        if [[ -e "${f}" ]]; then
            printf '  present  %s  %s\n' "$(sha256sum "${f}" | cut -c1-16)" "${f}"
        else
            printf '  missing  %-16s  %s\n' "" "${f}"
        fi
    done
    printf '  %s: %s\n' "${RECORD_UNIT}" "$(systemctl is-enabled "${RECORD_UNIT}" 2>/dev/null || true)"
    old_rule_is_ours && echo "  leftover old override: ${OLD_RULE} (re-run installer to migrate)"
    printf 'upstream %s semantics: booted %s, next boot %s\n' "${UPSTREAM_RULE}" \
        "$(upstream_ok "${UPSTREAM_RULE}" && echo ok || echo CHANGED)" \
        "$(upstream_ok "$(ostree_query next-root)${UPSTREAM_RULE}" && echo ok || echo CHANGED)"
    rc=0; overlay_in_sync || rc=$?
    case "${rc}" in
        0) echo "next-boot initramfs overlay $(next_overlay): in sync with /etc" ;;
        1) echo "next-boot initramfs overlay $(next_overlay): OUT OF SYNC with /etc (re-run installer)" ;;
        *) echo "next-boot initramfs overlay: not found" ;;
    esac
    echo "deployments (initramfs-etc tracked files):"
    ostree_query summary
    echo "degraded flags in ${FLAG_DIR}:"
    ls -l "${FLAG_DIR}" 2>/dev/null | sed 's/^/  /' || echo "  (none)"
    echo "persistent degraded markers in ${STATE_DIR}:"
    ls -l "${STATE_DIR}" 2>/dev/null | sed 's/^/  /' || echo "  (none)"
    systemctl list-units --all --no-pager --no-legend 'btrfs-last-resort@*' | sed 's/^/  /'
    summary "${uuid}" "${rc}"
}

# Plain-language verdict for --status: installed? active in the initramfs that
# booted? what did this boot do? ready for the next boot?
summary() {
    local uuid="$1" overlay_rc="$2" f missing=() booted_tracked untracked=() timer started stopped
    local ok=1
    echo
    echo "================ summary ================"
    for f in "${TRACKED[@]}" "${UNIT_DIR}/${RECORD_UNIT}"; do
        [[ -e "${f}" ]] || missing+=("${f}")
    done
    if (( ${#missing[@]} )) || ! systemctl is-enabled -q "${RECORD_UNIT}" 2>/dev/null; then
        echo "installed:        NO - missing ${missing[*]:-} / ${RECORD_UNIT} not enabled (re-run this script)"
        ok=0
    else
        echo "installed:        yes"
    fi
    booted_tracked="$(ostree_query booted-tracked)"
    for f in "${TRACKED[@]}"; do
        grep -qxF "${f}" <<<"${booted_tracked}" || untracked+=("${f}")
    done
    if (( ${#untracked[@]} )); then
        echo "booted initramfs: NOT active - not in this boot's overlay: ${untracked[*]}"
        ok=0
    else
        echo "booted initramfs: active (all ${#TRACKED[@]} files in this boot's initramfs-etc overlay)"
    fi
    # Every healthy boot exercises the rule: the LUKS members unlock one after
    # the other, so the fs is briefly incomplete, the timer starts, and it is
    # stopped as soon as the last member appears.
    timer="btrfs-last-resort@${uuid}.timer"
    # journalctl -g exits 1 when nothing matches: not an error here.
    started="$(journalctl -b -q -o short-unix --no-pager -u "${timer}" -g '^Started' 2>/dev/null | awk 'NR == 1 {print $1}' || true)"
    stopped="$(journalctl -b -q -o short-unix --no-pager -u "${timer}" -g '^Stopped|Deactivated' 2>/dev/null | awk 'NR == 1 {print $1}' || true)"
    if [[ -e "${FLAG_DIR}/${uuid}" ]]; then
        echo "this boot:        DEGRADED - a member was missing and the fs was mounted degraded. Restore RAID1!"
        ok=0
    elif [[ -n "${started}" && -n "${stopped}" ]]; then
        echo "this boot:        normal - rule ran in the initramfs: timer armed when the first member appeared,"
        printf '                  cancelled %.1f s later when the last one arrived\n' "$(awk -v a="${started}" -v b="${stopped}" 'BEGIN {print b - a}')"
    elif [[ -n "${started}" ]]; then
        echo "this boot:        timer started but never stopped and no degraded flag: check 'journalctl -b -u ${timer}'"
        ok=0
    else
        echo "this boot:        no trace of the rule (both members appeared at once, or it did not run)"
    fi
    if compgen -G "${STATE_DIR}/degraded-*" >/dev/null; then
        echo "past degraded:    YES - marker(s) in ${STATE_DIR}; cleared after a clean scrub with RAID1 complete"
        ok=0
    else
        echo "past degraded:    none recorded"
    fi
    case "${overlay_rc}" in
        0) echo "next boot:        ready (overlay in sync with /etc)" ;;
        1) echo "next boot:        OUT OF SYNC - re-run this script before rebooting"; ok=0 ;;
        *) echo "next boot:        overlay not found - re-run this script before rebooting"; ok=0 ;;
    esac
    if (( ok )); then
        echo "verdict:          OK - an NVMe failure at boot would mount degraded after 45 s and alert"
    else
        echo "verdict:          ATTENTION - see the lines above"
    fi
}

uninstall() {
    local f tracked to_untrack=()
    if compgen -G "${FLAG_DIR}/*" >/dev/null; then
        die "this boot is DEGRADED (${FLAG_DIR} has flags); /var and /home depend on the rule. Uninstall after restoring RAID1 and rebooting."
    fi
    tracked="$(ostree_query tracked)"
    for f in "${TRACKED[@]}" "${OLD_RULE}"; do
        if grep -qxF "${f}" <<<"${tracked}"; then
            if [[ "${f}" != "${OLD_RULE}" ]] || [[ ! -e "${OLD_RULE}" ]] || old_rule_is_ours; then
                to_untrack+=("--untrack=${f}")
            fi
        fi
    done
    if (( ${#to_untrack[@]} )); then
        rpm-ostree initramfs-etc "${to_untrack[@]}"
    else
        echo "not tracked in initramfs-etc: nothing to untrack"
    fi
    systemctl disable "${RECORD_UNIT}" 2>/dev/null || true
    systemctl stop 'btrfs-last-resort@*.timer' 2>/dev/null || true
    old_rule_is_ours && rm -fv "${OLD_RULE}"
    rm -fv "${TRACKED[@]}" "${UNIT_DIR}/${RECORD_UNIT}" "${SYNCED_SUM_FILE}" "${OLD_FILES[@]}"
    rmdir -v "${CONF_DIR}" 2>/dev/null || true
    systemctl daemon-reload
    udevadm control --reload
    if compgen -G "${STATE_DIR}/*" >/dev/null; then
        echo "kept ${STATE_DIR} (degraded-boot markers; remove once RAID1 is restored)"
    else
        rmdir "${STATE_DIR}" 2>/dev/null || true
    fi
    echo "Uninstalled (initramfs: from the next deployment on)."
}

case "${1:-}" in
    --uninstall)
        uninstall
        exit 0
        ;;
    --status)
        status
        exit 0
        ;;
    ""|--files-only)
        ;;
    *)
        sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
        exit 2
        ;;
esac

# ---------------------------------------------------------------- guards ----
[[ -e /run/ostree-booted ]] || die "not an ostree-booted system"
command -v rpm-ostree >/dev/null || die "rpm-ostree not found"
[[ "$(findmnt -rn -o FSTYPE --target /sysroot | head -n1)" == "btrfs" ]] || die "/sysroot is not btrfs"
UUID="$(root_fs_uuid)"
[[ "${UUID}" =~ ^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$ ]] || die "unexpected root fs UUID '${UUID}'"
ndev="$(btrfs filesystem show "${UUID}" | awk '/Total devices/ {print $3}')"
(( ${ndev:-0} >= 2 )) || die "root btrfs ${UUID} is not multi-device (devices: ${ndev:-?})"
NEXT_KARGS="$(rpm-ostree kargs)"
grep -Eq "(^| )root=UUID=${UUID}( |$)" <<<"${NEXT_KARGS}" || die "next-boot root= is not UUID=${UUID}"
grep -Eq '(^| )rootflags=([^ ]*,)?degraded(,| |$)' <<<"${NEXT_KARGS}" ||
    die "'degraded' missing from next-boot rootflags: a degraded mount would fail anyway"
grep -Eq '(^| )rd\.luks\.uuid=' <<<"${NEXT_KARGS}" || warn "no rd.luks.uuid= in next-boot kargs: locked-disk protection has nothing to check"
NEXT_ROOT="$(ostree_query next-root)"
[[ -d "${NEXT_ROOT}/usr" ]] || die "cannot resolve next-boot deployment"
for up in "${UPSTREAM_RULE}" "${NEXT_ROOT}${UPSTREAM_RULE}"; do
    upstream_ok "${up}" || die "UPSTREAM CHANGED: ${up} no longer sets SYSTEMD_READY=0 from ID_BTRFS_READY; review ${RULE} logic first. Nothing was changed."
done

# ------------------------------------------------------------------ files ---
write_if_changed "${HELPER}" 0755 <<'HELPER'
#!/usr/bin/bash
#
# btrfs-last-resort <fs-uuid>    (btrfs-last-resort@<fs-uuid>.service)
# btrfs-last-resort --record     (btrfs-degraded-boot-record.service)
#
# Generated by btrfs-degraded-boot-setup.sh (CST). Runs in the initrd
# or the real root. Declares the filesystem degraded only if it is still
# incomplete once no passphrase prompt/unlock is pending and no rd.luks.uuid
# source device is present-but-locked. Must only use tools present in the
# stock initramfs (bash, btrfs, udevadm, systemctl, systemd-escape, mkdir, mv,
# sleep, plymouth, mdraid_start).

set -u

flag_dir="/run/btrfs-allow-degraded"
state_dir="/var/lib/btrfs-degraded-boot"
dracut_crypt_wait="/lib/dracut/hooks/initqueue/finished/90-crypt.sh"
uuid_re='^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$'

log() { echo "<$1>$2" >&2; }

if [[ "${1:-}" == "--record" ]]; then
    mkdir -p "${state_dir}"
    for f in "${flag_dir}"/*; do
        [[ -e "${f}" ]] || continue
        u="${f##*/}"
        [[ "${u}" =~ ${uuid_re} ]] || continue
        since="$(stat -c %Y "${f}")"
        # A later complete_since= line is added by btrfs-health-check.
        printf 'since=%s\nsince_human=%s\n' "${since}" "$(date -d "@${since}" '+%F %T %Z')" >"${state_dir}/degraded-${u}"
        log 2 "btrfs ${u}: this boot is DEGRADED; recorded in ${state_dir}/degraded-${u} until a clean scrub"
    done
    exit 0
fi

uuid="${1:-}"
[[ "${uuid}" =~ ${uuid_re} ]] || { log 3 "invalid fs uuid '${uuid}'"; exit 1; }
[[ -e "${flag_dir}/${uuid}" ]] && { log 6 "${uuid}: already allowed degraded"; exit 0; }

read -r cmdline </proc/cmdline
force=0
[[ " ${cmdline} " == *" btrfs_last_resort.force=1 "* ]] && force=1

luks_uuids() {
    local w u
    for w in ${cmdline}; do
        case "${w}" in
            rd.luks.uuid=*) u="${w#rd.luks.uuid=}"; echo "${u#luks-}" ;;
        esac
    done
}

declare -A asked=() restarts=() dead=()
err_since=0

# systemctl failed: count as busy, but at most 60 s in a row.
systemctl_error() {
    (( err_since )) || err_since=${SECONDS}
    if (( SECONDS - err_since >= 60 )); then
        (( SECONDS - err_since < 62 )) && log 2 "${uuid}: systemctl keeps failing for 60 s; no longer treating that as a pending unlock"
        return 1
    fi
    return 0
}

# Remember which rd.luks.uuid devices ever showed a passphrase prompt.
note_asks() {
    local f u
    for f in /run/systemd/ask-password/ask.*; do
        [[ -e "${f}" ]] || continue
        for u in $(luks_uuids); do
            grep -qF "${u}" "${f}" 2>/dev/null && asked[${u}]=1
        done
    done
}

# A passphrase prompt or an unlock in progress.
unlock_busy() {
    local f out
    note_asks
    for f in /run/systemd/ask-password/ask.*; do
        [[ -e "${f}" ]] && return 0
    done
    if ! out="$(systemctl list-units --no-legend --plain --state=activating 'systemd-cryptsetup@*.service' 2>&1)"; then
        systemctl_error; return
    fi
    err_since=0
    [[ -n "${out}" ]]
}

# Queued start jobs of cryptsetup units (usually: waiting for their device).
jobs_queued() {
    local out
    if ! out="$(systemctl list-jobs --no-legend 2>&1)"; then
        systemctl_error; return
    fi
    err_since=0
    grep -q 'systemd-cryptsetup@' <<<"${out}"
}

job_wait=0
wait_settled() {
    local waited=0
    while :; do
        if unlock_busy; then
            (( waited % 30 == 0 )) && log 5 "${uuid}: passphrase prompt / unlock pending, not deciding yet"
            sleep 2; waited=$((waited + 2)); continue
        fi
        if (( job_wait < 120 )) && jobs_queued; then
            (( job_wait % 30 == 0 )) && log 5 "${uuid}: cryptsetup start jobs queued, waiting (at most 120 s in total)"
            sleep 2; job_wait=$((job_wait + 2)); continue
        fi
        return
    done
}

# rd.luks.uuid devices whose source is present but whose mapping is not
# (excluding sources already classified as unreadable).
locked_sources() {
    local u
    for u in $(luks_uuids); do
        [[ -n "${dead[${u}]:-}" ]] && continue
        compgen -G "/dev/disk/by-id/dm-uuid-CRYPT-LUKS?-${u//-/}-*" >/dev/null && continue
        [[ -b "/dev/disk/by-uuid/${u}" ]] && echo "${u}"
    done
}

# Why is present LUKS device $1 still locked? In this order:
#   locked = its unit logged a rejected passphrase: a human must answer,
#            NEVER degrade (broken keyslots: btrfs_last_resort.force=1)
#   dead   = its unit logged device / header errors (unreadable header,
#            I/O error reading keyslots), even if it asked for a passphrase
#   locked = it asked for a passphrase (e.g. cancelled prompt)
#   dead   = the unit failed 3 times without ever asking for a passphrase
#   retry  = not known yet
classify() {
    local u="$1" unit="$2" jl jerr
    # All invocations this boot, all priorities: any rejected passphrase wins.
    jl="$(journalctl -b -q -o cat -u "${unit}" 2>/dev/null)"
    # Device errors: only systemd-cryptsetup's own error lines (priority
    # err..emerg), never libcryptsetup debug output (rd.debug / log_level=debug).
    jerr="$(journalctl -b -q -o cat -p err -u "${unit}" 2>/dev/null)"
    if grep -qiE 'passphrase incorrect|incorrect passphrase|no key available|wrong passphrase' <<<"${jl}"; then
        echo locked
    elif grep -qE 'Failed to load LUKS superblock on device|is not a valid LUKS device|Cannot open device|Failed to activate.*Input/output error|IO error while decrypting keyslot' <<<"${jerr}"; then
        echo dead
    elif [[ -n "${asked[${u}]:-}" ]]; then
        echo locked
    elif (( ${restarts[${u}]:-0} >= 3 )); then
        echo dead
    else
        echo retry
    fi
}

# 0 = complete, 1 = incomplete, 2 = no member present at all.
fs_state() {
    local dev="/dev/disk/by-uuid/${uuid}"
    udevadm settle --timeout=30 >/dev/null 2>&1
    btrfs device scan >/dev/null 2>&1
    [[ -b "${dev}" ]] || return 2
    btrfs device ready "${dev}" >/dev/null 2>&1 && return 0
    return 1
}

check_or_exit() {
    fs_state; rc=$?
    case "${rc}" in
        0) log 6 "${uuid}: all member devices present, nothing to do"; exit 0 ;;
        2) log 4 "${uuid}: no member device present, nothing to do"; exit 0 ;;
    esac
}

while :; do
    wait_settled
    check_or_exit
    locked="$(locked_sources)"
    if [[ -n "${locked}" ]] && (( ! force )); then
        for u in ${locked}; do
            unit="systemd-cryptsetup@$(systemd-escape "luks-${u}").service"
            case "$(classify "${u}" "${unit}")" in
                dead)
                    dead[${u}]=1
                    log 2 "btrfs ${uuid}: LUKS device ${u} is present but UNREADABLE (unit ${unit} failed $(( ${restarts[${u}]:-0} + 1 ))x without a passphrase prompt / with device errors): treating it as missing"
                    continue
                    ;;
                locked)
                    log 2 "btrfs ${uuid}: incomplete, but LUKS device ${u} is PRESENT and locked: NOT mounting degraded, asking for its passphrase again" ;;
                *)
                    log 4 "btrfs ${uuid}: LUKS device ${u} present but not unlocked, retrying its unlock (attempt $(( ${restarts[${u}]:-0} + 1 )))" ;;
            esac
            case "$(systemctl show -P ActiveState "${unit}" 2>/dev/null)" in
                failed|inactive)
                    restarts[${u}]=$(( ${restarts[${u}]:-0} + 1 ))
                    systemctl reset-failed "${unit}" 2>/dev/null
                    systemctl --no-block start "${unit}" 2>/dev/null
                    ;;
            esac
        done
        if [[ -n "$(locked_sources)" ]]; then
            sleep 5
            continue
        fi
    fi
    sleep 5
    check_or_exit
    if [[ -n "$(locked_sources)" ]] && (( ! force )); then
        continue
    fi
    break
done

msg="btrfs ${uuid}: member device(s) MISSING - mounting DEGRADED (btrfs-last-resort). Replace the disk and restore RAID1!"
(( force )) && msg="${msg} [btrfs_last_resort.force=1]"
(( ${#dead[@]} )) && msg="${msg} [present but unreadable LUKS: ${!dead[*]}]"
mkdir -p "${flag_dir}"
: >"${flag_dir}/${uuid}"
log 2 "${msg}"
echo "<2>btrfs-last-resort: ${msg}" >/dev/kmsg 2>/dev/null
if plymouth --ping 2>/dev/null; then
    plymouth display-message --text="${msg}" 2>/dev/null
fi

if [[ -e /etc/initrd-release ]]; then
    # dracut waits for every rd.luks.uuid mapping before dracut-pre-mount (and
    # so sysroot.mount) may run; the missing disk's mapping will never appear.
    if [[ -e "${dracut_crypt_wait}" ]]; then
        mv -f "${dracut_crypt_wait}" "${dracut_crypt_wait}.disabled-by-btrfs-last-resort" &&
            log 4 "${uuid}: disabled dracut initqueue wait ${dracut_crypt_wait}"
    fi
    # The same disk carried md members (rd.md.uuid); dracut would only
    # force-run them from its initqueue timeout (~2 min). Do it now.
    if [[ " ${cmdline} " == *" rd.md.uuid="* && -x /usr/bin/mdraid_start ]]; then
        log 4 "${uuid}: force-running inactive rd.md.uuid arrays (dracut mdraid_start)"
        /usr/bin/mdraid_start >/dev/null 2>&1
    fi
fi

udevadm trigger --action=change --subsystem-match=block --property-match=ID_FS_UUID="${uuid}"
udevadm settle --timeout=30 >/dev/null 2>&1
exit 0
HELPER

write_if_changed "${TIMER}" 0644 <<'EOF'
# Generated by btrfs-degraded-boot-setup.sh (CST).
# Started by /etc/udev/rules.d/65-btrfs-last-resort.rules when a member of
# btrfs fs %i appears but the fs is incomplete. Starting it again while it
# runs is a no-op, so the delay counts from the first member, not from boot.
[Unit]
Description=Wait for missing members of btrfs filesystem %i before allowing it degraded
Documentation=file:/etc/btrfs-degraded-boot/btrfs-last-resort
DefaultDependencies=no
Conflicts=shutdown.target
Before=shutdown.target

[Timer]
OnActiveSec=45s
AccuracySec=1s
RemainAfterElapse=no
EOF

write_if_changed "${SERVICE}" 0644 <<'EOF'
# Generated by btrfs-degraded-boot-setup.sh (CST).
[Unit]
Description=Allow btrfs filesystem %i to mount degraded (member device missing)
Documentation=file:/etc/btrfs-degraded-boot/btrfs-last-resort
DefaultDependencies=no
Conflicts=shutdown.target
Before=shutdown.target

[Service]
Type=oneshot
ExecStart=/usr/bin/bash /etc/btrfs-degraded-boot/btrfs-last-resort %i
SyslogIdentifier=btrfs-last-resort
# Waits for pending passphrase prompts / locked disks, however long they take.
TimeoutStartSec=infinity
EOF

write_if_changed "${UNIT_DIR}/${RECORD_UNIT}" 0644 <<EOF
# Generated by btrfs-degraded-boot-setup.sh (CST). Real root only.
[Unit]
Description=Record a degraded btrfs boot persistently (btrfs-last-resort)
Documentation=file:/etc/btrfs-degraded-boot/btrfs-last-resort
ConditionPathExists=!/etc/initrd-release
ConditionDirectoryNotEmpty=${FLAG_DIR}
RequiresMountsFor=${STATE_DIR}

[Service]
Type=oneshot
ExecStart=/usr/bin/bash ${HELPER} --record

[Install]
WantedBy=multi-user.target
EOF

rule_tmp="$(mktemp)"
trap 'rm -f "${rule_tmp}"' EXIT
cat >"${rule_tmp}" <<EOF
# ${OURS_MARK} (CST). Do not edit.
#
# btrfs last resort for the root filesystem ${UUID}. Runs after the upstream
# ${UPSTREAM_RULE}, which imports ID_BTRFS_READY and sets
# SYSTEMD_READY=0 while the fs is incomplete:
#   complete   -> stop btrfs-last-resort@<uuid>.timer
#   incomplete -> start that timer (45 s after the first member appeared the
#                 service re-checks and may create the flag)
#   incomplete + flag /run/btrfs-allow-degraded/<uuid> -> SYSTEMD_READY=1, so
#                 the fs can be mounted with -o degraded

SUBSYSTEM!="block", GOTO="btrfs_last_resort_end"
ACTION=="remove", GOTO="btrfs_last_resort_end"
ENV{ID_FS_TYPE}!="btrfs", GOTO="btrfs_last_resort_end"
ENV{ID_FS_UUID}!="${UUID}", GOTO="btrfs_last_resort_end"
# Never override device-mapper's own "not ready" (suspended / rules disabled).
ENV{DM_UDEV_DISABLE_OTHER_RULES_FLAG}=="1", GOTO="btrfs_last_resort_end"
ENV{DM_SUSPENDED}=="1", GOTO="btrfs_last_resort_end"
ENV{.DM_SUSPENDED}=="1", GOTO="btrfs_last_resort_end"

ENV{ID_BTRFS_READY}=="1", RUN+="/usr/bin/systemctl --no-block stop btrfs-last-resort@\$env{ID_FS_UUID}.timer", GOTO="btrfs_last_resort_end"
# Only act on this event's result from 64-btrfs.rules.
ENV{ID_BTRFS_READY}!="0", GOTO="btrfs_last_resort_end"
TEST=="/run/btrfs-allow-degraded/\$env{ID_FS_UUID}", ENV{SYSTEMD_READY}="1", GOTO="btrfs_last_resort_end"
RUN+="/usr/bin/systemctl --no-block start btrfs-last-resort@\$env{ID_FS_UUID}.timer"

LABEL="btrfs_last_resort_end"
EOF
udevadm verify --resolve-names=never --no-style "${rule_tmp}" >/dev/null ||
    die "udevadm verify failed for the generated rule"
rule_was_present=0
if [[ -e "${RULE}" ]]; then
    rule_was_present=1
fi
write_if_changed "${RULE}" 0644 <"${rule_tmp}"

# ------------------------------------------------------------- self-test ----
bash -n "${HELPER}" || die "syntax error in ${HELPER}"
systemctl daemon-reload
udevadm control --reload
systemd-analyze verify "btrfs-last-resort@${UUID}.timer" "btrfs-last-resort@${UUID}.service" "${RECORD_UNIT}" ||
    die "systemd-analyze verify failed"
systemctl enable "${RECORD_UNIT}" 2>&1 | grep -v '^$' || true

# `udevadm test` skips the btrfs builtin in test mode, so it cannot show
# readiness (and the rule then only takes silent GOTOs). Check that udev loads
# the rule, and that every root fs member currently is ready.
selftest_ok=1
while read -r name fstype fsuuid; do
    [[ "${fstype}" == "btrfs" && "${fsuuid}" == "${UUID}" ]] || continue
    out="$(udevadm test --action=change "/sys/class/block/${name}" 2>&1)"
    if ! grep -qxF "Reading rules file: ${RULE}" <<<"${out}"; then
        echo "self-test: ${name}: udev does not load ${RULE}" >&2
        selftest_ok=0
    fi
    props="$(udevadm info -q property "/sys/class/block/${name}")"
    if grep -qx 'ID_BTRFS_READY=1' <<<"${props}" && ! grep -qx 'SYSTEMD_READY=0' <<<"${props}"; then
        echo "self-test: ${name}: rule loaded, ID_BTRFS_READY=1, SYSTEMD_READY not 0"
    else
        echo "self-test: ${name}: NOT ready: $(grep -E '^(ID_BTRFS_READY|SYSTEMD_READY)=' <<<"${props}" | xargs)" >&2
        selftest_ok=0
    fi
done < <(lsblk -rn -o KNAME,FSTYPE,UUID)
if (( ! selftest_ok )); then
    if (( ! rule_was_present )); then
        rm -f "${RULE}"
        udevadm control --reload
    fi
    die "udev self-test failed; no initramfs change made"
fi
if [[ -e "${FLAG_DIR}/${UUID}" ]]; then
    warn "${FLAG_DIR}/${UUID} exists: this boot is DEGRADED"
fi

# ------------------------------------------------------ initramfs overlay ---
if [[ "${1:-}" == "--files-only" ]]; then
    echo "--files-only: initramfs overlay not updated"
    exit 0
fi
tracked="$(ostree_query tracked)"
args=()
for f in "${TRACKED[@]}"; do
    if ! grep -qxF "${f}" <<<"${tracked}"; then
        args+=("--track=${f}")
    fi
done
# Migration from the first version (full shadow of the upstream rule).
if grep -qxF "${OLD_RULE}" <<<"${tracked}" && { [[ ! -e "${OLD_RULE}" ]] || old_rule_is_ours; }; then
    args+=("--untrack=${OLD_RULE}")
fi
if (( ${#args[@]} )); then
    echo "rpm-ostree initramfs-etc ${args[*]}"
    rpm-ostree initramfs-etc "${args[@]}"
else
    rc=0; overlay_in_sync || rc=$?
    if (( rc == 1 )) || { (( rc == 2 )) && ! tracked_sums | cmp -s - "${SYNCED_SUM_FILE}" 2>/dev/null; }; then
        echo "next-boot overlay differs from /etc: rpm-ostree initramfs-etc --force-sync"
        rpm-ostree initramfs-etc --force-sync
    else
        echo "unchanged: initramfs-etc (tracked, next-boot overlay in sync)"
    fi
fi
tracked_sums | write_if_changed "${SYNCED_SUM_FILE}" 0644

# Old files are only removed once the new overlay is in place.
if old_rule_is_ours; then
    rm -fv "${OLD_RULE}"
    udevadm control --reload
fi
for f in "${OLD_FILES[@]}"; do
    if [[ -e "${f}" ]]; then
        rm -fv "${f}"
    fi
done

echo
status
