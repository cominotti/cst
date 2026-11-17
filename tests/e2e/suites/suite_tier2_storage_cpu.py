# SPDX-License-Identifier: MPL-2.0
"""Docker Layer 2 E2E suite: --privileged loopback btrfs RAID1, DinD, cgroups v2, Polkit, virsh."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import glob
import os
import shlex
import subprocess
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
    if env:
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
    PRIV_CONTAINER = os.environ["CST_E2E_PRIV_CONTAINER"]
    NAS_CONTAINER = os.environ["CST_E2E_NAS_CONTAINER"]
    HOST_GUARD_BEFORE: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.HOST_GUARD_BEFORE = read_host_guard_snapshot()
        exec_bash(
            cls.PRIV_CONTAINER,
            "rm -rf /opt/cst-rw && cp -a /opt/cst /opt/cst-rw && chmod -R a+rX /opt/cst-rw",
        )

    @classmethod
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
            # The installer writes btrfs-snapshot-core as the zipapp a real install makes.
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-snapshot btrfs/btrfs-snapshot-core

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
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-snapshot btrfs/btrfs-snapshot-core
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
            ! systemctl is-active -q cpu-game-watchdog.timer
            """,
        )

    def test_07_btrfs_install_moves_the_f44_tools_names_and_a_rerun_changes_nothing(self) -> None:
        """`cst btrfs install --no-below` on a machine that a version named
        f44-tools set up: its alerts folder (an alert, and one the new folder
        already holds a newer copy of), its reader and global user unit
        (enabled) and below's drop-in move to the new names; the old folder
        becomes a link that the second install removes; a third install writes
        nothing (every file `unchanged:`, the same files before and after);
        `uninstall` leaves none of the old names."""
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
                /etc/systemd/system/btrfs-stall-monitor.service /usr/local/sbin/btrfs-stall-monitord
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
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-snapshot \
                btrfs/btrfs-snapshot-core btrfs/btrfs-notify btrfs/units/btrfs-snapshot.service \
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
        the recorder again."""
        out = exec_bash(
            self.PRIV_CONTAINER,
            r"""
            set -E
            trap 'echo "test_09 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            stub=/run/systemd/system/docker.service
            bin=/usr/local/sbin/docker-gc
            units=(docker-gc-recorder.service docker-gc.service docker-gc.timer)
            stamp=/var/lib/systemd/timers/stamp-docker-gc.timer
            remove_all() {
                systemctl disable --now "${units[@]}" 2>/dev/null || true
                rm -f "$bin" "${units[@]/#//etc/systemd/system/}" "$stamp"
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
            printf '[Unit]\nDescription=E2E stub of docker.service (dockerd runs in e2e-dind.service)\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n' \
                >"$stub"
            systemctl daemon-reload
            [[ "$(systemctl show -P FragmentPath docker.service)" == "$stub" ]]
            # As in test_05a: Fedora's generic service.d drop-ins may apply; one of docker.service may not.
            dropins="$(systemctl show -P DropInPaths docker.service)"
            if tr ' ' '\n' <<<"$dropins" | grep -q '/docker\.service\.d/'; then
                echo "a docker.service drop-in applies to the stub: $dropins" >&2
                exit 1
            fi
            systemctl start e2e-dind.service
            for _ in $(seq 1 30); do
                docker info >/dev/null 2>&1 && break
                sleep 0.5
            done
            docker info >/dev/null
            systemctl start docker.service

            # 1. enable.
            out="$(cst btrfs docker-gc enable 2>&1)" || { show enable "$out"; exit 1; }
            show enable "$out"
            grep -qF 'docker-gc enabled: image use is recorded from now on' <<<"$out"
            for f in "$bin" "${units[@]/#//etc/systemd/system/}"; do
                grep -qxF "written:   $f" <<<"$out" || { echo "no 'written:   $f'" >&2; exit 1; }
            done
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
