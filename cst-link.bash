# cst-link.bash: sourced (not run) by the `install` of btrfs-setup.sh and
# cpu-setup.sh, as root.
#
# Programs those installs put on the system (alerts, logs, timers) print
# hints such as "run 'cst btrfs bulk status'". This makes `cst` a real command
# for the user who ran the install, so the hints work as typed.

# link_cst_command USER: ~USER/.local/bin/cst becomes a link to the cst next
# to this file, created as USER. Fedora puts ~/.local/bin in the user's PATH.
# Something of the user's own at that path (a file, or a link elsewhere that
# still works) is left alone, with a note. A link that no longer works (the
# folder was moved) is replaced.
link_cst_command() {
    local user="$1" home target link
    [[ -n "${user}" && "${user}" != root ]] || return 0
    home="$(getent passwd "${user}" | cut -d: -f6)"
    [[ -n "${home}" && -d "${home}" ]] || return 0
    target="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/cst"
    link="${home}/.local/bin/cst"
    if [[ -L "${link}" && "$(readlink -f "${link}")" == "${target}" ]]; then
        return 0
    fi
    if [[ -L "${link}" && ! -e "${link}" ]]; then
        rm -f "${link}"
    elif [[ -e "${link}" || -L "${link}" ]]; then
        echo "NOTE: ${link} is not a link to ${target}: left alone. Hints of installed programs say 'cst ...'." >&2
        return 0
    fi
    if runuser -u "${user}" -- mkdir -p "${home}/.local/bin" &&
        runuser -u "${user}" -- ln -s "${target}" "${link}"; then
        echo "linked:    ${link} -> ${target} (the 'cst' command)"
    else
        echo "NOTE: could not create ${link}; hints of installed programs say 'cst ...' (see README.md, 'The cst command')." >&2
    fi
}
