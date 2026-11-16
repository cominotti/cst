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
            install -m 0755 /opt/cst-rw/btrfs/btrfs-snapshot /usr/local/sbin/btrfs-snapshot
            install -m 0755 /opt/cst-rw/btrfs/btrfs-snapshot-core /usr/local/sbin/btrfs-snapshot-core

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
            install -m 0755 /opt/cst-rw/btrfs/btrfs-snapshot "$RT"
            install -m 0755 /opt/cst-rw/btrfs/btrfs-snapshot-core "$CORE"
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
