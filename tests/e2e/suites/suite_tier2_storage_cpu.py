"""Docker Layer 2 E2E suite: --privileged loopback btrfs RAID1, DinD, cgroups v2, Polkit, virsh."""

import glob
import os
import re
import shlex
import subprocess
import time
import unittest


def docker_exec(container, cmd, *, check=True, user="root", env=None, input_text=None):
    full = ["docker", "exec"]
    if input_text is not None:
        full.append("-i")
    if user != "root":
        full.extend(["--user", user])
    if env:
        for k, v in env.items():
            full.extend(["-e", f"{k}={v}"])
    full.extend([container, *cmd])
    p = subprocess.run(full, input=input_text, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(
            f"command failed (rc={p.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )
    return p


def exec_bash(container, script, *, check=True):
    return docker_exec(container, ["bash", "-euo", "pipefail", "-c", script], check=check)


def as_tester_bash(container, script, *, check=True):
    quoted = shlex.quote(script)
    cmd = (
        "runuser -u tester -- env "
        "HOME=/home/tester USER=tester LOGNAME=tester "
        "XDG_RUNTIME_DIR=/run/user/1000 "
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
        f"bash -euo pipefail -c {quoted}"
    )
    return exec_bash(container, cmd, check=check)


def read_host_guard_snapshot():
    snap = {}
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
    HOST_GUARD_BEFORE = {}

    @classmethod
    def setUpClass(cls):
        cls.HOST_GUARD_BEFORE = read_host_guard_snapshot()
        exec_bash(
            cls.PRIV_CONTAINER,
            "rm -rf /opt/cst-rw && cp -a /opt/cst /opt/cst-rw && chmod -R a+rX /opt/cst-rw",
        )

    @classmethod
    def tearDownClass(cls):
        after = read_host_guard_snapshot()
        if after != cls.HOST_GUARD_BEFORE:
            raise AssertionError(
                f"HOST ISOLATION BREACH! Before={cls.HOST_GUARD_BEFORE}, After={after}"
            )

    def test_01_loop_btrfs_raid1_and_host_shadows_active(self):
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

    def test_02_btrfs_install_stall_monitor_scrub_and_reclaim(self):
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

    def test_03_btrfs_snapshots_convert_hourly_automount_and_nas(self):
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
            opts="$(findmnt -rn -o OPTIONS -M /var | tr ',' '\n' |
                grep -vE '^(rw|ro|seclabel|subvol=.*|subvolid=.*)$' | paste -sd, - || true)"
            opts="subvolid=5${{opts:+,$opts}}"
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

    def test_04_btrfs_bulk_real_subvolume_and_nocow(self):
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

    def test_05_dind_docker_volumes_nocow_builds_slice_and_docker_gc(self):
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

    def test_06_cpu_cgroups_game_profile_watchdog_polkit_and_vm_pin(self):
        """cpu install, cgroup v2 weights/IOWeight, game-profile on/off, Polkit check, vm-pin XML plan, and uninstall."""
        exec_bash(
            self.PRIV_CONTAINER,
            """
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst cpu install
            grep -q 'IOWeight=50' /etc/systemd/system/machine.slice.d/50-cpu-setup.conf
            grep -q 'Delegate=cpuset' /etc/systemd/system/user@.service.d/50-cpu-setup-cpuset.conf
            systemctl is-active cpu-game-watchdog.timer

            # Polkit rule validation (installed for user tester; non-seat invocation must be denied)
            grep -q 'subject.user == "tester"' /etc/polkit-1/rules.d/50-cpu-setup-game.rules
            systemctl start polkit.service
            systemctl is-active polkit.service
            ! runuser -u tester -- systemctl --no-ask-password start cpu-game.service >/dev/null 2>&1

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
            grep -q '10' /sys/fs/cgroup/machine.slice/io.weight
            gp_state="$(/usr/local/sbin/cpu-game-profile state)"
            grep -q 'game profile: ON' <<<"$gp_state"
            /usr/local/sbin/cpu-game-profile off
            grep -qx '50' /sys/fs/cgroup/machine.slice/cpu.weight
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

            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst cpu uninstall
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst btrfs uninstall
            test ! -e /etc/systemd/system/machine.slice.d/50-cpu-setup.conf
            test ! -e /etc/polkit-1/rules.d/50-cpu-setup-game.rules
            test ! -e /etc/tmpfiles.d/btrfs-automatic-reclaim.conf
            ! systemctl is-active -q cpu-game-watchdog.timer
            """,
        )
