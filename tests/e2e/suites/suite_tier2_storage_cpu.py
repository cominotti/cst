# SPDX-License-Identifier: MPL-2.0
"""Docker Layer 2 E2E suite: --privileged loopback btrfs RAID1, DinD, cgroups v2, Polkit, virsh."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import glob
import os
import shlex
import subprocess
from typing import ClassVar, override
import unittest


def docker_exec(
    container: str,
    cmd: Sequence[str],
    *,
    check: bool = True,
    user: str = "root",
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    full = ["docker", "exec"]
    if input_text is not None:
        full.append("-i")
    if user != "root":
        full.extend(["--user", user])
    if env is not None:
        for k, v in env.items():
            full.extend(["-e", f"{k}={v}"])
    full.extend([container, *cmd])
    p = subprocess.run(full, check=False, input=input_text, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(
            f"command failed (rc={p.returncode}): {' '.join(cmd)}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )
    return p


def exec_bash(container: str, script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker_exec(container, ["bash", "-euo", "pipefail", "-c", script], check=check)


# A bash function for the scripts that mount the btrfs top level (test_03,
# test_03a): the options of /var's mount, with subvolid=5 in place of its subvol.
TOP_LEVEL_OPTS_FN = r"""
top_level_opts() {
    local o
    o="$(findmnt -rn -o OPTIONS -M /var | tr ',' '\n' |
        grep -vE '^(rw|ro|seclabel|subvol=.*|subvolid=.*)$' | paste -sd, - || true)"
    echo "subvolid=5${o:+,$o}"
}
"""

# For the docker-gc tests (test_09, test_13): Docker up, as the units see it.
# The DinD daemon (e2e-dind.service, test_05) on the loop btrfs, and a stub
# docker.service (a oneshot that stays active, as in test_05a): the unit the
# recorder is bound to (BindsTo=) and docker-gc.service's ExecCondition asks.
DOCKER_UP_FN = r"""
stub=/run/systemd/system/docker.service
docker_up() {
    printf '[Unit]\nDescription=E2E stub of docker.service (dockerd runs in e2e-dind.service)\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n' \
        >"$stub"
    systemctl daemon-reload
    [[ "$(systemctl show -P FragmentPath docker.service)" == "$stub" ]]
    # As in test_05a: Fedora's generic service.d drop-ins may apply; one of docker.service may not.
    local dropins _
    dropins="$(systemctl show -P DropInPaths docker.service)"
    if tr ' ' '\n' <<<"$dropins" | grep -q '/docker\.service\.d/'; then
        echo "a docker.service drop-in applies to the stub: $dropins" >&2
        return 1
    fi
    systemctl start e2e-dind.service
    for _ in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        sleep 0.5
    done
    docker info >/dev/null
    systemctl start docker.service
}
"""

# test_05a: docker-volumes-nocow migrate on the real btrfs, with a stub
# docker.service (no dockerd: a oneshot that stays active) so that only its
# ActiveState matters. The helper and its unit come from test_02's
# `cst btrfs install`. A wrapper systemctl, first on the helper's PATH, logs
# what docker.service's ActiveState was when the helper asked (lines starting
# with E2E-NOCOW go to the CI log). Part 3 starts the unit and docker.service
# through one target, so both jobs are in one transaction, as at boot when
# multi-user.target pulls in both (`systemctl start A B` asks for each unit in
# turn, so the order of the names could decide instead). Parts 4 and 5 move
# /etc/btrfs-setup aside: Docker installed after install on such a machine,
# and the recovery of a machine an older helper left unscheduled.
NOCOW_MIGRATE_SCRIPT = r"""
helper=/usr/local/sbin/docker-volumes-nocow
vols=/var/lib/docker/volumes
flag=/etc/btrfs-setup/docker-nocow-pending
attempts=/var/lib/docker-volumes-nocow/attempts
postponed=/var/lib/docker-volumes-nocow/postponed
stub=/run/systemd/system/docker.service
dropin=/run/systemd/system/docker-volumes-nocow.service.d
target=/run/systemd/system/e2e-nocow-boot.target
# Not under /run: Docker mounts that tmpfs noexec, and a PATH entry that cannot
# be executed is skipped without a word (the real systemctl answers instead).
wrap=/usr/local/libexec/e2e-nocow-wrap
seen=/run/e2e-nocow-seen
# Parts 4 and 5 need a machine without /etc/btrfs-setup: the real one waits here.
saved=/etc/btrfs-setup.e2e-saved
echo "E2E-NOCOW /run mount options: $(findmnt -no OPTIONS /run)"

fake=/usr/local/libexec/e2e-nocow-other
other=   # the pid of a stand-in dockerd in another mount namespace (part 2)
user=    # the pid of a stand-in dockerd that does not run as root (part 2)
cleanup() {
    [[ -z "$other" ]] || kill "$other" 2>/dev/null || true
    [[ -z "$user" ]] || kill "$user" 2>/dev/null || true
    systemctl stop e2e-nocow-boot.target docker.service e2e-dind.service 2>/dev/null || true
    rm -rf "$stub" "$dropin" "$target" "$wrap" "$fake" "$seen"
    rm -rf "$vols/e2e-cow" "$vols/e2e-cow2" "$vols/e2e-cow3" "$vols/e2e-cow4"
    rm -f "$flag" "$attempts" "$postponed"
    if [[ -d "$saved" ]]; then
        rm -rf /etc/btrfs-setup
        mv "$saved" /etc/btrfs-setup
    fi
    chattr +C "$vols" 2>/dev/null || true   # as install left it
    systemctl daemon-reload
}
trap cleanup EXIT

test -x "$helper"
test -f /etc/systemd/system/docker-volumes-nocow.service
systemctl stop e2e-dind.service 2>/dev/null || true   # no dockerd on /var/lib/docker
mkdir -p "$vols"
[[ "$(lsattr -d "$vols" | awk '{print $1}')" == *C* ]]   # marked by install

# A volume as Docker had it before the volumes folder had +C: every folder and
# file copy-on-write, files with data, an empty file, a hard link, a symlink,
# a fifo, an empty folder, and _data and db owned by uid 1000 (README stays root's).
make_volume() {
    local d="$vols/$1/_data"
    mkdir "$vols/$1"
    chattr -C "$vols/$1"
    mkdir "$d"
    mkdir -p "$d/db/base" "$d/cache"
    head -c 8M /dev/urandom >"$d/db/base/1"
    head -c 300001 /dev/urandom >"$d/db/wal"
    printf 'hello volume\n' >"$d/README"
    : >"$d/db/empty"
    ln "$d/README" "$d/README.hard"
    ln -s db/wal "$d/latest"
    mkfifo "$d/db/pipe"
    chown -R 1000:1000 "$d/db"
    chown 1000:1000 "$d"   # as database images do: _data not owned by root
    chmod 0750 "$d"
    if find "$d" \( -type f -o -type d \) -exec lsattr -d {} + | awk '$1 ~ /C/ {bad=1} END {exit !bad}'; then
        echo "set-up: $d already has NOCOW entries" >&2
        return 1
    fi
}
# Paths, types, modes, owners, link counts, symlink targets and file contents.
manifest() {
    (cd "$1" && find . -printf '%p %y %m %U:%G %n %l\n' | sort && find . -type f -print0 | sort -z | xargs -0 sha256sum)
}
all_nocow() {
    local bad
    bad="$(find "$1" \( -type f -o -type d \) -exec lsattr -d {} + | awk '$1 !~ /C/')"
    [[ -z "$bad" ]] || { echo "not NOCOW:" >&2; echo "$bad" >&2; return 1; }
}

mkdir -p "$wrap"
cat >"$wrap/systemctl" <<'EOF'
#!/bin/bash
out="$(/usr/bin/systemctl "$@")"
rc=$?
{
    printf 'E2E-NOCOW helper asked: systemctl %s -> rc=%s answer=%s\n' "$*" "$rc" "$out"
    /usr/bin/systemctl list-jobs --no-legend --no-pager | sed 's/^/E2E-NOCOW   queued job: /'
} >>/run/e2e-nocow-seen
printf '%s\n' "$out"
exit "$rc"
EOF
chmod 0755 "$wrap/systemctl"

cat >"$stub" <<'EOF'
[Unit]
Description=E2E stub of docker.service (no dockerd)

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/true
EOF
systemctl daemon-reload
[[ "$(systemctl show -P FragmentPath docker.service)" == "$stub" ]]
# Fedora's generic service.d drop-ins may apply; one of docker.service may not.
dropins="$(systemctl show -P DropInPaths docker.service)"
echo "E2E-NOCOW stub docker.service drop-ins: ${dropins:-none}"
if tr ' ' '\n' <<<"$dropins" | grep -q '/docker\.service\.d/'; then
    echo "a docker.service drop-in applies to the stub: $dropins" >&2
    exit 1
fi

# 1. docker.service active: postponed, nothing touched, no attempt counted.
make_volume e2e-cow
d="$vols/e2e-cow/_data"
before="$(manifest "$d")"
ino="$(stat -c %i "$d")"
install -d -m 0755 /etc/btrfs-setup
touch "$flag"
rm -f "$attempts" "$postponed"
systemctl start docker.service
out="$(PATH="$wrap:$PATH" "$helper" migrate)"
printf '%s\n' "$out"
grep -q 'Docker is running: migration left for the next boot' <<<"$out"
[[ "$(stat -c %i "$d")" == "$ino" && "$(manifest "$d")" == "$before" ]]
[[ "$(lsattr -d "$d/db/base/1" | awk '{print $1}')" != *C* ]]
test -e "$flag"
test ! -e "$attempts"
[[ "$(ls -A "$vols/e2e-cow")" == _data ]]
# Not an attempt, but recorded for status and cst doctor (world-readable).
grep -q '"reason": "Docker is running", "boots": 1,' "$postponed"
[[ "$(stat -c %a "$postponed")" == 644 ]]
grep -q '^  migration postponed at 1 boot(s) in a row, last .*: Docker is running$' <<<"$("$helper" status)"
echo "E2E-NOCOW postponed: docker.service $(systemctl show -P ActiveState docker.service)"
if [[ ! -s "$seen" ]]; then
    echo "the helper did not go through the systemctl wrapper ($wrap)" >&2
    exit 1
fi
cat "$seen"
rm -f "$seen"
systemctl stop docker.service

# 1b. A dockerd outside docker.service on this data root (test_05's DinD unit
# runs dockerd --data-root=/var/lib/docker): postponed, found through /proc.
systemctl start e2e-dind.service
out="$("$helper" migrate)"
printf '%s\n' "$out"
grep -q "^<4>docker-volumes-nocow: dockerd (pid [0-9]*) uses $vols outside docker.service: migration left" <<<"$out"
[[ "$(stat -c %i "$d")" == "$ino" && "$(manifest "$d")" == "$before" ]]
test -e "$flag"
test ! -e "$attempts"
grep -q '"reason": "dockerd (pid [0-9]*) uses .*", "boots": 1,' "$postponed"   # the same boot: still 1
echo "E2E-NOCOW dockerd outside docker.service: $(grep -o 'dockerd (pid [0-9]*)' <<<"$out")"
systemctl stop e2e-dind.service

# A process named dockerd in another mount namespace with its own
# /var/lib/docker (as in a container) must not hold the migration back: it
# runs during part 2. A copy of sleep stands in for it.
mkdir -p "$fake"
cp /usr/bin/sleep "$fake/dockerd"
unshare --mount --propagation private sh -c \
    'mount -t tmpfs e2e-other-ns /var/lib/docker && mkdir /var/lib/docker/volumes && exec "$1" infinity' \
    _ "$fake/dockerd" &
other=$!
for _ in $(seq 1 50); do
    [[ "$(cat "/proc/$other/comm" 2>/dev/null)" == dockerd ]] && break
    sleep 0.1
done
[[ "$(cat "/proc/$other/comm")" == dockerd ]]
[[ "$(readlink "/proc/$other/ns/mnt")" != "$(readlink /proc/self/ns/mnt)" ]]
test -d "/proc/$other/root/var/lib/docker/volumes"
# And one in our mount namespace, with no flags (so the default data root,
# ours), that does not run as root: what rootless Docker looks like from
# here (plain dockerd as the user; its namespace keeps our /). It cannot
# write to the root-owned data root and must not hold the migration back.
setpriv --reuid=65534 --regid=65534 --clear-groups "$fake/dockerd" infinity &
user=$!
for _ in $(seq 1 50); do
    [[ "$(cat "/proc/$user/comm" 2>/dev/null)" == dockerd ]] && break
    sleep 0.1
done
[[ "$(cat "/proc/$user/comm")" == dockerd ]]
[[ "$(awk '/^Uid:/ {print $3}' "/proc/$user/status")" == 65534 ]]
[[ "$(readlink "/proc/$user/ns/mnt")" == "$(readlink /proc/self/ns/mnt)" ]]

# 2. docker.service inactive: the volume is rewritten NOCOW.
echo "E2E-NOCOW free (estimated) bytes: $(btrfs filesystem usage -b "$vols" | awk '/Free \(estimated\)/ {print $3}')"
out="$("$helper" migrate)"
printf '%s\n' "$out"
grep -q 'all Docker volumes are NOCOW now' <<<"$out"
test ! -e "$postponed"   # a boot not held back removes the record
kill "$other" "$user"
wait "$other" "$user" || true
other=''
user=''
[[ "$(manifest "$d")" == "$before" ]]
[[ "$(stat -c %i "$d")" != "$ino" ]]   # a new copy
all_nocow "$d"
[[ "$(ls -A "$vols/e2e-cow")" == _data ]]
test ! -e "$flag"
test ! -e "$attempts"
st="$("$helper" status)"
printf '%s\n' "$st"
if grep -q 'with copy-on-write data' <<<"$st"; then
    echo "status still shows copy-on-write data" >&2
    exit 1
fi
# A second run changes nothing more.
ino="$(stat -c %i "$d")"
[[ -z "$("$helper" migrate)" ]]
[[ "$(stat -c %i "$d")" == "$ino" ]]

# 3. The unit and docker.service started in one transaction (as at boot):
# docker.service's job waits for the unit (Before=docker.service), so the
# helper still migrates.
make_volume e2e-cow2
d2="$vols/e2e-cow2/_data"
before2="$(manifest "$d2")"
ino2="$(stat -c %i "$d2")"
touch "$flag"
mkdir -p "$dropin"
printf '[Service]\nEnvironment=PATH=%s:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin\n' "$wrap" \
    >"$dropin/50-e2e-wrap.conf"
printf '[Unit]\nDescription=E2E boot-like transaction\nWants=docker-volumes-nocow.service docker.service\n' \
    >"$target"
systemctl daemon-reload
systemctl start e2e-nocow-boot.target
cat "$seen"
grep -q 'helper asked: systemctl show -P ActiveState docker.service' "$seen"
echo "E2E-NOCOW transaction: helper saw docker.service" \
    "$(sed -n 's/.*ActiveState docker.service -> rc=[0-9]* answer=//p' "$seen" | head -n 1)"
[[ "$(systemctl show -P Result docker-volumes-nocow.service)" == success ]]
[[ "$(systemctl show -P ActiveState docker.service)" == active ]]
done_at="$(systemctl show -P InactiveEnterTimestampMonotonic docker-volumes-nocow.service)"
docker_at="$(systemctl show -P InactiveExitTimestampMonotonic docker.service)"
echo "E2E-NOCOW transaction: nocow unit done at ${done_at}, docker.service started at ${docker_at} (monotonic us)"
(( done_at > 0 && done_at <= docker_at ))
[[ "$(manifest "$d2")" == "$before2" ]]
[[ "$(stat -c %i "$d2")" != "$ino2" ]]
all_nocow "$d2"
[[ "$(ls -A "$vols/e2e-cow2")" == _data ]]
test ! -e "$flag"
test ! -e "$attempts"
journalctl -u docker-volumes-nocow.service -o cat --no-pager -n 5 2>/dev/null | sed 's/^/E2E-NOCOW journal: /' || true
systemctl stop e2e-nocow-boot.target docker.service

# 4. Docker installed after `cst btrfs install`, on a machine without
# /etc/btrfs-setup (install creates it only when it finds CoW data): the
# volumes folder has no +C and holds a CoW volume. The unit, at that boot,
# writes the flag (creating its folder) before it sets +C, and migrates.
mv /etc/btrfs-setup "$saved"
chattr -C "$vols"
make_volume e2e-cow3
d3="$vols/e2e-cow3/_data"
before3="$(manifest "$d3")"
ino3="$(stat -c %i "$d3")"
systemctl start docker-volumes-nocow.service
[[ "$(systemctl show -P Result docker-volumes-nocow.service)" == success ]]
journalctl -u docker-volumes-nocow.service -o cat --no-pager -n 3 2>/dev/null | sed 's/^/E2E-NOCOW part 4: /' || true
[[ "$(lsattr -d "$vols" | awk '{print $1}')" == *C* ]]
[[ "$(manifest "$d3")" == "$before3" ]]
[[ "$(stat -c %i "$d3")" != "$ino3" ]]
all_nocow "$d3"
[[ "$(ls -A "$vols/e2e-cow3")" == _data ]]
test -d /etc/btrfs-setup
test ! -e "$flag"
test ! -e "$attempts"

# 5. A machine an older helper left behind in that case: the volumes folder
# has +C, a volume still holds CoW data, and nothing is scheduled. Running
# `cst btrfs install` again schedules it (mark finds the CoW data; install
# creates /etc/btrfs-setup and the flag), and the next boot migrates it.
rm -rf /etc/btrfs-setup
make_volume e2e-cow4
d4="$vols/e2e-cow4/_data"
before4="$(manifest "$d4")"
ino4="$(stat -c %i "$d4")"
st="$("$helper" status)"
printf '%s\n' "$st"
grep -q 'with copy-on-write data (.*), migration not scheduled' <<<"$st"
SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs install --no-below
test -f "$flag"
grep -q 'migration at the next boot' <<<"$("$helper" status)"
systemctl start docker-volumes-nocow.service
[[ "$(systemctl show -P Result docker-volumes-nocow.service)" == success ]]
[[ "$(manifest "$d4")" == "$before4" ]]
[[ "$(stat -c %i "$d4")" != "$ino4" ]]
all_nocow "$d4"
[[ "$(ls -A "$vols/e2e-cow4")" == _data ]]
test ! -e "$flag"
test ! -e "$attempts"
echo "E2E-NOCOW parts 4 and 5: migrated without /etc/btrfs-setup, and after install on an affected machine"
"""


# test_05b, test_05c: docker-volumes-nocow's boot run (its unit, from test_02's
# install) when a state file cannot be changed, on the loop btrfs with Docker
# stopped. The volumes folder and the state are as test_05a left them: the
# folder with +C, no flag, no count. Each script names the journal lines of its
# own runs by time: mark gives a second after every line sent so far.
NOCOW_FAULT_FNS = r"""
helper=/usr/local/sbin/docker-volumes-nocow
unit=docker-volumes-nocow.service
vols=/var/lib/docker/volumes
flag=/etc/btrfs-setup/docker-nocow-pending
state=/var/lib/docker-volumes-nocow
attempts=$state/attempts
rewriting='without copy-on-write before Docker starts'
gave_up='docker-volumes-nocow: Gave up after two boots'
fail() { echo "$*" >&2; exit 1; }
idle_docker() {   # no Docker that could use the volumes: the boot's run goes on
    systemctl stop docker.service e2e-dind.service 2>/dev/null || true
    case "$(systemctl show -P ActiveState docker.service)" in
        inactive | failed) ;;
        *) fail "docker.service is not stopped" ;;
    esac
}
cow_volume() {   # cow_volume NAME: a volume whose data is copy-on-write
    mkdir "$vols/$1"
    chattr -C "$vols/$1"
    mkdir "$vols/$1/_data"
    head -c 1M /dev/urandom >"$vols/$1/_data/db"
    [[ "$(lsattr "$vols/$1/_data/db" | awk '{print $1}')" != *C* ]]
}
run_unit() {   # one boot's run, as systemd starts it
    systemctl reset-failed "$unit" 2>/dev/null || true
    systemctl start "$unit"
    [[ "$(systemctl show -P Result "$unit")" == success ]]
}
mark() { journalctl --sync; sleep 1.1; date +%s; }
since() { journalctl --sync; journalctl -q --no-pager -o cat -u "$unit" --since "@$1"; }
lines() { since "$1" | grep -cF -- "$2" || true; }   # lines T TEXT: the unit's lines since T with TEXT
wait_lines() {   # wait_lines T TEXT N: up to 10 s for N such lines; the journal on failure
    local _
    for _ in $(seq 1 20); do
        (( $(lines "$1" "$2") >= $3 )) && return 0
        sleep 0.5
    done
    since "$1" >&2
    return 1
}
"""

# test_05b: the third boot of a scheduled migration gives up, but the flag's
# folder is read-only (a read-only bind mount over /etc/btrfs-setup) while the
# count's is not. The count of 2 must stay with the flag: three boots each give
# up again at once (no copy, no alert); once the mount is gone, the next boot
# gives up for good, removes both and keeps one alert for the next login.
NOCOW_GIVE_UP_SCRIPT = (
    r"""
set -E
trap 'echo "test_05b FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
"""
    + NOCOW_FAULT_FNS
    + r"""
vol=e2e-give-up
title='Docker volumes: NOCOW migration gave up'
erofs="$vols: [Errno 30] Read-only file system: '$flag'"
pending=/var/lib/cst-alerts/pending
aside=/var/tmp/e2e-05b-alerts
made_etc=0
ours() { grep -lxF "title=$title" "$pending"/*.alert 2>/dev/null || true; }   # kept alerts of this title
cleanup() {
    local f
    while mountpoint -q /etc/btrfs-setup; do umount /etc/btrfs-setup || break; done
    rm -rf "${vols:?}/$vol"
    rm -f "$flag" "$attempts" "$state/postponed"
    for f in $(ours); do rm -f "$f"; done
    if [[ -d "$aside" ]]; then
        for f in "$aside"/*.alert; do
            if [[ -e "$f" ]]; then mv "$f" "$pending/"; fi
        done
        rm -rf "$aside"
    fi
    (( made_etc == 0 )) || rmdir /etc/btrfs-setup 2>/dev/null || true
    systemctl reset-failed "$unit" 2>/dev/null || true
}
trap cleanup EXIT

test -x "$helper"
idle_docker
[[ "$(lsattr -d "$vols" | awk '{print $1}')" == *C* ]]   # as install (and test_05a) left it
test ! -e "$flag"
if [[ ! -d /etc/btrfs-setup ]]; then
    made_etc=1
    install -d -m 0755 /etc/btrfs-setup
fi
# An alert of this title kept before (another test's) waits aside: this test counts its own.
mkdir -p "$aside"
for f in $(ours); do mv "$f" "$aside/"; done
cow_volume "$vol"
d="$vols/$vol/_data"
ino="$(stat -c %i "$d")"
sum="$(sha256sum <"$d/db")"
install -d -m 0755 "$state"
rm -f "$state/postponed"
touch "$flag"
printf 2 >"$attempts"   # two boots have tried: the next one gives up
mount --bind /etc/btrfs-setup /etc/btrfs-setup
mount -o remount,bind,ro /etc/btrfs-setup
if touch /etc/btrfs-setup/e2e-probe 2>/dev/null; then fail "/etc/btrfs-setup is still writable"; fi
t0="$(mark)"

# 1. Three boots with the flag stuck: each gives up at once and puts the count back.
for boot in 1 2 3; do
    run_unit
    [[ "$(cat "$attempts" 2>&1)" == 2 ]] || fail "boot $boot: the count is $(cat "$attempts" 2>&1)"
    test -e "$flag"
    wait_lines "$t0" "$erofs" "$boot"
done
[[ "$(lines "$t0" "$erofs")" == 3 ]]
[[ "$(lines "$t0" "$gave_up")" == 0 ]]
[[ "$(lines "$t0" "$rewriting")" == 0 ]]   # no try started over
[[ "$(stat -c %i "$d")" == "$ino" ]]
[[ "$(sha256sum <"$d/db")" == "$sum" ]]
[[ "$(ls -A "$vols/$vol")" == _data ]]   # no copy beside it
[[ "$(lsattr "$d/db" | awk '{print $1}')" != *C* ]]
[[ -z "$(ours)" ]] || fail "an alert was kept before the give-up: $(ours)"
echo "E2E-NOCOW give-up with /etc/btrfs-setup read-only: 3 boots, count $(cat "$attempts"), flag kept"

# 2. The flag's folder writable again: the next boot gives up for good and keeps one alert.
umount /etc/btrfs-setup
run_unit
wait_lines "$t0" "$gave_up" 1
test ! -e "$flag"
test ! -e "$attempts"
kept="$(ours)"
[[ -n "$kept" && "$(wc -l <<<"$kept")" == 1 ]] || fail "kept alerts of the give-up: ${kept:-none}"
grep -qx 'count=1' "$kept"
[[ "$(stat -c %i "$d")" == "$ino" ]]
[[ "$(lines "$t0" "$rewriting")" == 0 ]]
grep -q 'migration not scheduled' <<<"$("$helper" status)"

# 3. A later boot has nothing left to give up: no second alert.
run_unit
[[ "$(lines "$t0" "$gave_up")" == 1 ]]
[[ "$(lines "$t0" "$erofs")" == 3 ]]
grep -qx 'count=1' "$kept"
echo E2E-OK
"""
)

# test_05c: a count of boots an older helper left without its flag (2), the
# volumes folder without +C and a copy-on-write volume (a new request), and
# the count cannot be reset: a read-only bind mount of the count's file (a
# rename over a mount point fails; the folder stays writable for the postponed
# record). A read-only mount of the whole folder would fail the run earlier,
# at the postponed record's removal (unlink there is EROFS even for a missing
# file), and never reach the reset. The run makes no request (no flag, no +C)
# and logs it; once the mount is gone, the next boot makes the request and
# migrates.
NOCOW_RESET_SCRIPT = (
    r"""
set -E
trap 'echo "test_05c FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
"""
    + NOCOW_FAULT_FNS
    + r"""
vol=e2e-reset
refused='cannot schedule the migration ('
cleanup() {
    while mountpoint -q "$attempts"; do umount "$attempts" || break; done
    rm -rf "${vols:?}/$vol"
    rm -f "$flag" "$attempts" "$state/postponed"
    chattr +C "$vols" 2>/dev/null || true   # as install left it
    systemctl reset-failed "$unit" 2>/dev/null || true
}
trap cleanup EXIT

test -x "$helper"
idle_docker
test ! -e "$flag"
cow_volume "$vol"
chattr -C "$vols"   # Docker set up after install, say: the sign of a new request
d="$vols/$vol/_data"
ino="$(stat -c %i "$d")"
sum="$(sha256sum <"$d/db")"
install -d -m 0755 "$state"
rm -f "$state/postponed"
printf 2 >"$attempts"   # left by an older helper, without its flag
mount --bind "$attempts" "$attempts"
mount -o remount,bind,ro "$attempts"
t0="$(mark)"

# 1. The reset fails: no request, and the journal says so.
run_unit
wait_lines "$t0" "$refused" 1
grep -qF "$vols keeps copy-on-write, the next boot tries again" <<<"$(since "$t0" | grep -F "$refused")"
test ! -e "$flag"
[[ "$(lsattr -d "$vols" | awk '{print $1}')" != *C* ]]
[[ "$(cat "$attempts")" == 2 ]]
[[ "$(ls -A "$state")" == attempts ]]   # the reset's temporary file is gone
[[ "$(stat -c %i "$d")" == "$ino" ]]
[[ "$(lines "$t0" "$rewriting")" == 0 ]]
[[ "$(lines "$t0" "$gave_up")" == 0 ]]
echo "E2E-NOCOW reset refused: $(since "$t0" | grep -F "$refused")"

# 2. The count writable again: the next boot resets it, makes the request and migrates.
umount "$attempts"
run_unit
wait_lines "$t0" 'all Docker volumes are NOCOW now' 1
[[ "$(lsattr -d "$vols" | awk '{print $1}')" == *C* ]]
[[ "$(stat -c %i "$d")" != "$ino" ]]
[[ "$(sha256sum <"$d/db")" == "$sum" ]]
[[ "$(lsattr "$d/db" | awk '{print $1}')" == *C* ]]
[[ "$(ls -A "$vols/$vol")" == _data ]]
test ! -e "$flag"
test ! -e "$attempts"
[[ "$(lines "$t0" "$refused")" == 1 ]]
[[ "$(lines "$t0" "$gave_up")" == 0 ]]
echo E2E-OK
"""
)


# test_10a: runs a command on a new pseudo-terminal (crypt-perf needs a
# terminal: it checks [[ -t 0 ]] and cryptsetup reads /dev/tty) and answers
# each "Enter passphrase" prompt with argv[1]. cryptsetup prints the prompt,
# then turns echo off with TCSAFLUSH, which drops input already waiting: the
# answer goes only once echo is off (tcgetattr on the master side reads the
# terminal's settings). Prints what the command printed; exits with its code.
CRYPT_PTY_DRIVER = r"""
import os
import pty
import select
import sys
import termios
import time

answer = sys.argv[1].encode() + b"\n"
pid, fd = pty.fork()
if pid == 0:
    try:
        os.execvp(sys.argv[2], sys.argv[2:])
    finally:
        os._exit(127)  # exec failed: never run the loop below in the child
out = bytearray()
searched = 0
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    ready, _, _ = select.select([fd], [], [], 0.5)
    if not ready:
        continue
    try:
        data = os.read(fd, 4096)
    except OSError:  # EIO: the command ended and closed the terminal
        break
    if not data:
        break
    out += data
    at = out.find(b"Enter passphrase", searched)
    if at >= 0:
        searched = at + 1
        for _ in range(200):
            if not termios.tcgetattr(fd)[3] & termios.ECHO:
                break
            time.sleep(0.05)
        time.sleep(0.1)
        os.write(fd, answer)
else:
    os.kill(pid, 9)
    out += b"\nE2E-PTY: no end after 300 s\n"
_, status = os.waitpid(pid, 0)
sys.stdout.write(out.decode(errors="replace").replace("\r\n", "\n"))
sys.exit(os.waitstatus_to_exitcode(status))
"""


# test_20, test_21: runs a command on a new pseudo-terminal (`cst vm tune
# apply` and `nocow apply` ask [y/N] and refuse without a terminal) with
# argv[1] typed ahead: input() reads it at the question. The terminal echoes
# the answer when it is typed, so it comes first in the output and the prompt's
# line goes on with what the command prints next. Prints what the command
# printed, without the terminal control strings (OSC, ESC ] ... BEL or ESC \)
# that sudo's PAM session writes to the terminal (systemd's OSC 3008 context
# markers); exits with its code, or kills it after 300 s. On a failure the output
# goes to stderr too, so the test's log shows it.
PTY_ANSWER_DRIVER = r"""
import os
import pty
import re
import select
import sys
import time

pid, fd = pty.fork()
if pid == 0:
    try:
        os.execvp(sys.argv[2], sys.argv[2:])
    finally:
        os._exit(127)  # exec failed: never run the loop below in the child
os.write(fd, sys.argv[1].encode() + b"\n")  # typed ahead: input() reads it at the prompt
out = bytearray()
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    ready, _, _ = select.select([fd], [], [], 0.5)
    if not ready:
        continue
    try:
        data = os.read(fd, 4096)
    except OSError:  # EIO: the command ended and closed the terminal
        break
    if not data:
        break
    out += data
else:
    os.kill(pid, 9)
    out += b"\nE2E-PTY: no end after 300 s\n"
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
text = re.sub(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", b"", bytes(out)).decode(errors="replace").replace("\r\n", "\n")
sys.stdout.write(text)
if code != 0:  # the script stops at out="$(drive ...)" under set -e: the CI log gets the output from stderr
    sys.stderr.write(text)
sys.exit(code)
"""

# test_20: a Linux guest (its libosinfo id) with the hardware `cst vm tune`
# changes: a SATA disk with the plain driver and an e1000e network card, a
# CD-ROM it leaves alone, and a VNC password (8 characters, the most VNC
# takes) that only the --security-info dump shows. type='qemu': the
# container has no /dev/kvm.
VM_TUNE_DOMAIN = """<domain type='qemu'>
  <name>e2e-tune</name>
  <metadata>
    <libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
      <libosinfo:os id="http://fedoraproject.org/fedora/41"/>
    </libosinfo:libosinfo>
  </metadata>
  <memory unit='MiB'>512</memory>
  <vcpu placement='static'>2</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type></os>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/var/lib/libvirt/images/e2e-tune.qcow2'/>
      <target dev='sda' bus='sata'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <target dev='sdb' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <mac address='52:54:00:e2:e2:01'/>
      <source network='default'/>
      <model type='e1000e'/>
    </interface>
    <graphics type='vnc' port='-1' autoport='yes' passwd='e2esecr8'/>
  </devices>
</domain>
"""

# test_20: plan, apply (refused without a terminal, answered no, then yes), a
# second run, status and the restore of the saved definition, against the real
# virtqemud (qemu:///system, the URI cst vm always uses).
VM_TUNE_SCRIPT = r"""
set -E
trap 'echo "test_20 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
export LC_ALL=C
w=/var/tmp/e2e-vm-tune
vm=e2e-tune
defs="$w/backups/$vm/definitions"
V() { virsh -c qemu:///system "$@"; }
dump() { V dumpxml --inactive "$vm"; }
secret() { V dumpxml --inactive --security-info "$vm"; }
# cst vm as root, with the backup folder and settings file of the test (CST_VM_*).
vmc() { CST_VM_BACKUP_DIR="$w/backups" CST_VM_CONF="$w/vm.conf" /opt/cst-rw/cst vm "$@"; }
# tune apply on a pseudo-terminal (it asks [y/N]), answering $1.
drive() {
    CST_VM_BACKUP_DIR="$w/backups" CST_VM_CONF="$w/vm.conf" \
        /usr/bin/python3 -I "$w/pty-answer.py" "$1" /opt/cst-rw/cst vm tune apply "$vm"
}
show() { sed "s/^/test_20 $1: /" <<<"$2" >&2; }   # the CI log
same() {   # same LABEL FILE: the inactive definition is still FILE
    dump >"$w/now.xml"
    cmp -s "$w/now.xml" "$2" && return 0
    echo "test_20 $1 changed the definition:" >&2
    diff "$2" "$w/now.xml" >&2 || true
    return 1
}
saved() { find "$defs" -maxdepth 1 -name '*.before-tune.xml' 2>/dev/null | wc -l; }
no_secret() {   # no_secret LABEL TEXT: the graphics password is never printed
    if grep -qF e2esecr8 <<<"$2"; then echo "test_20 $1 printed the graphics password" >&2; return 1; fi
}

V define "$w/$vm.xml" >/dev/null
dump >"$w/d0.xml"
secret >"$w/s0.xml"
grep -qF "passwd='e2esecr8'" "$w/s0.xml"
if grep -qF e2esecr8 "$w/d0.xml"; then echo "the plain dump holds the password" >&2; exit 1; fi

# 1. plan: the changes, as a diff; nothing defined, nothing saved.
out="$(vmc tune plan "$vm")"
show plan "$out"
no_secret plan "$out"
for c in "disk sda: cache (default) -> none" "disk sda: io (default) -> native" \
    "disk sda: discard (default) -> unmap" "disk sda: detect_zeroes (default) -> unmap" \
    "add a virtio-scsi controller (index 0)" "disk sda: bus sata -> scsi (virtio-scsi controller 0, unit 0)" \
    "network card 52:54:00:e2:e2:01: e1000e -> virtio (same MAC)"; do
    grep -qxF "  change: $c" <<<"$out"
done
[[ "$(grep -c '^  change: ' <<<"$out")" == 7 ]]
grep -qxF "+      <target dev=\"sda\" bus=\"scsi\" />" <<<"$out"
grep -qF "Plan only. Apply with: " <<<"$out"
same plan "$w/d0.xml"
[[ ! -e "$w/backups" ]]
out="$(vmc status "$vm")"
grep -qE '^  hardware: 7 difference\(s\) from tune: .* vm tune plan$' <<<"$out"

# 2. apply without a terminal: refused before anything is saved or defined.
if out="$(vmc tune apply "$vm" </dev/null 2>&1)"; then echo "apply ran without a terminal" >&2; exit 1; fi
grep -qF "apply asks for confirmation: run it in a terminal" <<<"$out"
same "apply without a terminal" "$w/d0.xml"
[[ ! -e "$w/backups" ]]

# 3. answered no.
out="$(drive n)"
grep -qF "[y/N] Nothing changed." <<<"$out"   # the answer was echoed first, when typed ahead
same "apply answered no" "$w/d0.xml"
[[ ! -e "$w/backups" ]]

# 4. answered yes: the old definition (with its secrets) saved 0600, then defined.
out="$(drive y)"
show apply "$out"
no_secret apply "$out"
grep -qF "Defined. Effective at the next start of $vm." <<<"$out"
if grep -qF "libvirt changed the definition again" <<<"$out"; then echo "libvirt changed the tuned definition" >&2; exit 1; fi
[[ "$(saved)" == 1 ]]
f="$(find "$defs" -name '*.before-tune.xml')"
[[ "$(stat -c '%U %a' "$f")" == "root 600" ]]
[[ "$(stat -c '%a' "$defs")" == 700 ]]
cmp "$f" "$w/s0.xml"
dump >"$w/d1.xml"
python3 -I - "$w/d1.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
disks = {d.find("target").get("dev"): d for d in root.iter("disk")}
want = {"cache": "none", "io": "native", "discard": "unmap", "detect_zeroes": "unmap"}
drv = disks["sda"].find("driver").attrib
assert {k: drv.get(k) for k in want} == want, drv
assert disks["sda"].find("target").get("bus") == "scsi", ET.tostring(disks["sda"])
assert disks["sdb"].find("target").get("bus") == "sata", "the CD-ROM was moved"
models = [c.get("model") for c in root.iter("controller") if c.get("type") == "scsi"]
assert models == ["virtio-scsi"], models
nic = root.find("devices/interface")
assert nic.find("model").get("type") == "virtio", ET.tostring(nic)
assert nic.find("mac").get("address") == "52:54:00:e2:e2:01", ET.tostring(nic)
PY
secret | grep -qF "passwd='e2esecr8'"   # the graphics password survived the define

# 5. a second run changes nothing: plan and apply say so, apply asks nothing.
out="$(vmc tune plan "$vm")"
show "plan after apply" "$out"
grep -qxF "$vm: disks and network card already as /opt/cst-rw/cst vm tune recommends." <<<"$out"
if grep -q '^  change: ' <<<"$out"; then echo "plan after apply still has changes" >&2; exit 1; fi
out="$(vmc tune apply "$vm" </dev/null)"
grep -qxF "$vm: disks and network card already as /opt/cst-rw/cst vm tune recommends." <<<"$out"
same "second apply" "$w/d1.xml"
[[ "$(saved)" == 1 ]]
out="$(vmc status "$vm")"
grep -qxF "  hardware: disks and network card as tune recommends" <<<"$out"

# 6. undo: defining the saved file gives back the old definition.
V define "$f" >/dev/null
same "the restore of the saved definition" "$w/d0.xml"
out="$(vmc status "$vm")"
grep -qE '^  hardware: 7 difference\(s\) from tune: ' <<<"$out"

# 7. a knob set to no leaves its part out.
printf 'VM_TUNE_NIC=no\n' >"$w/vm.conf"
out="$(vmc tune plan "$vm")"
grep -qxF "  skipped by $w/vm.conf: VM_TUNE_NIC=no" <<<"$out"
if grep -q '^  change: network card' <<<"$out"; then echo "VM_TUNE_NIC=no still changes the network card" >&2; exit 1; fi
[[ "$(grep -c '^  change: ' <<<"$out")" == 6 ]]
same "plan with VM_TUNE_NIC=no" "$w/d0.xml"
echo E2E-OK
"""


# test_21: `cst vm nocow` on the loop btrfs (/var and /var/home): the system
# folder is a scratch path (CST_VM_SYSTEM_IMAGES) whose parent exists, the user
# folders are tester's.
VM_NOCOW_SCRIPT = r"""
set -E
trap 'echo "test_21 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
export LC_ALL=C
w=/var/tmp/e2e-vm-nocow
H=/home/tester
sys="$w/libvirt/images"
sess="$H/.local/share/libvirt/images"
boxes="$H/.local/share/gnome-boxes/images"
flat="$H/.var/app/org.gnome.Boxes/data/gnome-boxes/images"
# As tester, with the test's system folder and settings file (CST_VM_*).
t() {
    runuser -u tester -- env HOME="$H" USER=tester LOGNAME=tester PATH=/usr/bin:/bin \
        CST_VM_SYSTEM_IMAGES="$sys" CST_VM_CONF="$w/vm.conf" "$@"
}
nocow() { t /opt/cst-rw/cst vm nocow "$@"; }
# apply on a pseudo-terminal (it asks [y/N]), answering $1.
drive() { t /usr/bin/python3 -I "$w/pty-answer.py" "$1" /opt/cst-rw/cst vm nocow apply; }
# As root (the root-only folder is readable), for tester's folders (SUDO_USER).
as_root() { SUDO_USER=tester CST_VM_SYSTEM_IMAGES="$sys" CST_VM_CONF="$w/vm.conf" /opt/cst-rw/cst vm nocow "$@"; }
attrs() { lsattr -d "$1" | awk '{print $1}'; }
has_c() { [[ "$(attrs "$1")" == *C* ]]; }
# The flags of every folder and image, and whether the Flatpak tree exists.
state() {
    local p
    for p in "$sys" "$sess" "$boxes" "$sess/old.qcow2"; do
        if [[ -e "$p" ]]; then echo "$(attrs "$p") $p"; else echo "absent $p"; fi
    done
    if [[ -e "$H/.var/app/org.gnome.Boxes" ]]; then echo "present $H/.var/app/org.gnome.Boxes"; fi
}
same() {   # same LABEL BEFORE
    [[ "$(state)" == "$2" ]] && return 0
    printf 'test_21 %s changed the folders:\nbefore:\n%s\nafter:\n%s\n' "$1" "$2" "$(state)" >&2
    return 1
}
show() { sed "s/^/test_21 $1: /" <<<"$2" >&2; }   # the CI log
# The lines of plan's block for one folder (its header up to the next header).
block() { awk -v h="$1  [" 'index($0, h) == 1 {on = 1; print; next} on && /^[^ ]/ {exit} on {print}' <<<"$2"; }

rm -rf "$w/libvirt" "$w/vm.conf"
mkdir -p "$w/libvirt"   # libvirt is installed (its folder exists), its images folder not yet
t mkdir -p "$sess" "$boxes"
t dd if=/dev/urandom of="$sess/old.qcow2" bs=1M count=4 status=none
t touch "$sess/old2.qcow2.cow-old"
for p in "$sess" "$boxes" "$sess/old.qcow2"; do
    if has_c "$p"; then echo "$p has +C before apply: the test would prove nothing" >&2; exit 1; fi
done
before="$(state)"

# 1. plan: what apply would do; changes nothing.
out="$(nocow plan)"
show plan "$out"
grep -qxF "$sys  [libvirt qemu:///system: virt-manager, virsh]: not present" <<<"$out"
grep -qxF "$flat  [GNOME Boxes (Flatpak)]: not present" <<<"$out"
block "$sess" "$out" | grep -qF "  NOCOW mark (+C): no: "
block "$sess" "$out" | grep -qF "  still copy-on-write: old.qcow2 "
block "$sess" "$out" | grep -qF "chattr +C"
block "$sess" "$out" | grep -qxF "  leftover of a conversion: old2.qcow2.cow-old (remove it once the VM booted well from the new file)"
block "$boxes" "$out" | grep -qxF "  images: none"
grep -qF "(folders: $sys (not created yet), $sess, $boxes)" <<<"$out"
same plan "$before"

# 2. apply without a terminal: refused, nothing changed.
if out="$(nocow apply </dev/null 2>&1)"; then echo "apply ran without a terminal: $out" >&2; exit 1; fi
grep -qF "apply asks for confirmation: run it in a terminal" <<<"$out"
same "apply without a terminal" "$before"

# 3. answered no: nothing changed.
out="$(drive n)"
show "apply, no" "$out"
grep -qF "[y/N] Nothing changed." <<<"$out"   # the answer was echoed first, when typed ahead
same "apply answered no" "$before"

# 4. answered yes: the system folder created as libvirt does it, through sudo, and every folder marked.
out="$(drive y)"
show apply "$out"
# A folder about to be created has no mark to read: no "not readable" note on its line.
grep -qxF "  create (root:root 0711) and mark +C: $sys (sudo)" <<<"$out"
grep -qxF "  mark +C: $sess" <<<"$out"
grep -qxF "  mark +C: $boxes" <<<"$out"
grep -qF "  sudo install -d -o root -g root -m 0711 $sys" <<<"$out"   # on the line of the question
grep -qxF "  sudo chattr +C $sys" <<<"$out"
for p in "$sys" "$sess" "$boxes"; do
    grep -qxF "  done: $p" <<<"$out"
    has_c "$p"
done
[[ "$(stat -c '%U:%G %a' "$sys")" == "root:root 711" ]]
[[ "$(stat -c '%U' "$sess")" == tester ]]
# An existing image keeps its state; files created later get the mark.
if has_c "$sess/old.qcow2"; then echo "apply converted an existing image" >&2; exit 1; fi
[[ ! -e "$H/.var/app/org.gnome.Boxes" ]]   # a missing user folder is not created
t touch "$sess/new.qcow2"
has_c "$sess/new.qcow2"
touch "$sys/new.img"
has_c "$sys/new.img"
rm -f "$sys/new.img"
after="$(state)"

# 5. plan as tester: the user folders marked, the old image still to convert,
# the root-only folder unreadable (never "copy-on-write").
out="$(nocow plan)"
show "plan after apply" "$out"
block "$sess" "$out" | grep -qxF "  NOCOW mark (+C): yes"
block "$boxes" "$out" | grep -qxF "  NOCOW mark (+C): yes"
block "$sess" "$out" | grep -qxF "  NOCOW images: new.qcow2"
block "$sess" "$out" | grep -qF "  still copy-on-write: old.qcow2 "
block "$sys" "$out" | grep -qF "  NOCOW mark (+C): not readable as your user (root-only folder): "
grep -qF "(folders: $sys (mark not readable here: set again, harmless))" <<<"$out"
same "plan after apply" "$after"

# 6. As root everything is readable: nothing left to mark, and apply asks nothing.
out="$(as_root plan --root)"
show "plan --root" "$out"
block "$sys" "$out" | grep -qxF "  NOCOW mark (+C): yes"
grep -qxF "No folder to mark." <<<"$out"
out="$(as_root apply </dev/null)"
show "second apply as root" "$out"
grep -qxF "Every VM image folder present on btrfs already has the NOCOW mark (+C)." <<<"$out"
same "second apply as root" "$after"

# 7. A second apply as tester sets the mark again only on the folder it cannot read.
out="$(drive y)"
show "second apply as tester" "$out"
grep -qxF "  mark +C: $sys (sudo) (its mark is not readable as your user: set again, harmless)" <<<"$out"
if grep -qE "^  (create .*|mark )\+C: ($sess|$boxes)$" <<<"$out"; then echo "a second apply marked a user folder again" >&2; exit 1; fi
grep -qxF "  done: $sys" <<<"$out"
same "second apply as tester" "$after"

# 8. VM_NOCOW=no: the folders are not managed; a new folder stays copy-on-write.
printf 'VM_NOCOW=no\n' >"$w/vm.conf"
t mkdir -p "$flat"
out="$(nocow apply </dev/null)"
grep -qxF "VM_NOCOW=no in $w/vm.conf: nothing to do." <<<"$out"
out="$(nocow plan)"
grep -qxF "VM_NOCOW=no in $w/vm.conf: the VM image folders are not managed." <<<"$out"
if has_c "$flat"; then echo "VM_NOCOW=no marked a folder" >&2; exit 1; fi
echo E2E-OK
"""


def as_tester_bash(container: str, script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    quoted = shlex.quote(script)
    cmd = (
        "runuser -u tester -- env "
        "HOME=/home/tester USER=tester LOGNAME=tester "
        "XDG_RUNTIME_DIR=/run/user/1000 "
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
        f"bash -euo pipefail -c {quoted}"
    )
    return exec_bash(container, cmd, check=check)


def read_host_guard_snapshot() -> dict[str, str]:
    snap: dict[str, str] = {}
    paths = [
        "/proc/sys/vm/dirty_bytes",
        "/proc/sys/vm/dirty_background_bytes",
        "/proc/sys/vm/watermark_scale_factor",
        "/proc/sys/vm/min_free_kbytes",
        "/sys/kernel/mm/transparent_hugepage/defrag",
        "/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost",
        *sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*/energy_performance_preference")),
        *sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference")),
    ]
    for p in paths:
        try:
            with open(p) as f:
                snap[p] = f.read().strip()
        except OSError:
            pass
    return snap


class Tier2StorageCpuE2ETest(unittest.TestCase):
    PRIV_CONTAINER: ClassVar[str] = ""
    NAS_CONTAINER: ClassVar[str] = ""
    HOST_GUARD_BEFORE: ClassVar[dict[str, str]] = {}

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.PRIV_CONTAINER = os.environ["CST_E2E_PRIV_CONTAINER"]
        cls.NAS_CONTAINER = os.environ["CST_E2E_NAS_CONTAINER"]
        cls.HOST_GUARD_BEFORE = read_host_guard_snapshot()
        exec_bash(
            cls.PRIV_CONTAINER,
            "rm -rf /opt/cst-rw && cp -a /opt/cst /opt/cst-rw && chmod -R a+rX /opt/cst-rw",
        )

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        after = read_host_guard_snapshot()
        if after != cls.HOST_GUARD_BEFORE:
            raise AssertionError(f"HOST ISOLATION BREACH! Before={cls.HOST_GUARD_BEFORE}, After={after}")

    def test_01_loop_btrfs_raid1_and_host_shadows_active(self) -> None:
        """Loopback RAID1 covers /sysroot, /var, /var/home and shadow bind-mounts protect host kernel."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            """
            u_sys=$(findmnt -rn -o UUID --target /sysroot)
            u_var=$(findmnt -rn -o UUID --target /var)
            u_home=$(findmnt -rn -o UUID --target /var/home)
            opts_var=$(findmnt -rn -o OPTIONS --target /var)
            [[ -n "$u_sys" && "$u_sys" == "$u_var" && "$u_sys" == "$u_home" ]]
            [[ -L /home && "$(readlink /home)" == "/var/home" ]]
            echo "$opts_var" | grep -q 'compress=zstd:1'
            btrfs filesystem df /var | grep -q 'RAID1'
            findmnt -rn /proc/sys/vm/dirty_bytes >/dev/null
            findmnt -rn /proc/sys/vm/dirty_background_bytes >/dev/null
            if [[ -e /sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost ]]; then
                findmnt -rn /sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost >/dev/null
            fi
            echo "UUID=$u_sys"
            """,
        ).stdout
        self.assertIn("UUID=", out)

    def test_02_btrfs_install_stall_monitor_scrub_and_reclaim(self) -> None:
        """cst btrfs install --no-below configures systemd units, reclaim/dirty_bytes on loop UUID, and scrubs."""
        loop_uuid = exec_bash(self.PRIV_CONTAINER, "cat /run/e2e-loop-uuid").stdout.strip()
        exec_bash(
            self.PRIV_CONTAINER,
            f"""
            as_user() {{
                SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 "$@"
            }}
            as_user /opt/cst-rw/cst btrfs install --no-below
            grep -q "{loop_uuid}" /etc/tmpfiles.d/btrfs-automatic-reclaim.conf
            grep -qx '2147483648' /proc/sys/vm/dirty_bytes
            grep -qx '536870912' /proc/sys/vm/dirty_background_bytes
            grep -qx '1' "/sys/fs/btrfs/{loop_uuid}/allocation/data/dynamic_reclaim"
            systemctl is-active btrfs-stall-monitor.service
            btrfs scrub start -B /var >/dev/null
            as_user /opt/cst-rw/cst btrfs status
            """,
        )

    def test_03_btrfs_snapshots_convert_hourly_automount_and_nas(self) -> None:
        """btrfs-snapshot convert, hourly, top.automount, real restic+SFTP NAS upload, and purge work on RAID1."""
        pub = exec_bash(
            self.PRIV_CONTAINER,
            """
            mkdir -p /root/.ssh && chmod 0700 /root/.ssh
            rm -f /root/.ssh/id_ed25519 /root/.ssh/id_ed25519.pub
            ssh-keygen -q -t ed25519 -N '' -f /root/.ssh/id_ed25519
            cat /root/.ssh/id_ed25519.pub
            """,
        ).stdout.strip()
        exec_bash(
            self.NAS_CONTAINER,
            f"""
            mkdir -p /root/.ssh /srv/backup/tier2-repo && chmod 0700 /root/.ssh
            printf '%s\n' {shlex.quote(pub)} >/root/.ssh/authorized_keys
            chmod 0600 /root/.ssh/authorized_keys
            """,
        )

        # Configure snapshot units/conf for the 6G loopback RAID1 and test convert + hourly + nas + purge
        exec_bash(
            self.PRIV_CONTAINER,
            f"""
            install -d -m 0755 /home/tester/Workspace /home/tester/.cache /etc/btrfs-setup /var/lib/btrfs-snapshot
            echo 'workspace-file' >/home/tester/Workspace/hello.txt
            chown -R tester:tester /home/tester/Workspace /home/tester/.cache

            cat >/root/.ssh/config <<'EOF'
Host btrfs-setup-nas
    HostName {self.NAS_CONTAINER}
    Port 22
    User root
    IdentityFile /root/.ssh/id_ed25519
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    BatchMode yes
EOF
            chmod 0600 /root/.ssh/config
            printf 'tier2-secret-pass' >/etc/btrfs-setup/restic-password
            chmod 0600 /etc/btrfs-setup/restic-password
            RESTIC_REPOSITORY='sftp:btrfs-setup-nas:/srv/backup/tier2-repo' \
            RESTIC_PASSWORD_FILE=/etc/btrfs-setup/restic-password \
                restic init >/dev/null

            cat >/etc/btrfs-setup/snapshots.conf <<'EOF'
HOME_DIR=/var/home/tester
HOME_MOUNT=/var/home
HOME_SUBVOL=home
TOP=/var/mnt/btrfs-top
SNAP_DIR_NAME=snapshots
SKIP_AT_OR_BELOW_GIB=0
RESUME_ABOVE_GIB=0
EMERGENCY_AT_OR_BELOW_GIB=0
NAS_ENABLED=1
NAS_HOST=btrfs-setup-nas
NAS_PORT=22
NAS_USER=root
NAS_PATH=/srv/backup/tier2-repo
RESTIC_REPOSITORY=sftp:btrfs-setup-nas:/srv/backup/tier2-repo
EOF
            printf 'copy /var/home/tester/Workspace\ncopy /var/home/tester/.cache\n' >/etc/btrfs-setup/convert-plan

            uuid="$(cat /run/e2e-loop-uuid)"
            {TOP_LEVEL_OPTS_FN}
            opts="$(top_level_opts)"
            unit="$(systemd-escape --path /var/mnt/btrfs-top)"
            mkdir -p /var/mnt/btrfs-top
            sed -e "s/@UUID@/$uuid/g" -e "s|@OPTIONS@|$opts|g" \
                /opt/cst-rw/btrfs/snapshots/top.mount.in >"/etc/systemd/system/${{unit}}.mount"
            cp /opt/cst-rw/btrfs/snapshots/top.automount.in "/etc/systemd/system/${{unit}}.automount"
            systemctl daemon-reload
            systemctl enable "${{unit}}.automount"
            [[ "$(systemctl is-enabled "${{unit}}.automount")" == "enabled" ]]
            grep -q "What=/dev/disk/by-uuid/$uuid" "/etc/systemd/system/${{unit}}.mount"
            # Mount subvolid=5 directly in the container (systemd-udevd is masked so .device units do not plug)
            dev0="$(head -n1 /run/e2e-loop-devs)"
            mount -o "$opts" "$dev0" /var/mnt/btrfs-top
            findmnt -rn -M /var/mnt/btrfs-top >/dev/null
            # The installer writes btrfs-snapshot-core and btrfs-snapshot-runtime as the zipapps a real install makes.
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-snapshot-runtime btrfs/btrfs-snapshot btrfs/btrfs-snapshot-core

            /usr/local/sbin/btrfs-snapshot convert
            btrfs subvolume show /var/home/tester/Workspace >/dev/null
            btrfs subvolume show /var/home/tester/.cache >/dev/null

            /usr/local/sbin/btrfs-snapshot hourly
            snap_count=$(find /var/mnt/btrfs-top/snapshots -maxdepth 1 -mindepth 1 | wc -l)
            test "$snap_count" -ge 1

            list_out="$(/usr/local/sbin/btrfs-snapshot list)"
            grep -q 'home\\.' <<<"$list_out"
            /usr/local/sbin/btrfs-snapshot nas
            snaps_json="$(RESTIC_REPOSITORY='sftp:btrfs-setup-nas:/srv/backup/tier2-repo' \
                RESTIC_PASSWORD_FILE=/etc/btrfs-setup/restic-password restic snapshots --json)"
            grep -q 'home' <<<"$snaps_json"

            /usr/local/sbin/btrfs-snapshot purge
            if [[ -d /var/mnt/btrfs-top/snapshots ]]; then
                rem_count=$(find /var/mnt/btrfs-top/snapshots -maxdepth 1 -mindepth 1 | wc -l)
                test "$rem_count" -eq 0
            fi
            umount /var/mnt/btrfs-top
            systemctl disable "${{unit}}.automount"
            """,
        )

    def test_03a_snapshots_drop_cache_folders(self) -> None:
        """Cache folders (CACHEDIR.TAG) leave each new snapshot: plan, apply, second run, knob off,
        skip list, recovery of an interrupted snapshot, a foreign writable entry, upgrade, purge."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_03a FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            TOP=/var/mnt/btrfs-top
            SNAPS=$TOP/snapshots
            STATE=/var/lib/btrfs-snapshot
            CONF=/etc/btrfs-setup/snapshots.conf
            CORE=/usr/local/sbin/btrfs-snapshot-core
            RT=/usr/local/sbin/btrfs-snapshot
            H=/var/home/tester
            WS=$H/Workspace
            P=$WS/proj
            WS_SRC=$TOP/home/tester/Workspace
            HOME_SRC=$TOP/home
            SIG='Signature: 8a477f597d28d172789f06886806bc55'

            # The top level, mounted as test_03 does (no udev for the automount here).
            """
            + TOP_LEVEL_OPTS_FN
            + r"""
            opts="$(top_level_opts)"
            mkdir -p "$TOP"
            if ! mountpoint -q "$TOP"; then
                mount -o "$opts" "$(head -n1 /run/e2e-loop-devs)" "$TOP"
            fi
            trap 'umount "$TOP" 2>/dev/null || true' EXIT
            # $RT and $CORE are the manifest's dests; the core goes in as its zipapp.
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-snapshot-runtime btrfs/btrfs-snapshot btrfs/btrfs-snapshot-core
            # test_03 made these subvolumes; ~/Workspace is a source of its own.
            btrfs subvolume show "$WS" >/dev/null
            btrfs subvolume show "$H/.cache" >/dev/null

            # test_03's settings stay; only the two keys of this feature change (and go at the end).
            grep -qx 'HOME_DIR=/var/home/tester' "$CONF"
            grep -qx 'SKIP_AT_OR_BELOW_GIB=0' "$CONF"
            setkey() {   # setkey KEY [VALUE]: no VALUE = the key removed (the default)
                sed -i "/^$1=/d" "$CONF"
                if [[ $# -gt 1 ]]; then echo "$1=$2" >>"$CONF"; fi
            }
            knob() { if [[ -n "$1" ]]; then setkey SNAPSHOT_EXCLUDE_CACHES "$1"; else setkey SNAPSHOT_EXCLUDE_CACHES; fi; }
            setkey SNAPSHOT_EXCLUDE_CACHES
            setkey SNAPSHOT_CACHES_SKIP
            mktag() { mkdir -p "$1"; printf '%s\n# a cache directory tag\n' "$SIG" >"$1/CACHEDIR.TAG"; }
            hourly() {   # the hourly run; its log on stderr (the CI log) and on stdout
                local o
                o="$("$RT" hourly 2>&1)" || { printf '%s\n' "$o" >&2; return 1; }
                printf '%s\n' "$o" >&2
                printf '%s\n' "$o"
            }
            latest() { "$CORE" latest --snapdir "$SNAPS" --name "$1" --source-path "$2"; }
            count() { find "$SNAPS" -mindepth 1 -maxdepth 1 -name "$1.*" | wc -l; }
            ro_of() { btrfs property get -ts "$1" ro; }
            field() {
                btrfs subvolume show "$1" | awk -F':[[:space:]]*' -v k="$2" '$1 ~ ("^[[:space:]]*" k "$") {print $2}'
            }
            write_ws() { date +%s%N >>"$P/src/main.rs"; btrfs filesystem sync "$WS" >/dev/null; }
            write_home() { date +%s%N >>"$H/notes.txt"; btrfs filesystem sync /var/home >/dev/null; }
            owned() {   # owned NAME PATH: PATH is one of NAME's snapshots in `list --json`
                "$RT" list --json | python3 -I -c '
import json, sys
src = [s for s in json.load(sys.stdin)["sources"] if s["name"] == sys.argv[1]][0]
sys.exit(0 if sys.argv[2] in [x["path"] for x in src["snapshots"]] else 1)' "$1" "$2"
            }
            doctor() { runuser -u tester -- env CST_DOCTOR_CONTAINER=0 python3 -I /opt/cst-rw/doctor/cst-doctor || true; }

            # Fixtures: a cargo-like target/, look-alikes that must stay, skipped paths.
            mkdir -p "$P/src" "$P/target/debug/deps" "$P/untagged" "$P/wrongtag" "$P/tagdir/CACHEDIR.TAG" "$P/symtag"
            echo 'fn main() {}' >"$P/src/main.rs"
            mktag "$P/target"
            for i in $(seq 1 2000); do echo "$i" >"$P/target/debug/deps/f$i"; done
            dd if=/dev/urandom of="$P/target/debug/app" bs=1M count=8 status=none
            # A nested subvolume inside a cache folder: an empty placeholder in the snapshot,
            # which the drop must remove with the folder, while the source keeps the subvolume.
            btrfs subvolume create "$P/target/nested-sv" >/dev/null
            echo in-sv >"$P/target/nested-sv/file"
            echo keep >"$P/untagged/file"
            echo 'Signature: not the right one' >"$P/wrongtag/CACHEDIR.TAG"
            ln -s ../target/CACHEDIR.TAG "$P/symtag/CACHEDIR.TAG"
            ln -s target "$P/link-to-target"
            mktag "$P/.git/lfs/cache"
            mktag "$P/node_modules/.cache"
            mktag "$WS/skipme/target"
            containers_new=0
            [[ -e "$H/.local/share/containers" ]] || containers_new=1
            LAYER=.local/share/containers/e2e-storage/overlay/l1/diff/var/cache/dnf
            mktag "$H/$LAYER"
            mktag "$H/.e2e-tool/cache"
            echo x >"$H/.e2e-tool/cache/index"
            mktag "$H/.cache"                  # a nested subvolume: an empty placeholder in a snapshot
            echo notes >"$H/notes.txt"
            chown -R tester:tester "$WS" "$H/.local" "$H/.e2e-tool" "$H/notes.txt"

            # 0. The default skip list keeps podman's storage whole (and nothing in ~/Workspace).
            plan="$("$RT" caches)"
            grep -qF 'SKIP   tester/.local/share/containers' <<<"$plan"
            grep -qxF skipme/target <<<"$(awk '$1 == "DROP" {print $2}' <<<"$plan")"
            setkey SNAPSHOT_CACHES_SKIP "'.local/share/containers Workspace/skipme'"

            # 1. Upgrade: snapshots taken the old way (knob off: read-only at once) stay as they are.
            knob no
            write_ws
            write_home
            hourly >/dev/null
            old_ws="$(latest Workspace "$WS_SRC")"
            test -f "$old_ws/proj/target/debug/deps/f1"
            [[ "$(ro_of "$old_ws")" == ro=true ]]
            test ! -e "$STATE/core-unfinished"

            # 2. Plan: `caches` lists what would go and changes nothing, not even the
            #    source's generation (no access times written while reading).
            knob ""
            before="$(find "$SNAPS" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)"
            btrfs filesystem sync /var >/dev/null
            gen_before="$(field "$WS_SRC" Generation)"
            plan="$("$RT" caches)"
            printf '%s\n' "$plan" >&2
            drops="$(awk '$1 == "DROP" {print $2}' <<<"$plan")"
            grep -qxF proj/target <<<"$drops"
            grep -qxF tester/.e2e-tool/cache <<<"$drops"
            for bad in proj/untagged proj/wrongtag proj/tagdir proj/symtag proj/.git/lfs/cache \
                       proj/node_modules/.cache skipme/target tester/.cache tester/Workspace/proj/target \
                       "tester/$LAYER"; do
                if grep -qxF "$bad" <<<"$drops"; then echo "the plan drops $bad" >&2; exit 1; fi
            done
            grep -qF 'SKIP   skipme' <<<"$plan"
            grep -qF 'SKIP   tester/.local/share/containers' <<<"$plan"
            cst_plan="$(SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs snapshots caches)"
            grep -qF 'DROP   proj/target' <<<"$cst_plan"
            status_out="$(SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs snapshots status)"
            grep -qF 'cache folders (CACHEDIR.TAG): dropped from new snapshots' <<<"$status_out"
            [[ "$(find "$SNAPS" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)" == "$before" ]]
            btrfs filesystem sync /var >/dev/null
            [[ "$(field "$WS_SRC" Generation)" == "$gen_before" ]]
            test -f "$P/target/debug/deps/f2000"

            # 3. Apply (the default): the new snapshots lose their cache folders, the sources keep them.
            write_ws
            write_home
            meta_before="$(btrfs filesystem df -b /var | awk -F'used=' '/^Metadata/ {print $2}')"
            out="$(hourly)"
            btrfs filesystem sync /var >/dev/null
            meta_after="$(btrfs filesystem df -b /var | awk -F'used=' '/^Metadata/ {print $2}')"
            echo "METADATA used before=$meta_before after=$meta_after (bytes, whole filesystem, print only)"
            grep -m1 'dropped [0-9]* cache folder(s) from Workspace\.' <<<"$out"
            ws="$(latest Workspace "$WS_SRC")"
            [[ "$ws" != "$old_ws" ]]
            test ! -e "$ws/proj/target"
            test ! -L "$ws/proj/target"
            test -f "$ws/proj/src/main.rs"
            test -f "$ws/proj/untagged/file"
            test -f "$ws/proj/wrongtag/CACHEDIR.TAG"
            test -d "$ws/proj/tagdir/CACHEDIR.TAG"
            test -L "$ws/proj/symtag/CACHEDIR.TAG"
            test -L "$ws/proj/link-to-target"
            test -f "$ws/proj/.git/lfs/cache/CACHEDIR.TAG"
            test -f "$ws/proj/node_modules/.cache/CACHEDIR.TAG"
            test -f "$ws/skipme/target/CACHEDIR.TAG"
            [[ "$(ro_of "$ws")" == ro=true ]]
            [[ "$(field "$ws" 'Parent UUID')" == "$(field "$WS_SRC" UUID)" ]]
            owned Workspace "$ws"
            test -f "$P/target/debug/deps/f2000"
            test -f "$P/target/debug/app"
            test -f "$P/target/nested-sv/file"
            [[ "$(stat -c %i "$P/target/nested-sv")" == 256 ]]   # still a subvolume in the source
            home="$(latest home "$HOME_SRC")"
            test ! -e "$home/tester/.e2e-tool/cache"
            test -f "$home/tester/$LAYER/CACHEDIR.TAG"
            test -f "$home/tester/notes.txt"
            owned home "$home"
            test -f "$old_ws/proj/target/debug/deps/f1"       # the old snapshot is untouched
            test ! -s "$STATE/core-unfinished"
            test ! -e "$STATE/core-writable"

            # 4. Second run: the deletions inside the snapshot are not a change of the source.
            btrfs filesystem sync /var >/dev/null
            n="$(count Workspace)"
            out="$(hourly)"
            grep -q 'Workspace: unchanged since' <<<"$out"
            [[ "$(count Workspace)" == "$n" ]]
            write_ws
            hourly >/dev/null
            [[ "$(count Workspace)" == "$((n + 1))" ]]
            ws2="$(latest Workspace "$WS_SRC")"
            [[ "$ws2" != "$ws" ]]
            test ! -e "$ws2/proj/target"

            # 5. Recovery: a listed writable snapshot of ~/Workspace is finished; a listed writable
            #    subvolume that is not one is left alone; a listed name never created is forgotten.
            ts_r="$(date -d '-30 min' +%Y%m%dT%H%M)"
            ts_f="$(date -d '-40 min' +%Y%m%dT%H%M)"
            rec="$SNAPS/Workspace.$ts_r"
            foreign="$SNAPS/Workspace.$ts_f"
            test ! -e "$rec"
            test ! -e "$foreign"
            btrfs subvolume snapshot "$WS_SRC" "$rec" >/dev/null   # what a run killed after creating it leaves
            btrfs subvolume create "$foreign" >/dev/null
            printf '%s\n' "Workspace.$ts_r" "Workspace.$ts_f" Workspace.20200101T0000 >"$STATE/core-unfinished"
            out="$(hourly)"
            grep -qF "finishing Workspace.$ts_r" <<<"$out"
            grep -qF "Workspace.$ts_f is listed in $STATE/core-unfinished but is a snapshot of another subvolume" \
                <<<"$out"
            [[ "$(ro_of "$rec")" == ro=true ]]
            test ! -e "$rec/proj/target"
            test -f "$rec/proj/src/main.rs"
            owned Workspace "$rec"
            [[ "$(ro_of "$foreign")" == ro=false ]]
            [[ "$(cat "$STATE/core-unfinished")" == "Workspace.$ts_f" ]]
            grep -qxF "Workspace.$ts_f" "$STATE/core-writable"
            # cst doctor runs as the user: it reads the lists, not the root-only snapshot folder.
            doc="$(doctor)"
            printf '%s\n' "$doc" >&2
            grep -qF "WARN   snapshots: Workspace.$ts_f left writable by an interrupted run" <<<"$doc"
            # The user's fix: delete it and its line; the next run clears the record.
            btrfs subvolume delete "$foreign" >/dev/null
            sed -i "/^Workspace\.$ts_f\$/d" "$STATE/core-unfinished"
            hourly >/dev/null
            test ! -s "$STATE/core-unfinished"
            test ! -e "$STATE/core-writable"
            doc="$(doctor)"
            grep -qF "ok     snapshots: none left writable" <<<"$doc"

            # 6. Knob off: the old read-only snapshot again, cache folders and all.
            knob no
            write_ws
            hourly >/dev/null
            ws3="$(latest Workspace "$WS_SRC")"
            [[ "$ws3" != "$rec" ]]
            test -f "$ws3/proj/target/debug/deps/f1"
            status_out="$(SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs snapshots status)"
            grep -qF 'kept in new snapshots (SNAPSHOT_EXCLUDE_CACHES=no)' <<<"$status_out"
            knob ""

            # 7. Undo: purge finishes a listed writable snapshot first, so it goes too.
            rec2="$SNAPS/Workspace.$(date -d '-50 min' +%Y%m%dT%H%M)"
            test ! -e "$rec2"
            btrfs subvolume snapshot "$WS_SRC" "$rec2" >/dev/null
            basename "$rec2" >"$STATE/core-unfinished"
            "$RT" purge
            test ! -e "$rec2"
            if [[ -d "$SNAPS" ]]; then
                test "$(find "$SNAPS" -mindepth 1 -maxdepth 1 | wc -l)" -eq 0
            fi
            test ! -e "$STATE/core-unfinished"
            test ! -e "$STATE/core-writable"
            test -f "$P/target/debug/deps/f2000"                # the sources never lose anything
            test -f "$H/.e2e-tool/cache/index"

            # Leave the home as test_03 left it for the tests after this one.
            btrfs subvolume delete "$P/target/nested-sv" >/dev/null 2>&1 || true
            rm -rf "$P" "$WS/skipme" "$H/.local/share/containers/e2e-storage" "$H/.e2e-tool"
            if (( containers_new )); then rmdir "$H/.local/share/containers"; fi
            rm -f "$H/.cache/CACHEDIR.TAG" "$H/notes.txt"
            setkey SNAPSHOT_EXCLUDE_CACHES
            setkey SNAPSHOT_CACHES_SKIP
            """,
        ).stdout
        for line in out.splitlines():
            if line.startswith("METADATA"):
                print(f"\ntest_03a: {line}")

    def test_03b_snapshot_core_reads_its_state_file_as_ascii_digits(self) -> None:
        """A state file whose epoch is a non-ASCII digit (ARABIC-INDIC THREE, SUPERSCRIPT TWO) blocks
        deletions as an unreadable state (exit 4, the problem printed, no traceback); without the file
        the same prune deletes."""
        exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_03b FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            TOP=/var/mnt/btrfs-top
            CORE=/usr/local/sbin/btrfs-snapshot-core
            """
            + TOP_LEVEL_OPTS_FN
            + r"""
            opts="$(top_level_opts)"
            mkdir -p "$TOP"
            if ! mountpoint -q "$TOP"; then
                mount -o "$opts" "$(head -n1 /run/e2e-loop-devs)" "$TOP"
            fi
            SRC=$TOP/e2e-digits-src
            SNAPS=$TOP/e2e-digits-snaps
            STATE="$(mktemp -d)"
            cleanup() {
                for s in "$SNAPS"/digits.*; do
                    if [[ -e "$s" ]]; then btrfs subvolume delete "$s" >/dev/null 2>&1 || true; fi
                done
                rmdir "$SNAPS" 2>/dev/null || true
                btrfs subvolume delete "$SRC" >/dev/null 2>&1 || true
                rm -rf "$STATE"
                umount "$TOP" 2>/dev/null || true
            }
            trap cleanup EXIT
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-snapshot-core
            btrfs subvolume create "$SRC" >/dev/null
            mkdir -p "$SNAPS"
            # Five old read-only snapshots of the source: with the policy 1h, a prune keeps the three newest
            # and deletes the two oldest (fewer than the circuit breaker's 5).
            for day in 01 02 03 04 05; do
                btrfs subvolume snapshot -r "$SRC" "$SNAPS/digits.202001${day}T0000" >/dev/null
            done
            count() { find "$SNAPS" -mindepth 1 -maxdepth 1 -name 'digits.*' | wc -l; }
            prune() {
                "$CORE" prune --snapdir "$SNAPS" --source "digits=$SRC:1h" --state-dir "$STATE" \
                    --assume-clock-ok --no-lock --tz UTC
            }
            # The state file holds one digit that is not ASCII: ARABIC-INDIC DIGIT THREE (int() reads it as
            # epoch 3), then SUPERSCRIPT TWO (int() refuses it).
            for digit in '\u0663' '\u00b2'; do
                python3 -I -c 'import sys; open(sys.argv[1], "w", encoding="utf-8").write(sys.argv[2].encode().decode("unicode_escape"))' \
                    "$STATE/core-last-run" "$digit"
                rc=0
                out="$(prune 2>&1)" || rc=$?
                printf '%s\n' "$out" >&2
                test "$rc" -eq 4
                grep -qF 'unreadable state' <<<"$out"
                if grep -qF 'Traceback' <<<"$out"; then echo "a traceback for $digit" >&2; exit 1; fi
                test "$(count)" -eq 5
            done
            # Without the state file the same prune deletes the two oldest.
            rm -f "$STATE/core-last-run"
            prune
            test "$(count)" -eq 3
            test ! -e "$SNAPS/digits.20200101T0000"
            test ! -e "$SNAPS/digits.20200102T0000"
            """,
        )

    def test_04_btrfs_bulk_real_subvolume_and_nocow(self) -> None:
        """btrfs-bulk creates ~/Bulk subvolume, links items, sets +C NOCOW, and supports keep/unkeep."""
        as_tester_bash(
            self.PRIV_CONTAINER,
            """
            mkdir -p /home/tester/.android/avd
            dd if=/dev/zero of=/home/tester/.android/avd/pixel.qcow2 bs=1M count=16 status=none
            """,
        )
        exec_bash(
            self.PRIV_CONTAINER,
            """
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs bulk keep models steam
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs bulk plan >/dev/null
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs bulk apply
            btrfs subvolume show /home/tester/Bulk >/dev/null
            test -L /home/tester/.android/avd
            target="$(readlink -f /home/tester/.android/avd)"
            test -n "$target"
            [[ "$target" == /var/home/tester/Bulk/emulators/android/avd ]]
            [[ "$(lsattr -d "$target" | awk '{print $1}')" == *C* ]]
            [[ "$(lsattr -d "$target/pixel.qcow2" | awk '{print $1}')" == *C* ]]
            status_out="$(SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs bulk status)"
            grep -q 'emulators/android/avd' <<<"$status_out"
            grep -q '+C (NOCOW): yes' <<<"$status_out"
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs bulk unkeep models steam
            """,
        )

    def test_05_dind_docker_volumes_nocow_builds_slice_and_docker_gc(self) -> None:
        """DinD on btrfs + systemd cgroups configures builds.slice with NOCOW volumes and docker-gc."""
        exec_bash(
            self.PRIV_CONTAINER,
            """
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst cpu install
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst cpu docker-slice install
            grep -q '"cgroup-parent": "builds.slice"' /etc/docker/daemon.json
            systemctl is-active builds.slice || systemctl start builds.slice
            systemctl start e2e-dind.service
            for _ in $(seq 1 30); do
                docker info >/dev/null 2>&1 && break
                sleep 0.5
            done
            info_out="$(docker info)"
            grep -q 'Cgroup Driver: systemd' <<<"$info_out"
            mkdir -p /var/lib/docker/volumes
            /usr/local/sbin/docker-volumes-nocow mark
            [[ "$(lsattr -d /var/lib/docker/volumes | awk '{print $1}')" == *C* ]]
            gc_st="$(python3 -I /opt/cst-rw/btrfs/docker-gc status)"
            grep -qi 'recorder\\|usage\\|images\\|docker' <<<"$gc_st"
            python3 -I /opt/cst-rw/btrfs/docker-gc run --dry-run >/dev/null
            systemctl stop e2e-dind.service
            """,
        )

    def test_05a_docker_volumes_nocow_migrate_postponed_and_boot_order(self) -> None:
        """docker-volumes-nocow migrate on loopback btrfs: postponed while docker.service is active; a CoW volume
        rewritten NOCOW with the same data; run when its unit and docker.service start in one transaction; Docker
        installed after install without /etc/btrfs-setup; and install re-scheduling an affected machine."""
        out = exec_bash(self.PRIV_CONTAINER, NOCOW_MIGRATE_SCRIPT).stdout
        for line in out.splitlines():
            if line.startswith("E2E-NOCOW"):
                print(f"\ntest_05a: {line}")

    def test_05b_docker_volumes_nocow_gives_up_at_once_while_its_flag_cannot_be_removed(self) -> None:
        """docker-volumes-nocow's unit at the third boot of a scheduled migration, with /etc/btrfs-setup
        read-only (a bind mount) and /var/lib/docker-volumes-nocow writable: three boots end in success,
        the count stays 2 and the flag stays, no copy starts and no alert is kept; with the mount gone the
        next boot gives up for good (flag and count removed, one alert kept for the next login, still no
        copy), and a boot after it alerts no more."""
        out = exec_bash(self.PRIV_CONTAINER, NOCOW_GIVE_UP_SCRIPT).stdout
        for line in out.splitlines():
            if line.startswith("E2E-NOCOW"):
                print(f"\ntest_05b: {line}")
        self.assertIn("E2E-OK", out)

    def test_05c_docker_volumes_nocow_a_count_that_cannot_be_reset_makes_no_request(self) -> None:
        """docker-volumes-nocow's unit with a count of 2 left without its flag, the volumes folder without +C
        and a copy-on-write volume, the count's file a read-only bind mount: the run ends in success, makes
        no request (no flag, no +C, the count still 2, no copy) and logs "cannot schedule the migration";
        with the mount gone the next boot makes the request and migrates the volume (flag and count gone)."""
        out = exec_bash(self.PRIV_CONTAINER, NOCOW_RESET_SCRIPT).stdout
        for line in out.splitlines():
            if line.startswith("E2E-NOCOW"):
                print(f"\ntest_05c: {line}")
        self.assertIn("E2E-OK", out)

    def test_06_cpu_cgroups_game_profile_watchdog_polkit_and_vm_pin(self) -> None:
        """cpu install, cgroup v2 weights/IOWeight, game-profile on/off, Polkit check, vm-pin XML plan, and uninstall."""
        exec_bash(
            self.PRIV_CONTAINER,
            """
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst cpu install
            grep -q 'IOWeight=50' /etc/systemd/system/machine.slice.d/10-cpu-setup.conf
            grep -q 'Delegate=cpuset' /etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf
            systemctl is-active cpu-game-watchdog.timer

            # Polkit rule validation (installed for user tester; non-seat invocation must be denied)
            grep -q 'subject.user == "tester"' /etc/polkit-1/rules.d/50-cpu-setup-game.rules
            systemctl start polkit.service
            systemctl is-active polkit.service
            # A bare "! cmd" never fails a set -e script: test the denial explicitly.
            if runuser -u tester -- systemctl --no-ask-password start cpu-game.service >/dev/null 2>&1; then
                echo "polkit let a non-seat tester start cpu-game.service" >&2
                exit 1
            fi

            # Verify cgroup v2 weights when a transient scope sits in machine.slice
            systemd-run --unit=e2e-fake-vm --slice=machine.slice sleep 60
            grep -qx '50' /sys/fs/cgroup/machine.slice/cpu.weight
            grep -q '50' /sys/fs/cgroup/machine.slice/io.weight

            # Exercise game-profile on -> off and check runtime cgroup overrides + restoration
            # Bound BUILD_CPUS to online container CPUs (e.g., 0-1 or 1 if >=2 CPUs)
            ncpu="$(nproc)"
            if (( ncpu >= 2 )); then
                sed -i "s/BUILD_CPUS=.*/BUILD_CPUS='1'/" /etc/cpu-setup/cpu-setup.conf
            else
                sed -i "s/BUILD_CPUS=.*/BUILD_CPUS='0'/" /etc/cpu-setup/cpu-setup.conf
            fi
            /usr/local/sbin/cpu-game-profile on
            grep -qx '10' /sys/fs/cgroup/machine.slice/cpu.weight
            # The MemoryLow drop-in that wins over uresourced's at a reload.
            grep -qx 'MemoryLow=16G' /run/systemd/system/user.slice.d/zz-cst-game.conf
            grep -q '10' /sys/fs/cgroup/machine.slice/io.weight
            # A daemon-reload must not put the baseline drop-in back over the profile.
            systemctl daemon-reload
            grep -qx '10' /sys/fs/cgroup/machine.slice/cpu.weight
            gp_state="$(/usr/local/sbin/cpu-game-profile state)"
            grep -q 'game profile: ON' <<<"$gp_state"
            /usr/local/sbin/cpu-game-profile off
            grep -qx '50' /sys/fs/cgroup/machine.slice/cpu.weight
            test -z "$(ls /run/systemd/system/*.d/zz-cst-game.conf 2>/dev/null)"
            grep -q '50' /sys/fs/cgroup/machine.slice/io.weight
            systemctl stop e2e-fake-vm.service || true

            # Exercise cpu-vm-pin plan against dummy XML in local virtqemud
            systemctl start virtqemud.service
            virsh -c qemu:///system list --all >/dev/null
            cat >/tmp/dummy-vm.xml <<'EOF'
<domain type='qemu'>
  <name>win11-devbox</name>
  <memory unit='MiB'>512</memory>
  <vcpu placement='static'>4</vcpu>
  <os><type arch='x86_64'>hvm</type></os>
  <devices><emulator>/usr/bin/qemu-system-x86_64</emulator></devices>
</domain>
EOF
            virsh -c qemu:///system define /tmp/dummy-vm.xml
            pin_plan="$(/usr/local/sbin/cpu-vm-pin plan win11-devbox)"
            grep -q 'emulatorpin' <<<"$pin_plan"
            virsh -c qemu:///system undefine win11-devbox
            rm -f /tmp/dummy-vm.xml
            systemctl stop virtqemud.service virtqemud.socket virtqemud-ro.socket virtqemud-admin.socket || true

            # A drop-in left behind (no game.state): uninstall removes it too.
            mkdir -p /run/systemd/system/user-1000.slice.d
            echo '[Slice]' >/run/systemd/system/user-1000.slice.d/zz-cst-game.conf
            echo 'MemoryLow=16G' >>/run/systemd/system/user-1000.slice.d/zz-cst-game.conf
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst cpu uninstall
            test ! -e /run/systemd/system/user-1000.slice.d/zz-cst-game.conf
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs uninstall
            test ! -e /etc/systemd/system/machine.slice.d/10-cpu-setup.conf
            test ! -e /etc/polkit-1/rules.d/50-cpu-setup-game.rules
            test ! -e /etc/tmpfiles.d/btrfs-automatic-reclaim.conf
            if systemctl is-active -q cpu-game-watchdog.timer; then echo "the watchdog timer still runs" >&2; exit 1; fi
            """,
        )

    def test_07_btrfs_install_moves_the_f44_tools_names_and_a_rerun_changes_nothing(self) -> None:
        """`cst btrfs install --no-below` on a machine that a version named
        f44-tools set up: its alerts folder (an alert, and one the new folder
        already holds a newer copy of), its reader and global user unit
        (enabled) and below's drop-in move to the new names; the old folder
        becomes a link that the second install removes; a third install writes
        nothing (every file `unchanged:`, the same files before and after);
        `uninstall` leaves none of the old names. The stall-report launcher is
        in tester's install record, done, and `uninstall` removes it and its line."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_07 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            old=/var/lib/f44-alerts
            new=/var/lib/cst-alerts
            old_unit=/etc/systemd/user/f44-pending-alerts.service
            wants=/etc/systemd/user/graphical-session.target.wants/f44-pending-alerts.service
            old_reader=/usr/local/bin/f44-pending-alerts
            old_dropin=/etc/systemd/system/below.service.d/10-f44-tools.conf
            new_dropin=/etc/systemd/system/below.service.d/10-cst.conf
            # Names as btrfs-notify makes them (32 hex characters); cst-pending-alerts only shows such names.
            alert=0123456789abcdef0123456789abcdef.alert
            dup=fedcba9876543210fedcba9876543210.alert
            cleanup() {
                systemctl --global disable f44-pending-alerts.service 2>/dev/null || true
                rm -rf "$old" "$old_unit" "$wants" "$old_reader" "$old_dropin"
                rm -f "$new/pending/$alert" "$new/pending/$dup"
                # The test started from an uninstalled btrfs part: a failure half way leaves none for the next tests.
                cst btrfs uninstall >/dev/null 2>&1 || true
            }
            trap cleanup EXIT
            show() { sed "s/^/test_07 $1: /" <<<"$2" >&2; }   # the CI log
            gone() {
                local f
                for f in "$@"; do
                    if [[ -e "$f" || -L "$f" ]]; then echo "still there: $f" >&2; return 1; fi
                done
            }
            # What install and uninstall may change, one line per file or link.
            files() {
                find /etc/systemd/system /etc/systemd/user /etc/sysctl.d /etc/tmpfiles.d /usr/local/sbin \
                    /usr/local/bin /home/tester/.local/bin \( -type f -o -type l \) \
                    -printf '%p %m %U:%G %s %T@ %l\n' | sort
            }

            # A machine without CST's btrfs part (test_06 ends with an uninstall; this test does not rely on it).
            cst btrfs uninstall >/dev/null
            gone "$new_dropin" "$old" "$old_unit" "$wants" "$old_reader" "$old_dropin"

            # What a version named f44-tools left: its names (git history has no file of that version,
            # so the unit is today's, renamed).
            install -d -m 0755 "$old/pending" "$new/pending"
            printf 'old alert\n' >"$old/pending/$alert"
            # An hour old: btrfs-notify drops pending alerts older than 7 days (by mtime) whenever it runs,
            # and the install's health check runs it.
            touch -d '1 hour ago' "$old/pending/$alert"
            alert_mtime="$(stat -c %Y "$old/pending/$alert")"
            printf 'stale copy\n' >"$old/pending/$dup"
            printf 'newer copy\n' >"$new/pending/$dup"
            sed 's/cst-pending-alerts/f44-pending-alerts/g; s|/var/lib/cst-alerts|/var/lib/f44-alerts|g' \
                /opt/cst-rw/btrfs/units/user/cst-pending-alerts.service >"$old_unit"
            systemctl --global enable f44-pending-alerts.service
            [[ -L "$wants" ]]
            printf '#!/bin/sh\nexit 0\n' >"$old_reader"
            chmod 0755 "$old_reader"
            install -d -m 0755 "$(dirname "$old_dropin")"
            printf '[Service]\n# e2e: the drop-in of a version named f44-tools\n' >"$old_dropin"

            # 1. The upgrade.
            out="$(cst btrfs install --no-below 2>&1)" || { show "first install" "$out"; exit 1; }
            show "first install" "$out"
            grep -qxF "moved:     $old -> $new (alerts kept for the next login; the old name is a link until the next install)" \
                <<<"$out"
            [[ "$(cat "$new/pending/$alert")" == "old alert" ]]
            [[ "$(stat -c %Y "$new/pending/$alert")" == "$alert_mtime" ]]   # moved, not rewritten
            [[ "$(cat "$new/pending/$dup")" == "newer copy" ]]   # the new folder's copy wins
            [[ -L "$old" && "$(readlink "$old")" == "$new" ]]
            gone "$old_unit" "$wants" "$old_reader" "$old_dropin"
            # --no-below: the old drop-in is renamed, not dropped.
            grep -qxF '# e2e: the drop-in of a version named f44-tools' "$new_dropin"
            [[ "$(systemctl --global is-enabled cst-pending-alerts.service)" == enabled ]]
            systemctl is-active btrfs-stall-monitor.service
            # The stall-report launcher is in tester's install record, done.
            grep -qF '"/home/tester/.local/bin/btrfs-stall-report"' /home/tester/.local/state/cst/installed.json
            grep -qF '"btrfs:report-launcher": "done"' /home/tester/.local/state/cst/installed.json

            # 2. The second install removes the link left for an old btrfs-notify, and writes no file.
            out="$(cst btrfs install --no-below 2>&1)" || { show "second install" "$out"; exit 1; }
            show "second install" "$out"
            grep -qxF "removed:   $old (link left by the previous install)" <<<"$out"
            gone "$old"
            if grep -E '^(written:|moved:|linked:)' <<<"$out"; then echo "the second install wrote a file" >&2; exit 1; fi

            # 3. A third install changes nothing: every file unchanged, no link made or removed.
            before="$(files)"
            out="$(cst btrfs install --no-below 2>&1)" || { show "third install" "$out"; exit 1; }
            show "third install" "$out"
            if grep -E '^(written:|moved:|linked:|removed)|^Created symlink|^Removed ' <<<"$out"; then
                echo "the third install changed something" >&2
                exit 1
            fi
            for dest in /usr/local/sbin/btrfs-health-check /usr/local/sbin/btrfs-notify \
                /usr/local/sbin/btrfs-stall-monitord /usr/local/bin/btrfs-stall-report \
                /usr/local/sbin/docker-volumes-nocow /usr/local/bin/cst-pending-alerts \
                /etc/systemd/system/btrfs-health-check.service /etc/systemd/system/btrfs-health-check.timer \
                /etc/systemd/system/btrfs-scrub-weekly.service /etc/systemd/system/btrfs-scrub-weekly.timer \
                /etc/systemd/system/btrfs-stall-monitor.service /etc/systemd/system/btrfs-stall-alert.service \
                /etc/systemd/system/docker-volumes-nocow.service /etc/systemd/user/cst-pending-alerts.service \
                /etc/sysctl.d/90-btrfs-writeback.conf /etc/tmpfiles.d/btrfs-automatic-reclaim.conf \
                /home/tester/.local/bin/btrfs-stall-report; do
                grep -qxF "unchanged: $dest" <<<"$out" || { echo "no 'unchanged: $dest'" >&2; exit 1; }
            done
            after="$(files)"
            if [[ "$after" != "$before" ]]; then
                diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") >&2 || true
                exit 1
            fi

            # 4. Undo: none of the old names, nor the drop-in they became.
            cst btrfs uninstall >/dev/null
            gone "$old" "$old_unit" "$wants" "$old_reader" "$old_dropin" "$new_dropin" \
                /etc/systemd/system/btrfs-stall-monitor.service /usr/local/sbin/btrfs-stall-monitord \
                /home/tester/.local/bin/btrfs-stall-report
            if grep -qsF btrfs:report-launcher /home/tester/.local/state/cst/installed.json; then
                echo "uninstall left the launcher's record line" >&2
                exit 1
            fi
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_08_snapshots_disable_cycle(self) -> None:
        """`cst btrfs snapshots disable` on hourly snapshots set up the way
        test_03 sets them up (the installer's runtime and units, the top level
        mounted by hand: the automount cannot be started here, since device
        units never appear without udev). `snapshots status` and the plan of
        `snapshots setup` (no terminal: it prints the plan, then refuses)
        change nothing; `disable` stops and disables both timers, forgets
        their last runs, drops the convert-at-boot flag and the btrbk settings
        an older CST wrote (not anyone else's), keeps the snapshots, the
        ~/Bulk boot step and the top-level units; a second `disable` changes
        nothing; `disable --purge` deletes the snapshots the core owns (not a
        foreign subvolume next to them) and the top-level units, and unmounts
        it. `snapshots setup` itself (setup again) is not run: it starts the
        automount, which cannot work in this container."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_08 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            TOP=/var/mnt/btrfs-top
            SNAPS=$TOP/snapshots
            CONF=/etc/btrfs-setup/snapshots.conf
            saved=/var/tmp/e2e-test08-snapshots.conf
            convert_flag=/etc/btrfs-setup/convert-at-boot
            bulk_flag=/etc/btrfs-setup/bulk-at-boot
            ours=/etc/btrbk/btrbk.conf
            theirs=/etc/btrbk/btrbk-nas.conf
            stamp=/var/lib/systemd/timers/stamp-btrfs-snapshot.timer
            nas_stamp=/var/lib/systemd/timers/stamp-btrfs-snapshot-nas.timer
            unit="$(systemd-escape --path "$TOP")"
            dev0="$(head -n1 /run/e2e-loop-devs)"
            """
            + TOP_LEVEL_OPTS_FN
            + r"""
            opts="$(top_level_opts)"
            made_bulk_flag=0
            cleanup() {
                systemctl disable --now btrfs-snapshot.timer btrfs-snapshot-nas.timer btrfs-subvol-convert.service \
                    "$unit.automount" 2>/dev/null || true
                if ! mountpoint -q "$TOP"; then mount -o "$opts" "$dev0" "$TOP" 2>/dev/null || true; fi
                if mountpoint -q "$TOP"; then
                    /usr/local/sbin/btrfs-snapshot purge >/dev/null 2>&1 || true
                    btrfs subvolume delete "$SNAPS/e2e-foreign" >/dev/null 2>&1 || true
                    umount "$TOP" 2>/dev/null || true
                fi
                rm -f "/etc/systemd/system/$unit.mount" "/etc/systemd/system/$unit.automount" "$convert_flag" \
                    "$ours" "$theirs" "$stamp" "$nas_stamp"
                if (( made_bulk_flag )); then rm -f "$bulk_flag"; fi
                rmdir /etc/btrbk 2>/dev/null || true
                rm -rf /run/systemd/system/btrfs-snapshot.timer.d /run/systemd/system/btrfs-snapshot-nas.timer.d
                rm -f "$CONF"
                if [[ -e "$saved" ]]; then mv "$saved" "$CONF"; fi
                systemctl daemon-reload
            }
            trap cleanup EXIT
            show() { sed "s/^/test_08 $1: /" <<<"$2" >&2; }   # the CI log
            # Everything the commands may change, one line each.
            state() {
                local u
                for u in btrfs-snapshot.timer btrfs-snapshot-nas.timer btrfs-subvol-convert.service \
                    "$unit.automount"; do
                    printf '%s %s %s\n' "$u" "$(systemctl is-enabled "$u" 2>/dev/null || true)" \
                        "$(systemctl is-active "$u" 2>/dev/null || true)"
                done
                { find /etc/btrfs-setup /etc/btrbk /etc/systemd/system /var/lib/btrfs-snapshot /var/lib/btrfs-bulk \
                    "$stamp" "$nas_stamp" -printf '%p %y %m %s %T@ %l\n' 2>/dev/null || true; } | sort
                { find "$SNAPS" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null || true; } | sort
                if mountpoint -q "$TOP"; then echo "top level mounted"; fi
                sha256sum /var/home/tester/.cargo/config.toml /var/home/tester/.config/uv/uv.toml 2>/dev/null || true
            }
            same() {   # same LABEL BEFORE: the state is still BEFORE
                local now
                now="$(state)"
                [[ "$now" == "$2" ]] && return 0
                echo "$1 changed the state:" >&2
                diff <(printf '%s\n' "$2") <(printf '%s\n' "$now") >&2 || true
                return 1
            }
            owned() { find "$SNAPS" -mindepth 1 -maxdepth 1 -regextype posix-extended \
                -regex '.*/(home|Workspace)\.[0-9]{8}T[0-9]{4}(_[0-9]+)?' -printf '%f\n' | sort; }

            # Set up as test_03 does: settings, runtime and units from the installer, the top level by hand.
            [[ ! -e "$bulk_flag" ]]   # this test makes it; a leftover would mean ~/Bulk items wait for a boot
            if [[ -e "$CONF" ]]; then cp -a "$CONF" "$saved"; fi
            install -d -m 0755 /etc/btrfs-setup /var/lib/btrfs-snapshot
            printf '%s\n' HOME_DIR=/var/home/tester HOME_MOUNT=/var/home HOME_SUBVOL=home "TOP=$TOP" \
                SNAP_DIR_NAME=snapshots SKIP_AT_OR_BELOW_GIB=0 RESUME_ABOVE_GIB=0 EMERGENCY_AT_OR_BELOW_GIB=0 \
                NAS_ENABLED=0 >"$CONF"
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-snapshot-runtime \
                btrfs/btrfs-snapshot btrfs/btrfs-snapshot-core btrfs/btrfs-notify btrfs/units/btrfs-snapshot.service \
                btrfs/units/btrfs-snapshot.timer btrfs/units/btrfs-snapshot-nas.service \
                btrfs/units/btrfs-snapshot-nas.timer btrfs/units/btrfs-subvol-convert.service >/dev/null
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install --set "UUID=$(cat /run/e2e-loop-uuid)" \
                --set "OPTIONS=$opts" btrfs/snapshots/top.mount.in btrfs/snapshots/top.automount.in >/dev/null
            systemctl daemon-reload
            systemctl enable "$unit.automount"
            mkdir -p "$TOP"
            mountpoint -q "$TOP" || mount -o "$opts" "$dev0" "$TOP"
            install -d -m 0700 "$SNAPS"
            # Neither timer may fire during the test (the hourly one would take a snapshot, both touch their
            # stamps): a runtime drop-in moves their calendar far ahead; enable, disable and the stamps
            # work as without it.
            for t in btrfs-snapshot.timer btrfs-snapshot-nas.timer; do
                install -d -m 0755 "/run/systemd/system/$t.d"
                printf '[Timer]\nOnCalendar=\nOnCalendar=2099-01-01 00:00:00\n' >"/run/systemd/system/$t.d/e2e-never.conf"
            done
            systemctl daemon-reload
            systemctl enable --now btrfs-snapshot.timer btrfs-snapshot-nas.timer
            test -e "$stamp"
            test -e "$nas_stamp"
            /usr/local/sbin/btrfs-snapshot hourly >/dev/null
            snaps="$(owned)"
            grep -q '^home\.' <<<"$snaps"
            # Left by earlier steps of a setup (both flags stop the hourly run, so they come after it).
            touch "$convert_flag"
            systemctl enable btrfs-subvol-convert.service
            printf 'avd\n' >"$bulk_flag"
            made_bulk_flag=1
            install -d -m 0755 /etc/btrbk
            printf '# Managed by btrfs-snapshot (CST); an older version used btrbk\n' >"$ours"
            printf '# my own btrbk settings\n' >"$theirs"
            btrfs subvolume create "$SNAPS/e2e-foreign" >/dev/null

            # 1. Plans: status, and setup without a terminal (it prints its plan, then refuses).
            before="$(state)"
            out="$(cst btrfs snapshots status 2>&1)" || { show status "$out"; exit 1; }
            show status "$out"
            grep -qx 'hourly snapshots: enabled, keep .*' <<<"$out"
            grep -qF 'conversion of busy directories pending' <<<"$out"
            same "snapshots status" "$before"
            rc=0
            out="$(cst btrfs snapshots setup </dev/null 2>&1)" || rc=$?
            show "setup plan" "$out"
            [[ "$rc" == 1 ]]
            grep -qxF 'No terminal to confirm: re-run with --yes.' <<<"$out"
            same "snapshots setup without a terminal" "$before"

            # 2. disable.
            out="$(cst btrfs snapshots disable 2>&1)" || { show disable "$out"; exit 1; }
            show disable "$out"
            grep -qxF 'Hourly snapshots and the NAS backup are stopped; existing snapshots are kept.' <<<"$out"
            # PROG: cst sets CST_PROG to the path it was started by, plus the module.
            grep -qxF 'Delete them all with: /opt/cst-rw/cst btrfs snapshots disable --purge' <<<"$out"
            grep -qF "Note: ~/Bulk items wait for the next boot (avd ) and are still moved" <<<"$out"
            for u in btrfs-snapshot.timer btrfs-snapshot-nas.timer btrfs-subvol-convert.service; do
                [[ "$(systemctl is-enabled "$u" || true)" == disabled ]]
                [[ "$(systemctl is-active "$u" || true)" == inactive ]]
            done
            test ! -e "$stamp"
            test ! -e "$nas_stamp"
            test ! -e "$convert_flag"
            test -e "$bulk_flag"                        # ~/Bulk's boot step is not part of the snapshots
            test ! -e "$ours"
            grep -qxF '# my own btrbk settings' "$theirs"
            [[ "$(owned)" == "$snaps" ]]                # the snapshots are kept
            test -d "$SNAPS/e2e-foreign"
            test -f "/etc/systemd/system/$unit.mount"
            test -f "/etc/systemd/system/$unit.automount"
            out="$(cst btrfs snapshots status 2>&1)"
            grep -qx 'hourly snapshots: off, keep .*' <<<"$out"

            # 3. A second disable changes nothing.
            after="$(state)"
            out="$(cst btrfs snapshots disable 2>&1)" || { show "second disable" "$out"; exit 1; }
            show "second disable" "$out"
            grep -qxF 'Hourly snapshots and the NAS backup are stopped; existing snapshots are kept.' <<<"$out"
            same "a second disable" "$after"

            # 4. disable --purge: the core's snapshots and the top-level units go; anything else stays.
            out="$(cst btrfs snapshots disable --purge 2>&1)" || { show purge "$out"; exit 1; }
            show purge "$out"
            grep -qF 'Snapshots purged and their configuration removed' <<<"$out"
            test ! -e "/etc/systemd/system/$unit.mount"
            test ! -e "/etc/systemd/system/$unit.automount"
            if mountpoint -q "$TOP"; then echo "purge left the top level mounted" >&2; exit 1; fi
            mount -o "$opts" "$dev0" "$TOP"
            [[ -z "$(owned)" ]]
            test -d "$SNAPS/e2e-foreign"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_09_docker_gc_enable_disable_cycle(self) -> None:
        """`cst btrfs docker-gc enable` with Docker up (test_05's DinD daemon on
        the loop btrfs, behind a stub docker.service as in test_05a: the unit
        the recorder is bound to): the program and its three units written,
        the recorder and the timer enabled and running, the timer's stamp made;
        a second `enable` writes nothing; `disable` stops and disables all
        three and forgets the timer's last run, and keeps the program and the
        unit files; a second `disable` changes nothing; `enable` again runs
        the recorder again; `disable --purge` removes the program, the units
        and /var/lib/docker-gc, and status says the timer is not installed; a
        second purge changes nothing; `enable` after it writes everything again. Its settings
        /etc/cst/docker-gc.conf: the first `enable` writes it (root, 0644; nothing protected, no
        docker-gc was installed), the second leaves it, `disable --purge` keeps it."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            DOCKER_UP_FN
            + r"""
            set -E
            trap 'echo "test_09 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            bin=/usr/local/sbin/docker-gc
            units=(docker-gc-recorder.service docker-gc.service docker-gc.timer)
            stamp=/var/lib/systemd/timers/stamp-docker-gc.timer
            conf=/etc/cst/docker-gc.conf   # its settings: kept by a purge
            remove_all() {
                systemctl disable --now "${units[@]}" 2>/dev/null || true
                rm -f "$bin" "${units[@]/#//etc/systemd/system/}" "$stamp" "$conf"
            }
            cleanup() {
                remove_all
                systemctl stop docker.service e2e-dind.service 2>/dev/null || true
                rm -f "$stub"
                systemctl daemon-reload
                systemctl reset-failed "${units[@]}" 2>/dev/null || true
            }
            trap cleanup EXIT
            show() { sed "s/^/test_09 $1: /" <<<"$2" >&2; }   # the CI log
            state() {
                local u
                for u in "${units[@]}"; do
                    printf '%s %s %s\n' "$u" "$(systemctl is-enabled "$u" 2>/dev/null || true)" \
                        "$(systemctl is-active "$u" 2>/dev/null || true)"
                done
                { find "$bin" /etc/systemd/system "$stamp" -printf '%p %y %m %s %T@ %l\n' 2>/dev/null || true; } | sort
            }
            recorder_runs() {   # active, and not restarting (Restart=on-failure would hide a failing recorder)
                local _
                for _ in $(seq 1 30); do
                    [[ "$(systemctl is-active docker-gc-recorder.service || true)" == active ]] && break
                    sleep 1
                done
                sleep 3
                [[ "$(systemctl is-active docker-gc-recorder.service || true)" == active &&
                    "$(systemctl show -P NRestarts docker-gc-recorder.service)" == 0 ]] && return 0
                echo "the recorder does not run:" >&2
                systemctl status --no-pager docker-gc-recorder.service >&2 || true
                journalctl -q --no-pager -u docker-gc-recorder.service -n 30 >&2 || true
                return 1
            }
            no_writes() {   # no_writes LABEL OUTPUT: every file unchanged
                local f
                if grep -E '^written:' <<<"$2"; then echo "$1 wrote a file" >&2; return 1; fi
                for f in "$bin" "${units[@]/#//etc/systemd/system/}"; do
                    grep -qxF "unchanged: $f" <<<"$2" || { echo "$1: no 'unchanged: $f'" >&2; return 1; }
                done
            }

            # Docker up: the DinD daemon, and a stub docker.service for the recorder's BindsTo=.
            remove_all
            systemctl reset-failed "${units[@]}" 2>/dev/null || true
            docker_up

            # 1. enable.
            out="$(cst btrfs docker-gc enable 2>&1)" || { show enable "$out"; exit 1; }
            show enable "$out"
            grep -qF 'docker-gc enabled: image use is recorded from now on' <<<"$out"
            for f in "$bin" "${units[@]/#//etc/systemd/system/}"; do
                grep -qxF "written:   $f" <<<"$out" || { echo "no 'written:   $f'" >&2; exit 1; }
            done
            # Its settings first: no docker-gc was installed, so no registry is protected.
            grep -qxF "written:   $conf (DOCKER_GC_PROTECT=)" <<<"$out"
            [[ "$(stat -c '%a %U' "$conf")" == "644 root" ]]
            [[ "$(systemctl is-enabled docker-gc-recorder.service)" == enabled ]]
            [[ "$(systemctl is-enabled docker-gc.timer)" == enabled ]]
            systemctl is-active docker-gc.timer
            test -e "$stamp"
            recorder_runs
            out="$(cst btrfs docker-gc status 2>&1)" || { show status "$out"; exit 1; }
            show status "$out"
            grep -qx 'timer enabled, recorder active' <<<"$out"

            # 2. A second enable writes nothing.
            out="$(cst btrfs docker-gc enable 2>&1)" || { show "second enable" "$out"; exit 1; }
            show "second enable" "$out"
            no_writes "the second enable" "$out"
            grep -qxF "unchanged: $conf" <<<"$out"
            recorder_runs

            # 3. disable: nothing enabled or running, the stamp forgotten; the program and units stay.
            out="$(cst btrfs docker-gc disable 2>&1)" || { show disable "$out"; exit 1; }
            show disable "$out"
            grep -qxF 'docker-gc disabled (recorded state kept in /var/lib/docker-gc).' <<<"$out"
            for u in "${units[@]}"; do
                [[ "$(systemctl is-enabled "$u" 2>/dev/null || true)" != enabled ]]
                [[ "$(systemctl is-active "$u" 2>/dev/null || true)" != active ]]
            done
            test ! -e "$stamp"
            test -x "$bin"
            for f in "${units[@]/#//etc/systemd/system/}"; do test -f "$f"; done
            out="$(cst btrfs docker-gc status 2>&1)" || { show "status after disable" "$out"; exit 1; }
            show "status after disable" "$out"
            # One exact first line (an older btrfs-setup.sh added "off" on a line of its own).
            [[ "$(head -n 1 <<<"$out")" == 'timer disabled, recorder inactive' ]]
            if grep -qx 'off.*' <<<"$out"; then echo "status printed a second timer state" >&2; exit 1; fi
            out="$(cst btrfs status 2>&1)" || { show "btrfs status" "$out"; exit 1; }
            grep -qxF '  off (/opt/cst-rw/cst btrfs docker-gc enable)' <<<"$out"   # "${PROG} docker-gc enable"

            # 4. A second disable changes nothing.
            before="$(state)"
            out="$(cst btrfs docker-gc disable 2>&1)" || { show "second disable" "$out"; exit 1; }
            after="$(state)"
            if [[ "$after" != "$before" ]]; then
                echo "a second disable changed the state:" >&2
                diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") >&2 || true
                exit 1
            fi

            # 5. enable again: running again, nothing written.
            out="$(cst btrfs docker-gc enable 2>&1)" || { show "enable again" "$out"; exit 1; }
            show "enable again" "$out"
            no_writes "enable after disable" "$out"
            [[ "$(systemctl is-enabled docker-gc.timer)" == enabled ]]
            systemctl is-active docker-gc.timer
            recorder_runs
            test -d /var/lib/docker-gc

            # 6. disable --purge: stopped, and the program, the units and the recorded state removed.
            out="$(cst btrfs docker-gc disable --purge 2>&1)" || { show purge "$out"; exit 1; }
            show purge "$out"
            grep -qF 'docker-gc removed: program, units and recorded state' <<<"$out"
            grep -qxF "kept:      $conf (the settings: which registries are protected)" <<<"$out"
            test -f "$conf"
            for f in "$bin" "${units[@]/#//etc/systemd/system/}" "$stamp" /var/lib/docker-gc; do
                if [[ -e "$f" ]]; then echo "purge left $f" >&2; exit 1; fi
            done
            # A dry run after the purge adds nothing to the state folder (the lock is in /run/docker-gc).
            out="$(cst btrfs docker-gc run --dry-run 2>&1)" || { show "dry run after purge" "$out"; exit 1; }
            grep -qF '(dry run: nothing removed)' <<<"$out"
            if [[ -e /var/lib/docker-gc ]]; then echo "the dry run made /var/lib/docker-gc" >&2; exit 1; fi
            for u in "${units[@]}"; do
                [[ "$(systemctl show -P LoadState "$u")" == not-found ]]
                [[ "$(systemctl is-active "$u" 2>/dev/null || true)" != active ]]
            done
            out="$(cst btrfs docker-gc status 2>&1)" || { show "status after purge" "$out"; exit 1; }
            show "status after purge" "$out"
            [[ "$(head -n 1 <<<"$out")" == 'timer not installed, recorder inactive' ]]
            if grep -q '^still installed:' <<<"$out"; then echo "status lists a purged file" >&2; exit 1; fi

            # 7. A second purge changes nothing.
            before="$(state)"
            out="$(cst btrfs docker-gc disable --purge 2>&1)" || { show "second purge" "$out"; exit 1; }
            after="$(state)"
            if [[ "$after" != "$before" ]]; then
                echo "a second purge changed the state:" >&2
                diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") >&2 || true
                exit 1
            fi

            # 8. enable after a purge installs everything again.
            out="$(cst btrfs docker-gc enable 2>&1)" || { show "enable after purge" "$out"; exit 1; }
            show "enable after purge" "$out"
            for f in "$bin" "${units[@]/#//etc/systemd/system/}"; do
                grep -qxF "written:   $f" <<<"$out" || { echo "no 'written:   $f'" >&2; exit 1; }
            done
            recorder_runs
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_10_crypt_perf_refuses_without_dm_crypt_members(self) -> None:
        """`cst btrfs crypt-perf`: a usage error exits 2; on this loop btrfs
        (no dm-crypt member) status, enable and disable exit 1 and say so,
        before any terminal or passphrase is asked for."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_10 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            cst() { /opt/cst-rw/cst "$@"; }
            rc=0
            out="$(cst btrfs crypt-perf bogus 2>&1)" || rc=$?
            [[ "$rc" == 2 ]]
            grep -qxF 'crypt-perf: status|enable|disable' <<<"$out"
            rc=0
            out="$(cst btrfs crypt-perf status extra 2>&1)" || rc=$?
            [[ "$rc" == 2 ]]
            grep -qxF "crypt-perf: unexpected argument 'extra'" <<<"$out"
            for action in status enable disable; do
                rc=0
                out="$(cst btrfs crypt-perf "$action" </dev/null 2>&1)" || rc=$?
                [[ "$rc" == 1 ]]
                grep -qxF 'crypt-perf: no dm-crypt member under the root btrfs' <<<"$out"
            done
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_10a_crypt_perf_enable_disable_on_a_loop_luks2_device(self) -> None:
        """`cst btrfs crypt-perf` on a btrfs whose member is a real dm-crypt
        mapping (LUKS2 on a loop device, opened with allow_discards), mounted
        at /sysroot in a mount namespace of its own. status and a run without
        a terminal (refused) change nothing; a wrong passphrase leaves the
        device as it was and exits 1; enable sets no_read_workqueue and
        no_write_workqueue in the live table and in the LUKS2 header and keeps
        allow_discards; a second enable keeps that; a plain device added to
        the btrfs makes enable refuse; disable clears both flags (still with
        allow_discards), and a second disable keeps that."""
        docker_exec(
            self.PRIV_CONTAINER,
            ["bash", "-c", "mkdir -p /var/tmp/e2e-crypt && cat >/var/tmp/e2e-crypt/pty-drive.py"],
            input_text=CRYPT_PTY_DRIVER,
        )
        docker_exec(
            self.PRIV_CONTAINER,
            ["bash", "-c", "cat >/var/tmp/e2e-crypt/inner.sh"],
            input_text=r"""
set -E
trap 'echo "test_10a FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
export LC_ALL=C
name="$E2E_NAME"
loop="$E2E_LOOP"
plain="$E2E_PLAIN"
pass="$E2E_PASS"
w=/var/tmp/e2e-crypt
cst() { /opt/cst-rw/cst "$@"; }
drive() { /usr/bin/python3 -I "$w/pty-drive.py" "$1" /opt/cst-rw/cst btrfs crypt-perf "${@:2}"; }
show() { sed "s/^/test_10a $1: /" <<<"$2" >&2; }   # the CI log
# The flags of the live table, and those stored in the LUKS2 header.
active() { dmsetup table "$name" | tr ' ' '\n' | grep -E '^(allow_discards|no_read_workqueue|no_write_workqueue)$' | sort | xargs; }
header() { cryptsetup luksDump "$loop" | awk -F: '/^Flags:/ {print $2}' | xargs; }
flags() { printf '%s | %s\n' "$(active)" "$(header)"; }
same() {   # same LABEL BEFORE
    [[ "$(flags)" == "$2" ]] && return 0
    echo "$1 changed the flags: $2 -> $(flags)" >&2
    return 1
}

# This namespace's /sysroot is the encrypted btrfs (the outer one is the loop RAID1). The loop RAID1 is
# unmounted here first: on a stacked mount, findmnt --target lists the lower one first, and crypt-perf
# (root_fs_uuid: its first line) would read the RAID1.
umount -l /sysroot
mount "/dev/mapper/$name" /sysroot
uuid="$(findmnt -rn -o UUID --target /sysroot)"
[[ "$(ls "/sys/fs/btrfs/$uuid/devices")" == dm-* ]]
[[ "$(active)" == allow_discards ]]

# 1. Plans and refusals change nothing.
before="$(flags)"
out="$(cst btrfs crypt-perf status 2>&1)" || { show status "$out"; exit 1; }
show status "$out"
grep -qE "^$name \(dm-[0-9]+ on $loop\)$" <<<"$out"
grep -qxF '  active:     allow_discards' <<<"$out"
same "crypt-perf status" "$before"
rc=0
out="$(cst btrfs crypt-perf enable </dev/null 2>&1)" || rc=$?
[[ "$rc" == 1 ]]
grep -qxF 'crypt-perf: needs a terminal for the LUKS passphrases' <<<"$out"
same "enable without a terminal" "$before"
rc=0
out="$(drive not-the-passphrase enable)" || rc=$?
show "wrong passphrase" "$out"
[[ "$rc" == 1 ]]
grep -qF "FAILED on $name: unchanged" <<<"$out"
grep -qxF 'Applied to 0 of 1 member(s).' <<<"$out"
same "a wrong passphrase" "$before"

# 2. enable, twice.
for run in first second; do
    out="$(drive "$pass" enable)" || { show "$run enable" "$out"; exit 1; }
    show "$run enable" "$out"
    grep -qxF 'Applied to 1 of 1 member(s).' <<<"$out"
    [[ "$(active)" == "allow_discards no_read_workqueue no_write_workqueue" ]]
    grep -qw no-read-workqueue <<<"$(header)"
    grep -qw no-write-workqueue <<<"$(header)"
done
out="$(cst btrfs crypt-perf status 2>&1)"
grep -qxF '  active:     allow_discards no_read_workqueue no_write_workqueue' <<<"$out"

# 3. A member without dm-crypt: enable refuses before asking anything.
btrfs device add -f "$plain" /sysroot >/dev/null
enabled="$(flags)"
rc=0
out="$(drive "$pass" enable)" || rc=$?
show "plain member" "$out"
[[ "$rc" == 1 ]]
grep -qF 'not every btrfs member is present and encrypted' <<<"$out"
same "enable with a plain member" "$enabled"
btrfs device remove "$plain" /sysroot

# 4. disable, twice.
for run in first second; do
    out="$(drive "$pass" disable)" || { show "$run disable" "$out"; exit 1; }
    show "$run disable" "$out"
    grep -qxF 'Applied to 1 of 1 member(s).' <<<"$out"
    [[ "$(active)" == allow_discards ]]
    if grep -qwE 'no-read-workqueue|no-write-workqueue' <<<"$(header)"; then
        echo "disable left a flag in the header: $(header)" >&2
        exit 1
    fi
done
echo E2E-OK
""",
        )
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_10a FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            # No udev in this container: device-mapper makes its device nodes itself.
            export DM_DISABLE_UDEV=1
            w=/var/tmp/e2e-crypt
            name=e2e-crypt-perf
            pass=e2e-crypt-passphrase
            loop=""
            plain=""
            cleanup() {
                # The namespace's mount goes when its last process ends; the mapping may stay busy a moment.
                for _ in 1 2 3 4 5 6 7 8 9 10; do
                    cryptsetup close "$name" 2>/dev/null && break
                    [[ -e "/dev/mapper/$name" ]] || break
                    sleep 1
                done
                dmsetup remove "$name" 2>/dev/null || true
                if [[ -n "$loop" ]]; then losetup -d "$loop" 2>/dev/null || true; fi
                if [[ -n "$plain" ]]; then losetup -d "$plain" 2>/dev/null || true; fi
                rm -rf "$w"
            }
            trap cleanup EXIT
            truncate -s 512M "$w/luks.img" "$w/plain.img"
            loop="$(losetup --find --show "$w/luks.img")"
            plain="$(losetup --find --show "$w/plain.img")"
            # LUKS2 (refresh --persistent needs it); a cheap key derivation keeps each passphrase check short.
            printf '%s' "$pass" | cryptsetup luksFormat --type luks2 --batch-mode --pbkdf pbkdf2 \
                --pbkdf-force-iterations 1000 --key-file=- "$loop"
            printf '%s' "$pass" | cryptsetup open --allow-discards --key-file=- "$loop" "$name"
            mkfs.btrfs -f -L e2e-crypt "/dev/mapper/$name" >/dev/null
            unshare -m --propagation private env E2E_NAME="$name" E2E_LOOP="$loop" E2E_PLAIN="$plain" \
                E2E_PASS="$pass" bash -euo pipefail "$w/inner.sh"
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_11_bulk_steam_plan_changes_nothing(self) -> None:
        """`cst btrfs bulk steam plan` on a Steam (Flatpak) library built from
        btrfs/tests/fixtures/steam (a game, Dota 2, and a tool, the Steam Linux
        Runtime), after `bulk apply` made ~/Bulk/steam. flatpak itself is a
        stand-in (the image has none): Steam installed, `override --user`
        kept in a file. The plan lists the game as a reflink move, the tool as
        skipped, ~/Bulk/steam as library 1 to add and the shader cache that
        stays, exits 0 and changes nothing. With a process named steam
        running, the plan says so, and `bulk steam apply` exits 5 with
        nothing changed. A bad argument exits 2."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_11 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            as_tester() { runuser -u tester -- "$@"; }
            H=/var/home/tester
            app_root=$H/.var/app/com.valvesoftware.Steam
            root=$app_root/.local/share/Steam
            sa=$root/steamapps
            T=$H/Bulk/steam
            fx=/opt/cst-rw/btrfs/tests/fixtures/steam
            real_root=/var/home/danilo/.var/app/com.valvesoftware.Steam/.local/share/Steam
            fake=/usr/local/bin/flatpak
            fp=/var/tmp/e2e-flatpak
            steam_dir=/var/tmp/e2e-steam
            steam_pid=""
            made_T=0
            unkept=0
            cleanup() {
                if [[ -n "$steam_pid" ]]; then kill "$steam_pid" 2>/dev/null || true; fi
                rm -rf "$fake" "$fp" "$steam_dir" "$app_root"
                if (( made_T )); then rmdir "$T" 2>/dev/null || true; fi
                if (( unkept )); then cst btrfs bulk keep steam >/dev/null 2>&1 || true; fi
            }
            trap cleanup EXIT
            show() { sed "s/^/test_11 $1: /" <<<"$2" >&2; }   # the CI log
            state() {
                { find "$root" "$T" /var/lib/btrfs-bulk /etc/btrfs-setup -printf '%p %y %m %U %s %T@ %l\n' 2>/dev/null ||
                    true; } | sort
                sha256sum "$root/config/libraryfolders.vdf" "$sa/libraryfolders.vdf"
                cat "$fp/overrides"
            }
            same() {   # same LABEL BEFORE
                local now
                now="$(state)"
                [[ "$now" == "$2" ]] && return 0
                echo "$1 changed something:" >&2
                diff <(printf '%s\n' "$2") <(printf '%s\n' "$now") >&2 || true
                return 1
            }

            # flatpak: a stand-in. Steam is installed; other apps are not; overrides go to a file.
            [[ ! -e "$fake" ]]
            install -d -m 1777 "$fp"
            : >"$fp/overrides"
            chmod 0666 "$fp/overrides"
            printf '%s\n' '#!/bin/bash' \
                '# E2E stand-in for flatpak (test_11): Steam installed; override --user kept in a file.' \
                "st=$fp/overrides" \
                'case "$1" in' \
                '    info) [[ "$2" == com.valvesoftware.Steam ]]; exit ;;' \
                '    override)' \
                '        [[ "$2" == --user && "${@: -1}" == com.valvesoftware.Steam ]] || exit 1' \
                '        case "$3" in' \
                '            --show) [[ -s "$st" ]] && printf "[Context]\nfilesystems=%s;\n" "$(paste -sd ";" "$st")"; exit 0 ;;' \
                '            --filesystem=*) printf "%s\n" "${3#--filesystem=}" >>"$st"; exit 0 ;;' \
                '        esac ;;' \
                'esac' \
                'echo "flatpak stand-in: unexpected arguments: $*" >&2' \
                'exit 1' >"$fake"
            chmod 0755 "$fake"

            # The main Steam library, as the user: both libraryfolders.vdf, the two manifests, their files.
            [[ ! -e "$app_root" ]]
            as_tester mkdir -p "$root/config" "$sa/common/dota 2 beta/game/bin/linuxsteamrt64" \
                "$sa/common/dota 2 beta/game/dota" "$sa/common/SteamLinuxRuntime_sniper" \
                "$sa/workshop/content/570/128726494" "$sa/compatdata/570" "$sa/downloading" "$sa/temp" \
                "$sa/workshop/downloads" "$sa/workshop/temp" "$sa/shadercache/570/fozpipelinesv6"
            for f in "$root/config/libraryfolders.vdf" "$sa/libraryfolders.vdf"; do
                sed "s|$real_root|$root|" "$fx/libraryfolders.vdf" | as_tester tee "$f" >/dev/null
            done
            for f in appmanifest_570.acf appmanifest_1628350.acf; do as_tester cp "$fx/$f" "$sa/$f"; done
            as_tester cp "$fx/libraryfolder.vdf" "$root/libraryfolder.vdf"
            as_tester cp "$fx/appworkshop_570.acf" "$sa/workshop/appworkshop_570.acf"
            as_tester sh -c 'head -c 4M /dev/urandom >"$1/game/dota/pak01_dir.vpk"; echo elf >"$1/game/bin/linuxsteamrt64/dota2"' \
                sh "$sa/common/dota 2 beta"
            as_tester sh -c 'echo run >"$1/common/SteamLinuxRuntime_sniper/run"; echo item >"$1/workshop/content/570/128726494/item.vpk"; echo cache >"$1/shadercache/570/fozpipelinesv6/cache.foz"' \
                sh "$sa"

            # ~/Bulk/steam as `bulk apply` makes it (with the Flatpak's access to it).
            if grep -qsx steam /etc/btrfs-setup/bulk-keep; then cst btrfs bulk unkeep steam >/dev/null; unkept=1; fi
            [[ -e "$T" ]] || made_T=1
            out="$(cst btrfs bulk apply 2>&1)" || { show "bulk apply" "$out"; exit 1; }
            show "bulk apply" "$out"
            test -d "$T"
            grep -qxF "$H/Bulk/steam" "$fp/overrides" || grep -qxF /home/tester/Bulk/steam "$fp/overrides"

            # 1. The plan.
            before="$(state)"
            out="$(cst btrfs bulk steam plan 2>&1)" || { show plan "$out"; exit 1; }
            show plan "$out"
            grep -qE '^  Steam running +no$' <<<"$out"
            grep -qE '^  library ~/Bulk/steam +add as library 1 \(contentid new; both libraryfolders.vdf, backed up first\)$' \
                <<<"$out"
            grep -qE '^  570 Dota 2 +move .* \(reflink, no data written\)$' <<<"$out"
            grep -qE '^  1628350 Steam Linux Runtime 3.0 \(sniper\) +skipped: a tool, not a game; move it with --app 1628350$' \
                <<<"$out"
            grep -qE '^  shader cache of 570 ' <<<"$out"
            grep -qxF 'Dry run, nothing changed. Do it with: cst btrfs bulk steam apply (Steam closed)' <<<"$out"
            same "bulk steam plan" "$before"
            if compgen -G '/var/lib/btrfs-bulk/journal-steam-*' >/dev/null; then echo "plan left a journal" >&2; exit 1; fi

            # 2. Steam running: the plan says so; apply does nothing and exits 5.
            mkdir -p "$steam_dir"
            cp /usr/bin/sleep "$steam_dir/steam"
            runuser -u tester -- "$steam_dir/steam" 600 &
            for _ in $(seq 1 50); do
                steam_pid="$(pgrep -x -u tester steam || true)"
                [[ -n "$steam_pid" ]] && break
                sleep 0.1
            done
            [[ -n "$steam_pid" ]]
            out="$(cst btrfs bulk steam plan 2>&1)" || { show "plan, Steam running" "$out"; exit 1; }
            show "plan, Steam running" "$out"
            grep -qE "^  Steam running +YES \(process $steam_pid \(steam\)\): close it" <<<"$out"
            same "bulk steam plan with Steam running" "$before"
            rc=0
            out="$(cst btrfs bulk steam apply 2>&1)" || rc=$?
            show "apply, Steam running" "$out"
            [[ "$rc" == 5 ]]
            grep -qF 'Steam is running: nothing done.' <<<"$out"
            same "bulk steam apply with Steam running" "$before"

            # 3. A bad argument.
            rc=0
            out="$(cst btrfs bulk steam bogus 2>&1)" || rc=$?
            [[ "$rc" == 2 ]]
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_13_docker_gc_sandboxed_units_record_and_run_with_real_docker(self) -> None:
        """docker-gc-recorder.service and docker-gc.service as shipped, sandboxed
        (ProtectSystem=strict, no capability, AF_UNIX only; /var/lib/docker-gc
        and, for the weekly run, /run/docker-gc the only writable places), against the real DinD daemon behind a stub
        docker.service: the recorder runs without restarts and writes an
        image use it saw in `docker events` (an image imported offline, then
        tagged every 5 s until it is there: one event sent before the recorder
        subscribed would be lost) to last-used.json; if it does not, the CI log
        says whether the same program records it outside the sandbox; `cst btrfs docker-gc run` starts
        docker-gc.service, which ends in success, takes its lock in
        /run/docker-gc (its RuntimeDirectory, kept after the run) and writes
        last-run.json with no failed step
        (the build-cache prune runs buildx, whose state is in
        /var/lib/docker-gc/buildx). A runner booted less than
        10 minutes ago would make the run skip itself (MIN_UPTIME): then, and
        only then, a test drop-in shows the unit a later /proc/uptime. Before that run, the
        sandboxed docker-gc.service reads /etc/cst/docker-gc.conf: with a line it cannot read it
        ends with 78 and names the line in its journal."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            DOCKER_UP_FN
            + r"""
            set -E
            trap 'echo "test_13 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            units=(docker-gc-recorder.service docker-gc.service docker-gc.timer)
            state=/var/lib/docker-gc
            uptime_dropin=/run/systemd/system/docker-gc.service.d/90-e2e-uptime.conf
            show() { sed "s/^/test_13 $1: /" <<<"$2" >&2; }   # the CI log
            conf=/etc/cst/docker-gc.conf
            cleanup() {
                cst btrfs docker-gc disable --purge >/dev/null 2>&1 || true
                rm -f "$conf"
                docker images -q e2e-gc-probe | sort -u | xargs -r docker image rm -f >/dev/null 2>&1 || true
                rm -rf "$(dirname "$uptime_dropin")" /run/e2e-uptime /var/tmp/e2e-gc-probe /run/e2e-gc-plain
                systemctl stop docker.service e2e-dind.service 2>/dev/null || true
                rm -f "$stub"
                systemctl daemon-reload
                systemctl reset-failed "${units[@]}" 2>/dev/null || true
            }
            trap cleanup EXIT
            journal() { journalctl -q --no-pager -o cat -u "$1" -n 30 >&2 || true; }

            docker_up
            t_enable="$(date +%s)"
            out="$(cst btrfs docker-gc enable 2>&1)" || { show enable "$out"; exit 1; }
            for u in docker-gc-recorder.service docker-gc.service; do
                [[ "$(systemctl show -P ProtectSystem "$u")" == strict ]]
                [[ "$(systemctl show -P CapabilityBoundingSet "$u")" == "" ]]
                [[ "$(systemctl show -P StateDirectory "$u")" == docker-gc ]]
            done
            [[ "$(systemctl show -P RuntimeDirectory docker-gc.service)" == docker-gc ]]
            [[ "$(systemctl show -P RuntimeDirectory docker-gc-recorder.service)" == "" ]]
            for _ in $(seq 1 30); do
                [[ "$(systemctl is-active docker-gc-recorder.service || true)" == active ]] && break
                sleep 1
            done
            [[ "$(systemctl is-active docker-gc-recorder.service || true)" == active ]] ||
                { journal docker-gc-recorder.service; exit 1; }
            # Active comes before the recorder follows the events (it first lists the containers
            # and writes its state): wait for its line of this start.
            for _ in $(seq 1 30); do
                journalctl -q --no-pager -o cat -u docker-gc-recorder.service --since "@$t_enable" |
                    grep -qF 'recording image use from docker events' && break
                sleep 1
            done

            # An image use, offline: import a one-file image, then tag it again every 5 s until the
            # recorder has it. An event Docker sends before `docker events` subscribed is lost, so
            # one tag right after the start would race the recorder (the first CI run of this test
            # did not record it); a later tag cannot.
            rm -rf /var/tmp/e2e-gc-probe && mkdir -p /var/tmp/e2e-gc-probe
            echo probe >/var/tmp/e2e-gc-probe/probe.txt
            t_import="$(date +%s)"
            tar -C /var/tmp/e2e-gc-probe -cf - probe.txt | docker import - e2e-gc-probe:1 >/dev/null
            iid="$(docker image inspect --format '{{.Id}}' e2e-gc-probe:1)"
            recorded() {   # recorded STATE_FILE: the probe image is in it
                python3 -I -c 'import json, sys; sys.exit(sys.argv[2] not in json.load(open(sys.argv[1]))["images"])' \
                    "$1" "$iid" 2>/dev/null
            }
            # Why not, for the CI log: what was saved and sent, and whether the same program
            # records the probe outside the sandbox (DOCKER_GC_FLUSH=2, its own state folder).
            diagnose() {
                echo "the recorder did not record $iid" >&2
                cat "$state/last-used.json" >&2 || true
                systemctl show -p NRestarts -p MainPID -p ActiveEnterTimestamp docker-gc-recorder.service >&2 || true
                journal docker-gc-recorder.service
                docker events --since "$t_import" --until "$(date +%s)" --format '{{json .}}' | tail -n 5 >&2 || true
                rm -rf /run/e2e-gc-plain
                DOCKER_GC_STATE=/run/e2e-gc-plain DOCKER_GC_FLUSH=2 timeout 25 /usr/local/sbin/docker-gc record >&2 &
                sleep 5
                docker tag e2e-gc-probe:1 e2e-gc-probe:plain
                sleep 8
                if recorded /run/e2e-gc-plain/last-used.json; then
                    echo "OUTSIDE the sandbox the same recorder records it" >&2
                else
                    echo "outside the sandbox the recorder does not record it either" >&2
                fi
                wait || true
            }
            seen=0
            for i in $(seq 0 59); do
                (( i % 5 )) || docker tag e2e-gc-probe:1 "e2e-gc-probe:t$i"
                if recorded "$state/last-used.json"; then
                    seen=1
                    break
                fi
                sleep 1
            done
            (( seen )) || { diagnose; exit 1; }
            [[ "$(systemctl is-active docker-gc-recorder.service)" == active ]]
            [[ "$(systemctl show -P NRestarts docker-gc-recorder.service)" == 0 ]]

            # The weekly run, as 'cst btrfs docker-gc run' starts it.
            if (( $(cut -d. -f1 /proc/uptime) < 610 )); then
                echo "test_13: uptime under 10 minutes: the run sees a later /proc/uptime" >&2
                echo '100000.00 100000.00' >/run/e2e-uptime
                mkdir -p "$(dirname "$uptime_dropin")"
                printf '[Service]\nBindReadOnlyPaths=/run/e2e-uptime:/proc/uptime\n' >"$uptime_dropin"
                systemctl daemon-reload
            fi
            # The sandboxed run reads /etc/cst/docker-gc.conf (ProtectSystem=strict leaves /etc readable):
            # a line it cannot read stops it with 78 before it asks docker anything; then a valid file.
            grep -qxF 'DOCKER_GC_PROTECT=' "$conf"   # enable wrote it: no docker-gc was installed before
            echo 'DOCKER_GC_PROTCT=e2e-registry.example' >>"$conf"
            t_bad="$(date +%s)"
            if systemctl start docker-gc.service; then echo "a settings file it cannot read did not stop the run" >&2; exit 1; fi
            [[ "$(systemctl show -P ExecMainStatus docker-gc.service)" == 78 ]] || { journal docker-gc.service; exit 1; }
            journalctl -q --no-pager -o cat -u docker-gc.service --since "@$t_bad" |
                grep -qF "$conf line 9: DOCKER_GC_PROTCT: unknown setting" || { journal docker-gc.service; exit 1; }
            printf 'DOCKER_GC_PROTECT=e2e-registry.example\n' >"$conf"
            systemctl reset-failed docker-gc.service
            rm -f "$state/last-run.json"
            out="$(cst btrfs docker-gc run 2>&1)" || { show run "$out"; journal docker-gc.service; exit 1; }
            show run "$out"
            [[ "$(systemctl show -P Result docker-gc.service)" == success ]]
            [[ "$(systemctl show -P ExecMainStatus docker-gc.service)" == 0 ]]
            # Every step worked under the sandbox: a failed removal or build-cache prune is only counted.
            python3 -I -c 'import json, sys; d = json.load(open(sys.argv[1])); sys.exit(d["failed"] != 0)' \
                "$state/last-run.json" || { cat "$state/last-run.json" >&2; journal docker-gc.service; exit 1; }
            test -f /run/docker-gc/run.lock
            test ! -e "$state/run.lock"
            test ! -e /run/docker-gc.lock
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_14_btrfs_watch_waits_for_the_real_recorder(self) -> None:
        """`cst btrfs watch` as the user with the real recorder
        (btrfs-stall-monitor.service from `cst btrfs install`) on the loop
        btrfs: a run that writes and syncs 64 MiB passes on its exit status,
        and the report it saves covers the end of the run (the wait for the
        recorder's next write works), with samples and a verdict that is not
        NO DATA. `uninstall` at the end leaves the machine as test_06 left it."""
        # Registered first: an install that fails half way is undone too.
        self.addCleanup(
            exec_bash,
            self.PRIV_CONTAINER,
            "SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs uninstall >/dev/null; "
            "rm -rf /var/tmp/e2e-watch2 /home/tester/e2e-watch.bin",
            check=False,
        )
        exec_bash(
            self.PRIV_CONTAINER,
            r"""
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs install --no-below >/dev/null
            systemctl is-active btrfs-stall-monitor.service
            rm -rf /var/tmp/e2e-watch2 && install -d -m 0777 /var/tmp/e2e-watch2
            """,
        )
        out = as_tester_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_14 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C XDG_STATE_HOME=/var/tmp/e2e-watch2
            base=$XDG_STATE_HOME/btrfs-stall-watch
            rc=0
            t0="$(date +%s)"
            out="$(/opt/cst-rw/cst btrfs watch -l e2e-io -- sh -c \
                'dd if=/dev/zero of="$HOME/e2e-watch.bin" bs=1M count=64 conv=fsync status=none; rm -f "$HOME/e2e-watch.bin"; exit 4' \
                2>&1)" || rc=$?
            sed 's/^/test_14 watch: /' <<<"$out" >&2   # the CI log
            [[ "$rc" == 4 ]]
            # The recorder writes every ~10 s: watch must not run out its 60 s wait.
            (( $(date +%s) - t0 < 45 )) || { echo "watch waited $(( $(date +%s) - t0 )) s" >&2; exit 1; }
            IFS=$'\t' read -r _ _ r_exit r_label r_dir < <(sed -n 2p "$base/runs.tsv")
            [[ "$r_exit" == 4 && "$r_label" == e2e-io && -d "$r_dir" ]]
            grep -q '^verdict ' "$r_dir/report.txt"
            if grep -q '^verdict  NO DATA' "$r_dir/report.txt"; then echo "the recorder recorded nothing" >&2; exit 1; fi
            python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
w, c = d["window"], d["coverage"]
# watch waits for a sample at or after the end of the run; the saved window ends just after it.
assert c["last_sample"] is not None and c["last_sample"] >= w["until"] - 2, (w, c)
assert d["samples"] >= 1, d["samples"]
' "$r_dir/report.json"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_15_scrub_speed_limit_is_set_for_the_run_and_reset_after(self) -> None:
        """SCRUB_LIMIT on the loop RAID1 with the real btrfs and systemd:
        `cst btrfs install` writes the drop-in (a second install changes
        nothing), systemd merges it (ExecStartPre sets the limit, ExecStopPost
        resets it) and the timer has RandomizedDelaySec=15min. A test drop-in
        that sorts after CST's copies scrub_speed_max of each device while the
        unit starts (a scrub of the small loop filesystem is too short to
        sample from outside): the limit during the run, 0 after a good run and
        after a failed one. A bad setting makes install stop with nothing
        changed; no limit removes the drop-in, and a run then has none;
        `uninstall` removes the drop-in. The machine is left as test_14 left
        it (uninstalled, snapshots.conf as it was)."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_15 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            show() { sed "s/^/test_15 $1: /" <<<"$2" >&2; }   # the CI log
            uuid="$(cat /run/e2e-loop-uuid)"
            conf=/etc/btrfs-setup/snapshots.conf
            svc=btrfs-scrub-weekly.service
            dropin="/etc/systemd/system/$svc.d/10-cst-limit.conf"
            probe="/run/systemd/system/$svc.d"
            during=/run/e2e-scrub-during
            devmax=("/sys/fs/btrfs/$uuid"/devinfo/*/scrub_speed_max)
            (( ${#devmax[@]} == 2 ))
            rm -f /run/e2e-snapshots.conf.orig
            if [[ -e "$conf" ]]; then cp -a "$conf" /run/e2e-snapshots.conf.orig; fi
            cleanup() {
                rm -rf "$probe" "$during"
                if [[ -e /run/e2e-snapshots.conf.orig ]]; then mv -f /run/e2e-snapshots.conf.orig "$conf"; else rm -f "$conf"; fi
                cst btrfs uninstall >/dev/null 2>&1 || true
                local f
                for f in "${devmax[@]}"; do echo 0 >"$f" 2>/dev/null || true; done
                systemctl daemon-reload
                systemctl reset-failed "$svc" btrfs-health-check.service 2>/dev/null || true
            }
            trap cleanup EXIT
            limits() { tr '\n' ' ' <"$1"; }
            now() { cat "${devmax[@]}" | tr '\n' ' '; }
            set_limit() {   # set_limit LINE: the SCRUB_LIMIT line of snapshots.conf ("" = none)
                mkdir -p "$(dirname "$conf")"
                touch "$conf"
                sed -i '/^SCRUB_LIMIT=/d' "$conf"
                if [[ -n "$1" ]]; then printf '%s\n' "$1" >>"$conf"; fi
            }
            run_scrub() {   # run_scrub WANT: the unit's result
                rm -f "$during"
                systemctl start "$svc" || true
                [[ "$(systemctl show -P Result "$svc")" == "$1" ]] ||
                    { systemctl status --no-pager "$svc" >&2 || true; return 1; }
            }

            # 1. install with a limit, then the merged unit.
            set_limit "SCRUB_LIMIT='8M'"
            out="$(cst btrfs install --no-below 2>&1)" || { show install "$out"; exit 1; }
            grep -qxF "written:   $dropin" <<<"$out"
            [[ "$(systemctl show -P RandomizedDelayUSec btrfs-scrub-weekly.timer)" == 15min ]]
            systemctl show -p ExecStartPre "$svc" | grep -qF 'argv[]=/usr/sbin/btrfs scrub limit -a -l 8M /var'
            systemctl show -p ExecStopPost "$svc" | grep -qF 'argv[]=/usr/sbin/btrfs scrub limit -a -l 0 /var'
            out="$(cst btrfs status 2>&1)" || { show status "$out"; exit 1; }
            grep -qxF '  service: 8M, as set' <<<"$out"

            # 2. A second install writes nothing new.
            out="$(cst btrfs install --no-below 2>&1)" || { show "second install" "$out"; exit 1; }
            grep -qxF "unchanged: $dropin" <<<"$out"

            # 3. The limit during a good run, 0 after it.
            mkdir -p "$probe"
            printf '[Service]\nExecStartPre=/bin/sh -c "cat %s >%s"\n' "${devmax[*]}" "$during" >"$probe/20-e2e-probe.conf"
            systemctl daemon-reload
            run_scrub success
            [[ "$(limits "$during")" == "8388608 8388608 " ]]
            [[ "$(now)" == "0 0 " ]]

            # 4. A failed run resets it too.
            printf '[Service]\nExecStart=\nExecStart=/bin/false\n' >"$probe/30-e2e-fail.conf"
            systemctl daemon-reload
            run_scrub exit-code
            [[ "$(limits "$during")" == "8388608 8388608 " ]]
            [[ "$(now)" == "0 0 " ]]
            rm -f "$probe/30-e2e-fail.conf"
            systemctl daemon-reload
            systemctl reset-failed "$svc"

            # 5. A bad setting: install stops before it changes anything.
            set_limit 'SCRUB_LIMIT=fast'
            before="$(stat -c '%i %Y' "$dropin")"
            rc=0
            out="$(cst btrfs install --no-below 2>&1)" || rc=$?
            show "install, bad setting" "$out"
            [[ "$rc" == 1 ]]
            grep -qF "SCRUB_LIMIT='fast' is not a size" <<<"$out"
            if grep -q '^written:' <<<"$out"; then echo "install wrote a file" >&2; exit 1; fi
            [[ "$(stat -c '%i %Y' "$dropin")" == "$before" ]]
            grep -qF '  setting: INVALID:' <<<"$(cst btrfs status 2>&1)"

            # 6. No limit: the drop-in goes, and a run has none.
            set_limit ''
            out="$(cst btrfs install --no-below 2>&1)" || { show "install, no limit" "$out"; exit 1; }
            grep -qxF "removed:   $dropin" <<<"$out"
            test ! -e "$(dirname "$dropin")"
            if systemctl show -p ExecStartPre "$svc" | grep -qF 'scrub limit'; then echo "a limit is still set" >&2; exit 1; fi
            run_scrub success
            [[ "$(limits "$during")" == "0 0 " ]]

            # 7. uninstall removes the drop-in.
            set_limit 'SCRUB_LIMIT=1G'
            out="$(cst btrfs install --no-below 2>&1)" || { show "install again" "$out"; exit 1; }
            test -f "$dropin"
            out="$(cst btrfs uninstall 2>&1)" || { show uninstall "$out"; exit 1; }
            test ! -e "$(dirname "$dropin")"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_20_vm_tune_plan_apply_second_run_and_restore(self) -> None:
        """`cst vm tune` against the real virtqemud (qemu:///system), on a
        defined Linux guest with a SATA disk and an e1000e network card.
        `plan` prints the seven changes and the diff and changes nothing
        (the inactive definition byte for byte, no backup folder); `status`
        counts the differences. `apply` without a terminal refuses, answered
        no changes nothing; answered yes it saves the old definition (the
        --security-info dump, mode 0600, in a 0700 folder), defines the new
        one (virtio-scsi controller, the disk on it with cache, io, discard
        and detect_zeroes, the network card virtio with the same MAC, the
        CD-ROM left on SATA) and the VNC password survives; libvirt keeps
        the result as it is. The password is never printed. A second plan
        and apply say there is nothing to do, apply asks nothing and saves
        nothing; `status` agrees. Defining the saved file restores the old
        definition byte for byte. VM_TUNE_NIC=no leaves the network card out.
        The libvirt qemu driver needs no /dev/kvm to define a type='qemu'
        guest; no VM is started."""
        w = "/var/tmp/e2e-vm-tune"
        self.addCleanup(
            exec_bash,
            self.PRIV_CONTAINER,
            "virsh -c qemu:///system undefine e2e-tune >/dev/null 2>&1 || true; "
            f"rm -rf {w}; "
            "systemctl stop virtqemud.service virtqemud.socket virtqemud-ro.socket virtqemud-admin.socket || true",
            check=False,
        )
        exec_bash(self.PRIV_CONTAINER, f"rm -rf {w} && mkdir -p {w}")
        for name, text in (("e2e-tune.xml", VM_TUNE_DOMAIN), ("pty-answer.py", PTY_ANSWER_DRIVER)):
            docker_exec(self.PRIV_CONTAINER, ["bash", "-c", f"cat >{w}/{name}"], input_text=text)
        exec_bash(
            self.PRIV_CONTAINER, "systemctl start virtqemud.service && virsh -c qemu:///system list --all >/dev/null"
        )
        out = exec_bash(self.PRIV_CONTAINER, VM_TUNE_SCRIPT).stdout
        self.assertIn("E2E-OK", out)

    def test_21_vm_nocow_plan_apply_second_run(self) -> None:
        """`cst vm nocow` on the loop btrfs, as tester (sudo for the system
        folder, as on the workstation). The system folder is missing (its
        parent exists, as when libvirt is installed); the session folder holds
        a copy-on-write image and a conversion leftover; the RPM Boxes folder
        is empty; the Flatpak one is missing. `plan` lists each folder, the
        image still copy-on-write with the commands to convert it and the
        leftover, and changes nothing. `apply` without a terminal refuses,
        answered no changes nothing; answered yes it creates the system
        folder through sudo (root:root 0711) and sets +C on it and on the two
        user folders; the existing image keeps no +C, files created later get
        it, the missing Flatpak folder is not created. Afterwards plan as
        tester shows the user folders marked and the root-only one not
        readable (never "copy-on-write"); as root `plan --root` has no
        folder to mark and a second `apply` asks nothing and changes
        nothing; a second `apply` as tester sets the mark again only on the
        root-only folder (the documented "set again, harmless").
        VM_NOCOW=no leaves a new folder alone. There is no undo command
        (the undo table of the top-level README says chattr -C by hand)."""
        w = "/var/tmp/e2e-vm-nocow"
        h = "/home/tester"
        self.addCleanup(
            exec_bash,
            self.PRIV_CONTAINER,
            f"rm -rf {w} {h}/.local/share/libvirt {h}/.local/share/gnome-boxes {h}/.var/app/org.gnome.Boxes",
            check=False,
        )
        exec_bash(self.PRIV_CONTAINER, f"rm -rf {w} && mkdir -p {w}")
        docker_exec(self.PRIV_CONTAINER, ["bash", "-c", f"cat >{w}/pty-answer.py"], input_text=PTY_ANSWER_DRIVER)
        out = exec_bash(self.PRIV_CONTAINER, VM_NOCOW_SCRIPT).stdout
        self.assertIn("E2E-OK", out)

    def test_22_btrfs_compress_force_plan_apply_idempotent_and_revert(self) -> None:
        """`cst btrfs compress-force` toggles live kernel mount options on the loopback Btrfs
        RAID1 (`/var` and `/sysroot`), keeps `/etc/fstab` in sync, reports healthy in
        `cst btrfs status` / `cst doctor`, and cleanly reverts."""
        w = "/var/tmp/e2e-btrfs-compress-t2"
        self.addCleanup(
            exec_bash,
            self.PRIV_CONTAINER,
            f"""
            /opt/cst-rw/cst btrfs compress-force revert --yes >/dev/null 2>&1 || true
            if [[ -f {w}/fstab.bak ]]; then
                cp -a {w}/fstab.bak /etc/fstab
            else
                rm -f /etc/fstab
            fi
            rm -rf {w}
            """,
            check=False,
        )
        out = exec_bash(
            self.PRIV_CONTAINER,
            f"""
            set -euo pipefail
            rm -rf {w} && mkdir -p {w}
            [[ ! -f /etc/fstab ]] || cp -a /etc/fstab {w}/fstab.bak
            loop_uuid="$(cat /run/e2e-loop-uuid)"
            printf 'UUID=%s /sysroot btrfs subvol=root,degraded,compress=zstd:1,noatime,discard=async,space_cache=v2,ssd,flushoncommit 0 0\\nUUID=%s /var btrfs subvol=var,degraded,compress=zstd:1,noatime,discard=async,space_cache=v2,ssd,flushoncommit 0 0\\nUUID=%s /var/home btrfs subvol=home,degraded,compress=zstd:1,noatime,discard=async,space_cache=v2,ssd,flushoncommit 0 0\\n' "$loop_uuid" "$loop_uuid" "$loop_uuid" >/etc/fstab

            plan_out="$(/opt/cst-rw/cst btrfs compress-force plan)"
            grep -q 'compress-force=zstd:1' <<<"$plan_out"
            findmnt -rn -o OPTIONS --target /var | grep -q 'compress=zstd:1'

            /opt/cst-rw/cst btrfs compress-force apply --yes >/dev/null
            findmnt -rn -o OPTIONS --target /var | grep -q 'compress-force=zstd:1'
            findmnt -rn -o OPTIONS --target /sysroot | grep -q 'compress-force=zstd:1'
            grep -q 'compress-force=zstd:1' /etc/fstab

            second_out="$(/opt/cst-rw/cst btrfs compress-force apply)"
            grep -q 'nothing to do' <<<"$second_out"

            btrfs_status="$(/opt/cst-rw/cst btrfs status)"
            grep -q 'compress-force=zstd:1' <<<"$btrfs_status"
            grep -q 'coherent (compress-force=zstd:1)' <<<"$btrfs_status"

            doc_out="$(/opt/cst-rw/cst doctor || true)"
            grep -q 'ok     root btrfs compression: coherent (compress-force=zstd:1)' <<<"$doc_out"

            /opt/cst-rw/cst btrfs compress-force revert --yes >/dev/null
            findmnt -rn -o OPTIONS --target /var | grep -q 'compress=zstd:1'
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_23_btrfs_iocost_plan_apply_and_revert_live(self) -> None:
        """`cst btrfs iocost` writes real kernel `/sys/fs/cgroup/io.cost.qos` on a loopback block
        device and cleanly reverts only CST-managed rows without disturbing other entries.

        io.cost.qos exists only in the root cgroup. This container runs systemd in a cgroup namespace of
        its own (other tests read its machine.slice), so where the root is hidden the file is absent and
        the test checks what cst does then: plan warns, apply --live-only skips with exit 0, status says
        absent, revert succeeds. Where the file is there, it checks the real write and revert."""
        self.addCleanup(
            exec_bash,
            self.PRIV_CONTAINER,
            "/opt/cst-rw/cst btrfs iocost revert >/dev/null 2>&1 || true",
            check=False,
        )
        out = exec_bash(
            self.PRIV_CONTAINER,
            """
            set -euo pipefail
            loop_src="$(findmnt -rn -o SOURCE --target /var | sed 's/\\[.*//')"
            loop_name="$(basename "$loop_src")"
            devno="$(cat "/sys/block/$loop_name/dev")"

            plan_out="$(/opt/cst-rw/cst btrfs iocost plan --live-only --device "$loop_name")"
            grep -qF "$devno enable=1 ctrl=auto" <<<"$plan_out"

            if [[ ! -e /sys/fs/cgroup/io.cost.qos ]]; then
                grep -qF "/sys/fs/cgroup/io.cost.qos is absent" <<<"$plan_out"
                apply_out="$(/opt/cst-rw/cst btrfs iocost apply --live-only --device "$loop_name")"
                grep -qF "skipped:   /sys/fs/cgroup/io.cost.qos unavailable" <<<"$apply_out"
                st="$(/opt/cst-rw/cst btrfs iocost status)"
                grep -qF "/sys/fs/cgroup/io.cost.qos absent" <<<"$st"
                /opt/cst-rw/cst btrfs iocost revert --live-only >/dev/null
                echo E2E-IOCOST-T2-OK absent
                exit 0
            fi

            # A save of the managed-devices file that fails (a folder where it belongs: the rename over it
            # fails): apply saves the devices it is about to enable first, so it writes no line at all.
            rm -rf /run/btrfs-setup/iocost-managed
            mkdir -p /run/btrfs-setup/iocost-managed/x
            rc=0; /opt/cst-rw/cst btrfs iocost apply --live-only --device "$loop_name" >/var/tmp/e2e-23-out 2>&1 || rc=$?
            rm -rf /run/btrfs-setup/iocost-managed
            [[ $rc == 1 ]] || { echo "apply with the managed file unwritable: exit $rc" >&2; cat /var/tmp/e2e-23-out >&2; exit 1; }
            grep -qF "ERROR: cannot write /run/btrfs-setup/iocost-managed: " /var/tmp/e2e-23-out
            grep -qF "nothing was written to io.cost.qos" /var/tmp/e2e-23-out
            if grep -q Traceback /var/tmp/e2e-23-out; then cat /var/tmp/e2e-23-out >&2; exit 1; fi
            if grep -qE "^${devno} enable=1" /sys/fs/cgroup/io.cost.qos; then
                echo "io.cost.qos has enable=1 for $devno after an apply whose save failed" >&2
                exit 1
            fi

            /opt/cst-rw/cst btrfs iocost apply --live-only --device "$loop_name" >/dev/null
            grep -qE "^${devno} enable=1" /sys/fs/cgroup/io.cost.qos
            grep -qx "$devno" /run/btrfs-setup/iocost-managed

            st="$(/opt/cst-rw/cst btrfs iocost status)"
            grep -qE "[1-9][0-9]* active device" <<<"$st"

            /opt/cst-rw/cst btrfs iocost revert --live-only >/dev/null
            if grep -qE "^${devno} enable=1" /sys/fs/cgroup/io.cost.qos; then
                echo "io.cost.qos still has enable=1 for $devno after revert" >&2
                exit 1
            fi
            echo E2E-IOCOST-T2-OK live
            """,
        ).stdout
        self.assertIn("E2E-IOCOST-T2-OK", out)
