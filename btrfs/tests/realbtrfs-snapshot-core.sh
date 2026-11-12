#!/usr/bin/env bash
#
# realbtrfs-snapshot-core.sh SCRATCH
#
# Opt-in test of btrfs-snapshot-core against a real btrfs, as root, confined
# to SCRATCH: an existing, otherwise unused btrfs subvolume (e.g.
# `btrfs subvolume create ~/.snaptest` as the user). Everything happens in a
# fresh subvolume SCRATCH/run.<pid> that is deleted again at the end; nothing
# outside SCRATCH is read or written (own lock, state and pin files, NTP check
# skipped with --assume-clock-ok).
#
#   sudo bash btrfs/tests/realbtrfs-snapshot-core.sh ~/.snaptest
#   # from a toolbox:
#   flatpak-spawn --host sudo -n bash "$PWD/btrfs/tests/realbtrfs-snapshot-core.sh" ~/.snaptest
#
# Covers: idle source -> no new snapshot (kernel >= 7.1 keeps an idle
# source's generation below the snapshot's), writes -> new snapshot, writes in
# a nested subvolume don't count and its content is not in the snapshot,
# dry-run changes nothing, deletion by retention, pins, foreign entries never
# deleted (writable, other source, symlink, plain dir), circuit breaker, lock,
# a future-dated newest snapshot not blocking creation.

set -Euo pipefail

CORE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/btrfs-snapshot-core"
SCRATCH="${1:-}"

die() { echo "realbtrfs: $*" >&2; exit 2; }
(( EUID == 0 )) || die "run as root (btrfs subvolume show/delete need it)"
[[ "${SCRATCH}" == /* ]] || die "usage: $0 /absolute/scratch/subvolume"
[[ -d "${SCRATCH}" && ! -L "${SCRATCH}" ]] || die "${SCRATCH} is not a directory"
[[ "$(stat -f -c %T "${SCRATCH}")" == btrfs ]] || die "${SCRATCH} is not on btrfs"
# Only a subvolume root: the run lives in a subvolume of its own below it.
[[ "$(stat -c %i "${SCRATCH}")" == 256 ]] || die "${SCRATCH} is not a subvolume"
[[ -x "${CORE}" || -r "${CORE}" ]] || die "${CORE} not found"

RUN="${SCRATCH}/run.$$"
btrfs subvolume create "${RUN}" >/dev/null || die "cannot create ${RUN}"
cleanup() {
    # -R: the run subvolume and everything below it (sources, snapshots).
    btrfs subvolume delete -R "${RUN}" >/dev/null 2>&1 ||
        echo "realbtrfs: could not delete ${RUN}; remove it with: btrfs subvolume delete -R ${RUN}" >&2
}
trap cleanup EXIT

SRC="${RUN}/src"
SNAPS="${RUN}/snaps"
OTHER="${RUN}/other"
LOCK="${RUN}/lock"
PINS="${RUN}/pinned"
STATE="${RUN}/state"
OUT="${RUN}/out"
btrfs subvolume create "${SRC}" >/dev/null
btrfs subvolume create "${SRC}/nested" >/dev/null
btrfs subvolume create "${OTHER}" >/dev/null
mkdir "${SNAPS}"
echo data >"${SRC}/file"
echo nested >"${SRC}/nested/inner"
echo other >"${OTHER}/file"
sync

# A source's Generation moves when the transaction with the write commits
# (every 30 s, or at a sync): a write in the last seconds before a run is
# picked up by the next one. The hourly timer never sees that window matter;
# the test commits explicitly after each write.
commit() { btrfs filesystem sync "${SRC}" >/dev/null; }

POLICY="2h 1d"
fails=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; sed 's/^/    | /' "${OUT}"; fails=$((fails + 1)); }

core() {   # core CMD [args...]: rc in $rc, output in ${OUT}
    local cmd="$1"; shift
    local extra=()
    case "${cmd}" in
        snapshot|prune) extra=(--lock "${LOCK}") ;;
    esac
    python3 -I "${CORE}" "${cmd}" --snapdir "${SNAPS}" --source "t=${SRC}:${POLICY}" \
        --state-dir "${STATE}" --pin-file "${PINS}" --assume-clock-ok "${extra[@]}" "$@" >"${OUT}" 2>&1
    rc=$?
}
count() { find "${SNAPS}" -mindepth 1 -maxdepth 1 -name 't.*' -inum 256 | wc -l; }
listing() { (cd "${SNAPS}" && find . -mindepth 1 -maxdepth 1 -printf '%f %i %y\n' | sort); }
old_snapshot() {   # NAME.TS from SRC, read-only, with an old date in the name
    btrfs subvolume snapshot -r "${SRC}" "${SNAPS}/$1" >/dev/null
}
uuid_of() { btrfs subvolume show "$1" | awk -F':[[:space:]]*' '$1 ~ /^[[:space:]]*UUID$/ {print $2}'; }
parent_of() { btrfs subvolume show "$1" | awk -F':[[:space:]]*' '$1 ~ /^[[:space:]]*Parent UUID$/ {print $2}'; }

# 1. First run creates a read-only snapshot of SRC.
core snapshot
first="$(find "${SNAPS}" -mindepth 1 -maxdepth 1 -name 't.*' -printf '%p\n' | head -n1)"
if (( rc == 0 )) && [[ "$(count)" == 1 && "$(btrfs property get -ts "${first}" ro)" == ro=true &&
      "$(parent_of "${first}")" == "$(uuid_of "${SRC}")" ]]; then
    pass "first run creates a read-only snapshot owned by the source"
else
    fail "first run (rc=${rc}, count=$(count))"
fi

# 2. Idle source: nothing new.
commit
core snapshot
if (( rc == 0 )) && [[ "$(count)" == 1 ]] && grep -q unchanged "${OUT}"; then
    pass "idle source: second run creates nothing"
else
    fail "idle source (rc=${rc}, count=$(count))"
fi

# 3. Writes inside the nested subvolume don't change SRC.
echo more >>"${SRC}/nested/inner"
commit
core snapshot
if (( rc == 0 )) && [[ "$(count)" == 1 ]]; then
    pass "writes in the nested subvolume do not trigger a snapshot"
else
    fail "nested writes (rc=${rc}, count=$(count))"
fi

# 4. Dry run after a write: says what it would do, changes nothing.
echo changed >>"${SRC}/file"
commit
before="$(listing)"
core snapshot --dry-run
if (( rc == 0 )) && [[ "$(listing)" == "${before}" ]] && grep -q 'would create' "${OUT}"; then
    pass "dry run changes nothing"
else
    fail "dry run (rc=${rc})"
fi

# 5. A write creates a new snapshot (same minute: _1 suffix).
core snapshot
if (( rc == 0 )) && [[ "$(count)" == 2 ]] && grep -q 'created t\.' "${OUT}"; then
    pass "a write creates a new snapshot"
else
    fail "write (rc=${rc}, count=$(count))"
fi

# 6. The nested subvolume is not in the snapshot (an empty placeholder).
newest="$(python3 -I "${CORE}" latest --snapdir "${SNAPS}" --name t --source-path "${SRC}" 2>/dev/null)"
if [[ -n "${newest}" && -d "${newest}/nested" && -z "$(ls -A "${newest}/nested")" &&
      "$(cat "${newest}/file")" == $'data\nchanged' ]]; then
    pass "nested subvolume excluded from the snapshot; latest = ${newest##*/}"
else
    echo "latest: ${newest}" >"${OUT}"; ls -la "${newest}" "${newest}/nested" >>"${OUT}" 2>&1
    fail "nested exclusion / latest"
fi

# 7. Foreign entries with matching names; none may ever be deleted.
btrfs subvolume snapshot "${SRC}" "${SNAPS}/t.20250101T0100" >/dev/null           # writable
btrfs subvolume snapshot -r "${OTHER}" "${SNAPS}/t.20250101T0200" >/dev/null      # other source
mkdir "${SNAPS}/t.20250101T0400"                                                  # plain directory
# Old, owned, deletable snapshots; one pinned; one reachable through a symlink.
for ts in 20250201T0000 20250301T0000 20250401T0000 20250501T0000; do old_snapshot "t.${ts}"; done
ln -s "${SNAPS}/t.20250301T0000" "${SNAPS}/t.20250101T0300"
echo "${SNAPS}/t.20250401T0000" >"${PINS}"
# Three newer snapshots, so the old ones are not among the 3 newest by generation;
# '*h' keeps everything in those runs, so only the prune below deletes.
POLICY="*h"; for _ in 1 2 3; do core snapshot --force; done; POLICY="2h 1d"
core prune
gone=""; for ts in 20250201T0000 20250301T0000 20250501T0000; do [[ -e "${SNAPS}/t.${ts}" ]] || gone+="${ts} "; done
if (( rc == 0 )) && [[ "${gone}" == "20250201T0000 20250301T0000 20250501T0000 " ]] &&
   [[ -d "${SNAPS}/t.20250401T0000" && -d "${SNAPS}/t.20250101T0100" && -d "${SNAPS}/t.20250101T0200" &&
      -L "${SNAPS}/t.20250101T0300" && -d "${SNAPS}/t.20250101T0400" ]] &&
   [[ "$(grep -c 'deleted /' "${OUT}")" == 3 ]]; then
    pass "retention deletes old owned snapshots; pinned and foreign entries stay"
else
    fail "deletion (rc=${rc}, gone=${gone})"
fi

# 8. Circuit breaker: 6 deletions at once are refused, then allowed explicitly.
: >"${PINS}"
for ts in 20240101T0000 20240201T0000 20240301T0000 20240401T0000 20240501T0000 20240601T0000; do
    old_snapshot "t.${ts}"
done
POLICY="*h"; for _ in 1 2 3; do core snapshot --force; done; POLICY="2h 1d"
before="$(listing)"
core prune
if (( rc == 4 )) && [[ "$(listing)" == "${before}" ]] && grep -q 'circuit breaker' "${OUT}"; then
    pass "circuit breaker refuses a mass deletion (exit 4)"
else
    fail "circuit breaker (rc=${rc})"
fi
core prune --allow-mass-delete
left="$(find "${SNAPS}" -mindepth 1 -maxdepth 1 -name 't.2024*' | wc -l)"
if (( rc == 0 )) && [[ "${left}" == 0 ]]; then
    pass "--allow-mass-delete deletes them"
else
    fail "allow mass delete (rc=${rc}, left=${left})"
fi

# 9. Lock busy -> exit 3, nothing done.
echo again >>"${SRC}/file"
commit
before="$(listing)"
flock "${LOCK}" python3 -I "${CORE}" snapshot --snapdir "${SNAPS}" --source "t=${SRC}:${POLICY}" \
    --state-dir "${STATE}" --pin-file "${PINS}" --assume-clock-ok --lock "${LOCK}" >"${OUT}" 2>&1
rc=$?
if (( rc == 3 )) && [[ "$(listing)" == "${before}" ]]; then
    pass "lock busy -> exit 3"
else
    fail "lock busy (rc=${rc})"
fi

# 10. list --json agrees with what is on disk.
core list --json
cp "${OUT}" "${OUT}.json"
if (( rc == 0 )) && python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))["sources"][0]
assert d["changed"] is True, d["changed"]
assert not [s for s in d["snapshots"] if s["action"] == "delete"], d["snapshots"]
ignored = sorted(i["path"].rsplit("/", 1)[1] for i in d["ignored"])
assert ignored == ["t.20250101T0100", "t.20250101T0200", "t.20250101T0300", "t.20250101T0400"], ignored
' "${OUT}.json" 2>>"${OUT}"; then
    pass "list --json"
else
    fail "list --json (rc=${rc})"
fi

# 11. A snapshot named by a clock that ran ahead (in 2 days) does not block the
#     synchronized clock: a correctly named one is taken, with a warning, and
#     then counts as the newest (by generation) for the next runs.
future="t.$(date -d "+2 days" +%Y%m%d)T0000"
old_snapshot "${future}"
echo future >>"${SRC}/file"
commit
before="$(count)"
core snapshot
if (( rc == 0 )) && [[ "$(count)" == $((before + 1)) && -d "${SNAPS}/${future}" ]] &&
   grep -q "newest snapshot ${future} is dated in the future" "${OUT}"; then
    pass "a future-dated newest snapshot does not block creation with a synchronized clock"
else
    fail "future-dated newest (rc=${rc}, count=$(count), before=${before})"
fi
commit
core snapshot
if (( rc == 0 )) && [[ "$(count)" == $((before + 1)) ]] && grep -q unchanged "${OUT}"; then
    pass "after that, an idle source again creates nothing"
else
    fail "idle after future-dated (rc=${rc}, count=$(count))"
fi

echo
if (( fails )); then
    echo "realbtrfs: ${fails} check(s) FAILED"
    exit 1
fi
echo "realbtrfs: all checks passed"
