"""Docker Layer 1 E2E suite: unprivileged systemd PID 1 + user manager + SFTP sidecar."""

import os
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


def exec_bash(container, script, *, check=True, env=None):
    return docker_exec(container, ["bash", "-euo", "pipefail", "-c", script], check=check, env=env)


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


class Tier1SystemdE2ETest(unittest.TestCase):
    SYSTEMD_CONTAINER = os.environ["CST_E2E_SYSTEMD_CONTAINER"]
    NAS_CONTAINER = os.environ["CST_E2E_NAS_CONTAINER"]
    IMAGE = os.environ["CST_E2E_SYSTEMD_IMAGE"]
    CST_ROOT_MOUNT = os.environ["CST_E2E_REPO_ROOT"]

    @classmethod
    def setUpClass(cls):
        # Provide a working writable copy of CST inside the container so symlink tests
        # and user-owned operations do not rely on mutating the read-only host repo.
        exec_bash(
            cls.SYSTEMD_CONTAINER,
            "rm -rf /opt/cst-rw && cp -a /opt/cst /opt/cst-rw && chmod -R a+rX /opt/cst-rw",
        )

    def test_01_in_container_guardrail_blocks_mutations(self):
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
                    "docker", "run", "--rm",
                    *extra_flags,
                    "-e", "E2E_KEEP_DOCKERENV=1",
                    "-v", f"{repo}:/opt/cst:ro",
                    "--entrypoint", "bash",
                    self.IMAGE,
                    "-c", f"{subcmd}",
                ],
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

    def test_02_systemd_and_user_dbus_healthy(self):
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

    def test_03_cst_root_link_status_and_doctor(self):
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

    def test_04_toolchains_e2e_timer_config_and_prune(self):
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
            /opt/cst-rw/cst toolchains auto on
            systemctl --user is-enabled cst-toolchains-prune.timer
            systemctl --user is-active cst-toolchains-prune.timer
            systemctl --user start cst-toolchains-prune.service
            test ! -e /home/tester/.cache/cargo-build/ab/cdef0123456789
            st_out="$(/opt/cst-rw/cst toolchains status --brief)"
            grep -q 'cargo-build\\|toolchains' <<<"$st_out"
            /opt/cst-rw/cst toolchains auto off
            ! systemctl --user is-enabled cst-toolchains-prune.timer >/dev/null 2>&1
            """,
        )
        # Clean up demo workspace for later tests
        as_tester_bash(self.SYSTEMD_CONTAINER, "rm -rf /home/tester/work/demo")

    def test_05_bluetooth_config_wp_roles_and_ldac_unit(self):
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
        self.assertEqual(plan_rc.returncode, 10, f"expected plan rc=10, got {plan_rc.returncode}\n{plan_rc.stdout}\n{plan_rc.stderr}")

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
            line.strip()
            for line in reverted_main.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertFalse(
            any("6fbaf188-05e0-496a-9885-d6ddfdb4e03e" in line or line.startswith("Experimental") for line in active_lines),
            f"unexpected active BlueZ config after revert: {active_lines!r}",
        )
        ldac_enabled = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "systemctl --user is-enabled cst-bt-ldac48.service",
            check=False,
        )
        self.assertNotEqual(ldac_enabled.returncode, 0, "cst-bt-ldac48.service still enabled after revert")

    def test_06_btrfs_notify_pending_alerts_and_sftp_restic_nas(self):
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

    def test_07_cpu_baseline_dropin_loses_to_runtime_overrides_after_reload(self):
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

    def test_08_cpu_game_profile_idles_escaped_user_boxes_and_pins_memory_low(self):
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
