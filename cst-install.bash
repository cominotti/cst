# shellcheck shell=bash
# SPDX-License-Identifier: MPL-2.0
#
# cst-install.bash: how the setup scripts install files. Sourced, not run.
#
# Every file a setup script copies from CST is an entry of INSTALL_MANIFEST
# (cst_core/domain/install.py), which says where it goes and with which
# mode; cst_core/installer.py writes it (a python-entry as a zipapp, a
# `*.in` template with --set KEY=VALUE filled in), only when it changed,
# atomically, and prints `unchanged: DEST` or `written:   DEST`.
# Always on the system Python, so the zipapp bytes do not depend on PATH.

CST_INSTALLER="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/cst_core/installer.py"

# _cst_installer_as USER COMMAND ARGS...: the installer as USER (through
# runuser), or as the current user when USER is "". CST_LC_CTYPE hands the
# caller's LC_CTYPE over ("" when unset, "=VALUE" when set): Python changes it
# at start under a C locale, and installer.py puts it back for its commands.
_cst_installer_as() {
    local user="$1"
    shift
    local -a as=()
    # The current user needs no runuser (which only root may call).
    [[ -z "${user}" || "${user}" == "$(id -un)" ]] || as=(runuser -u "${user}" --)
    CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" "${as[@]}" /usr/bin/python3 -I -B "${CST_INSTALLER}" "$@"
}

# cst_installer COMMAND ARGS...: the installer as the current user (root in a setup script).
cst_installer() {
    _cst_installer_as "" "$@"
}

# cst_install [--set KEY=VALUE]... (--component C | SRC...): root-owned
# manifest files. Each is listed in the install record (cst_core/domain/record.py);
# a component is "pending" there until cst_mark says its setup finished.
cst_install() {
    cst_installer install "$@"
}

# cst_mark COMPONENT: its setup command finished (the record says done; a
# crash between the install and this leaves it pending: cst doctor says so).
cst_mark() {
    cst_installer mark --component "$1" "done"
}

# cst_remove (--component C | SRC...): root-owned manifest files removed
# (a link itself), with their lines in the install record; `removed:   DEST`
# for each that was there. The files are the manifest's, so it also works
# for an install from before the record.
cst_remove() {
    cst_installer remove "$@"
}

# cst_remove_text DEST...: generated files (cst_install_text with a
# component) removed, with their record lines.
cst_remove_text() {
    cst_installer remove-text "$@"
}

# cst_forget (--component C | SRC...): root-owned manifest files dropped
# from the install record, the files left as they are (one a setup script
# keeps because it did not write it); prints nothing.
cst_forget() {
    cst_installer forget "$@"
}

# cst_install_as USER [--set KEY=VALUE]... SRC...: files of USER's
# home (dest `~/...`), written by a process running as
# USER: no root-owned folders in the home, no link followed with root rights.
cst_install_as() {
    local user="$1" home
    shift
    home="$(getent passwd "${user}" | cut -d: -f6)"
    [[ -n "${home}" ]] || { echo "cst_install_as: no home folder for user '${user}'" >&2; return 1; }
    _cst_installer_as "${user}" install --home "${home}" "$@"
}

# _cst_user_home USER: USER's home folder (an error when there is none).
_cst_user_home() {
    local home
    home="$(getent passwd "$1" | cut -d: -f6)"
    [[ -n "${home}" ]] || { echo "cst-install.bash: no home folder for user '$1'" >&2; return 1; }
    printf '%s\n' "${home}"
}

# cst_mark_as USER COMPONENT / cst_remove_as USER (--component C | SRC...):
# cst_mark and cst_remove for a component of USER's home, run as USER.
cst_mark_as() {
    local home
    home="$(_cst_user_home "$1")" || return 1
    _cst_installer_as "$1" mark --home "${home}" --component "$2" "done"
}
cst_remove_as() {
    local user="$1" home
    shift
    home="$(_cst_user_home "${user}")" || return 1
    _cst_installer_as "${user}" remove --home "${home}" "$@"
}

# cst_remove_text_as USER DEST... / cst_forget_text_as USER DEST...: generated
# files of USER (cst_install_text DEST MODE USER COMPONENT) removed with their
# record lines, or only their lines dropped (the files left as they are), as USER.
cst_remove_text_as() {
    _cst_text_as "$1" remove-text "${@:2}"
}
cst_forget_text_as() {
    _cst_text_as "$1" forget-text "${@:2}"
}
_cst_text_as() {
    local home
    home="$(_cst_user_home "$1")" || return 1
    _cst_installer_as "$1" "$2" --home "${home}" "${@:3}"
}

# _cst_installer_text COMMAND DEST MODE [OWNER [COMPONENT]] < content: installer.py text
# or text-plan, as OWNER unless OWNER is root (the default).
_cst_installer_text() {
    local command="$1" dest="$2" mode="$3" owner="${4:-root}" user=""
    local -a comp=()
    [[ "${owner}" == root ]] || user="${owner}"
    [[ -z "${5:-}" ]] || comp=(--component "$5")
    _cst_installer_as "${user}" "${command}" "${dest}" "${mode}" "${comp[@]}"
}

# cst_install_text DEST MODE [OWNER [COMPONENT]] < content: generated text (not a file of
# CST), with the same rules. OWNER other than root: written as that user.
# COMPONENT: listed in the install record under it (cst_remove_text removes it).
cst_install_text() {
    _cst_installer_text text "$@"
}

# cst_install_text_plan DEST MODE [OWNER] < content: what cst_install_text
# would do, changing nothing: `unchanged: DEST` or `would write: DEST`.
cst_install_text_plan() {
    _cst_installer_text text-plan "$@"
}

# cst_installed_dests [--home DIR] (--module M | --component C | SRC...) [--kind K]:
# the destinations, one per line (for uninstall).
cst_installed_dests() {
    cst_installer dests "$@"
}

# cst_component_programs COMPONENT: the sources (relative to the CST folder)
# of its programs (python-entry and shell), one per line: what a setup script
# checks for syntax errors before it installs anything.
cst_component_programs() {
    cst_installer srcs --component "$1" --kind python-entry &&
        cst_installer srcs --component "$1" --kind shell
}

# cst_component_units COMPONENT: the names of its unit files (drop-ins left
# out: they are no unit of their own), one per line, for systemctl.
cst_component_units() {
    local d dests
    dests="$(cst_installer dests --component "$1" --kind unit)" || return 1
    while IFS= read -r d; do
        [[ -n "${d}" ]] || continue
        [[ "${d%/*}" == *.d ]] || printf '%s\n' "${d##*/}"
    done <<<"${dests}"
}
