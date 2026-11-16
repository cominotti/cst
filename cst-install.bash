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

# cst_installer COMMAND ARGS...: the installer as the current user (root in a setup script).
cst_installer() {
    /usr/bin/python3 -I -B "${CST_INSTALLER}" "$@"
}

# cst_install [--set KEY=VALUE]... [--dest DEST] SRC...: root-owned manifest files.
cst_install() {
    cst_installer install "$@"
}

# cst_install_as USER [--set KEY=VALUE]... [--dest DEST] SRC...: files of USER's
# home (dest `~/...`, or a --dest inside it), written by a process running as
# USER: no root-owned folders in the home, no link followed with root rights.
cst_install_as() {
    local user="$1" home
    shift
    home="$(getent passwd "${user}" | cut -d: -f6)"
    [[ -n "${home}" ]] || { echo "cst_install_as: no home folder for user '${user}'" >&2; return 1; }
    runuser -u "${user}" -- /usr/bin/python3 -I -B "${CST_INSTALLER}" install --home "${home}" "$@"
}

# cst_install_text DEST MODE [OWNER] < content: generated text (not a file of
# CST), with the same rules. OWNER other than root: written as that user.
cst_install_text() {
    local dest="$1" mode="$2" owner="${3:-root}"
    if [[ "${owner}" == root ]]; then
        cst_installer text "${dest}" "${mode}"
    else
        runuser -u "${owner}" -- /usr/bin/python3 -I -B "${CST_INSTALLER}" text "${dest}" "${mode}"
    fi
}

# cst_installed_dests [--home DIR] (--module M | SRC...) [--kind K]: the
# destinations, one per line (for uninstall).
cst_installed_dests() {
    cst_installer dests "$@"
}
