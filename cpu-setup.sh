#!/usr/bin/env bash
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
#         P-cores EPP performance, the game's memory protected along the
#         whole cgroup chain; runtime-only, exact revert
#       * baseline weights: VM (machine.slice) 50, toolboxes 30 (like
#         background.slice), so they never tie with the desktop
#       * toolboxes and their host commands (flatpak-spawn --host) always on
#         the E-cores (cgroup AllowedCPUs; cpuset delegated to the user manager,
#         next login): Claude Code, shells, cargo/go/gradle/maven size their
#         parallelism from that set, no per-toolchain config needed. Side
#         effect: host commands of Flatpak apps with org.freedesktop.Flatpak
#         access (here OBS, Remmina, RustConn, Resources, TestCenter) also
#         run on the E-cores. Not covered: `systemd-run --user` jobs (app.slice)
#         and Docker without docker-slice (dockerd is a system service)
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
#                          12-23 (diff first; apply asks; effective at the VM's
#                          next start, which is left to you)
#   docker-slice install|status|uninstall   (root) Docker containers born in
#                          builds.slice (weight 20, E-cores only; idle + 12-23
#                          during games):
#                          daemon.json "cgroup-parent"; restarting dockerd (stops
#                          containers) is left to you
#   experiment list|on NAME|off NAME   (root) runtime-only memory A/B knobs
#                          (thp-defer, wsf-300, minfree-256m); persist the
#                          winners in swap/sysctl/ (`cst swap apply`)
#   watch -l LABEL [--game NAME] [--seconds N] [--mangohud DIR] [-- CMD]
#                          (user) record game wait/memory stalls/frequencies
#   report [RUN...]        (user) compare recorded runs
#   help
#
# Root commands must run on the host, not inside toolbox; watch/report work in both.

set -Eeuo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SRC="$(dirname "${SELF}")/cpu"
UNIT_DIR="/etc/systemd/system"
CONF=/etc/cpu-setup/cpu-setup.conf
POLKIT_RULE=/etc/polkit-1/rules.d/50-cpu-setup-game.rules
MACHINE_DROPIN="${UNIT_DIR}/machine.slice.d/50-cpu-setup.conf"
DELEGATE_DROPIN="${UNIT_DIR}/user@.service.d/50-cpu-setup-cpuset.conf"
DAEMON_JSON=/etc/docker/daemon.json
# intel_pstate: raise the frequency at once when a task wakes up after an I/O
# wait (builds with many fsyncs, a game loading assets). Applied at boot by
# systemd-tmpfiles, and at once by install.
HWP_BOOST=/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost
HWP_TMPFILES=/etc/tmpfiles.d/cpu-setup-hwp-boost.conf
declare -A BINARIES=(
    [game-profile]=/usr/local/sbin/cpu-game-profile
    [vm-pin]=/usr/local/sbin/cpu-vm-pin
    [cpu-watch]=/usr/local/bin/cpu-watch
)
UNITS=(cpu-game.service cpu-game-watchdog.service cpu-game-watchdog.timer)
# Runtime-only memory experiments: name -> "file value".
declare -A EXPERIMENTS=(
    [thp-defer]="/sys/kernel/mm/transparent_hugepage/defrag defer"
    [wsf-300]="/proc/sys/vm/watermark_scale_factor 300"
    [minfree-256m]="/proc/sys/vm/min_free_kbytes 262144"
)

# Shown in the help and in hints: "cst cpu" when run through the cst command
# (it sets CST_PROG), else the name this script was started with.
PROG="${CST_PROG:-$0}"

usage() {
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    echo "Usage: ${PROG} <command> [args]"
}
in_container() { [[ -e /run/.containerenv || -e /.dockerenv ]]; }

cmd="${1:-help}"
[[ $# -gt 0 ]] && shift
orig_args=("${cmd}" "$@")

case "${cmd}" in
    help|-h|--help) usage; exit 0 ;;
    watch|report)
        if in_container; then
            exec flatpak-spawn --host python3 -I "${SRC}/cpu-watch" "${cmd}" "$@"
        fi
        exec python3 -I "${SRC}/cpu-watch" "${cmd}" "$@"
        ;;
    install|status|uninstall|game|vm-pin|docker-slice|experiment) ;;
    *) echo "Unknown command: ${cmd}" >&2; usage >&2; exit 2 ;;
esac

# ---- root commands -------------------------------------------------------------
if in_container; then
    echo "'${cmd}' must run on the host, not inside a toolbox/container." >&2
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
if [[ -z "${target_user}" || "${target_user}" == root ]]; then
    # status/game/... may run as root later: the user recorded at install.
    target_user="$(sed -n "s/^TARGET_USER='\{0,1\}\([^']*\)'\{0,1\}$/\1/p" "${CONF}" 2>/dev/null || true)"
fi
user_home() { getent passwd "${target_user}" | cut -d: -f6; }
usys() { systemctl --user -M "${target_user}@" "$@"; }

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
        runuser -u "${owner}" -- install -D -m "${mode}" "${src}" "${dest}"
    fi
    echo "written:   ${dest}"
}
install_text() {
    local tmp
    tmp="$(mktemp)"
    cat >"${tmp}"
    chmod 0644 "${tmp}"
    install_file "${tmp}" "$@"
    rm -f "${tmp}"
}
check_syntax() {
    if head -n1 "$1" | grep -q python; then
        python3 -I -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$1"
    else
        bash -n "$1"
    fi
}
section() { printf '\n== %s\n' "$1"; }
toolbox_dropin() { echo "$(user_home)/.config/systemd/user/user.slice.d/50-cpu-setup.conf"; }
helper_dropin() { echo "$(user_home)/.config/systemd/user/flatpak-session-helper.service.d/50-cpu-setup.conf"; }
e_cores() { cat /sys/devices/cpu_atom/cpus 2>/dev/null; }
# cpuset in use by the user manager (not just offered to it): it shows in
# the manager's own subtree_control only once a manager started with the
# delegation enabled it (next login after install).
cpuset_delegated() {
    grep -qw cpuset "/sys/fs/cgroup/user.slice/user-$(id -u "${target_user}").slice/user@$(id -u "${target_user}").service/cgroup.subtree_control" 2>/dev/null
}
gamemode_ini() { echo "$(user_home)/.config/gamemode.ini"; }

# ---- install ---------------------------------------------------------------------
install_all() {
    local f ini group_line ecores
    [[ -n "${target_user}" && "${target_user}" != root ]] ||
        { echo "install: run through sudo from your user account (needs SUDO_USER)" >&2; exit 1; }
    for f in "${!BINARIES[@]}"; do
        check_syntax "${SRC}/${f}" || { echo "${SRC}/${f}: syntax error" >&2; exit 1; }
    done
    install -d -m 0755 "$(dirname "${CONF}")"
    if [[ ! -e "${CONF}" ]]; then
        printf '%s\n' "# Managed by cpu-setup.sh (CST); values may be edited." \
            "TARGET_USER='${target_user}'" "BUILD_CPUS='12-23'" "P_CORES='0-7'" "VM_WEIGHT=10" \
            "GAME_MEMORY_LOW='16G'" "GAME_ZSWAP_WRITEBACK='no'" "VM_NAME='win11-devbox'" "VM_VCPUS=12" | install_text "${CONF}" 0644
    fi
    for f in "${!BINARIES[@]}"; do
        install_file "${SRC}/${f}" "${BINARIES[${f}]}" 0755
    done
    for f in "${UNITS[@]}"; do
        install_file "${SRC}/units/${f}" "${UNIT_DIR}/${f}" 0644
    done
    install_file "${SRC}/units/machine.slice.d-50-cpu-setup.conf" "${MACHINE_DROPIN}" 0644
    install_file "${SRC}/units/user@.service.d-50-cpu-setup-cpuset.conf" "${DELEGATE_DROPIN}" 0644
    if [[ -w "${HWP_BOOST}" ]]; then
        printf '%s\n' "# Managed by cpu-setup.sh (CST): intel_pstate HWP dynamic boost on." \
            "w ${HWP_BOOST} - - - - 1" | install_text "${HWP_TMPFILES}" 0644
        echo 1 >"${HWP_BOOST}" && echo "set:       ${HWP_BOOST} = 1"
    else
        echo "NOTE: ${HWP_BOOST} not found (no intel_pstate in HWP mode): HWP dynamic boost skipped" >&2
    fi
    # E-cores only, always: everything in the toolboxes (Claude Code, shells,
    # cargo/go/gradle/maven: they size their parallelism from this set), and
    # what toolboxes run on the host through `flatpak-spawn --host` (spawned
    # by flatpak-session-helper, outside the toolbox's cgroup and affinity).
    ecores="$(e_cores)"
    if [[ -n "${ecores}" ]]; then
        printf '%s\n' "# Managed by cpu-setup.sh (CST): toolbox containers (inner user.slice of the" \
            "# user manager, sibling of app.slice) on the E-cores, weighing like background.slice." \
            "[Slice]" "CPUWeight=30" "AllowedCPUs=${ecores}" | install_text "$(toolbox_dropin)" 0644 "${target_user}"
        printf '%s\n' "# Managed by cpu-setup.sh (CST): host commands of toolboxes and Flatpak apps" \
            "# (flatpak-spawn --host) on the E-cores, like the toolboxes themselves." \
            "[Service]" "AllowedCPUs=${ecores}" | install_text "$(helper_dropin)" 0644 "${target_user}"
    else
        echo "NOTE: no E-cores found (/sys/devices/cpu_atom/cpus): toolbox CPU restriction skipped" >&2
        printf '%s\n' "# Managed by cpu-setup.sh (CST)." "[Slice]" "CPUWeight=30" |
            install_text "$(toolbox_dropin)" 0644 "${target_user}"
        rm -fv "$(helper_dropin)"
    fi
    sed "s/@USER@/${target_user}/g" "${SRC}/cpu-game.rules.in" | install_text "${POLKIT_RULE}" 0644
    # shellcheck source=cst-link.bash
    source "$(dirname "${SELF}")/cst-link.bash"
    link_cst_command "${target_user}"
    # GameMode: our ini unless the user has one of their own.
    ini="$(gamemode_ini)"
    if [[ ! -e "${ini}" ]] || grep -q 'Managed by cpu-setup.sh' "${ini}"; then
        install_file "${SRC}/gamemode.ini" "${ini}" 0644 "${target_user}"
    else
        echo "NOTE: ${ini} is your own: add [custom] start/end from ${SRC}/gamemode.ini by hand" >&2
    fi
    # Silverblue: system groups live in /usr/lib/group; usermod needs them in /etc/group.
    if ! getent group gamemode | grep -q .; then
        echo "no 'gamemode' group on this system (is the gamemode package installed?)" >&2
    else
        if ! grep -q '^gamemode:' /etc/group; then
            group_line="$(grep '^gamemode:' /usr/lib/group)"
            printf '%s\n' "${group_line}" >>/etc/group
            grep -q '^gamemode:' /etc/gshadow 2>/dev/null || printf 'gamemode:!::\n' >>/etc/gshadow
        fi
        if ! id -nG "${target_user}" | tr ' ' '\n' | grep -qx gamemode; then
            usermod -aG gamemode "${target_user}"
            echo "added ${target_user} to the 'gamemode' group (effective at the next login)"
        fi
    fi
    systemctl daemon-reload
    usys daemon-reload 2>/dev/null || true
    systemctl enable --now cpu-game-watchdog.timer
    echo
    echo "Done. Still to do by you:"
    echo "  - log out and in once (gamemode group; cpuset delegation, which makes the"
    echo "    toolboxes' E-core restriction effective);"
    echo "  - Steam > game > Properties > Launch options: gamemoderun %command%"
    echo "  - optional, confirmed separately: ${PROG} vm-pin plan   and   ${PROG} docker-slice install"
    echo "  - measure: ${PROG} watch -l baseline --game dota2 --seconds 300   (while a build runs)"
}

uninstall() {
    local f ini
    systemctl stop cpu-game.service 2>/dev/null || true
    systemctl disable --now cpu-game-watchdog.timer 2>/dev/null || true
    for f in "${UNITS[@]}"; do rm -fv "${UNIT_DIR}/${f}"; done
    rm -fv "${MACHINE_DROPIN}" "${DELEGATE_DROPIN}" "${POLKIT_RULE}"
    if [[ -f "${HWP_TMPFILES}" ]]; then
        rm -fv "${HWP_TMPFILES}"
        [[ -w "${HWP_BOOST}" ]] && echo 0 >"${HWP_BOOST}" && echo "set:       ${HWP_BOOST} = 0"
    fi
    rmdir "$(dirname "${MACHINE_DROPIN}")" "$(dirname "${DELEGATE_DROPIN}")" 2>/dev/null || true
    for f in "${BINARIES[@]}"; do rm -fv "${f}"; done
    if [[ -n "${target_user}" ]]; then
        rm -fv "$(toolbox_dropin)" "$(helper_dropin)"
        rmdir "$(dirname "$(toolbox_dropin)")" "$(dirname "$(helper_dropin)")" 2>/dev/null || true
        ini="$(gamemode_ini)"
        grep -qs 'Managed by cpu-setup.sh' "${ini}" && rm -fv "${ini}"
        usys daemon-reload 2>/dev/null || true
    fi
    systemctl daemon-reload
    echo "Kept: ~/.local/bin/cst (the cst command; the other modules use it too)"
    echo "Kept: ${CONF}, the 'gamemode' group membership, builds.slice/daemon.json"
    echo "('${PROG} docker-slice uninstall') and the VM definition ('${PROG} vm-pin status';"
    echo "the previous one is in /etc/cpu-setup/*.xml.before-vm-pin)."
}

# ---- docker builds.slice ---------------------------------------------------------
docker_slice() {
    local action="${1:-status}"
    case "${action}" in
        install)
            # Docker builds (also those Claude Code starts through flatpak-spawn
            # --host: dockerd is a system service) on the E-cores, like the toolboxes.
            {
                cat "${SRC}/units/builds.slice"
                if [[ -n "$(e_cores)" ]]; then echo "AllowedCPUs=$(e_cores)"; fi
            } | install_text "${UNIT_DIR}/builds.slice" 0644
            systemctl daemon-reload
            systemctl start builds.slice
            python3 -I - "${DAEMON_JSON}" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path) as f:
        d = json.load(f)
except FileNotFoundError:
    d = {}
if d.get("cgroup-parent") == "builds.slice":
    print(f"unchanged: {path}")
    sys.exit(0)
if d.get("cgroup-parent"):
    sys.exit(f"{path} already sets cgroup-parent={d['cgroup-parent']!r}: left alone")
d["cgroup-parent"] = "builds.slice"
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
print(f"written:   {path} (cgroup-parent: builds.slice)")
PY
            echo "Restart Docker when convenient (stops running containers): sudo systemctl restart docker"
            ;;
        uninstall)
            [[ -e "${DAEMON_JSON}" ]] && python3 -I - "${DAEMON_JSON}" <<'PY'
import json, os, sys
path = sys.argv[1]
d = json.load(open(path))
if d.get("cgroup-parent") == "builds.slice":
    del d["cgroup-parent"]
    if d:
        json.dump(d, open(path, "w"), indent=2)
    else:
        os.remove(path)
    print(f"removed cgroup-parent from {path}")
PY
            rm -fv "${UNIT_DIR}/builds.slice"
            systemctl daemon-reload
            echo "Restart Docker when convenient for new containers to leave builds.slice."
            ;;
        status)
            printf 'builds.slice: %s; daemon.json cgroup-parent: %s; containers in it: %s\n' \
                "$(systemctl is-active builds.slice 2>/dev/null || true)" \
                "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("cgroup-parent","-"))' "${DAEMON_JSON}" 2>/dev/null || echo -)" \
                "$(find /sys/fs/cgroup/builds.slice -maxdepth 1 -name 'docker-*.scope' 2>/dev/null | wc -l)"
            ;;
        *) echo "docker-slice: install|status|uninstall" >&2; exit 2 ;;
    esac
}

# ---- experiments -----------------------------------------------------------------
experiment() {
    local action="${1:-list}" name="${2:-}" spec file val saved
    case "${action}" in
        list)
            for name in "${!EXPERIMENTS[@]}"; do
                read -r file val <<<"${EXPERIMENTS[${name}]}"
                printf '  %-14s %s -> %-8s now: %s%s\n' "${name}" "${file}" "${val}" "$(tr -d '\n' <"${file}")" \
                    "$([[ -e /run/cpu-setup/exp-${name} ]] && echo '  (ON)')"
            done
            ;;
        on|off)
            spec="${EXPERIMENTS[${name}]:-}"
            [[ -n "${spec}" ]] || { echo "experiment: unknown '${name}' (see: ${PROG} experiment list)" >&2; exit 2; }
            read -r file val <<<"${spec}"
            saved="/run/cpu-setup/exp-${name}"
            if [[ "${action}" == on ]]; then
                install -d -m 0755 /run/cpu-setup
                # sysfs THP shows "[current]" among the choices.
                [[ -e "${saved}" ]] || sed -n 's/.*\[\(.*\)\].*/\1/p;t;p' "${file}" >"${saved}"
                echo "${val}" >"${file}"
                echo "${name}: on (${file} = ${val}; runtime only, reverted by '${PROG} experiment off ${name}' or a reboot)"
            else
                [[ -e "${saved}" ]] || { echo "${name}: not on"; return 0; }
                if ! cat "${saved}" >"${file}"; then
                    echo "${name}: could not restore $(cat "${saved}") into ${file}" >&2; exit 1
                fi
                rm -f "${saved}"
                echo "${name}: off (${file} restored)"
            fi
            ;;
        *) echo "experiment: list|on NAME|off NAME" >&2; exit 2 ;;
    esac
}

# ---- status ----------------------------------------------------------------------
status() (
    set +e +o pipefail
    echo "kernel $(uname -r), scheduler EEVDF$( [[ "$(cat /sys/kernel/sched_ext/state 2>/dev/null)" == enabled ]] && echo ' + sched_ext!'), preempt $(sed -n 's/.*(\([a-z]*\)).*/\1/p' /sys/kernel/debug/sched/preempt 2>/dev/null)"
    section "game profile"
    bash "${SRC}/game-profile" state | sed 's/^/  /'
    printf '  watchdog timer: %s\n' "$(systemctl is-active cpu-game-watchdog.timer 2>/dev/null)"
    section "GameMode"
    if [[ -n "${target_user}" ]]; then
        printf '  group: %s; ini: %s; gamemoded: %s\n' \
            "$(id -nG "${target_user}" | tr ' ' '\n' | grep -qx gamemode && echo member || echo 'NOT a member')" \
            "$(grep -qs 'Managed by cpu-setup.sh' "$(gamemode_ini)" && echo managed || echo 'own/absent')" \
            "$(usys is-active gamemoded.service 2>/dev/null || true)"
    fi
    printf '  HWP dynamic boost: %s (%s)\n' "$(cat "${HWP_BOOST}" 2>/dev/null || echo n/a)" \
        "$([[ -f "${HWP_TMPFILES}" ]] && echo "kept at boot by ${HWP_TMPFILES}" || echo 'not managed')"
    section "baseline weights"
    printf '  machine.slice %s, toolboxes %s, app.slice %s, background.slice %s\n' \
        "$(systemctl show -P CPUWeight machine.slice)" "$(usys show -P CPUWeight user.slice 2>/dev/null)" \
        "$(usys show -P CPUWeight app.slice 2>/dev/null)" "$(usys show -P CPUWeight background.slice 2>/dev/null)"
    section "E-cores for toolboxes and host commands"
    if [[ -n "${target_user}" ]]; then
        printf '  cpuset delegated: %s; toolboxes AllowedCPUs=%s; flatpak-spawn --host AllowedCPUs=%s\n' \
            "$(cpuset_delegated && echo yes || echo 'no (next login)')" \
            "$(usys show -P AllowedCPUs user.slice 2>/dev/null)" \
            "$(usys show -P AllowedCPUs flatpak-session-helper.service 2>/dev/null)"
        printf '  effective (cgroup): toolboxes [%s]\n' \
            "$(cat "/sys/fs/cgroup/user.slice/user-$(id -u "${target_user}").slice/user@$(id -u "${target_user}").service/user.slice/cpuset.cpus.effective" 2>/dev/null)"
    fi
    section "VM"
    python3 -I "${SRC}/vm-pin" status 2>&1 | sed -n '1,3p;$p' | sed 's/^/  /'
    section "Docker"
    docker_slice status | sed 's/^/  /'
    section "memory experiments"
    experiment list
    section "last measurements"
    [[ -n "${target_user}" ]] && runuser -u "${target_user}" -- env HOME="$(user_home)" python3 -I "${SRC}/cpu-watch" report 2>/dev/null | tail -n 5 | sed 's/^/  /'
)

case "${cmd}" in
    install) install_all ;;
    uninstall) uninstall ;;
    status) status ;;
    game)
        case "${1:-state}" in
            # By hand: stays on until 'game off' (the watchdog leaves it alone).
            on) install -d -m 0755 /run/cpu-setup; touch /run/cpu-setup/manual; systemctl start cpu-game.service
                cpu-game-profile state ;;
            off) install -d -m 0755 /run/cpu-setup; touch /run/cpu-setup/suppress   # until this game ends
                 systemctl stop cpu-game.service; cpu-game-profile state ;;
            state) bash "${SRC}/game-profile" state ;;
            *) echo "game: on|off|state" >&2; exit 2 ;;
        esac
        ;;
    vm-pin) python3 -I "${SRC}/vm-pin" "$@" ;;
    docker-slice) docker_slice "$@" ;;
    experiment) experiment "$@" ;;
esac
