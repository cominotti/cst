# swap/swap-lib.bash: helpers of swap-setup.sh (sourced, not run), kept apart
# so the tests can call them on scratch files.
#
# Two kinds of managed things:
#   - one line in a shared file (/etc/crypttab, /etc/fstab), found by a marker
#     comment at its end;
#   - a whole file of our own (tmpfiles.d, sysctl.d, a systemd drop-in).
# Both are compared first and written only when needed, atomically (a temp
# file in the same directory, then rename(2)).
#
# Older names of this tool wrote other markers. They are still recognised:
# a line or file that differs from ours only in its marker or comments is
# "adopt": the next apply rewrites the comment and changes nothing else (no
# swap restart; only a daemon-reload, so systemd does not keep flagging the
# units read from that file as "changed on disk").

SWAP_MARKER="# managed-by: swap-setup.sh (CST)"
SWAP_OLD_MARKERS=(
    "# managed-by: apply-swap-config.sh"   # the standalone script this module replaces
    "# managed-by: apply-zram-config.sh"   # the zram script before it
)

# is_marked LINE: whether LINE carries our marker or an old one.
is_marked() {
    local m
    for m in "${SWAP_MARKER}" "${SWAP_OLD_MARKERS[@]}"; do
        [[ "$1" == *"${m}"* ]] && return 0
    done
    return 1
}

# marker_payload LINE: LINE without its marker comment and trailing blanks.
marker_payload() {
    local line="$1" m
    for m in "${SWAP_MARKER}" "${SWAP_OLD_MARKERS[@]}"; do
        line="${line%%"${m}"*}"
    done
    line="${line%"${line##*[![:space:]]}"}"
    printf '%s\n' "${line}"
}

# marked_line_state PATH LINE KEY: prints one word.
#   ok          exactly one marked line, equal to LINE
#   adopt       exactly one marked line, equal to LINE except for the marker
#   duplicate   more than one marked line, all equal to LINE except for the
#               marker (e.g. the old script ran again and appended its own)
#   change      a marked line that differs
#   missing     no marked line
#   conflict    an unmarked, active line whose first field is KEY (set up by
#               hand: we would add a second entry for the same device)
#   unreadable  PATH cannot be read (crypttab needs root)
# A missing PATH counts as an empty file.
marked_line_state() {
    local path="$1" line="$2" key="$3" l n=0 first="" want same=1
    want="$(marker_payload "${line}")"
    if [[ -e "${path}" && ! -r "${path}" ]]; then echo unreadable; return; fi
    if [[ -e "${path}" ]]; then
        while IFS= read -r l || [[ -n "${l}" ]]; do
            if is_marked "${l}"; then
                n=$((n + 1))
                [[ ${n} -eq 1 ]] && first="${l}"
                [[ "$(marker_payload "${l}")" == "${want}" ]] || same=0
            elif [[ "${l}" =~ ^[[:space:]]*([^[:space:]#][^[:space:]]*) && "${BASH_REMATCH[1]}" == "${key}" ]]; then
                echo conflict
                return
            fi
        done <"${path}"
    fi
    if (( n == 0 )); then
        echo missing
    elif (( n > 1 )); then
        if (( same )); then echo duplicate; else echo change; fi
    elif [[ "${first}" == "${line}" ]]; then
        echo ok
    elif (( same )); then
        echo adopt
    else
        echo change
    fi
}

# atomic_replace TMP DEST MODE: moves the finished TMP (same directory as
# DEST) over DEST. Being in the same directory makes mv a rename(2) and gives
# the file the directory's SELinux type; restorecon afterwards as belt and
# braces (a /tmp file moved into /etc keeps user_tmp_t, which bit us once).
atomic_replace() {
    local tmp="$1" dest="$2" mode="$3"
    chmod "${mode}" "${tmp}"
    [[ ${EUID} -eq 0 ]] && chown root:root "${tmp}"
    mv -f "${tmp}" "${dest}"
    if [[ ${EUID} -eq 0 ]] && command -v restorecon >/dev/null; then
        restorecon -F "${dest}" 2>/dev/null || true
    fi
}

# write_marked_line PATH LINE MODE: the first marked line (ours or an old
# one) becomes LINE, in place; further marked lines are dropped; LINE is
# appended when there was none. A one-time backup PATH.pre-swap-config.bak
# is taken (the name the old script used, so an existing one is kept).
write_marked_line() {
    local path="$1" line="$2" mode="$3" tmp l placed=0
    [[ -e "${path}" ]] || : >"${path}"
    [[ -e "${path}.pre-swap-config.bak" ]] || cp -a "${path}" "${path}.pre-swap-config.bak"
    tmp="$(mktemp -p "$(dirname -- "${path}")" ".$(basename -- "${path}").XXXXXX")"
    while IFS= read -r l || [[ -n "${l}" ]]; do
        if is_marked "${l}"; then
            (( placed )) || printf '%s\n' "${line}"
            placed=1
        else
            printf '%s\n' "${l}"
        fi
    done <"${path}" >"${tmp}"
    (( placed )) || printf '%s\n' "${line}" >>"${tmp}"
    atomic_replace "${tmp}" "${path}" "${mode}"
}

# effective_lines < FILE: the lines that matter to the program reading the
# file (no comments, no blank lines).
effective_lines() { grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' || true; }

# file_state SRC DEST: prints ok | adopt (only comments differ) | change |
# missing | unreadable. A symlink at DEST is a change (it gets replaced).
file_state() {
    local src="$1" dest="$2"
    if [[ -L "${dest}" ]]; then echo change; return; fi
    if [[ ! -e "${dest}" ]]; then echo missing; return; fi
    if [[ ! -r "${dest}" ]]; then echo unreadable; return; fi
    if cmp -s "${src}" "${dest}"; then echo ok; return; fi
    if cmp -s <(effective_lines <"${src}") <(effective_lines <"${dest}"); then
        echo adopt
    else
        echo change
    fi
}

# write_file SRC DEST MODE: DEST becomes a copy of SRC, atomically.
write_file() {
    local src="$1" dest="$2" mode="$3" tmp
    mkdir -p "$(dirname -- "${dest}")"
    [[ -L "${dest}" ]] && rm -f "${dest}"
    tmp="$(mktemp -p "$(dirname -- "${dest}")" ".$(basename -- "${dest}").XXXXXX")"
    cat "${src}" >"${tmp}"
    atomic_replace "${tmp}" "${dest}" "${mode}"
}
