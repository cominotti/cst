#!/usr/bin/env bash
#
# swap-setup.sh  (the `swap` module of CST: run it as `cst swap ...`)
#
# Swap and memory of this workstation (93 GiB RAM, a 48 GiB Windows VM):
# zswap, a compressed cache in RAM, in front of an encrypted swap partition
# on NVMe (swap0), zram off, swap-friendly VM sysctls, and a memory floor for
# the VM. Replaces the standalone apply-swap-config.sh; what that script
# wrote is recognised and adopted. Desired files live in ./swap/; the
# rationale is in swap/README.md.
#
# Commands:
#   plan [--root]          what apply would change (dry run). Works as your
#                          user, also in a toolbox; only /etc/crypttab needs
#                          root to read (--root: through sudo, on the host)
#   apply                  (root) idempotent:
#       * swap0: a marked line in /etc/crypttab (plain dm-crypt, new random
#         key every boot) and one in /etc/fstab; activated if not active
#       * zswap at boot: /etc/tmpfiles.d/zswap.conf (zstd, pool up to 20 %
#         of RAM, shrinker on), also set live
#       * zram off: vendor zram-generator.conf masked; zram0 swapped off now
#         only when free RAM can take its contents
#       * /etc/sysctl.d/99-swap-tuning.conf (swappiness 100, page-cluster 0,
#         watermarks), applied live
#       * machine.slice MemoryLow=48G (drop-in 50-memory-low.conf, next to
#         cpu-setup.sh's 50-cpu-setup.conf; different settings, no clash)
#       * verification at the end
#   status [--root]        read-only: config files, live settings, zswap pool
#                          and traffic, swap devices, per-cgroup swap, memory
#                          pressure. Works as your user, also in a toolbox;
#                          the zswap debug counters need root (--root)
#   help
#
# apply runs on the host only. Nothing here changes memory.zswap.writeback.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SRC="$(dirname "${SELF}")/swap"
# shellcheck source=swap/swap-lib.bash
source "${SRC}/swap-lib.bash"

# NVMe swap partition, addressed by PARTUUID (GPT) because the swap-signature
# UUID inside the partition is overwritten by dm-crypt.
DISK_SWAP_PARTUUID="612551b1-ca02-4514-86ad-7d8902e612dc"
DISK_SWAP_NAME="swap0"
DISK_SWAP_MAPPER="/dev/mapper/${DISK_SWAP_NAME}"
DISK_SWAP_DEV="/dev/disk/by-partuuid/${DISK_SWAP_PARTUUID}"
# Exact size of the partition. Asserted on every apply because the "swap"
# crypttab option destroys the target on every boot: if the PARTUUID ever
# resolved to a different device (disk clone, repartition), we want to stop.
DISK_SWAP_SIZE_BYTES=137438953472

# Plain dm-crypt (no LUKS header), fresh key from /dev/urandom every boot:
# nothing to type, yesterday's swap contents become noise today.
# aes-xts-plain64 with size=512 = two 256-bit keys (XTS). sector-size=4096
# matches the page size so one swap page is one crypto sector.
CRYPTTAB_LINE="${DISK_SWAP_NAME} ${DISK_SWAP_DEV} /dev/urandom swap,cipher=aes-xts-plain64,size=512,sector-size=4096,discard ${SWAP_MARKER}"
# nofail keeps boot going if the disk is missing; discard lets the NVMe
# reclaim blocks as pages leave swap. No pri= needed: single swap device.
FSTAB_LINE="${DISK_SWAP_MAPPER} none swap defaults,nofail,discard 0 0 ${SWAP_MARKER}"

ZSWAP_PARAMS="/sys/module/zswap/parameters"
SRC_TMPFILES="${SRC}/tmpfiles/zswap.conf"
SRC_SYSCTL="${SRC}/sysctl/99-swap-tuning.conf"
SRC_DROPIN="${SRC}/units/machine.slice.d-50-memory-low.conf"
# The cpu module's runtime experiment that sets one of our sysctls on purpose
# (cpu-setup.sh records it here). While it is on, apply leaves that sysctl
# alone; swap-status shows it as "exp", not DIFF.
WSF_EXPERIMENT=/run/cpu-setup/exp-wsf-300
WSF_KEY=vm.watermark_scale_factor

# Shown in the help and in hints: "cst swap" when run through the cst command
# (it sets CST_PROG), else the name this script was started with.
PROG="${CST_PROG:-$0}"

usage() {
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    echo "Usage: ${PROG} <command> [args]"
}
in_container() { [[ -e /run/.containerenv || -e /.dockerenv ]]; }
die() { echo "${PROG}: ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift
orig_args=("${cmd}" "$@")
want_root=0

case "${cmd}" in
    help|-h|--help) usage; exit 0 ;;
    plan|status)
        case "${1:-}" in
            "") ;;
            --root) want_root=1 ;;
            *) echo "${cmd}: unknown argument '$1' (only --root)" >&2; exit 2 ;;
        esac
        ;;
    apply)
        [[ $# -eq 0 ]] || { echo "apply: no arguments" >&2; exit 2; }
        want_root=1
        ;;
    *) echo "Unknown command: ${cmd}" >&2; usage >&2; exit 2 ;;
esac

if (( want_root )); then
    if in_container; then
        echo "'${PROG} ${orig_args[*]}' must run on the host, not inside a toolbox/container." >&2
        exit 1
    fi
    if [[ ${EUID} -ne 0 ]]; then
        # sudo resets the environment: CST_PROG is handed over explicitly.
        if [[ -n "${CST_PROG:-}" ]]; then
            exec sudo -- env "CST_PROG=${CST_PROG}" "${SELF}" "${orig_args[@]}"
        fi
        exec sudo -- "${SELF}" "${orig_args[@]}"
    fi
fi

# Inside a toolbox the host's /etc is at /run/host/etc (read-only use only:
# apply never runs there). /proc, /sys and /dev are the host's anyway.
ETC=/etc
if in_container; then ETC=/run/host/etc; fi
CRYPTTAB="${ETC}/crypttab"
FSTAB="${ETC}/fstab"
TMPFILES_CONF="${ETC}/tmpfiles.d/zswap.conf"
SYSCTL_CONF="${ETC}/sysctl.d/99-swap-tuning.conf"
OLD_SYSCTL_CONF="${ETC}/sysctl.d/99-zram-tuning.conf"
SLICE_DROPIN="${ETC}/systemd/system/machine.slice.d/50-memory-low.conf"
ZRAM_CONF="${ETC}/systemd/zram-generator.conf"

# ---- facts ---------------------------------------------------------------------
# swap0 as /proc/swaps names it (/dev/dm-N, not the mapper symlink).
swap0_real() { readlink -f "${DISK_SWAP_MAPPER}" 2>/dev/null || echo "${DISK_SWAP_MAPPER}"; }
swap0_active() { awk -v d="$(swap0_real)" '$1 == d {f=1} END {exit !f}' /proc/swaps; }
swap0_used_kib() { awk -v d="$(swap0_real)" '$1 == d {print $4}' /proc/swaps; }
zram0_active() { grep -q '^/dev/zram0 ' /proc/swaps; }
zram_masked() { [[ -L "${ZRAM_CONF}" && "$(readlink "${ZRAM_CONF}")" == /dev/null ]]; }
# Size without root: sysfs counts 512-byte sectors.
partition_size() {
    local dev
    dev="$(readlink -f "${DISK_SWAP_DEV}" 2>/dev/null)" || return 1
    [[ -b "${dev}" ]] || return 1
    echo $(( $(cat "/sys/class/block/$(basename "${dev}")/size") * 512 ))
}
# zram0 can be swapped off when MemAvailable covers its contents + 1 GiB.
zram_fits() {
    local used avail
    used="$(awk '/^\/dev\/zram0 / {print $4}' /proc/swaps)"
    avail="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
    (( ${used:-0} + 1024 * 1024 < avail ))
}

# ---- plan / config part of status ---------------------------------------------
item() { printf '  %-9s %-38s %s\n' "$1" "$2" "${3:-}"; }
# short PATH: PATH under /etc without the directories everyone knows.
short() {
    local p="${1#"${ETC}"/}"
    p="${p#systemd/system/}"
    echo "${p}"
}

# describe_state STATE: plan/status wording of a file or line state.
describe_state() {
    case "$1" in
        ok) echo "ok" ;;
        adopt) echo "adopt" ;;
        duplicate) echo "dedupe" ;;
        missing) echo "write" ;;
        change) echo "update" ;;
        conflict) echo "CONFLICT" ;;
        unreadable) echo "?" ;;
        *) echo "$1" ;;
    esac
}
state_note() {
    case "$1" in
        ok) echo "up to date" ;;
        adopt) echo "up to date; written by an older name: apply only renews the comment" ;;
        duplicate) echo "the same entry twice (the old script ran again?): apply keeps one, no restart" ;;
        missing) echo "missing" ;;
        change) echo "differs from swap/ in CST" ;;
        conflict) echo "an unmarked ${DISK_SWAP_NAME} entry exists: apply refuses; remove it by hand" ;;
        unreadable) echo "needs root to read: on the host, ${PROG} $2 --root" ;;
    esac
}

# config_report VERB: the files and runtime state apply manages. VERB is
# "plan" or "status" (only used in hints).
config_report() {
    local verb="$1" st size tier_change=0 c f pair
    if [[ ! -d "${ETC}" ]]; then
        echo "  The host's /etc is not visible here (${ETC}): run '${PROG} ${verb}' on the host."
        return
    fi
    printf '\n== files (in %s)\n' "${ETC}"
    c="$(marked_line_state "${CRYPTTAB}" "${CRYPTTAB_LINE}" "${DISK_SWAP_NAME}")"
    f="$(marked_line_state "${FSTAB}" "${FSTAB_LINE}" "${DISK_SWAP_MAPPER}")"
    item "$(describe_state "${c}")" "crypttab: ${DISK_SWAP_NAME} line" "$(state_note "${c}" "${verb}")"
    item "$(describe_state "${f}")" "fstab: ${DISK_SWAP_MAPPER} line" "$(state_note "${f}" "${verb}")"
    [[ "${c}" == missing || "${c}" == change || "${f}" == missing || "${f}" == change ]] && tier_change=1
    for pair in "${SRC_TMPFILES}:${TMPFILES_CONF}" "${SRC_SYSCTL}:${SYSCTL_CONF}" "${SRC_DROPIN}:${SLICE_DROPIN}"; do
        st="$(file_state "${pair%%:*}" "${pair#*:}")"
        item "$(describe_state "${st}")" "$(short "${pair#*:}")" "$(state_note "${st}" "${verb}")"
    done
    if [[ -e "${OLD_SYSCTL_CONF}" ]]; then
        item remove "$(short "${OLD_SYSCTL_CONF}")" "legacy file of the zram setup"
    fi
    if zram_masked; then
        item ok "$(short "${ZRAM_CONF}")" "masked (-> /dev/null): no zram devices at boot"
    else
        item mask "$(short "${ZRAM_CONF}")" "not masked: zram-generator would create zram0 at boot"
    fi

    printf '\n== runtime\n'
    if size="$(partition_size)"; then
        if [[ "${size}" == "${DISK_SWAP_SIZE_BYTES}" ]]; then
            item ok "partition ${DISK_SWAP_PARTUUID:0:8}..." "$(readlink -f "${DISK_SWAP_DEV}"), ${size} bytes as expected"
        else
            item REFUSE "partition ${DISK_SWAP_PARTUUID:0:8}..." "${size} bytes, expected ${DISK_SWAP_SIZE_BYTES}: apply stops"
        fi
    else
        item REFUSE "partition ${DISK_SWAP_PARTUUID:0:8}..." "${DISK_SWAP_DEV} not found: apply stops"
    fi
    if swap0_active; then
        if (( tier_change )); then
            if [[ "$(swap0_used_kib)" -eq 0 ]]; then
                item restart "${DISK_SWAP_NAME}" "active and empty: apply re-activates it with the new options"
            else
                item later "${DISK_SWAP_NAME}" "active with $(( $(swap0_used_kib) / 1024 )) MiB in use: new options only after a reboot"
            fi
        else
            item ok "${DISK_SWAP_NAME}" "active as swap ($(swap0_real))"
        fi
    else
        item start "${DISK_SWAP_NAME}" "not active: apply opens it (dm-crypt + mkswap) and swaps on"
    fi
    if ! zram0_active; then
        item ok "zram0" "not active as swap"
    elif zram_fits; then
        item swapoff "zram0" "active; free RAM can take its contents: apply swaps it off"
    else
        item later "zram0" "active; not enough free RAM to swap it off now: gone after a reboot"
    fi
}

# ---- apply -----------------------------------------------------------------------
preconditions() {
    local actual part_type
    [[ -d "${ZSWAP_PARAMS}" ]] || die "zswap is not built into this kernel (no ${ZSWAP_PARAMS})."
    # The compressor must be known to the crypto API. On Fedora zstd is a
    # module; loading it here also proves the name before we write it.
    local comp
    comp="$(awk '$2 ~ /\/compressor$/ {print $NF}' "${SRC_TMPFILES}")"
    modprobe "${comp}" 2>/dev/null || true
    grep -qx "name *: ${comp}" /proc/crypto || die "Compressor '${comp}' is not available in /proc/crypto."
    # The partition must exist with the exact size. Refuse if it carries a
    # filesystem we would destroy; plain swap (dm-crypt overwrites it, by
    # design) or an already-opened mapper are fine.
    [[ -b "${DISK_SWAP_DEV}" ]] || die "Swap partition ${DISK_SWAP_DEV} not found."
    actual="$(blockdev --getsize64 "${DISK_SWAP_DEV}")"
    [[ "${actual}" == "${DISK_SWAP_SIZE_BYTES}" ]] ||
        die "${DISK_SWAP_DEV} is ${actual} bytes, expected ${DISK_SWAP_SIZE_BYTES}: the PARTUUID points at an unexpected device; refusing."
    if [[ ! -b "${DISK_SWAP_MAPPER}" ]]; then
        part_type="$(blkid -o value -s TYPE "${DISK_SWAP_DEV}" 2>/dev/null || true)"
        case "${part_type}" in
            # "" accepted only because the size assertion above pinned the device
            ""|swap|crypto_PLAIN) ;;
            *) die "${DISK_SWAP_DEV} has a '${part_type}' signature: refusing to overwrite it." ;;
        esac
    fi
}

# apply_line PATH LINE KEY MODE: sets LINE_CHANGED=1 when the entry itself
# changed (adopting an old marker or dropping a duplicate does not count), and
# LINE_WRITTEN=1 when the file was rewritten at all (then systemd needs a
# daemon-reload, or it flags the units it read from the file as changed).
LINE_CHANGED=0
LINE_WRITTEN=0
apply_line() {
    local path="$1" line="$2" key="$3" mode="$4" st
    st="$(marked_line_state "${path}" "${line}" "${key}")"
    case "${st}" in
        ok) echo "unchanged: ${path} (${DISK_SWAP_NAME} entry)" ;;
        adopt)
            write_marked_line "${path}" "${line}" "${mode}"
            echo "adopted:   ${path} (${DISK_SWAP_NAME} entry: marker renewed, entry unchanged)"
            LINE_WRITTEN=1
            ;;
        duplicate)
            write_marked_line "${path}" "${line}" "${mode}"
            echo "deduped:   ${path} (${DISK_SWAP_NAME} entry was there twice: one kept, entry unchanged)"
            LINE_WRITTEN=1
            ;;
        missing|change)
            write_marked_line "${path}" "${line}" "${mode}"
            echo "written:   ${path} (${DISK_SWAP_NAME} entry)"
            LINE_CHANGED=1
            LINE_WRITTEN=1
            ;;
        conflict) die "${path} has an unmarked entry for ${key}: remove it by hand, then run apply again." ;;
        *) die "${path}: ${st}" ;;
    esac
}

# apply_file SRC DEST: sets FILE_WRITTEN=1 when the file was rewritten,
# for new settings or only for renewed comments ("adopted").
FILE_WRITTEN=0
apply_file() {
    local src="$1" dest="$2" st
    FILE_WRITTEN=0
    st="$(file_state "${src}" "${dest}")"
    case "${st}" in
        ok) echo "unchanged: ${dest}" ;;
        adopt)
            write_file "${src}" "${dest}" 0644
            echo "adopted:   ${dest} (comments renewed, settings unchanged)"
            FILE_WRITTEN=1
            ;;
        *) write_file "${src}" "${dest}" 0644; echo "written:   ${dest}"; FILE_WRITTEN=1 ;;
    esac
}

zswap_live() {
    # Same order as the file: compressor before enabled, so a first pool is
    # zstd from the start (changing the compressor later creates a second
    # pool that coexists until the old one drains). The other two are live
    # tunables.
    local name val live want
    while read -r name val; do
        live="$(cat "${ZSWAP_PARAMS}/${name}")"
        want="${val}"
        case "${val}" in 1) want=Y ;; 0) want=N ;; esac
        if [[ "${live}" != "${want}" ]]; then
            echo "${val}" >"${ZSWAP_PARAMS}/${name}"
            echo "set:       zswap ${name} = ${val} (was ${live})"
        fi
    done < <(sed -n 's|^w */sys/module/zswap/parameters/\([^ ]*\) .* \([^ ]*\)$|\1 \2|p' "${SRC_TMPFILES}")
}

apply_all() {
    local swap_unit used_kib settings fail=0
    preconditions

    echo "== ${DISK_SWAP_NAME}: crypttab and fstab"
    # Both files are checked before either is written.
    [[ "$(marked_line_state "${CRYPTTAB}" "${CRYPTTAB_LINE}" "${DISK_SWAP_NAME}")" != conflict ]] ||
        die "${CRYPTTAB} has an unmarked entry for ${DISK_SWAP_NAME}: remove it by hand, then run apply again."
    [[ "$(marked_line_state "${FSTAB}" "${FSTAB_LINE}" "${DISK_SWAP_MAPPER}")" != conflict ]] ||
        die "${FSTAB} has an unmarked entry for ${DISK_SWAP_MAPPER}: remove it by hand, then run apply again."
    LINE_CHANGED=0
    LINE_WRITTEN=0
    apply_line "${CRYPTTAB}" "${CRYPTTAB_LINE}" "${DISK_SWAP_NAME}" 0600
    apply_line "${FSTAB}" "${FSTAB_LINE}" "${DISK_SWAP_MAPPER}" 0644
    # Also after a mere marker renewal: systemd watches these files' times
    # (the .swap, .mount and cryptsetup units come from them) and would show
    # "changed on disk" until a reload. A reload restarts nothing.
    (( LINE_WRITTEN )) && systemctl daemon-reload
    swap_unit="$(systemd-escape -p --suffix=swap "${DISK_SWAP_MAPPER}")"
    if swap0_active; then
        if (( LINE_CHANGED )); then
            # The live device was set up under the previous lines. Re-activate
            # it so cipher/options match, but only if nothing is in it.
            used_kib="$(swap0_used_kib)"
            if [[ "${used_kib:-0}" -eq 0 ]]; then
                echo "${DISK_SWAP_MAPPER}: entry changed and the device is empty: re-activating"
                systemctl stop "${swap_unit}"
                systemctl restart "systemd-cryptsetup@${DISK_SWAP_NAME}.service"
                systemctl start "${swap_unit}"
            else
                warn "${DISK_SWAP_MAPPER}: entry changed but $((used_kib / 1024)) MiB are in use; the live device keeps the old options until reboot."
            fi
        else
            echo "${DISK_SWAP_MAPPER} is active as swap."
        fi
    else
        echo "${DISK_SWAP_MAPPER}: activating (dm-crypt open + mkswap + swapon)"
        # The crypt unit creates the mapper; start it explicitly rather than
        # relying on the .swap unit's device dependency to pull it in.
        systemctl start "systemd-cryptsetup@${DISK_SWAP_NAME}.service"
        systemctl start "${swap_unit}"
    fi

    echo "== zswap"
    apply_file "${SRC_TMPFILES}" "${TMPFILES_CONF}"
    zswap_live

    # After zswap is on, so the pages coming out of zram can land in its pool.
    echo "== zram"
    if zram_masked; then
        echo "unchanged: ${ZRAM_CONF} (masked)"
    else
        # zram-generator's documented way to disable a vendor config: a
        # /dev/null symlink of the same name in /etc.
        rm -f "${ZRAM_CONF}"
        ln -s /dev/null "${ZRAM_CONF}"
        echo "masked:    ${ZRAM_CONF} -> /dev/null (no zram devices at boot)"
        systemctl daemon-reload
    fi
    if zram0_active; then
        # swapoff decompresses everything in zram back into RAM (or pushes it
        # to swap0): only when MemAvailable covers it with 1 GiB headroom.
        if zram_fits; then
            echo "zram0: swapping off"
            if systemctl stop dev-zram0.swap; then
                # The setup unit's ExecStop resets the device; the explicit
                # reset is a harmless fallback.
                systemctl stop systemd-zram-setup@zram0.service 2>/dev/null || true
                zramctl --reset /dev/zram0 2>/dev/null || true
                echo "zram0: removed"
            else
                warn "swapoff of zram0 failed; it is disabled in the config and gone after a reboot."
            fi
        else
            warn "not enough free RAM to swap off zram0 now; it is gone after a reboot (or run apply again under lower pressure)."
        fi
    fi

    echo "== sysctl"
    if [[ -e "${OLD_SYSCTL_CONF}" ]]; then
        rm -f "${OLD_SYSCTL_CONF}"
        echo "removed:   ${OLD_SYSCTL_CONF} (legacy)"
    fi
    apply_file "${SRC_SYSCTL}" "${SYSCTL_CONF}"
    if [[ -e "${WSF_EXPERIMENT}" ]]; then
        # The experiment is a measurement in progress: setting the file's
        # value now would end it silently while cpu-setup.sh still reports
        # it as on.
        echo "kept:      ${WSF_KEY} = $(cat /proc/sys/vm/watermark_scale_factor) (the cpu experiment wsf-300 is on;"
        echo "           after 'cst cpu experiment off wsf-300', run apply again, or reboot)"
        sysctl -q -p <(grep -v '^[[:space:]]*vm\.watermark_scale_factor[[:space:]]*=' "${SYSCTL_CONF}")
    else
        sysctl -q -p "${SYSCTL_CONF}"
    fi

    echo "== machine.slice"
    apply_file "${SRC_DROPIN}" "${SLICE_DROPIN}"
    # Takes effect on the running slice at once; an inactive slice picks it
    # up when the next VM starts. Also after a header renewal, so systemd does
    # not flag machine.slice as "changed on disk".
    (( FILE_WRITTEN )) && systemctl daemon-reload

    echo "== verification"
    settings="$(python3 -I "${SRC}/swap-status" --settings-only)"
    printf '%s\n' "${settings}" | sed '/^$/d;/^==/d'
    grep -q '^  DIFF' <<<"${settings}" && fail=1
    swap0_active || { echo "  BAD   ${DISK_SWAP_NAME} is not active as swap"; fail=1; }
    zram0_active && echo "  later zram0 still active as swap (gone after a reboot, or apply again under lower pressure)"
    swapon --show || true
    (( fail == 0 )) || die "one or more settings did not apply as expected (see above)."
    echo "Done. Details: ${PROG} status"
}

hint="${PROG} ${cmd} --root, on the host"
case "${cmd}" in
    plan)
        echo "Dry run: nothing is changed. ok = nothing to do; adopt = apply only renews a comment;"
        echo "dedupe = apply drops a second copy of the same entry; exp = set by a cpu experiment, left alone;"
        echo "write/update/mask/remove/start/restart/swapoff/DIFF = apply would change that;"
        echo "later = needs a reboot; ? = could not be read; REFUSE/CONFLICT = apply stops."
        config_report plan
        python3 -I "${SRC}/swap-status" --settings-only
        echo
        echo "Apply with: ${PROG} apply"
        ;;
    status)
        config_report status
        python3 -I "${SRC}/swap-status" --hint "${hint}"
        ;;
    apply) apply_all ;;
esac
