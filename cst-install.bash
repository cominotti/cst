# shellcheck shell=bash
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
    [[ -z "${user}" ]] || as=(runuser -u "${user}" --)
    CST_LC_CTYPE="${LC_CTYPE+=${LC_CTYPE}}" "${as[@]}" /usr/bin/python3 -I -B "${CST_INSTALLER}" "$@"
}

# cst_installer COMMAND ARGS...: the installer as the current user (root in a setup script).
cst_installer() {
    _cst_installer_as "" "$@"
}

# cst_install [--set KEY=VALUE]... SRC...: root-owned manifest files.
cst_install() {
    cst_installer install "$@"
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

# _cst_installer_text COMMAND DEST MODE [OWNER] < content: installer.py text
# or text-plan, as OWNER unless OWNER is root (the default).
_cst_installer_text() {
    local command="$1" dest="$2" mode="$3" owner="${4:-root}" user=""
    [[ "${owner}" == root ]] || user="${owner}"
    _cst_installer_as "${user}" "${command}" "${dest}" "${mode}"
}

# cst_install_text DEST MODE [OWNER] < content: generated text (not a file of
# CST), with the same rules. OWNER other than root: written as that user.
cst_install_text() {
    _cst_installer_text text "$@"
}

# cst_install_text_plan DEST MODE [OWNER] < content: what cst_install_text
# would do, changing nothing: `unchanged: DEST` or `would write: DEST`.
cst_install_text_plan() {
    _cst_installer_text text-plan "$@"
}

# cst_installed_dests [--home DIR] (--module M | SRC...) [--kind K]: the
# destinations, one per line (for uninstall).
cst_installed_dests() {
    cst_installer dests "$@"
}
