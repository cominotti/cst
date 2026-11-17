"""Docker Layer 1 E2E suite: unprivileged systemd PID 1 + user manager + SFTP sidecar."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import shlex
import subprocess
import time
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


def exec_bash(
    container: str,
    script: str,
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return docker_exec(container, ["bash", "-euo", "pipefail", "-c", script], check=check, env=env)


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


# tests/e2e test_13: the fakes `btrfs-degraded-boot-setup.sh` runs against
# (sourced by every step) and the fake deployment, boot entry and overlay.
DEGRADED_BOOT_FAKES = r"""export E2E_W=/var/tmp/e2e-degraded-boot
export E2E_UUID=0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0
export E2E_CSUM=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee

# The fakes are exported bash functions: the setup script resets PATH, and
# nothing in /usr/bin is replaced. rpm-ostree keeps its tracked list in
# $W/tracked and rebuilds the one deployment's overlay (a tar.gz here, read
# back by the cpio fake) after every initramfs-etc call.
rpm-ostree() {
    echo "rpm-ostree $*" >>"$E2E_W/calls"
    local a
    case "$1" in
        kargs) echo "root=UUID=$E2E_UUID rootflags=subvol=root,degraded rd.luks.uuid=luks-11111111-2222-3333-4444-555555555555" ;;
        status)
            python3 -I -c 'import json, sys
tracked = [p for p in open(sys.argv[1]).read().splitlines() if p]
d = {"osname": "e2e", "checksum": sys.argv[2], "serial": 0, "booted": True, "version": "45.e2e", "initramfs-etc": tracked}
print(json.dumps({"deployments": [d]}))' "$E2E_W/tracked" "$E2E_CSUM" ;;
        initramfs-etc)
            shift
            for a in "$@"; do
                case "$a" in
                    --track=*) echo "${a#--track=}" >>"$E2E_W/tracked" ;;
                    --untrack=*) { grep -vxF "${a#--untrack=}" "$E2E_W/tracked" || true; } >"$E2E_W/tracked.new"; mv "$E2E_W/tracked.new" "$E2E_W/tracked" ;;
                    --force-sync) ;;
                    *) return 64 ;;
                esac
            done
            rm -f /boot/ostree/initramfs-overlays/e2e.img
            if [[ -s "$E2E_W/tracked" ]]; then
                sed 's|^/||' "$E2E_W/tracked" | tar -czf /boot/ostree/initramfs-overlays/e2e.img -C / -T -
            fi ;;
        *) return 64 ;;
    esac
}
cpio() { tar -xf -; }
findmnt() {
    case "$*" in
        "-rn -o FSTYPE --target /sysroot") echo btrfs ;;
        "-rn -o UUID --target /sysroot") echo "$E2E_UUID" ;;
        *) command findmnt "$@" ;;
    esac
}
btrfs() {
    [[ "$1 $2" == "filesystem show" ]] || return 64
    printf 'Label: none  uuid: %s\n\tTotal devices 2 FS bytes used 1.00GiB\n' "$E2E_UUID"
}
lsblk() { printf 'e2e0 btrfs %s\ne2e1 btrfs %s\nloop0 ext4 x\n' "$E2E_UUID" "$E2E_UUID"; }
# udevd does not run in the container: only verify is real.
udevadm() {
    echo "udevadm $*" >>"$E2E_W/calls"
    case "$1" in
        verify) command udevadm "$@" ;;
        control) ;;
        test) echo "Reading rules file: /etc/udev/rules.d/65-btrfs-last-resort.rules" ;;
        info) echo ID_BTRFS_READY=1 ;;
        *) return 64 ;;
    esac
}
export -f rpm-ostree cpio findmnt btrfs lsblk udevadm
"""

DEGRADED_BOOT_PREPARE = r""". /var/tmp/e2e-degraded-boot/fakes.bash
D="/ostree/deploy/e2e/deploy/$E2E_CSUM.0"
mkdir -p "$D/usr/lib/udev/rules.d" "/ostree/boot.1/e2e/$E2E_CSUM" /boot/loader/entries /boot/ostree/initramfs-overlays
cp /usr/lib/udev/rules.d/64-btrfs.rules "$D/usr/lib/udev/rules.d/"
ln -sfn "../../../deploy/e2e/deploy/$E2E_CSUM.0" "/ostree/boot.1/e2e/$E2E_CSUM/0"
printf 'title e2e\noptions root=UUID=%s rootflags=subvol=root,degraded ostree=/ostree/boot.1/e2e/%s/0\ninitrd /ostree/e2e/initramfs.img /ostree/initramfs-overlays/e2e.img\n' \
    "$E2E_UUID" "$E2E_CSUM" >/boot/loader/entries/ostree-1-e2e.conf
touch /run/ostree-booted
: >"$E2E_W/tracked"
: >"$E2E_W/calls"
"""


class Tier1SystemdE2ETest(unittest.TestCase):
    SYSTEMD_CONTAINER = os.environ["CST_E2E_SYSTEMD_CONTAINER"]
    NAS_CONTAINER = os.environ["CST_E2E_NAS_CONTAINER"]
    IMAGE = os.environ["CST_E2E_SYSTEMD_IMAGE"]
    CST_ROOT_MOUNT = os.environ["CST_E2E_REPO_ROOT"]

    @classmethod
    def setUpClass(cls) -> None:
        # Provide a working writable copy of CST inside the container so symlink tests
        # and user-owned operations do not rely on mutating the read-only host repo.
        exec_bash(
            cls.SYSTEMD_CONTAINER,
            "rm -rf /opt/cst-rw && cp -a /opt/cst /opt/cst-rw && chmod -R a+rX /opt/cst-rw",
        )

    def test_01_in_container_guardrail_blocks_mutations(self) -> None:
        """When /.dockerenv is present, install/apply commands must refuse cleanly."""
        repo = self.CST_ROOT_MOUNT
        for extra_flags, subcmd in (
            ([], "/opt/cst/cst cpu install"),
            ([], "/opt/cst/cst btrfs install --no-below"),
            ([], "/opt/cst/cst swap apply"),
            (["--user", "tester"], "/opt/cst/cst bluetooth apply --no-restart --yes"),
        ):
            p = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *extra_flags,
                    "-e",
                    "E2E_KEEP_DOCKERENV=1",
                    "-v",
                    f"{repo}:/opt/cst:ro",
                    "--entrypoint",
                    "bash",
                    self.IMAGE,
                    "-c",
                    f"{subcmd}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(p.returncode, 0, f"{subcmd} should refuse inside /.dockerenv container")
            combined = p.stdout + p.stderr
            self.assertRegex(
                combined.lower(),
                r"container|toolbox|host",
                f"expected container refusal message for {subcmd}, got: {combined}",
            )

    def test_02_systemd_and_user_dbus_healthy(self) -> None:
        """systemd PID 1, system dbus-broker and user@1000.service run without --privileged."""
        st = exec_bash(self.SYSTEMD_CONTAINER, "systemctl is-system-running").stdout.strip()
        self.assertEqual(st, "running")
        ust = as_tester_bash(self.SYSTEMD_CONTAINER, "systemctl --user is-system-running").stdout.strip()
        self.assertEqual(ust, "running")
        docker_env_check = exec_bash(
            self.SYSTEMD_CONTAINER,
            "test ! -e /.dockerenv && test ! -e /run/.containerenv && echo OK",
        ).stdout.strip()
        self.assertEqual(docker_env_check, "OK")

    def test_03_cst_root_link_status_and_doctor(self) -> None:
        """cst dispatch, ~/.local/bin/cst symlink, cst status and cst doctor work live."""
        as_tester_bash(
            self.SYSTEMD_CONTAINER,
            """
            mkdir -p /home/tester/.local/bin
            ln -sfn /opt/cst-rw/cst /home/tester/.local/bin/cst
            /home/tester/.local/bin/cst help >/dev/null
            /home/tester/.local/bin/cst doctor --brief >/dev/null
            """,
        )
        # Create a controlled failed user unit, verify doctor detects it, then reset-failed.
        as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "systemd-run --user --unit=e2e-doctor-fail /bin/false || true",
        )
        # Wait briefly for unit failure state to settle
        time.sleep(0.5)
        doc = as_tester_bash(self.SYSTEMD_CONTAINER, "/opt/cst-rw/cst doctor", check=False)
        self.assertIn("e2e-doctor-fail.service", doc.stdout)
        as_tester_bash(self.SYSTEMD_CONTAINER, "systemctl --user reset-failed e2e-doctor-fail.service")
        clean = as_tester_bash(self.SYSTEMD_CONTAINER, "/opt/cst-rw/cst doctor")
        self.assertIn("0 ERROR, 0 WARN", clean.stdout)

        status_res = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "/home/tester/.local/bin/cst status",
            check=False,
        )
        self.assertIn("==== toolchains", status_res.stdout)
        self.assertIn("==== bluetooth", status_res.stdout)
        self.assertIn("==== vm", status_res.stdout)
        self.assertIn("==== doctor", status_res.stdout)
        # Reset any hardwareless bluetooth.service D-Bus activation failure inside the container
        exec_bash(self.SYSTEMD_CONTAINER, "systemctl reset-failed bluetooth.service 2>/dev/null || true")

    def test_04_toolchains_e2e_timer_config_and_prune(self) -> None:
        """toolchains config apply, hourly systemd --user timer, service run, and auto off."""
        # Configure cargo/uv settings & create stale cache under ~/.cache/cargo-build (cst-toolchains layout)
        as_tester_bash(
            self.SYSTEMD_CONTAINER,
            """
            stale_dir="/home/tester/.cache/cargo-build/ab/cdef0123456789/release"
            mkdir -p "${stale_dir}" /home/tester/.cache/uv /home/tester/work/demo
            printf '[package]\nname = "demo"\nversion = "0.1.0"\n' >/home/tester/work/demo/Cargo.toml
            git -C /home/tester/work/demo init -q
            dd if=/dev/zero of="${stale_dir}/artifact.bin" bs=1K count=64 status=none
            find /home/tester/.cache/cargo-build/ab/cdef0123456789 -exec touch -d '40 days ago' {} +
            /opt/cst-rw/cst toolchains config apply --lean-debuginfo
            grep -q 'target-dir\\|build-dir' /home/tester/.cargo/config.toml
            grep -q 'link-mode = "clone"' /home/tester/.config/uv/uv.toml
            # An older CST installed the program as a plain script (it holds
            # CST's marker): auto on replaces it with the zipapp.
            prog=/home/tester/.local/lib/cst/cst-toolchains
            mkdir -p /home/tester/.local/lib/cst
            printf '#!/usr/bin/python3 -I\nMARKER = "# managed-by: toolchains-setup.sh (CST)"\n' >"$prog"
            /opt/cst-rw/cst toolchains auto on
            python3 -I -c 'import sys, zipfile; sys.exit(not zipfile.is_zipfile(sys.argv[1]))' "$prog"
            (cd / && env -i PATH=/usr/bin:/bin HOME=/home/tester /usr/bin/python3 -I "$prog" help) | grep -q 'cst-toolchains:'
            again="$(/opt/cst-rw/cst toolchains auto on)"
            grep -q 'unchanged: ~/.local/lib/cst/cst-toolchains' <<<"$again"
            systemctl --user is-enabled cst-toolchains-prune.timer
            systemctl --user is-active cst-toolchains-prune.timer
            systemctl --user start cst-toolchains-prune.service
            test ! -e /home/tester/.cache/cargo-build/ab/cdef0123456789
            st_out="$(/opt/cst-rw/cst toolchains status --brief)"
            grep -q 'cargo-build\\|toolchains' <<<"$st_out"
            """,
        )
        self.toolchains_auto_off_without_a_user_manager()
        self.toolchains_auto_off()
        self.toolchains_auto_off_stops_an_orphan_timer()
        # Clean up demo workspace for later tests
        as_tester_bash(self.SYSTEMD_CONTAINER, "rm -rf /home/tester/work/demo")

    # What `auto on` installed (test_04 checks them after each `auto off`).
    TOOLCHAINS_INSTALLED = (
        "/home/tester/.config/systemd/user/cst-toolchains-prune.timer "
        "/home/tester/.config/systemd/user/cst-toolchains-prune.service "
        "/home/tester/.local/lib/cst/cst-toolchains "
        "/home/tester/.local/lib/cst/toolchains.conf"
    )

    # Fails unless the user manager says the timer is inactive (also when
    # systemctl cannot ask it: is-active then prints no state).
    TIMER_INACTIVE = (
        'state="$(systemctl --user is-active cst-toolchains-prune.timer || true)"; '
        '[ "$state" = inactive ] || { echo "timer state after auto off: [$state]" >&2; exit 1; }'
    )

    def toolchains_auto_off_without_a_user_manager(self) -> None:
        """auto off when the user manager cannot be reached: exit 1, nothing removed, timer still running."""
        res = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            no_manager="$(mktemp -d)"
            # The same call works with the session's variables, and fails with
            # XDG_RUNTIME_DIR at an empty folder and no DBUS_SESSION_BUS_ADDRESS
            # (no socket of the user manager to talk to).
            systemctl --user show-environment >/dev/null
            if env -u DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR="$no_manager" \\
                systemctl --user show-environment >/dev/null 2>&1; then
                echo "systemctl --user still reached the user manager: the test below would prove nothing" >&2
                exit 1
            fi
            rc=0
            env -u DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR="$no_manager" \\
                /opt/cst-rw/cst toolchains auto off >"$no_manager.out" 2>&1 || rc=$?
            cat "$no_manager.out"
            if [ "$rc" -ne 1 ]; then
                echo "auto off without a user manager: exit $rc, expected 1" >&2
                exit 1
            fi
            grep -q 'disable --now cst-toolchains-prune.timer failed' "$no_manager.out"
            grep -q 'nothing was removed' "$no_manager.out"
            for f in {self.TOOLCHAINS_INSTALLED}; do
                test -e "$f" || {{ echo "removed although auto off refused: $f" >&2; exit 1; }}
            done
            test ! -e /home/tester/.config/cst/toolchains-auto.off
            systemctl --user is-enabled cst-toolchains-prune.timer
            systemctl --user is-active cst-toolchains-prune.timer
            rm -rf "$no_manager" "$no_manager.out"
            """,
        )
        self.assertIn("nothing was removed", res.stdout)

    def toolchains_auto_off(self) -> None:
        """A normal auto off (after the refusal above): the timer stops and the files go."""
        as_tester_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            /opt/cst-rw/cst toolchains auto off
            {self.TIMER_INACTIVE}
            for f in {self.TOOLCHAINS_INSTALLED}; do
                test ! -e "$f" || {{ echo "still there after auto off: $f" >&2; exit 1; }}
            done
            test -e /home/tester/.config/cst/toolchains-auto.off
            """,
        )

    def toolchains_auto_off_stops_an_orphan_timer(self) -> None:
        """What auto off left before it checked `disable`: the timer still running, its files gone, and its
        timers.target.wants link pointing at nothing. is-enabled reads the unit files, so it answers as if
        the timer were off (why the checks here ask is-active). status shows it; a second auto off stops
        it and removes the link."""
        res = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            d=/home/tester/.config/systemd/user
            link="$d/timers.target.wants/cst-toolchains-prune.timer"
            /opt/cst-rw/cst toolchains auto on >/dev/null
            test -L "$link"
            # The removal of the unchecked auto off: no disable, no daemon-reload that worked.
            rm {self.TOOLCHAINS_INSTALLED}
            test -L "$link" && test ! -e "$link"
            en_rc=0
            en="$(systemctl --user is-enabled cst-toolchains-prune.timer 2>&1)" || en_rc=$?
            ac_rc=0
            ac="$(systemctl --user is-active cst-toolchains-prune.timer)" || ac_rc=$?
            echo "is-enabled: [$en] exit $en_rc; is-active: [$ac] exit $ac_rc"
            /opt/cst-rw/cst toolchains status --brief | grep 'still running although its file is gone'
            /opt/cst-rw/cst toolchains auto off
            {self.TIMER_INACTIVE}
            test ! -L "$link" || {{ echo "auto off left $link" >&2; exit 1; }}
            test -e /home/tester/.config/cst/toolchains-auto.off
            # Nothing more on a second run.
            again="$(/opt/cst-rw/cst toolchains auto off)"
            if grep -q 'stopped:\\|removed:' <<<"$again"; then
                echo "a second auto off changed something: $again" >&2
                exit 1
            fi
            """,
        )
        # Seen in the Fedora 45 image: is-enabled says the timer is not enabled (non-zero exit) while
        # is-active says it runs. So `! systemctl --user is-enabled` passed even when the timer kept running.
        self.assertRegex(res.stdout, r"is-enabled: \[[^]]*\] exit [1-9]")
        self.assertIn("is-active: [active] exit 0", res.stdout)
        self.assertIn("stopped:   cst-toolchains-prune.timer", res.stdout)
        self.assertIn("removed:   ~/.config/systemd/user/timers.target.wants/cst-toolchains-prune.timer", res.stdout)

    def test_05_bluetooth_config_wp_roles_and_ldac_unit(self) -> None:
        """bluetooth plan, apply --no-restart, real libwireplumber wp-roles validation, and revert."""
        exec_bash(
            self.SYSTEMD_CONTAINER,
            """
            mkdir -p /etc/bluetooth
            printf '[General]\n# Name = BlueZ\n' >/etc/bluetooth/main.conf
            """,
        )
        as_tester_bash(
            self.SYSTEMD_CONTAINER,
            """
            mkdir -p /home/tester/.config/cst
            printf 'DEVICE=00:11:22:33:44:55\nLDAC_QUALITY=auto\nLDAC_RATE=48000\nLE_AUDIO=off\n' \
                >/home/tester/.config/cst/bluetooth.conf
            """,
        )
        # plan returns 10 when changes are pending
        plan_rc = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "/opt/cst-rw/cst bluetooth plan --device 00:11:22:33:44:55",
            check=False,
        )
        self.assertEqual(
            plan_rc.returncode, 10, f"expected plan rc=10, got {plan_rc.returncode}\n{plan_rc.stdout}\n{plan_rc.stderr}"
        )

        # apply --no-restart --yes writes /etc/bluetooth/main.conf, WirePlumber drop-ins and
        # enables/starts cst-bt-ldac48.service. Live verify() reports missing HCI adapter/pipewire
        # in the unprivileged container (expected exit 1 from verify() after writing all artifacts).
        apply_res = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "/opt/cst-rw/cst bluetooth apply --yes --no-restart --device 00:11:22:33:44:55",
            check=False,
        )
        combined = apply_res.stdout + apply_res.stderr
        self.assertIn("Experimental = true", combined)
        self.assertIn("no Bluetooth adapter in org.bluez", combined)

        main_conf = exec_bash(self.SYSTEMD_CONTAINER, "cat /etc/bluetooth/main.conf").stdout
        self.assertIn("Experimental = true", main_conf)
        self.assertIn("6fbaf188-05e0-496a-9885-d6ddfdb4e03e", main_conf)

        # Real libwireplumber-0.5.so.0 verification of generated WirePlumber drop-ins
        # bt-helper wp-roles emits "<state>|<raw bluez5.roles value>"
        wp_out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "python3 -I /opt/cst-rw/bluetooth/bt-helper wp-roles",
        ).stdout.strip()
        wp_state, _, wp_roles_raw = wp_out.partition("|")
        self.assertEqual(wp_state, "classic", f"unexpected wp-roles output: {wp_out!r}")
        self.assertIn("a2dp_sink", wp_roles_raw)
        self.assertNotIn("bap_sink", wp_roles_raw)

        # Check user systemd unit cst-bt-ldac48.service enabled + active, then clean revert
        as_tester_bash(
            self.SYSTEMD_CONTAINER,
            """
            systemctl --user is-enabled cst-bt-ldac48.service
            systemctl --user is-active cst-bt-ldac48.service
            /opt/cst-rw/cst bluetooth revert --yes --no-restart --device 00:11:22:33:44:55
            test ! -e /home/tester/.config/wireplumber/wireplumber.conf.d/90-cst-le-audio-off.conf
            test ! -e /home/tester/.config/systemd/user/cst-bt-ldac48.service
            """,
        )
        reverted_main = exec_bash(self.SYSTEMD_CONTAINER, "cat /etc/bluetooth/main.conf").stdout
        active_lines = [
            line.strip() for line in reverted_main.splitlines() if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(
            any(
                "6fbaf188-05e0-496a-9885-d6ddfdb4e03e" in line or line.startswith("Experimental")
                for line in active_lines
            ),
            f"unexpected active BlueZ config after revert: {active_lines!r}",
        )
        ldac_enabled = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "systemctl --user is-enabled cst-bt-ldac48.service",
            check=False,
        )
        self.assertNotEqual(ldac_enabled.returncode, 0, "cst-bt-ldac48.service still enabled after revert")

    def test_06_btrfs_notify_pending_alerts_and_sftp_restic_nas(self) -> None:
        """btrfs-notify queue -> cst-pending-alerts delivery + doctor detection + real restic over SFTP."""
        # 1. Queue pending alert, detect via cst doctor, deliver via cst-pending-alerts
        exec_bash(
            self.SYSTEMD_CONTAINER,
            """
            install -d -m 0755 /var/lib/cst-alerts/pending
            /opt/cst-rw/btrfs/btrfs-notify -u critical -a e2e-test "E2E Alert Title" "Simulated disk alert"
            test -n "$(find /var/lib/cst-alerts/pending -name '*.alert')"
            """,
        )
        doc_alert = as_tester_bash(self.SYSTEMD_CONTAINER, "/opt/cst-rw/cst doctor", check=False)
        self.assertIn("E2E Alert Title", doc_alert.stdout)

        # Exercise cst-pending-alerts --list and delivered tracking
        listed = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "bash /opt/cst-rw/btrfs/cst-pending-alerts --list",
        ).stdout
        self.assertIn("E2E Alert Title", listed)

        # 2. Configure SSH client + restic password to back up real payload to cst-e2e-nas via SFTP
        pub = exec_bash(
            self.SYSTEMD_CONTAINER,
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
            mkdir -p /root/.ssh /srv/backup/e2e-repo && chmod 0700 /root/.ssh
            printf '%s\n' {shlex.quote(pub)} >/root/.ssh/authorized_keys
            chmod 0600 /root/.ssh/authorized_keys
            """,
        )
        exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
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
            install -d -m 0755 /etc/btrfs-setup /tmp/nas-payload
            printf 'e2e-secret-pass' >/etc/btrfs-setup/restic-password
            chmod 0600 /etc/btrfs-setup/restic-password
            echo 'hello-from-cst-e2e' >/tmp/nas-payload/probe.txt
            sftp -q -b - btrfs-setup-nas <<<'pwd' >/dev/null
            RESTIC_REPOSITORY='sftp:btrfs-setup-nas:/srv/backup/e2e-repo' \
            RESTIC_PASSWORD_FILE=/etc/btrfs-setup/restic-password \
                restic init
            RESTIC_REPOSITORY='sftp:btrfs-setup-nas:/srv/backup/e2e-repo' \
            RESTIC_PASSWORD_FILE=/etc/btrfs-setup/restic-password \
                restic backup /tmp/nas-payload
            RESTIC_REPOSITORY='sftp:btrfs-setup-nas:/srv/backup/e2e-repo' \
            RESTIC_PASSWORD_FILE=/etc/btrfs-setup/restic-password \
                restic snapshots --json | grep -q '/tmp/nas-payload'
            """,
        )

    def test_07_cpu_baseline_dropin_loses_to_runtime_overrides_after_reload(self) -> None:
        """systemd's real drop-in order: a `set-property --runtime` value (the game
        profile) must survive a daemon-reload next to CST's baseline drop-in,
        which only works because its name sorts before 50-<Property>.conf. And a
        uresourced-style /etc/systemd/system.control/<unit>.d/50-MemoryLow.conf
        hides the runtime MemoryLow at a reload, but not the game profile's
        zz- drop-in in /run/systemd/system, which sorts after it."""
        exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            src="$(ls /opt/cst-rw/cpu/units/machine.slice.d-*-cpu-setup.conf)"
            name="${src##*/machine.slice.d-}"
            d=/etc/systemd/system/e2e-order.slice.d
            printf '[Unit]\nDescription=CST E2E drop-in order\n' >/etc/systemd/system/e2e-order.slice
            mkdir -p "$d"
            cp "$src" "$d/$name"
            systemctl daemon-reload
            systemctl start e2e-order.slice
            [[ "$(systemctl show -P CPUWeight e2e-order.slice)" == 50 ]]
            systemctl set-property --runtime e2e-order.slice CPUWeight=10 IOWeight=10
            systemctl daemon-reload
            [[ "$(systemctl show -P CPUWeight e2e-order.slice)" == 10 ]]
            [[ "$(systemctl show -P IOWeight e2e-order.slice)" == 10 ]]
            # The old name, 50-cpu-setup.conf, sorts after 50-CPUWeight.conf and wins
            # after a reload: the bug the rename fixed.
            mv "$d/$name" "$d/50-cpu-setup.conf"
            systemctl daemon-reload
            [[ "$(systemctl show -P CPUWeight e2e-order.slice)" == 50 ]]
            # What uresourced does: a same-named file in /etc/systemd/system.control
            # hides the runtime one after a reload, so only the watchdog's drift
            # repair (cpu/game-profile tick) can put MemoryLow back.
            mkdir -p /etc/systemd/system.control/e2e-order.slice.d
            printf '[Slice]\nMemoryLow=0\n' >/etc/systemd/system.control/e2e-order.slice.d/50-MemoryLow.conf
            systemctl set-property --runtime e2e-order.slice MemoryLow=16G
            [[ "$(systemctl show -P MemoryLow e2e-order.slice)" == 17179869184 ]]
            systemctl daemon-reload
            [[ "$(systemctl show -P MemoryLow e2e-order.slice)" == 0 ]]
            # The game profile's answer: a drop-in with another name, which sorts
            # after 50-MemoryLow.conf, is applied after it whatever its folder.
            zz="$(sed -n 's/^GAME_DROPIN=//p' /opt/cst-rw/cpu/game-profile)"
            [[ "$zz" == zz-* ]]
            mkdir -p /run/systemd/system/e2e-order.slice.d
            printf '[Slice]\nMemoryLow=16G\n' >"/run/systemd/system/e2e-order.slice.d/$zz"
            systemctl daemon-reload
            [[ "$(systemctl show -P MemoryLow e2e-order.slice)" == 17179869184 ]]
            rm -f "/run/systemd/system/e2e-order.slice.d/$zz"
            systemctl daemon-reload
            [[ "$(systemctl show -P MemoryLow e2e-order.slice)" == 0 ]]
            systemctl stop e2e-order.slice
            rm -rf /etc/systemd/system/e2e-order.slice "$d" /run/systemd/system.control/e2e-order.slice.d \
                /etc/systemd/system.control/e2e-order.slice.d /run/systemd/system/e2e-order.slice.d
            systemctl daemon-reload
            """,
        )

    def test_08_cpu_game_profile_idles_escaped_user_boxes_and_pins_memory_low(self) -> None:
        """The real game profile (cpu/game-profile on/tick/off) with the real
        system and user managers: a top-level user slice of its own and a
        systemd-run job straight in app.slice get CPUWeight=idle; the slice
        keeps it when it empties and comes back; a tick adds nothing more to
        game.state; the zz- MemoryLow drop-in beats a uresourced-style file at
        a daemon-reload; off removes the overrides and the drop-ins, also those
        systemd keeps in its transient folder (a Docker-style scope) and that of
        a slice already gone, which then comes back at its normal weight; with
        the profile off, a tick removes a zz- drop-in left behind. The real
        cst doctor flags the escaped slice and job and the leftover drop-in,
        and leaves a user-<x>.slice alone."""
        ucg = "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
        try:
            exec_bash(
                self.SYSTEMD_CONTAINER,
                r"""
                [[ ! -e /etc/cpu-setup/cpu-setup.conf ]] || cp -a /etc/cpu-setup/cpu-setup.conf /tmp/e2e-cpu-conf.bak
                install -d /etc/cpu-setup
                printf '%s\n' "TARGET_USER='tester'" "BUILD_CPUS='0'" "P_CORES='0'" "VM_WEIGHT=10" \
                    "GAME_MEMORY_LOW='16G'" "GAME_ZSWAP_WRITEBACK='yes'" >/etc/cpu-setup/cpu-setup.conf
                # What uresourced keeps for the session's chain.
                mkdir -p /etc/systemd/system.control/user-1000.slice.d
                printf '[Slice]\nMemoryLow=0\n' >/etc/systemd/system.control/user-1000.slice.d/50-MemoryLow.conf
                systemctl daemon-reload
                [[ "$(systemctl show -P MemoryLow user-1000.slice)" == 0 ]]
                # A Docker container outside builds.slice, as a scope of the system manager.
                setsid nohup systemd-run --quiet --scope --unit=docker-e2eprobe.scope sleep 600 >/dev/null 2>&1 &
                for _ in $(seq 50); do systemctl -q is-active docker-e2eprobe.scope && break; sleep 0.1; done
                systemctl -q is-active docker-e2eprobe.scope
                """,
            )
            as_tester_bash(
                self.SYSTEMD_CONTAINER,
                """
                systemd-run --user --quiet --slice=e2eprobe.slice --unit=e2e-probe-job sleep 600
                systemd-run --user --quiet --unit=run-e2eprobe.service sleep 600
                # Named user-<x>.slice, it nests under user.slice: nothing to flag.
                systemd-run --user --quiet --slice=user-e2eok.slice --unit=e2e-ok-job sleep 600
                """,
            )
            out = exec_bash(
                self.SYSTEMD_CONTAINER,
                rf"""
                trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
                ucg={ucg}
                u() {{ systemctl --user -M tester@ "$@"; }}
                gp() {{ bash /opt/cst-rw/cpu/game-profile "$@"; }}
                ut() {{ runuser -u tester -- env HOME=/home/tester USER=tester LOGNAME=tester \
                    XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"; }}
                doc() {{ ut /opt/cst-rw/cst doctor 2>&1 || true; }}   # exits non-zero on a WARN
                test -d "$ucg/e2eprobe.slice" && test -d "$ucg/app.slice/run-e2eprobe.service"
                test -d "$ucg/user.slice/user-e2eok.slice"
                # The doctor names the escaped boxes the real systemd made, and not user-<x>.slice.
                d="$(doc)"
                grep -qE 'WARN +user manager: e2eprobe\.slice is a slice of its own' <<<"$d"
                grep -qE 'info +user manager: [0-9]+ systemd-run job\(s\) run straight in app\.slice.*run-e2eprobe\.service' <<<"$d"
                if grep -q 'user-e2eok' <<<"$d"; then echo "doctor flagged user-e2eok.slice" >&2; exit 1; fi
                install -d -m 0755 /run/cpu-setup
                touch /run/cpu-setup/manual      # no GameMode client here: tick must not switch off
                gp on
                test -e /run/cpu-setup/game.state
                [[ "$(systemctl show -P AllowedCPUs docker-e2eprobe.scope)" == 0 ]]
                dctl=/run/systemd/transient/docker-e2eprobe.scope.d/50-AllowedCPUs.conf
                test -e "$dctl"
                [[ "$(u show -P CPUWeight e2eprobe.slice)" == idle ]]
                [[ "$(u show -P CPUWeight run-e2eprobe.service)" == idle ]]
                [[ "$(u show -P CPUWeight app.slice)" != idle ]]
                [[ "$(u show -P CPUWeight session.slice)" != idle ]]
                if [[ -e "$ucg/e2eprobe.slice/cpu.idle" ]]; then [[ "$(cat "$ucg/e2eprobe.slice/cpu.idle")" == 1 ]]; fi
                ctl=/run/user/1000/systemd/user.control/e2eprobe.slice.d/50-CPUWeight.conf
                test -e "$ctl"
                # A transient unit's override goes to the transient folder.
                tctl=/run/user/1000/systemd/transient/run-e2eprobe.service.d/50-CPUWeight.conf
                test -e "$tctl"
                grep -q 'e2eprobe.slice CPUWeight idle idle' /run/cpu-setup/game.intended
                if grep -q run-e2eprobe /run/cpu-setup/game.intended; then exit 1; fi
                # The toolboxes' user.slice is a known box: its children are left alone.
                [[ "$(u show -P CPUWeight user-e2eok.slice)" != idle ]]
                # The MemoryLow chain holds across a reload despite the uresourced-style file.
                test -e /run/systemd/system/user-1000.slice.d/zz-cst-game.conf
                grep -qx '\[Service\]' /run/systemd/system/user@1000.service.d/zz-cst-game.conf
                systemctl daemon-reload
                for unit in user.slice user-1000.slice user@1000.service; do   # [Slice] and [Service]
                    [[ "$(systemctl show -P MemoryLow "$unit")" == 17179869184 ]]
                done
                # The slice empties and goes away (stopped here, as systemd may
                # do with a slice nothing uses), and the next job makes it again.
                u stop e2e-probe-job.service
                u stop e2eprobe.slice
                for _ in $(seq 50); do [[ -d "$ucg/e2eprobe.slice" ]] || break; sleep 0.1; done
                test ! -d "$ucg/e2eprobe.slice"
                ut systemd-run --user --quiet --slice=e2eprobe.slice --unit=e2e-probe-job2 sleep 600
                [[ "$(u show -P CPUWeight e2eprobe.slice)" == idle ]]
                if [[ -e "$ucg/e2eprobe.slice/cpu.idle" ]]; then [[ "$(cat "$ucg/e2eprobe.slice/cpu.idle")" == 1 ]]; fi
                # tick skips what game.state already has.
                gp tick
                n1="$(wc -l </run/cpu-setup/game.state)"
                gp tick
                n2="$(wc -l </run/cpu-setup/game.state)"
                [[ "$n1" == "$n2" ]]
                grep -q 'other boxes .*e2eprobe.slice' <<<"$(gp state)"   # no pipe: grep -q would cut it
                # The slice is gone again when the game ends: off must still drop its override.
                u stop e2e-probe-job2.service
                u stop e2eprobe.slice
                for _ in $(seq 50); do [[ -d "$ucg/e2eprobe.slice" ]] || break; sleep 0.1; done
                test ! -d "$ucg/e2eprobe.slice"
                gp off
                test ! -e /run/cpu-setup/game.state
                test ! -e "$ctl"
                test ! -e "$tctl"
                test ! -e "$dctl"
                [[ -z "$(systemctl show -P AllowedCPUs docker-e2eprobe.scope)" ]]
                # Made again after the game, the slice runs at its normal weight.
                ut systemd-run --user --quiet --slice=e2eprobe.slice --unit=e2e-probe-job3 sleep 600
                [[ "$(u show -P CPUWeight e2eprobe.slice)" != idle ]]
                if [[ -e "$ucg/e2eprobe.slice/cpu.idle" ]]; then [[ "$(cat "$ucg/e2eprobe.slice/cpu.idle")" == 0 ]]; fi
                [[ "$(u show -P CPUWeight run-e2eprobe.service)" != idle ]]
                test -z "$(ls /run/systemd/system/*.d/zz-cst-game.conf 2>/dev/null)"
                # Without the zz- file the uresourced-style one wins again.
                [[ "$(systemctl show -P MemoryLow user-1000.slice)" == 0 ]]
                # A drop-in left behind (a crash, no game.state): the watchdog
                # removes it and reloads, so its protection goes too.
                mkdir -p /run/systemd/system/user-1000.slice.d
                printf '[Slice]\nMemoryLow=16G\n' >/run/systemd/system/user-1000.slice.d/zz-cst-game.conf
                systemctl daemon-reload
                [[ "$(systemctl show -P MemoryLow user-1000.slice)" == 17179869184 ]]
                grep -qE 'WARN +game profile: off, but its MemoryLow drop-ins are still there: /run/systemd/system/user-1000\.slice\.d/zz-cst-game\.conf' <<<"$(doc)"
                gp tick
                test ! -e /run/systemd/system/user-1000.slice.d/zz-cst-game.conf
                [[ "$(systemctl show -P MemoryLow user-1000.slice)" == 0 ]]
                if grep -q 'MemoryLow drop-ins are still there' <<<"$(doc)"; then exit 1; fi
                echo E2E-OK
                """,
            ).stdout
            self.assertIn("E2E-OK", out)
        finally:
            exec_bash(
                self.SYSTEMD_CONTAINER,
                """
                u() { systemctl --user -M tester@ "$@"; }
                for j in e2e-probe-job.service e2e-probe-job2.service e2e-probe-job3.service run-e2eprobe.service \
                        e2eprobe.slice e2e-ok-job.service user-e2eok.slice; do
                    u stop "$j" 2>/dev/null || true
                done
                systemctl stop docker-e2eprobe.scope 2>/dev/null || true
                bash /opt/cst-rw/cpu/game-profile off || true
                rm -rf /run/cpu-setup /etc/systemd/system.control/user-1000.slice.d
                rm -f /etc/cpu-setup/cpu-setup.conf
                if [[ -e /tmp/e2e-cpu-conf.bak ]]; then mv /tmp/e2e-cpu-conf.bak /etc/cpu-setup/cpu-setup.conf; fi
                rmdir /etc/cpu-setup 2>/dev/null || true
                systemctl daemon-reload
                """,
                check=False,
            )

    def test_09_sccache_wrapper_keeps_local_crates_out_when_asked(self) -> None:
        """The real rustc wrapper, installed by `cst toolchains config apply
        --sccache`, under real cargo (offline: no network in the container), with
        a fake sccache that logs each crate it is handed. A workspace member and
        a path dependency get no --cap-lints from cargo; a git dependency (a
        local file:// repository) and a vendored one (a [source] directory that
        replaces crates.io) do, also under -vv (--cap-lints warn). What a fake
        cannot prove: that this is cargo's real rule. CARGO_INCREMENTAL=0,
        because cargo compiles local crates of a dev build incrementally, and
        those skip sccache anyway. sccache runs with TMPDIR=/tmp under a
        caller's own TMPDIR. Also the setting's cycle: plan changes
        nothing, apply writes it in, a second apply keeps it, a wrapper without
        the setting (the previous version) is rewritten, --no-sccache undoes."""
        self.addCleanup(
            as_tester_bash,
            self.SYSTEMD_CONTAINER,
            """
            /opt/cst-rw/cst toolchains config apply --no-sccache >/dev/null 2>&1 || true
            rm -rf /home/tester/work/knob /home/tester/.config/cst/toolchains.conf /home/tester/.cargo/git
            """,
            check=False,
        )
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            cst=/opt/cst-rw/cst
            w=/home/tester/work/knob
            wrapper=/home/tester/.local/libexec/cst/rustc-wrapper
            conf=/home/tester/.config/cst/toolchains.conf
            rm -rf "$w" "$conf"
            mkdir -p "$w/bin" "$w/pathdep/src" "$w/gitdep/src" "$w/ws/app/src" "$w/ws/.cargo" \
                "$w/ws/vendor/vendep/src" /home/tester/.config/cst
            # A fake sccache: log the crate name and its TMPDIR, then run the compiler.
            printf '%s\n' '#!/bin/sh' 'echo "${TMPDIR-unset}" >>"$SCCACHE_E2E_LOG.tmpdir"' 'prev=' \
                'for a in "$@"; do' \
                '    [ "$prev" = --crate-name ] && echo "$a" >>"$SCCACHE_E2E_LOG"' '    prev=$a' 'done' \
                'exec "$@"' >"$w/bin/sccache"
            chmod 755 "$w/bin/sccache"
            export PATH="$w/bin:$PATH"
            [ "$(command -v sccache)" = "$w/bin/sccache" ]

            pkg() { printf '[package]\nname = "%s"\nversion = "0.1.0"\nedition = "2021"\n' "$1"; }
            pkg pathdep >"$w/pathdep/Cargo.toml"
            echo 'pub fn p() -> u32 { 1 }' >"$w/pathdep/src/lib.rs"
            pkg gitdep >"$w/gitdep/Cargo.toml"
            echo 'pub fn g() -> u32 { 2 }' >"$w/gitdep/src/lib.rs"
            git -C "$w/gitdep" init -q
            git -C "$w/gitdep" add -A
            git -C "$w/gitdep" -c user.name=e2e -c user.email=e2e@e2e commit -q -m init
            pkg vendep >"$w/ws/vendor/vendep/Cargo.toml"
            echo 'pub fn v() -> u32 { 3 }' >"$w/ws/vendor/vendep/src/lib.rs"
            echo '{"files":{},"package":null}' >"$w/ws/vendor/vendep/.cargo-checksum.json"
            printf '[source.crates-io]\nreplace-with = "vendored"\n\n[source.vendored]\ndirectory = "vendor"\n' \
                >"$w/ws/.cargo/config.toml"
            printf '[workspace]\nmembers = ["app"]\nresolver = "2"\n' >"$w/ws/Cargo.toml"
            { pkg app; printf '\n[dependencies]\npathdep = { path = "../../pathdep" }\n'
              printf 'gitdep = { git = "file://%s/gitdep" }\nvendep = "0.1.0"\n' "$w"; } >"$w/ws/app/Cargo.toml"
            echo 'pub fn all() -> u32 { pathdep::p() + gitdep::g() + vendep::v() }' >"$w/ws/app/src/lib.rs"

            # expect LABEL WANTED [VAR=value...] CARGO-ARGS...: a build from scratch
            # (its own target and build folders), then compare the crates sccache
            # was handed (cargo's ___ probe left out) with WANTED.
            expect() {
                local label=$1 want=$2 got
                shift 2
                : >"$w/log-$label"
                if ! (cd "$w/ws" && env SCCACHE_E2E_LOG="$w/log-$label" CARGO_INCREMENTAL=0 \
                        CARGO_TARGET_DIR="$w/t-$label" CARGO_BUILD_BUILD_DIR="$w/b-$label" "$@" \
                        >"$w/cargo-$label.out" 2>&1); then
                    cat "$w/cargo-$label.out" >&2
                    exit 1
                fi
                got=$({ grep -v '^___$' "$w/log-$label" || true; } | LC_ALL=C sort -u | tr '\n' ' ')
                [ "$got" = "$want" ] || { echo "e2e: $label: sccache was handed '$got', expected '$want'" >&2; exit 1; }
            }
            all="app gitdep pathdep vendep "
            deps="gitdep vendep "

            # Default (RUST_SCCACHE_WORKSPACE=yes): every crate goes through sccache.
            "$cst" toolchains config apply --sccache >/dev/null
            grep -q '^workspace=yes # cst: RUST_SCCACHE_WORKSPACE' "$wrapper"
            grep -q "^rustc-wrapper = \"$wrapper\"" /home/tester/.cargo/config.toml
            expect default "$all" cargo build
            # sccache always runs with TMPDIR=/tmp, whatever the caller's: a server
            # it starts keeps that TMPDIR, and a caller's private one may be deleted.
            mkdir -p "$w/caller-tmp"
            expect tmpdir "$all" TMPDIR="$w/caller-tmp" cargo build
            got=$(LC_ALL=C sort -u "$w/log-tmpdir.tmpdir" | tr '\n' ' ')
            [ "$got" = "/tmp " ] || { echo "e2e: sccache ran with TMPDIR '$got', expected '/tmp '" >&2; exit 1; }
            # For one build: only the git and vendored dependencies.
            expect env0 "$deps" CST_SCCACHE_WORKSPACE=0 cargo build

            # The setting: plan shows the wrapper outdated and changes nothing.
            printf 'RUST_SCCACHE=yes\nRUST_SCCACHE_WORKSPACE=no\n' >"$conf"
            cp "$wrapper" "$w/wrapper.before"
            plan=$("$cst" toolchains config plan)
            grep -q 'sccache wrapper (outdated; cache size 25G; local crates straight to rustc)' <<<"$plan"
            cmp -s "$wrapper" "$w/wrapper.before"
            "$cst" toolchains config apply >/dev/null
            grep -q '^workspace=no # cst: RUST_SCCACHE_WORKSPACE' "$wrapper"
            st=$("$cst" toolchains status)
            grep -q 'sccache wrapper up to date (cache size 25G; local crates straight to rustc)' <<<"$st"
            # A second apply changes nothing.
            cp "$wrapper" "$w/wrapper.applied"
            again=$("$cst" toolchains config apply)
            grep -q "kept:      $wrapper (sccache wrapper)" <<<"$again"
            cmp -s "$wrapper" "$w/wrapper.applied"

            expect setting-no "$deps" cargo build
            expect release "$deps" env -u CARGO_INCREMENTAL cargo build --release
            # -vv: cargo passes --cap-lints warn instead of allow; dependencies still go through.
            expect vv "$deps" cargo build -vv
            grep -q -- '--cap-lints warn' "$w/cargo-vv.out"
            if grep -q -- '--cap-lints allow' "$w/cargo-vv.out"; then exit 1; fi
            # The one-build override wins over the written no.
            expect env1 "$all" CST_SCCACHE_WORKSPACE=1 cargo build

            # Upgrade: the previous version's wrapper had no workspace line.
            sed -i '/^workspace=.*# cst: RUST_SCCACHE_WORKSPACE/d' "$wrapper"
            if grep -q '^workspace=' "$wrapper"; then exit 1; fi
            plan=$("$cst" toolchains config plan)
            grep -q 'sccache wrapper (outdated; cache size 25G; local crates straight to rustc)' <<<"$plan"
            "$cst" toolchains config apply >/dev/null
            cmp -s "$wrapper" "$w/wrapper.applied"

            # Undo: --no-sccache removes CST's rustc-wrapper line; cargo runs rustc directly.
            rm -f "$conf"
            "$cst" toolchains config apply --no-sccache >/dev/null
            if grep -q '^rustc-wrapper' /home/tester/.cargo/config.toml; then exit 1; fi
            expect undone "" cargo build
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_10_cpu_quiet_reads_the_real_proc_files(self) -> None:
        """`cst cpu quiet` against the real kernel files (/proc/stat, /proc/vmstat,
        /proc/pressure/*) and the real game profile flag: a plain run exits 0 or
        1 with one line per test and no crash; busy loops on every CPU must fail
        the CPU test with its value beside its limit; /run/cpu-setup/game.state
        fails the game profile test; a usage error exits 2. Here the container
        markers are gone (host simulation), so `cst cpu quiet` runs the
        checkout's cpu-watch directly, as on the host; the toolbox path through
        flatpak-spawn is a unit test."""
        made = exec_bash(
            self.SYSTEMD_CONTAINER,
            "if [ -d /run/cpu-setup ]; then echo kept; else mkdir /run/cpu-setup && echo made; fi",
        ).stdout.strip()
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            "rm -f /run/cpu-setup/game.state" + ("; rmdir /run/cpu-setup" if made == "made" else ""),
            check=False,
        )
        tester_env = r"""
            set -E
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            cst=/opt/cst-rw/cst
            w=$(mktemp -d)
            pids=()
            trap 'kill "${pids[@]}" 2>/dev/null || true; rm -rf "$w"' EXIT
            busy_loops() {
                local i
                for i in $(seq "$(nproc)"); do
                    timeout 60 sh -c 'while :; do :; done' &
                    pids+=("$!")
                done
                sleep 1
            }
            stop_loops() {
                kill "${pids[@]}" 2>/dev/null || true
                wait "${pids[@]}" 2>/dev/null || true
                pids=()
            }
        """
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            tester_env
            + r"""
            # 1. A plain run: 0 or 1, every test on its own line, no traceback.
            rc=0
            "$cst" cpu quiet --for 2 >"$w/plain" 2>"$w/plain.err" || rc=$?
            [ "$rc" = 0 ] || [ "$rc" = 1 ]
            for label in "CPU busy" "CPU pressure" "IO pressure" "memory pressure" "disk writes" "game profile"; do
                grep -q "^  $label " "$w/plain"
            done
            grep -Eq '^  CPU busy +[0-9]+\.[0-9]{2} % +limit < 10 % +(ok|FAIL)$' "$w/plain"
            grep -Eq '^  disk writes +[0-9]+\.[0-9]{2} MB/s +limit < 10 MB/s +(ok|FAIL)$' "$w/plain"
            grep -Eq '^(quiet|NOT quiet: .+) \(2 s, ' "$w/plain"
            if grep -q Traceback "$w/plain.err"; then cat "$w/plain.err" >&2; exit 1; fi
            # --json straight from cpu-watch: the exit code matches "quiet".
            rc=0
            python3 -I /opt/cst-rw/cpu/cpu-watch quiet --for 1 --json >"$w/json" || rc=$?
            names='["cpu", "cpu-pressure", "io-pressure", "memory-pressure", "writes", "game-profile"]'
            python3 -I -c 'import json, sys; r = json.load(open(sys.argv[1])); assert r["quiet"] == (sys.argv[2] == "0"), r; assert [t["name"] for t in r["tests"]] == json.loads(sys.argv[3]), r' \
                "$w/json" "$rc" "$names"

            # 2. Busy loops on every CPU: the CPU test fails, with value and limit.
            busy_loops
            rc=0
            "$cst" cpu quiet --for 2 >"$w/busy" || rc=$?
            stop_loops
            cat "$w/busy"
            [ "$rc" = 1 ]
            grep -Eq '^  CPU busy +[0-9]+\.[0-9]{2} % +limit < 10 % +FAIL$' "$w/busy"
            grep -Eq '^NOT quiet: CPU busy[ ,]' "$w/busy"

            # 3. A usage error: exit 2, nothing sampled.
            rc=0
            "$cst" cpu quiet --for 5 --wait 3 2>"$w/usage" || rc=$?
            [ "$rc" = 2 ]
            grep -q 'quiet: --wait (3 s) is shorter than the window' "$w/usage"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)
        self.assertRegex(out, r"(?m)^  CPU busy +\d+\.\d\d % +limit < 10 % +FAIL$")

        # 4. The game profile's flag (written by root, as cpu-game-profile does).
        exec_bash(self.SYSTEMD_CONTAINER, "touch /run/cpu-setup/game.state")
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            tester_env
            + r"""
            rc=0
            "$cst" cpu quiet --for 2 --cpu 100 >"$w/game" || rc=$?
            cat "$w/game"
            [ "$rc" = 1 ]
            grep -Eq '^  game profile +on in 3 of 3 readings +limit off +FAIL$' "$w/game"
            grep -Eq '^NOT quiet: (.+, )?game profile \(' "$w/game"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_11_manifest_installs_and_every_entry_runs_from_root(self) -> None:
        """The real installer writes the whole INSTALL_MANIFEST (staged under
        /var/tmp/e2e-dist, so the other tests see no change): root files as
        root, the user's files as tester through runuser (what cst_install_as
        does). A second run changes nothing. Then every python-entry runs its
        help mode from `/` on /usr/bin/python3 -I, as root and as tester, with
        no ImportError: the zipapps hold everything they import."""
        stage = "/var/tmp/e2e-dist"
        self.addCleanup(exec_bash, self.SYSTEMD_CONTAINER, f"rm -rf {stage}", check=False)
        installer = "/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
        sets = "--set UUID=00000000-0000-0000-0000-000000000000 --set OPTIONS=subvolid=5 --set USER=tester"
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            rm -rf {stage} && install -d -m 0755 {stage} {stage}/home && install -d -o tester {stage}/home/tester
            {installer} install --root {stage} --owner root {sets}
            runuser -u tester -- {installer} install --root {stage} --home /home/tester --owner user
            # A --set no template of the call uses is refused (exit 2), and nothing is written: a user
            # file removed first is still missing afterwards.
            u1="{stage}$(runuser -u tester -- {installer} dests --owner user --home /home/tester | head -n 1)"
            rm -f "$u1"
            rc=0; runuser -u tester -- {installer} install --root {stage} --home /home/tester --owner user {sets} || rc=$?
            [[ "$rc" == 2 ]] || {{ echo "an unused --set was not refused with exit 2 (exit $rc)" >&2; exit 1; }}
            [[ ! -e "$u1" ]] || {{ echo "a refused install wrote $u1" >&2; exit 1; }}
            runuser -u tester -- {installer} install --root {stage} --home /home/tester --owner user >/dev/null
            again="$({installer} install --root {stage} --owner root {sets})"
            if grep -v '^unchanged: ' <<<"$again"; then echo "second root run changed files" >&2; exit 1; fi
            ugain="$(runuser -u tester -- {installer} install --root {stage} --home /home/tester --owner user)"
            if grep -v '^unchanged: ' <<<"$ugain"; then echo "second user run changed files" >&2; exit 1; fi
            # A dotfiles-style link to the same content stays a link and counts as unchanged.
            d="{stage}$(runuser -u tester -- {installer} dests --owner user --home /home/tester | head -n 1)"
            runuser -u tester -- cp -p "$d" /home/tester/.e2e-link-target
            runuser -u tester -- ln -sfn /home/tester/.e2e-link-target "$d"
            lgain="$(runuser -u tester -- {installer} install --root {stage} --home /home/tester --owner user)"
            if grep -v '^unchanged: ' <<<"$lgain"; then echo "a link to the same content was rewritten" >&2; exit 1; fi
            [[ -L "$d" ]] || {{ echo "the link was replaced" >&2; exit 1; }}
            rm -f "$d" /home/tester/.e2e-link-target
            # At a root destination a link is replaced, even one to the same content; and as root
            # a root file owned by a user (a hand copy) is rewritten, owned by root again.
            r="{stage}/usr/local/sbin/btrfs-notify"
            cp -p "$r" /var/tmp/e2e-root-link-target
            ln -sfn /var/tmp/e2e-root-link-target "$r"
            chown tester:tester "{stage}/etc/sysctl.d/90-btrfs-writeback.conf"
            rgain="$({installer} install --root {stage} --owner root {sets})"
            grep -qxF "written:   $r" <<<"$rgain" || {{ echo "a link at a root destination was kept" >&2; exit 1; }}
            [[ ! -L "$r" ]] || {{ echo "the root link is still a link" >&2; exit 1; }}
            grep -qxF "written:   {stage}/etc/sysctl.d/90-btrfs-writeback.conf" <<<"$rgain" ||
                {{ echo "a root file owned by tester was left" >&2; exit 1; }}
            [[ "$(stat -c %u:%g {stage}/etc/sysctl.d/90-btrfs-writeback.conf)" == 0:0 ]] ||
                {{ echo "the rewritten file is not root's" >&2; exit 1; }}
            rm -f /var/tmp/e2e-root-link-target
            echo E2E-INSTALLED
            """,
        ).stdout
        self.assertIn("E2E-INSTALLED", out)
        # (dest under the stage, help argv, expected exit status), from tests/test_distribution.py
        entries = {
            "/usr/local/sbin/btrfs-bulk": ("--help", 2),
            "/usr/local/sbin/btrfs-snapshot-core": ("--help", 0),
            "/usr/local/sbin/btrfs-stall-monitord": ("--help", 0),
            "/usr/local/bin/btrfs-stall-report": ("--help", 0),
            "/usr/local/sbin/docker-gc": ("--help", 2),
            "/usr/local/sbin/docker-volumes-nocow": ("--help", 2),
            "/usr/local/bin/cpu-watch": ("--help", 2),
            "/usr/local/sbin/cpu-vm-pin": ("--help", 2),
            "/home/tester/.local/lib/cst/bt-ldac-watch": ("--help", 0),
            "/home/tester/.local/lib/cst/cst-toolchains": ("help", 0),
            "/usr/local/sbin/cst-vm": ("help", 0),
        }
        for dest, (arg, rc) in entries.items():
            path = stage + dest
            runners = ["root"] if "/sbin/" in dest else ["root", "tester"]
            if dest.startswith("/home/"):
                runners = ["tester"]
            for who in runners:
                with self.subTest(dest=dest, who=who):
                    prefix = "" if who == "root" else "runuser -u tester -- "
                    p = exec_bash(
                        self.SYSTEMD_CONTAINER,
                        f"cd / && {prefix}env -i PATH=/usr/bin:/bin /usr/bin/python3 -I {path} {arg}",
                        check=False,
                    )
                    self.assertNotRegex(p.stderr, r"ImportError|ModuleNotFoundError|Traceback", p.stderr)
                    self.assertEqual(p.returncode, rc, p.stdout + p.stderr)

    def test_12_vm_backup_timer_installs_the_zipapp(self) -> None:
        """`cst vm backup-timer on` with the real systemd: it installs cst-vm
        as the zipapp `install` builds (replacing the plain-script copy an
        older CST left), the service and the timer, and enables the timer;
        the installed copy runs from / without the checkout; a second `on`
        writes nothing; `off` disables and removes everything. The NAS
        settings and the program's path are scratch files (CST_VM_*)."""
        w = "/var/tmp/e2e-vm-timer"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"systemctl disable --now cst-vm-backup@e2evm.timer; rm -rf {w} "
            "/etc/systemd/system/cst-vm-backup@.service /etc/systemd/system/cst-vm-backup@.timer; "
            "systemctl daemon-reload",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            set -euo pipefail
            w={w}
            rm -rf "$w" && mkdir -p "$w/sbin"
            printf 'NAS_ENABLED=1\nNAS_PATH=/x\n' >"$w/nas.conf"
            printf 'secret\n' >"$w/pw" && chmod 600 "$w/pw"
            export CST_PROG="cst vm" CST_VM_NAS_CONF="$w/nas.conf" CST_VM_RESTIC_PASSWORD="$w/pw" \
                CST_VM_RESTIC_CACHE="$w/cache" CST_VM_SBIN_COPY="$w/sbin/cst-vm" CST_VM_INDEX="$w/index.json"
            tool=/opt/cst-rw/vm/cst-vm
            # The copy an older CST installed: the script itself, as text.
            printf '#!/usr/bin/python3 -I\n\"\"\"cst-vm: an older CST\n' >"$w/sbin/cst-vm"
            /usr/bin/python3 -I "$tool" backup-timer status e2evm >"$w/status1"
            grep -q "$w/sbin/cst-vm: installed, OLDER" "$w/status1"
            /usr/bin/python3 -I "$tool" backup-timer on e2evm >"$w/on1"
            grep -q "updated: $w/sbin/cst-vm" "$w/on1"
            systemctl is-enabled cst-vm-backup@e2evm.timer
            grep -qF "ExecStart=/usr/bin/python3 -I $w/sbin/cst-vm backup %i --nas" \
                /etc/systemd/system/cst-vm-backup@.service
            [ "$(head -n 1 "$w/sbin/cst-vm")" = "#!/usr/bin/python3 -I" ]
            python3 -c 'import sys, zipfile; sys.exit(not zipfile.is_zipfile(sys.argv[1]))' "$w/sbin/cst-vm"
            # The installed copy runs on its own, from /, as root and as tester.
            (cd / && env -i PATH=/usr/bin:/bin /usr/bin/python3 -I "$w/sbin/cst-vm" help) >"$w/help-root"
            grep -q 'backup-timer on' "$w/help-root"
            (cd / && runuser -u tester -- env -i PATH=/usr/bin:/bin /usr/bin/python3 -I "$w/sbin/cst-vm" help) \
                >"$w/help-tester"
            grep -q 'backup-timer on' "$w/help-tester"
            again="$(/usr/bin/python3 -I "$tool" backup-timer on e2evm)"
            if grep -E 'installed:|updated:' <<<"$again"; then echo "second on wrote files" >&2; exit 1; fi
            /usr/bin/python3 -I "$tool" backup-timer status e2evm >"$w/status2"
            grep -q "$w/sbin/cst-vm: installed, up to date" "$w/status2"
            /usr/bin/python3 -I "$tool" backup-timer off e2evm
            if systemctl is-enabled cst-vm-backup@e2evm.timer; then echo "still enabled" >&2; exit 1; fi
            [ ! -e "$w/sbin/cst-vm" ] && [ ! -e /etc/systemd/system/cst-vm-backup@.service ]
            [ ! -e /etc/systemd/system/cst-vm-backup@.timer ]
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_13_degraded_boot_install_cycle(self) -> None:
        """`cst btrfs degraded-boot` with the real systemd and the real udevadm
        verify: install writes the rule, the units and the helper, enables the
        record unit and tracks the four files in the initramfs; the generated
        rule passes `udevadm verify` and the units `systemd-analyze verify`;
        a second install writes nothing and makes no rpm-ostree change;
        `status` changes nothing and ends in "verdict: OK"; uninstall removes
        everything and untracks it. rpm-ostree, findmnt, btrfs, lsblk, cpio and
        the udevd parts of udevadm (udevd does not run here) are exported bash
        functions, because the script resets PATH: nothing in /usr/bin is
        replaced. The fake rpm-ostree keeps one booted deployment (no staged
        one) with a boot entry and a tar.gz overlay under /boot."""
        c = self.SYSTEMD_CONTAINER
        w = "/var/tmp/e2e-degraded-boot"
        uuid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        files = (
            "/etc/udev/rules.d/65-btrfs-last-resort.rules",
            "/etc/systemd/system/btrfs-last-resort@.service",
            "/etc/systemd/system/btrfs-last-resort@.timer",
            "/etc/btrfs-degraded-boot/btrfs-last-resort",
        )
        record = "/etc/systemd/system/btrfs-degraded-boot-record.service"
        synced = "/etc/btrfs-degraded-boot/initramfs-synced.sha256"
        self.addCleanup(
            exec_bash,
            c,
            f"""
            systemctl disable btrfs-degraded-boot-record.service 2>/dev/null || true
            rm -rf {w} /ostree /boot/loader /boot/ostree /run/ostree-booted /etc/btrfs-degraded-boot \
                /run/btrfs-allow-degraded /var/lib/btrfs-degraded-boot {" ".join(files)} {record}
            systemctl daemon-reload
            """,
            check=False,
        )
        docker_exec(
            c, ["bash", "-c", f"rm -rf {w} && mkdir -p {w} && cat >{w}/fakes.bash"], input_text=DEGRADED_BOOT_FAKES
        )
        exec_bash(c, DEGRADED_BOOT_PREPARE)

        def step(script: str) -> str:
            return exec_bash(c, f". {w}/fakes.bash\n{script}").stdout

        def calls() -> list[str]:
            return exec_bash(c, f"cat {w}/calls").stdout.splitlines()

        first = step("/opt/cst-rw/cst btrfs degraded-boot")
        for dest in (*files, record, synced):
            self.assertIn(f"written:   {dest}\n", first)
        self.assertIn("rpm-ostree initramfs-etc " + " ".join(f"--track={f}" for f in files) + "\n", first)
        self.assertIn("verdict:          OK", first)
        modes = step(f"stat -c '%a %U %n' {' '.join(files)} {record}").splitlines()
        self.assertEqual(modes, [f"{'755' if f.endswith('last-resort') else '644'} root {f}" for f in (*files, record)])
        self.assertEqual(step("systemctl is-enabled btrfs-degraded-boot-record.service").strip(), "enabled")

        # The install already ran the real `udevadm verify` on the rule it
        # wrote (it stops when verify fails), so this needs no skip.
        with self.subTest("udevadm verify on the installed rule"):
            out = exec_bash(c, f"udevadm verify --resolve-names=never --no-style {files[0]}").stdout
            self.assertIn("Success: 1", out)
        # Without --recursive-errors=no, verify exits 0 on an unknown key (only a
        # warning); with it, any problem in the named units fails.
        with self.subTest("systemd-analyze verify on the installed units"):
            exec_bash(
                c,
                f"systemd-analyze verify --recursive-errors=no btrfs-last-resort@{uuid}.timer btrfs-last-resort@{uuid}.service "
                "btrfs-degraded-boot-record.service",
            )

        n = len(calls())
        second = step("/opt/cst-rw/cst btrfs degraded-boot")
        self.assertNotIn("written:", second)
        self.assertIn("unchanged: initramfs-etc (tracked, next-boot overlay in sync)", second)
        self.assertEqual([x for x in calls()[n:] if x.startswith("rpm-ostree initramfs-etc")], [])

        sums = f"sha256sum {' '.join(files)} {record} {synced}"
        before = step(sums)
        n = len(calls())
        status = step("/opt/cst-rw/cst btrfs degraded-boot status")
        self.assertIn(
            "verdict:          OK - an NVMe failure at boot would mount degraded after 45 s and alert", status
        )
        self.assertEqual(step(sums), before)
        self.assertEqual([x for x in calls()[n:] if x.startswith(("rpm-ostree initramfs-etc", "udevadm control"))], [])

        gone = step("/opt/cst-rw/cst btrfs degraded-boot uninstall")
        self.assertIn("rpm-ostree initramfs-etc " + " ".join(f"--untrack={f}" for f in files), calls())
        self.assertIn("Uninstalled (initramfs: from the next deployment on).", gone)
        left = step(
            f"for f in {' '.join(files)} {record} {synced} /etc/btrfs-degraded-boot; do [[ ! -e $f ]] || echo $f; done"
        )
        self.assertEqual(left, "")
        self.assertEqual(step(f"cat {w}/tracked"), "")
        self.assertNotEqual(
            exec_bash(c, "systemctl is-enabled btrfs-degraded-boot-record.service", check=False).returncode, 0
        )
