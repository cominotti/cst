# SPDX-License-Identifier: MPL-2.0
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
DEGRADED_BOOT_DIR = "/var/tmp/e2e-degraded-boot"
DEGRADED_BOOT_UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
DEGRADED_BOOT_FAKES = (
    f"export E2E_W={DEGRADED_BOOT_DIR}\nexport E2E_UUID={DEGRADED_BOOT_UUID}\n"
    + r"""export E2E_CSUM=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee

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
)

DEGRADED_BOOT_PREPARE = (
    f". {DEGRADED_BOOT_DIR}/fakes.bash\n"
    + r"""D="/ostree/deploy/e2e/deploy/$E2E_CSUM.0"
mkdir -p "$D/usr/lib/udev/rules.d" "/ostree/boot.1/e2e/$E2E_CSUM" /boot/loader/entries /boot/ostree/initramfs-overlays
cp /usr/lib/udev/rules.d/64-btrfs.rules "$D/usr/lib/udev/rules.d/"
ln -sfn "../../../deploy/e2e/deploy/$E2E_CSUM.0" "/ostree/boot.1/e2e/$E2E_CSUM/0"
printf 'title e2e\noptions root=UUID=%s rootflags=subvol=root,degraded ostree=/ostree/boot.1/e2e/%s/0\ninitrd /ostree/e2e/initramfs.img /ostree/initramfs-overlays/e2e.img\n' \
    "$E2E_UUID" "$E2E_CSUM" >/boot/loader/entries/ostree-1-e2e.conf
touch /run/ostree-booted
: >"$E2E_W/tracked"
: >"$E2E_W/calls"
"""
)


def stub_stall_monitor_unit(shipped: str) -> str:
    """btrfs-stall-monitor.service as shipped, with the daemon replaced by a
    sleep. Nice=-5 goes too: a negative nice needs CAP_SYS_NICE, which the
    tier 1 container does not have (the unit would fail with 201/NICE).
    Every other line stays, RuntimeDirectory= and its options included."""
    out: list[str] = []
    swapped = dropped = 0
    for line in shipped.splitlines():
        if line.startswith("ExecStart="):
            out.append("ExecStart=/usr/bin/sleep infinity")
            swapped += 1
        elif line.startswith("Nice="):
            out.append(f"# {line}: dropped, no CAP_SYS_NICE in the container")
            dropped += 1
        else:
            out.append(line)
    if (swapped, dropped) != (1, 1):
        raise AssertionError(f"the shipped unit changed shape (ExecStart= {swapped}, Nice= {dropped}):\n{shipped}")
    return "\n".join(out) + "\n"


# journal_named IDENT MATCH: waits up to 60 s for journal lines under SyslogIdentifier=IDENT
# (journalctl -t), as root, which also reads the user managers' services. On failure it shows the
# identifiers that the lines of MATCH (a journal field, the run's unit) carry.
JOURNAL_NAMED = """
journal_named() {
    journalctl --sync 2>/dev/null || true
    for _ in $(seq 1 120); do
        if [ -n "$(journalctl -q --no-pager -o cat -n 1 -t "$1")" ]; then echo "E2E-JOURNAL-OK $1"; return 0; fi
        sleep 0.5
    done
    echo "journalctl -t $1 finds nothing; the identifiers of the run's lines:" >&2
    journalctl -q --no-pager -o verbose -n 20 "$2" | grep -E 'SYSLOG_IDENTIFIER=|MESSAGE=' >&2 || true
    return 1
}
"""


# test_80: run as tester once the test has written main.conf and the settings
# file: apply, a second apply, then le-audio on, on again and off. Each run ends with
# the live checks, which fail in this container (no adapter, bluetoothd not
# running): exit 1 with that error only.
BT_SECOND_APPLY_SCRIPT = r"""
set -E
trap 'echo "test_80 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
export LC_ALL=C
H=/home/tester
mac=00:11:22:33:44:55
bt() { /opt/cst-rw/cst bluetooth "$@"; }
wp="$H/.config/wireplumber/wireplumber.conf.d"
rule="$wp/50-bluez-00_11_22_33_44_55.conf"
le="$wp/90-cst-le-audio-off.conf"
unit="$H/.config/systemd/user/cst-bt-ldac48.service"
watch="$H/.local/lib/cst/bt-ldac-watch"
conf="$H/.config/cst/bluetooth.conf"
# Each file apply manages, by inode, mtime and content, and the number of main.conf backups.
files() {
    local f
    for f in /etc/bluetooth/main.conf "$rule" "$le" "$unit" "$watch" "$conf"; do
        if [[ -e "$f" ]]; then echo "$(stat -c '%i %y' "$f") $(sha256sum <"$f" | cut -c1-16) $f"; else echo "absent $f"; fi
    done
    echo "backups: $(find /etc/bluetooth -maxdepth 1 -name 'main.conf.bak-*' | wc -l)"
}
same() {   # same LABEL BEFORE
    [[ "$(files)" == "$2" ]] && return 0
    printf 'test_80 %s changed files:\nbefore:\n%s\nafter:\n%s\n' "$1" "$2" "$(files)" >&2
    return 1
}
show() { sed "s/^/test_80 $1: /" <<<"$2" >&2; }   # the CI log
verify_failed_only() {   # verify_failed_only LABEL RC OUT: the live checks failed, nothing else
    if [[ "$2" != 1 ]] || ! grep -qF "ERROR: verification found problems (see above)." <<<"$3" \
        || ! grep -qF "no Bluetooth adapter in org.bluez" <<<"$3"; then
        echo "test_80 $1: exit $2, not only the live checks failing" >&2
        return 1
    fi
}
roles() { python3 -I /opt/cst-rw/bluetooth/bt-helper wp-roles | cut -d'|' -f1; }
wp_pid() { systemctl --user show -p MainPID --value wireplumber.service; }
# step LABEL ARGS...: bt ARGS into $out, shown in the CI log, ending with only the live checks failing.
step() {
    local rc=0
    out="$(bt "${@:2}" 2>&1)" || rc=$?
    show "$1" "$out"
    verify_failed_only "$1" "$rc" "$out"
}
no_writes() {   # no_writes LABEL OUT: apply's report has no line of a change (every verb it uses)
    if grep -E '^(adopted|backup|enabled|reconnect|removed|restart|restarted|restored|set|started|written): ' <<<"$2"; then
        echo "test_80 $1 changed something" >&2
        return 1
    fi
}
systemctl --user is-active wireplumber.service

# 1. apply (no restart: bluetoothd does not run here), then plan has nothing left.
step apply apply --yes --no-restart
for f in "$rule" "$le" "$unit" "$watch"; do [[ -f "$f" ]]; done
grep -qxF "Experimental = true" /etc/bluetooth/main.conf
rc=0; out="$(bt plan 2>&1)" || rc=$?
show "plan after apply" "$out"
[[ "$rc" == 0 ]]
grep -qxF "Nothing to do." <<<"$out"
s1="$(files)"
le_text="$(cat "$le")"

# 2. a second apply changes nothing: no file written, no backup, no restart.
pid="$(wp_pid)"
step "second apply" apply --yes
no_writes "second apply" "$out"
grep -qxF "unchanged: /etc/bluetooth/main.conf (Experimental and KernelExperimental already set in [General])" <<<"$out"
grep -qxF "unchanged: $rule" <<<"$out"
grep -qxF "unchanged: LE Audio off" <<<"$out"
grep -qxF "unchanged: cst-bt-ldac48.service enabled and active" <<<"$out"
grep -qxF "none needed" <<<"$out"
same "second apply" "$s1"
[[ "$(wp_pid)" == "$pid" ]]

# 3. le-audio without on/off only shows the setting.
out="$(bt le-audio)"
grep -qxF "LE Audio in PipeWire: off (~/.config/cst/bluetooth.conf)" <<<"$out"
same "le-audio (show)" "$s1"

# 4. le-audio on: the setting written (other lines kept), the drop-in removed,
# WirePlumber restarted and reading the PipeWire default roles again.
[[ "$(roles)" == classic ]]
step "le-audio on" le-audio on --yes
grep -qxF "written:   ~/.config/cst/bluetooth.conf: LE_AUDIO=on" <<<"$out"
grep -qxF "removed:   $le (LE Audio on)" <<<"$out"
grep -qxF "restart:   wireplumber (your session; audio stops for a moment)" <<<"$out"
[[ "$(cat "$conf")" == "$(printf 'DEVICE=%s\nLDAC_QUALITY=auto\nLDAC_RATE=48000\nLE_AUDIO=on' "$mac")" ]]
[[ ! -e "$le" ]]
[[ "$(roles)" == default ]]
[[ "$(wp_pid)" != "$pid" ]]
systemctl --user is-active wireplumber.service
# Only the settings and the drop-in changed.
diff <(grep -vF -e "$le" -e "$conf" <<<"$s1") <(files | grep -vF -e "$le" -e "$conf")
bt plan >/dev/null 2>&1   # exit 0: nothing to do
s2="$(files)"
pid="$(wp_pid)"

# 5. le-audio on again: nothing written, nothing restarted.
step "le-audio on again" le-audio on --yes
grep -qxF "unchanged: ~/.config/cst/bluetooth.conf already has LE_AUDIO=on" <<<"$out"
grep -qxF "unchanged: LE Audio on (no drop-in)" <<<"$out"
no_writes "le-audio on again" "$out"
same "le-audio on again" "$s2"
[[ "$(wp_pid)" == "$pid" ]]
out="$(bt le-audio)"
grep -qxF "LE Audio in PipeWire: on (~/.config/cst/bluetooth.conf)" <<<"$out"
same "le-audio (show, on)" "$s2"

# 6. le-audio off: the same drop-in back, the classic roles again.
step "le-audio off" le-audio off --yes
grep -qxF "written:   ~/.config/cst/bluetooth.conf: LE_AUDIO=off" <<<"$out"
grep -qxF "restart:   wireplumber (your session; audio stops for a moment)" <<<"$out"
[[ "$(cat "$le")" == "$le_text" ]]
[[ "$(roles)" == classic ]]
[[ "$(wp_pid)" != "$pid" ]]
grep -qxF "LE_AUDIO=off" "$conf"
bt plan >/dev/null 2>&1   # exit 0: nothing to do
echo E2E-OK
"""


class Tier1SystemdE2ETest(unittest.TestCase):
    SYSTEMD_CONTAINER = os.environ["CST_E2E_SYSTEMD_CONTAINER"]
    NAS_CONTAINER = os.environ["CST_E2E_NAS_CONTAINER"]
    IMAGE = os.environ["CST_E2E_SYSTEMD_IMAGE"]
    CST_ROOT_MOUNT = os.environ["CST_E2E_REPO_ROOT"]
    # run.py's host_lsm_security_opts (label=disable where SELinux is on), for the containers test_01 starts.
    SECURITY_OPTS = os.environ.get("CST_E2E_SECURITY_OPTS", "").split()

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
                    *self.SECURITY_OPTS,
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
        # The run's journal lines carry the program's name (SyslogIdentifier=), not python3.
        res = exec_bash(
            self.SYSTEMD_CONTAINER,
            JOURNAL_NAMED + "journal_named cst-toolchains _SYSTEMD_USER_UNIT=cst-toolchains-prune.service",
        )
        self.assertIn("E2E-JOURNAL-OK cst-toolchains", res.stdout)
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
        sets = (
            "--set UUID=00000000-0000-0000-0000-000000000000 --set OPTIONS=subvolid=5 --set USER=tester "
            "--set ALLOWED=AllowedCPUs=0"
        )
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
            # The license line reaches the rendered templates in their own comment syntax, and an
            # installed zipapp still names who installed it right after its shebang.
            [[ "$(head -n 1 {stage}/etc/polkit-1/rules.d/50-cpu-setup-game.rules)" == "// SPDX-License-Identifier: MPL-2.0" ]] ||
                {{ echo "the rendered polkit rules lost their license line" >&2; exit 1; }}
            [[ "$(head -n 1 {stage}/etc/tmpfiles.d/btrfs-automatic-reclaim.conf)" == "# SPDX-License-Identifier: MPL-2.0" ]] ||
                {{ echo "the rendered tmpfiles rule lost its license line" >&2; exit 1; }}
            [[ "$(sed -n 2p {stage}/home/tester/.local/lib/cst/bt-ldac-watch)" == "# managed-by: bluetooth-setup.sh (CST)"* ]] ||
                {{ echo "the installed bt-ldac-watch lost its managed-by line" >&2; exit 1; }}
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
        # The install records list every destination with the sha256 of the file on disk (root's under
        # the stage's /var/lib/cst, tester's in tester's home), keyed by the destination as installed.
        check_records = f"""
import hashlib, json, sys
stage = {stage!r}
for path, prefix in ((stage + "/var/lib/cst/installed.json", ""), (stage + "/home/tester/.local/state/cst/installed.json", "/home/")):
    rec = json.load(open(path))
    assert rec["format"] == 1, rec["format"]
    for dest, f in rec["files"].items():
        data = open(stage + dest, "rb").read()
        assert f["sha256"] == hashlib.sha256(data).hexdigest(), dest
        assert dest.startswith(prefix), dest
    print(len(rec["files"]), sorted(rec["files"])[0])
"""
        rec = exec_bash(self.SYSTEMD_CONTAINER, f"/usr/bin/python3 -I -c {shlex.quote(check_records)}").stdout.split()
        counts = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"{installer} dests --owner root | wc -l; {installer} dests --owner user | wc -l",
        ).stdout.split()
        self.assertEqual(rec[0], counts[0], "the root record does not list every root destination")
        self.assertEqual(rec[2], counts[1], "tester's record does not list every user destination")
        # An edited file: check says not current; a second install puts it back; remove --component takes a
        # component's files and record lines away.
        cycle = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            f={stage}/usr/local/sbin/docker-gc
            echo '# edited' >>"$f"
            rc=0; {installer} check --root {stage} --owner root {sets} || rc=$?
            [[ "$rc" == 1 ]] || {{ echo "check did not see the edited file (exit $rc)" >&2; exit 1; }}
            # cst doctor, pointed at the stage, names it (its install area reads the stage's record).
            doc="$(CST_DOCTOR_ROOT={stage} CST_DOCTOR_CONTAINER=0 /usr/bin/python3 -I /opt/cst-rw/doctor/cst-doctor || true)"
            grep -qF "/usr/local/sbin/docker-gc: changed since CST installed it (btrfs:docker-gc)" <<<"$doc" ||
                {{ echo "doctor did not report the edited file: $doc" >&2; exit 1; }}
            {installer} install --root {stage} --owner root {sets} | grep -qxF "written:   $f" ||
                {{ echo "a second install did not restore the edited file" >&2; exit 1; }}
            {installer} check --root {stage} --owner root {sets}
            {installer} remove --root {stage} --component btrfs:docker-gc
            [[ ! -e "$f" ]] || {{ echo "remove --component left $f" >&2; exit 1; }}
            grep -q docker-gc {stage}/var/lib/cst/installed.json && {{ echo "the record still lists docker-gc" >&2; exit 1; }}
            {installer} install --root {stage} --component btrfs:docker-gc >/dev/null
            echo E2E-RECORD-CYCLE
            """,
        ).stdout
        self.assertIn("E2E-RECORD-CYCLE", cycle)
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

    # The --set values of the components with templates (as their setup commands pass them).
    COMPONENT_SETS: Mapping[str, str] = {
        "btrfs:core": "--set UUID=00000000-0000-0000-0000-000000000000",
        "btrfs:top-mount": "--set UUID=00000000-0000-0000-0000-000000000000 --set OPTIONS=subvolid=5",
        "cpu:core": "--set USER=tester",
        "cpu:docker-slice": "--set ALLOWED=AllowedCPUs=0",
    }
    # Components whose units run a program of another one (btrfs-stall-alert.service runs btrfs-notify):
    # installed first, as btrfs-setup.sh install does, and removed after.
    COMPONENT_NEEDS: Mapping[str, tuple[str, ...]] = {"btrfs:core": ("btrfs:alerts",)}

    def test_19_every_component_full_cycle_on_the_container_root(self) -> None:
        """Each component of the manifest, installed for real on the container's / (root's as root,
        the user's as tester): plan changes nothing; install over a plain-script copy of each of its
        Python programs (what CST installed before the zipapps) writes the zipapps and lists every
        file in the install record; once marked done, cst doctor's install area has no WARN about it;
        a second install changes nothing, the record included; remove --component removes the
        files and their record lines. A component with a file already in place (another test's) is
        skipped, so nothing of another test is removed. /usr/sbin/btrfs, which a btrfs unit runs and
        the image lacks, is an empty program for the time of the test (as in test_16)."""
        c = self.SYSTEMD_CONTAINER
        installer = "/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
        stub = "/usr/sbin/btrfs"
        made_stub = exec_bash(c, f"[[ -e {stub} ]] || {{ install -m 0755 /dev/null {stub}; echo made; }}").stdout
        if "made" in made_stub:
            self.addCleanup(exec_bash, c, f"rm -f {stub}", check=False)
        script = 'import sys; sys.path.insert(0, "/opt/cst-rw"); from cst_core.domain.install import COMPONENTS; print(" ".join(COMPONENTS))'
        components = exec_bash(c, f"/usr/bin/python3 -I -c {shlex.quote(script)}").stdout.split()
        self.assertIn("btrfs:core", components)
        for comp in components:
            with self.subTest(component=comp):
                self._component_cycle(c, installer, comp)

    def _component_cycle(self, c: str, installer: str, comp: str) -> None:
        sets = self.COMPONENT_SETS.get(comp, "")
        owner = exec_bash(c, f"{installer} dests --component {comp} | head -n 1").stdout.strip()
        user = owner.startswith("~")
        run = "runuser -u tester -- " if user else ""
        home = "--home /home/tester" if user else ""
        record = "/home/tester/.local/state/cst/installed.json" if user else "/var/lib/cst/installed.json"
        dests = exec_bash(c, f"{installer} dests {home} --component {comp}").stdout.split()
        present = exec_bash(c, " ".join(f"[[ ! -e {shlex.quote(d)} ]] || echo {shlex.quote(d)};" for d in dests)).stdout
        if present.strip():
            self.skipTest(f"{comp}: already there (another test's): {present.split()}")
        cleanup = f"{run}{installer} remove {home} --component {comp} >/dev/null"
        self.addCleanup(exec_bash, c, cleanup, check=False)
        for need in self.COMPONENT_NEEDS.get(comp, ()):
            exec_bash(c, f"{installer} install --component {need} >/dev/null")
            self.addCleanup(exec_bash, c, f"{installer} remove --component {need} >/dev/null", check=False)
        out = exec_bash(
            c,
            f"""
            set -e
            plan="$({run}{installer} plan {home} {sets} --component {comp})"
            for d in {" ".join(shlex.quote(d) for d in dests)}; do
                [[ ! -e "$d" ]] || {{ echo "plan wrote $d" >&2; exit 1; }}
            done
            [[ "$(grep -c '^would write: ' <<<"$plan")" == {len(dests)} ]] || {{ echo "plan: $plan" >&2; exit 1; }}
            # What an older CST installed: each Python program as the plain script.
            for s in $({installer} srcs --component {comp} --kind python-entry); do
                d="$({installer} dests {home} "$s")"
                {run}install -D -m 0755 "/opt/cst-rw/$s" "$d"
            done
            {run}{installer} install {home} {sets} --component {comp} >/dev/null
            {run}{installer} mark {home} --component {comp} done
            for s in $({installer} srcs --component {comp} --kind python-entry); do
                d="$({installer} dests {home} "$s")"
                /usr/bin/python3 -I -c 'import sys, zipfile; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$d" ||
                    {{ echo "$d is still the plain script" >&2; exit 1; }}
            done
            before="$(sha256sum {record})"
            again="$({run}{installer} install {home} {sets} --component {comp})"
            if grep -v '^unchanged: ' <<<"$again"; then echo "a second install changed files" >&2; exit 1; fi
            [[ "$(sha256sum {record})" == "$before" ]] || {{ echo "a second install changed the record" >&2; exit 1; }}
            doc="$({run}env HOME={"/home/tester" if user else "/root"} CST_DOCTOR_CONTAINER=0 /usr/bin/python3 -I /opt/cst-rw/doctor/cst-doctor || true)"
            # Only its install area: the other areas report on the container itself.
            inst="$(sed -n '/^== install/,/^== /p' <<<"$doc")"
            [[ -n "$inst" ]] || {{ echo "doctor has no install area: $doc" >&2; exit 1; }}
            if grep -E '^ *WARN' <<<"$inst" | grep -F -e '{comp}' {" ".join(f"-e {shlex.quote(d)}" for d in dests)}; then
                echo "doctor warns about {comp}" >&2; exit 1
            fi
            {run}{installer} remove {home} --component {comp} >/dev/null
            for d in {" ".join(shlex.quote(d) for d in dests)}; do
                [[ ! -e "$d" ]] || {{ echo "remove left $d" >&2; exit 1; }}
            done
            if grep -qF '"{comp}"' {record}; then echo "the record still lists {comp}" >&2; exit 1; fi
            echo E2E-CYCLE-OK
            """,
        ).stdout
        self.assertIn("E2E-CYCLE-OK", out)

    def test_20_units_are_verified_before_they_replace_the_installed_ones(self) -> None:
        """As root on the container's /, the installer runs the real `systemd-analyze verify
        --recursive-errors=no` on the units an install changes, after their programs and before any of
        them is written. Negative control: a scratch copy of the checkout whose docker-gc.service has an
        unknown key (only a warning without --recursive-errors=no) is refused with exit 1, naming the
        key, and none of the component's units is written (its program is). The same install from the
        real checkout writes them. Not verified (and not tested here): drop-ins, template units, a
        staged install (--root)."""
        c = self.SYSTEMD_CONTAINER
        dests = "/usr/local/sbin/docker-gc /etc/systemd/system/docker-gc-recorder.service /etc/systemd/system/docker-gc.service /etc/systemd/system/docker-gc.timer"
        if exec_bash(c, f"for f in {dests}; do [[ ! -e $f ]] || echo $f; done").stdout.strip():
            self.skipTest("docker-gc is installed already (another test's)")
        copy = "/var/tmp/e2e-cst-verify"
        self.addCleanup(
            exec_bash,
            c,
            f"rm -rf {copy}; /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py remove --component btrfs:docker-gc",
            check=False,
        )
        out = exec_bash(
            c,
            f"""
            rm -rf {copy} && cp -a /opt/cst-rw {copy}
            sed -i 's/^\\[Service\\]$/[Service]\\nBogusKeyE2E=1/' {copy}/btrfs/units/docker-gc.service
            grep -q '^BogusKeyE2E=1$' {copy}/btrfs/units/docker-gc.service || {{ echo "the unit was not broken" >&2; exit 1; }}
            rc=0; err="$(/usr/bin/python3 -I -B {copy}/cst_core/installer.py install --component btrfs:docker-gc 2>&1 >/dev/null)" || rc=$?
            [[ "$rc" == 1 ]] || {{ echo "a broken unit was not refused (exit $rc): $err" >&2; exit 1; }}
            grep -qF "BogusKeyE2E" <<<"$err" || {{ echo "the refusal does not name the problem: $err" >&2; exit 1; }}
            for u in docker-gc-recorder.service docker-gc.service docker-gc.timer; do
                [[ ! -e /etc/systemd/system/$u ]] || {{ echo "$u was written although verify failed" >&2; exit 1; }}
            done
            [[ -x /usr/local/sbin/docker-gc ]] || {{ echo "the program was not written first" >&2; exit 1; }}
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install --component btrfs:docker-gc >/dev/null
            [[ -f /etc/systemd/system/docker-gc.service ]] || {{ echo "the good unit was not written" >&2; exit 1; }}
            echo E2E-VERIFY-OK
            """,
        ).stdout
        self.assertIn("E2E-VERIFY-OK", out)

    def test_12_vm_backup_timer_installs_the_zipapp(self) -> None:
        """`cst vm backup-timer on` with the real systemd: it installs cst-vm
        as the zipapp `install` builds (replacing the plain-script copy an
        older CST left), the service and the timer, and enables the timer;
        the installed copy runs from / without the checkout; a second `on`
        writes nothing; one run of the service logs under its own name
        (journalctl -t cst-vm); `off` disables and removes everything, the
        timer's stamp too. The NAS settings and the program's path are
        scratch files (CST_VM_*)."""
        w = "/var/tmp/e2e-vm-timer"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"systemctl disable --now cst-vm-backup@e2evm.timer; systemctl stop cst-vm-backup@e2evm.service; "
            f"systemctl reset-failed cst-vm-backup@e2evm.service; rm -rf {w} "
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
            grep -qx 'SyslogIdentifier=cst-vm' /etc/systemd/system/cst-vm-backup@.service
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
            # systemd made the stamp of the Persistent= timer when it started it; off must remove it.
            stamp=/var/lib/systemd/timers/stamp-cst-vm-backup@e2evm.timer
            if [ ! -e "$stamp" ]; then
                echo "no $stamp while the timer is on: the check after off would prove nothing" >&2
                ls -la /var/lib/systemd/timers >&2 || true
                exit 1
            fi
            # One run of the service, for its journal name. It refuses at once: the service reads the
            # default settings (not the CST_VM_* above), and no NAS backup is set up there.
            systemctl start --no-block cst-vm-backup@e2evm.service
            {JOURNAL_NAMED}
            journal_named cst-vm _SYSTEMD_UNIT=cst-vm-backup@e2evm.service
            for _ in $(seq 1 120); do
                case "$(systemctl show -p ActiveState --value cst-vm-backup@e2evm.service)" in
                    activating|active|deactivating) sleep 0.5 ;;
                    *) break ;;
                esac
            done
            systemctl reset-failed cst-vm-backup@e2evm.service 2>/dev/null || true
            /usr/bin/python3 -I "$tool" backup-timer status e2evm >"$w/status2"
            grep -q "$w/sbin/cst-vm: installed, up to date" "$w/status2"
            /usr/bin/python3 -I "$tool" backup-timer off e2evm
            if systemctl is-enabled cst-vm-backup@e2evm.timer; then echo "still enabled" >&2; exit 1; fi
            if [ -e "$stamp" ]; then echo "off left $stamp" >&2; exit 1; fi
            [ ! -e "$w/sbin/cst-vm" ] && [ ! -e /etc/systemd/system/cst-vm-backup@.service ]
            [ ! -e /etc/systemd/system/cst-vm-backup@.timer ]
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_13_degraded_boot_install_cycle(self) -> None:
        """`cst btrfs degraded-boot` with the real systemd and the real udevadm
        verify: `plan` first says what install would write and run and changes
        nothing; install writes the rule, the units and the helper, enables the
        record unit and tracks the four files in the initramfs; the generated
        rule passes `udevadm verify` and the units `systemd-analyze verify`;
        a second install writes nothing and makes no rpm-ostree change, and
        `plan` then has nothing left to do; `status` changes nothing and ends
        in "verdict: OK"; a typo in a unit (an unknown key in a drop-in of the
        record unit) stops an install at its `systemd-analyze verify`, before
        any rpm-ostree change; uninstall removes everything and untracks it. rpm-ostree, findmnt, btrfs, lsblk, cpio and
        the udevd parts of udevadm (udevd does not run here) are exported bash
        functions, because the script resets PATH: nothing in /usr/bin is
        replaced. The fake rpm-ostree keeps one booted deployment (no staged
        one) with a boot entry and a tar.gz overlay under /boot."""
        c = self.SYSTEMD_CONTAINER
        w = DEGRADED_BOOT_DIR
        uuid = DEGRADED_BOOT_UUID
        files = (
            "/etc/udev/rules.d/65-btrfs-last-resort.rules",
            "/etc/systemd/system/btrfs-last-resort@.service",
            "/etc/systemd/system/btrfs-last-resort@.timer",
            "/etc/btrfs-degraded-boot/btrfs-last-resort",
        )
        record = "/etc/systemd/system/btrfs-degraded-boot-record.service"
        synced = "/etc/btrfs-degraded-boot/initramfs-synced.sha256"
        typo = f"{record}.d"
        self.addCleanup(
            exec_bash,
            c,
            f"""
            systemctl disable btrfs-degraded-boot-record.service 2>/dev/null || true
            rm -rf {w} /ostree /boot/loader /boot/ostree /run/ostree-booted /etc/btrfs-degraded-boot \
                /run/btrfs-allow-degraded /var/lib/btrfs-degraded-boot {" ".join(files)} {record} {typo}
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

        everything = f"{' '.join(files)} {record} {synced} /etc/btrfs-degraded-boot"
        exists = f"for f in {everything}; do [[ ! -e $f ]] || echo $f; done"
        plan = step("/opt/cst-rw/cst btrfs degraded-boot plan")
        for dest in (*files, record, synced):
            self.assertIn(f"would write: {dest}\n", plan)
        self.assertIn("would run: rpm-ostree initramfs-etc " + " ".join(f"--track={f}" for f in files) + "\n", plan)
        self.assertIn("would run: systemctl enable btrfs-degraded-boot-record.service\n", plan)
        self.assertIn(f"self-test: runs at install; lsblk lists 2 member(s) of {uuid}, btrfs counts 2\n", plan)
        self.assertTrue(plan.endswith("plan: nothing was changed\n"), plan)
        self.assertEqual(step(exists), "")
        self.assertEqual(step(f"cat {w}/tracked"), "")
        self.assertEqual([x for x in calls() if x.startswith(("rpm-ostree initramfs-etc", "udevadm control"))], [])
        self.assertNotEqual(
            exec_bash(c, "systemctl is-enabled btrfs-degraded-boot-record.service", check=False).returncode, 0
        )

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
        again = step("/opt/cst-rw/cst btrfs degraded-boot plan")
        self.assertNotIn("would ", again)
        self.assertIn("unchanged: btrfs-degraded-boot-record.service enabled\n", again)
        self.assertIn("unchanged: initramfs-etc (tracked, next-boot overlay in sync)\n", again)

        sums = f"sha256sum {' '.join(files)} {record} {synced}"
        before = step(sums)
        n = len(calls())
        status = step("/opt/cst-rw/cst btrfs degraded-boot status")
        self.assertIn(
            "verdict:          OK - an NVMe failure at boot would mount degraded after 45 s and alert", status
        )
        self.assertEqual(step(sums), before)
        self.assertEqual([x for x in calls()[n:] if x.startswith(("rpm-ostree initramfs-etc", "udevadm control"))], [])

        # verify reads a unit's drop-ins with it; an unknown key there is only
        # a warning unless the script passes --recursive-errors=no.
        with self.subTest("a typo in a unit stops the install"):
            step(f"mkdir -p {typo} && printf '[Service]\\nTimeoutStratSec=5\\n' >{typo}/e2e-typo.conf")
            n = len(calls())
            bad = exec_bash(c, f". {w}/fakes.bash\n/opt/cst-rw/cst btrfs degraded-boot", check=False)
            self.assertNotEqual(bad.returncode, 0, bad.stdout)
            self.assertIn("Unknown key 'TimeoutStratSec'", bad.stdout + bad.stderr)
            self.assertIn("ERROR: systemd-analyze verify failed", bad.stdout + bad.stderr)
            self.assertNotIn("self-test:", bad.stdout)
            self.assertEqual([x for x in calls()[n:] if x.startswith("rpm-ostree initramfs-etc")], [])
        step(f"rm -rf {typo} && systemctl daemon-reload")

        gone = step("/opt/cst-rw/cst btrfs degraded-boot uninstall")
        self.assertIn("rpm-ostree initramfs-etc " + " ".join(f"--untrack={f}" for f in files), calls())
        self.assertIn("Uninstalled (initramfs: from the next deployment on).", gone)
        self.assertEqual(step(exists), "")
        self.assertEqual(step(f"cat {w}/tracked"), "")
        self.assertNotEqual(
            exec_bash(c, "systemctl is-enabled btrfs-degraded-boot-record.service", check=False).returncode, 0
        )

    # systemd-analyze security's exposure of every service CST installs or
    # generates, on its 0-100 scale (--threshold takes it so: 25 = 2.5). Only
    # goes down: a hardened unit lowers its line in the same change.
    SECURITY_CEILINGS: dict[str, int] = {
        "btrfs-bulk-boot.service": 94,
        "btrfs-degraded-boot-record.service": 94,
        "btrfs-health-check.service": 94,
        "btrfs-last-resort@e2e.service": 94,
        "btrfs-scrub-weekly.service": 94,
        "btrfs-snapshot-nas.service": 94,
        "btrfs-snapshot.service": 94,
        "btrfs-stall-alert.service": 94,
        "btrfs-stall-monitor.service": 25,
        "btrfs-subvol-convert.service": 94,
        "cpu-game.service": 94,
        "cpu-game-watchdog.service": 94,
        "cst-vm-backup@e2e.service": 94,
        "docker-gc-recorder.service": 21,
        "docker-gc.service": 21,
        "docker-volumes-nocow.service": 94,
    }

    def test_16_units_pass_systemd_analyze_verify_and_security(self) -> None:
        """Every unit CST installs (the manifest, staged by the real installer
        as in test_11) or generates (tests/unit_files.py: cst-vm, cst-toolchains,
        the LDAC watcher, the degraded-boot setup) passes `systemd-analyze
        verify` with no output, under --root of the stage so that ExecStart=
        paths resolve to the staged programs. A command a unit names outside
        /usr/local gets an empty executable in the stage only when the real one
        exists here, so a wrong path still fails. User units are checked by the
        system manager's parser (verify has no --root for --user). Then
        `systemd-analyze security --offline` of every service stays at or under
        its SECURITY_CEILINGS entry (lower the entry when a unit gets better; a
        score under it does not fail, so a systemd update in the image that
        rates a unit better does not turn tier 1 red). Drop-ins (below, machine.slice, user@.service) are not
        units of their own and are not verified here."""
        c = self.SYSTEMD_CONTAINER
        stage = "/var/tmp/e2e-units"
        # Commands of packages the tier 1 image does not install: staged even so.
        not_in_image = "/usr/sbin/btrfs"
        self.addCleanup(exec_bash, c, f"rm -rf {stage}", check=False)
        installer = "/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
        sets = (
            "--set UUID=00000000-0000-0000-0000-000000000000 --set OPTIONS=subvolid=5 --set USER=tester "
            "--set ALLOWED=AllowedCPUs=0"
        )
        exec_bash(
            c,
            f"""
            rm -rf {stage} && install -d -m 0755 {stage}
            {installer} install --root {stage} --owner root {sets} >/dev/null
            /usr/bin/python3 -I -B /opt/cst-rw/tests/unit_files.py write {stage} >/dev/null
            cd {stage}/etc/systemd
            grep -rhoE '^Exec[A-Za-z]*=[-@:+!|]*/[^ ]+' system user | sed -E 's/^[^=]+=[-@:+!|]*//' | sort -u |
            while read -r p; do
                case "$p" in /usr/local/*) continue ;; esac
                if [[ ( -x "$p" || " {not_in_image} " == *" $p "* ) && ! -e "{stage}$p" ]]; then
                    install -d "{stage}$(dirname "$p")" && install -m 0755 /dev/null "{stage}$p"
                fi
            done
            for f in user/*; do cp "$f" "system/user-check-$(basename "$f")"; done
            """,
        )
        names = exec_bash(
            c,
            f"cd {stage}/etc/systemd/system && for f in *; do [[ -f $f ]] || continue; "
            'n="$f"; [[ "$n" == *@.* ]] && n="${n/@./@e2e.}"; echo "$n"; done',
        ).stdout.split()
        self.assertIn("btrfs-snapshot.service", names)
        self.assertIn("cst-vm-backup@e2e.service", names)
        self.assertIn("user-check-cst-bt-ldac48.service", names)
        verify = f"systemd-analyze verify --man=no --recursive-errors=no --root={stage} "
        v = exec_bash(c, verify + " ".join(shlex.quote(n) for n in names), check=False)
        self.assertEqual((v.returncode, v.stdout + v.stderr), (0, ""))
        services = sorted(n for n in names if n.endswith(".service") and not n.startswith("user-check-"))
        self.assertEqual(services, sorted(self.SECURITY_CEILINGS), "every service has a SECURITY_CEILINGS entry")
        for name in services:
            with self.subTest(unit=name):
                ceiling = self.SECURITY_CEILINGS[name]
                s = exec_bash(
                    c,
                    f"systemd-analyze security --offline=yes --root={stage} --threshold={ceiling} {shlex.quote(name)}",
                    check=False,
                )
                self.assertEqual(s.returncode, 0, f"exposure above {ceiling / 10}:\n{s.stdout[-600:]}")

    def test_14_stall_monitor_unit_keeps_queued_alerts_across_a_restart(self) -> None:
        """btrfs-stall-monitor.service as shipped (read from the checkout), only
        its daemon swapped for a sleep and Nice=-5 dropped (see
        stub_stall_monitor_unit): a file in its RuntimeDirectory, where the
        daemon queues alerts for btrfs-stall-alert.service, survives
        `systemctl restart` (RuntimeDirectoryPreserve=restart; with the default
        a restart deleted it), and `stop` removes the folder."""
        shipped = docker_exec(
            self.SYSTEMD_CONTAINER, ["cat", "/opt/cst/btrfs/units/btrfs-stall-monitor.service"]
        ).stdout
        stub = stub_stall_monitor_unit(shipped)
        unit = "e2e-stall-monitor.service"
        path = f"/etc/systemd/system/{unit}"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"systemctl stop {unit}; rm -f {path}; systemctl daemon-reload; systemctl reset-failed {unit}",
            check=False,
        )
        docker_exec(self.SYSTEMD_CONTAINER, ["bash", "-c", f"cat >{path}"], input_text=stub)
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            diag() {{ systemctl status --no-pager {unit} >&2 || true; journalctl -q --no-pager -u {unit} -n 30 >&2 || true; }}
            d=/run/btrfs-stall-monitor
            systemctl daemon-reload
            # Type=simple: start returns at the fork, and the stub runs (or fails) after it. Running, and
            # never restarted: a failing stub would also keep the folder (Restart=on-failure + Preserve).
            running() {{
                for _ in $(seq 1 20); do
                    [ "$(systemctl show -p ActiveState --value {unit})" = active ] && [ -d "$d" ] && break
                    sleep 0.5
                done
                [ "$(systemctl show -p ActiveState --value {unit})" = active ] && [ -d "$d" ] &&
                    [ "$(systemctl show -p NRestarts --value {unit})" = 0 ] ||
                    {{ echo "$1: the unit derived from the shipped one is not running here" >&2; diag; exit 1; }}
            }}
            systemctl start {unit} || {{ diag; exit 1; }}
            running "after start"
            touch "$d/alert-e2e.txt"
            systemctl restart {unit} || {{ diag; exit 1; }}
            running "after restart"
            [ -e "$d/alert-e2e.txt" ] || {{ echo "restart removed the queued alert in $d" >&2; ls -la "$d" >&2 || true; exit 1; }}
            systemctl stop {unit}
            [ ! -e "$d" ] || {{ echo "stop left $d" >&2; ls -la "$d" >&2; exit 1; }}
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_15_toolchains_auto_off_forgets_the_timer_stamp(self) -> None:
        """systemd keeps the last run of a Persistent= timer (for a user timer
        ~/.local/share/systemd/timers/stamp-NAME; made when the timer starts,
        touched at each run) after the timer is gone. `auto off` removes it
        (systemctl --user clean --what=state, before the unit file goes).
        No prune runs here: `auto on` starts the timer, and starting a
        Persistent= timer that never ran makes its stamp (systemd's
        timer_start). The guard below fails loudly if it did not."""
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            """
            stamp=/home/tester/.local/share/systemd/timers/stamp-cst-toolchains-prune.timer
            /opt/cst-rw/cst toolchains auto on >/dev/null
            systemctl --user is-active cst-toolchains-prune.timer
            if [ ! -e "$stamp" ]; then
                echo "no $stamp after auto on started the timer: the check below would prove nothing" >&2
                ls -la /home/tester/.local/share/systemd/timers >&2 || true
                exit 1
            fi
            /opt/cst-rw/cst toolchains auto off
            if [ -e "$stamp" ]; then echo "auto off left $stamp" >&2; exit 1; fi
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_17_snapshots_conf_written_by_an_older_cst_is_rewritten_in_quotes(self) -> None:
        """An older CST wrote snapshots.conf values with printf %q (a backslash
        before a blank, $'...').
        The real `cst btrfs snapshots nas disable` (as root, the real systemctl)
        rewrites them in quotes with the values bash read from the old file,
        the cst_core reader then finds no problem, and a second run leaves the
        file as it is (same inode, same time)."""
        conf = "/etc/btrfs-setup/snapshots.conf"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"rm -f {conf}; if [ -e /var/tmp/e2e-snapconf.bak ]; then mv /var/tmp/e2e-snapconf.bak {conf}; fi",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            conf={conf}
            mkdir -p /etc/btrfs-setup
            if [ -e "$conf" ]; then cp -a "$conf" /var/tmp/e2e-snapconf.bak; fi
            # The old writer's form, made by bash's own printf %q.
            {{
                echo "# Managed by btrfs-setup.sh (CST); read by /usr/local/sbin/btrfs-snapshot."
                printf 'NAS_ENABLED=%q\\nNAS_PATH=%q\\nNAS_RESTIC_HOST=%q\\nNAS_USER=%q\\n' \\
                    1 '/volume1/My Backups' "host #1 it's" "$(printf 'a\\tb')"
            }} >"$conf"
            grep -qF 'NAS_PATH=/volume1/My\\ Backups' "$conf"
            values() {{ (. "$conf"; printf '%s|' "$NAS_ENABLED" "$NAS_PATH" "$NAS_RESTIC_HOST" "$NAS_USER"); }}
            before="$(values)"
            /opt/cst-rw/cst btrfs snapshots nas disable
            after="$(values)"
            [ "$after" = "0|${{before#1|}}" ] || {{ echo "values changed: $before -> $after" >&2; exit 1; }}
            grep -qx "NAS_PATH='/volume1/My Backups'" "$conf"
            grep -qx 'NAS_RESTIC_HOST="host #1 it'"'"'s"' "$conf"
            if grep -q '\\\\' "$conf"; then echo "a backslash is left:" >&2; cat "$conf" >&2; exit 1; fi
            python3 -I -B -c '
import sys
sys.path.insert(0, "/opt/cst-rw")
from cst_core.domain.text import parse_shell_assignments
got = parse_shell_assignments(open(sys.argv[1], encoding="utf-8").read())
assert got.problems == (), got.problems
assert got.values["NAS_PATH"] == "/volume1/My Backups", got.values
' "$conf"
            st="$(stat -c '%i %y' "$conf")"
            cp "$conf" /var/tmp/e2e-snapconf.first
            /opt/cst-rw/cst btrfs snapshots nas disable
            [ "$(stat -c '%i %y' "$conf")" = "$st" ] || {{ echo "the second run wrote the file" >&2; exit 1; }}
            cmp "$conf" /var/tmp/e2e-snapconf.first
            rm -f /var/tmp/e2e-snapconf.first
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_18_a_masked_unit_stays_masked(self) -> None:
        """`systemctl mask` leaves a link to /dev/null where the unit file goes.
        `cst vm backup-timer on` (root, the system manager) and `cst toolchains
        auto on` (tester, the user manager) refuse, write nothing and leave the
        unit masked for the real systemctl; status says masked. Once unmasked,
        `backup-timer on` works and `off` removes everything again."""
        w = "/var/tmp/e2e-vm-mask"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            "systemctl unmask cst-vm-backup@e2emask.timer; systemctl disable --now cst-vm-backup@e2emask.timer; "
            f"rm -rf {w} /etc/systemd/system/cst-vm-backup@.service /etc/systemd/system/cst-vm-backup@.timer; "
            "systemctl daemon-reload",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            w={w}
            rm -rf "$w" && mkdir -p "$w/sbin"
            printf 'NAS_ENABLED=1\nNAS_PATH=/x\n' >"$w/nas.conf"
            printf 'secret\n' >"$w/pw" && chmod 600 "$w/pw"
            export CST_PROG="cst vm" CST_VM_NAS_CONF="$w/nas.conf" CST_VM_RESTIC_PASSWORD="$w/pw" \
                CST_VM_RESTIC_CACHE="$w/cache" CST_VM_SBIN_COPY="$w/sbin/cst-vm" CST_VM_INDEX="$w/index.json"
            tool=/opt/cst-rw/vm/cst-vm
            unit=cst-vm-backup@e2emask.timer
            systemctl mask "$unit"
            [ "$(readlink "/etc/systemd/system/$unit")" = /dev/null ] || {{ echo "mask made no link to /dev/null" >&2; exit 1; }}
            rc=0
            /usr/bin/python3 -I "$tool" backup-timer on e2emask >"$w/on.out" 2>&1 || rc=$?
            [ "$rc" -ne 0 ] || {{ echo "on did not refuse a masked unit" >&2; cat "$w/on.out" >&2; exit 1; }}
            grep -qF "$unit is masked (systemctl mask); unmask it first: sudo systemctl unmask $unit" "$w/on.out"
            for f in /etc/systemd/system/cst-vm-backup@.service /etc/systemd/system/cst-vm-backup@.timer "$w/sbin/cst-vm"; do
                if [ -e "$f" ]; then echo "on wrote $f" >&2; exit 1; fi
            done
            [ "$(systemctl is-enabled "$unit" || true)" = masked ]
            # grep reads all of it: status gets SIGPIPE back at its default, and pipefail would see it.
            /usr/bin/python3 -I "$tool" backup-timer status e2emask | grep -F "/etc/systemd/system/$unit: masked" >/dev/null
            systemctl unmask "$unit"
            /usr/bin/python3 -I "$tool" backup-timer on e2emask >/dev/null
            systemctl is-enabled "$unit"
            /usr/bin/python3 -I "$tool" backup-timer off e2emask >/dev/null
            [ ! -e /etc/systemd/system/cst-vm-backup@.timer ] && [ ! -e "$w/sbin/cst-vm" ]
            echo E2E-OK-VM
            """,
        ).stdout
        self.assertIn("E2E-OK-VM", out)
        self.addCleanup(
            as_tester_bash,
            self.SYSTEMD_CONTAINER,
            "systemctl --user unmask cst-toolchains-prune.timer; rm -f /home/tester/.config/cst/toolchains-auto.off",
            check=False,
        )
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            """
            d=/home/tester/.config/systemd/user
            /opt/cst-rw/cst toolchains auto off >/dev/null 2>&1 || true
            systemctl --user mask cst-toolchains-prune.timer
            [ "$(readlink "$d/cst-toolchains-prune.timer")" = /dev/null ]
            rc=0
            /opt/cst-rw/cst toolchains auto on >/tmp/e2e-tc-mask.out 2>&1 || rc=$?
            [ "$rc" -ne 0 ] || { echo "auto on did not refuse a masked unit" >&2; cat /tmp/e2e-tc-mask.out >&2; exit 1; }
            grep -qF "cst-toolchains-prune.timer is masked (systemctl mask); unmask it first: systemctl --user unmask cst-toolchains-prune.timer" /tmp/e2e-tc-mask.out
            if [ -e "$d/cst-toolchains-prune.service" ]; then echo "auto on wrote the service" >&2; exit 1; fi
            [ "$(systemctl --user is-enabled cst-toolchains-prune.timer || true)" = masked ]
            /opt/cst-rw/cst toolchains status | grep -F "cst-toolchains-prune.timer masked" >/dev/null
            /opt/cst-rw/cst toolchains auto off >/dev/null 2>&1
            [ "$(readlink "$d/cst-toolchains-prune.timer")" = /dev/null ] || { echo "auto off removed the mask" >&2; exit 1; }
            systemctl --user unmask cst-toolchains-prune.timer
            rm -f /tmp/e2e-tc-mask.out
            echo E2E-OK-TOOLCHAINS
            """,
        ).stdout
        self.assertIn("E2E-OK-TOOLCHAINS", out)

    def test_25_snapshots_chrome_cache_apply_and_off(self) -> None:
        """`cst btrfs snapshots chrome-cache` (as root through sudo's
        variables, the tool itself run as the user by runuser) on Chrome's
        flags file: without the Flatpak, apply writes nothing; `status`
        changes nothing; `apply` adds the marker and the two flags after the
        user's own line, in a file the user owns; a second `apply` leaves the
        file as it is; `off` removes the two flags and keeps a marker that a
        later `apply` respects; a second `off` changes nothing; the marker of
        a version named f44-tools counts as ours (`apply` keeps it, `off`
        removes its flags)."""
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "test_25 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            cst() { SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst "$@"; }
            H=/home/tester
            app_dir=$H/.local/share/flatpak/app/com.google.Chrome
            var_dir=$H/.var/app/com.google.Chrome
            conf=$var_dir/config/chrome-flags.conf
            rel=.var/app/com.google.Chrome/config/chrome-flags.conf
            cleanup() { rm -rf "$app_dir" "$var_dir"; }
            trap cleanup EXIT
            show() { sed "s/^/test_25 $1: /" <<<"$2" >&2; }   # the CI log
            file_state() { stat -c '%i %Y %s %U:%G %a' "$conf"; sha256sum <"$conf"; }
            same() {   # same LABEL BEFORE
                [[ "$(file_state)" == "$2" ]] && return 0
                echo "$1 changed $conf:" >&2
                cat "$conf" >&2
                return 1
            }
            cc() {   # cc ACTION: the command's output (also to the CI log)
                local out
                out="$(cst btrfs snapshots chrome-cache "$1" 2>&1)" || { show "$1" "$out"; return 1; }
                show "$1" "$out"
                printf '%s\n' "$out"
            }
            no_flags() {
                if grep -qE '^--disk-cache-(dir|size)=' "$conf"; then echo "cache flags left:" >&2; cat "$conf" >&2; return 1; fi
            }
            [[ ! -e "$app_dir" && ! -e "$var_dir" ]]

            # 0. Chrome's Flatpak not installed: nothing written.
            out="$(cc apply)"
            grep -qxF 'kept:      Chrome Flatpak not installed: nothing to do' <<<"$out"
            [[ ! -e "$var_dir" ]]

            # Chrome installed for the user (the folder flatpak makes), and a flag line of the user's own.
            runuser -u tester -- mkdir -p "$app_dir/current/active" "$(dirname "$conf")"
            runuser -u tester -- sh -c 'printf "%s\n" --enable-features=E2eUserFlag >"$1"' sh "$conf"

            # 1. status changes nothing.
            before="$(file_state)"
            out="$(cc status)"
            grep -qxF '  Chrome Flatpak:        installed (user)' <<<"$out"
            grep -qF "absent; 'snapshots setup' (or 'snapshots chrome-cache apply') adds them" <<<"$out"
            same status "$before"

            # 2. apply: the user's line first, then the marker and the two flags; the file stays the user's.
            out="$(cc apply)"
            grep -qxF "written:   ~/$rel: add the cache flags (sandbox /tmp, at most 512 MiB)" <<<"$out"
            [[ "$(head -n1 "$conf")" == --enable-features=E2eUserFlag ]]
            grep -q '^# btrfs-setup.sh (CST) chrome-cache: ' "$conf"
            grep -qx -- '--disk-cache-dir=/tmp/chrome-cache' "$conf"
            grep -qx -- '--disk-cache-size=536870912' "$conf"
            [[ "$(wc -l <"$conf")" == 4 ]]
            [[ "$(stat -c %U:%G "$conf")" == tester:tester ]]
            grep -qxF '                         flags present (managed by CST)' <<<"$(cc status)"

            # 3. A second apply leaves the file as it is.
            applied="$(file_state)"
            out="$(cc apply)"
            grep -qxF "kept:      ~/$rel: flags present (managed by CST)" <<<"$out"
            same "a second apply" "$applied"

            # 4. off: the flags go, the user's line stays, an "off" marker stays.
            out="$(cc off)"
            grep -qxF "written:   ~/$rel: off: our flag lines removed (your other lines kept); setup will not add them again" \
                <<<"$out"
            no_flags
            [[ "$(head -n1 "$conf")" == --enable-features=E2eUserFlag ]]
            grep -q '^# btrfs-setup.sh (CST) chrome-cache off: ' "$conf"
            [[ "$(wc -l <"$conf")" == 2 ]]
            [[ "$(stat -c %U:%G "$conf")" == tester:tester ]]

            # 5. A second off, and an apply after off, change nothing.
            off_state="$(file_state)"
            out="$(cc off)"
            grep -qxF "kept:      ~/$rel: already off: nothing changed" <<<"$out"
            same "a second off" "$off_state"
            out="$(cc apply)"
            grep -qxF "kept:      ~/$rel: off by your choice (marker without the flags): nothing added" <<<"$out"
            same "apply after off" "$off_state"

            # 6. Upgrade: the flags under the marker of a version named f44-tools are ours.
            runuser -u tester -- sh -c 'printf "%s\n" --enable-features=E2eUserFlag \
                "# btrfs-setup.sh (f44-tools) chrome-cache: HTTP cache in the sandbox /tmp (RAM, emptied at reboot), at most 512 MiB." \
                --disk-cache-dir=/tmp/chrome-cache --disk-cache-size=536870912 >"$1"' sh "$conf"
            old="$(file_state)"
            out="$(cc apply)"
            grep -qxF "kept:      ~/$rel: flags present (managed by CST)" <<<"$out"
            same "apply on the f44-tools marker" "$old"
            out="$(cc off)"
            grep -qF "written:   ~/$rel: off: our flag lines removed" <<<"$out"
            no_flags
            [[ "$(head -n1 "$conf")" == --enable-features=E2eUserFlag ]]
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_26_btrfs_watch_saves_the_report_of_a_run(self) -> None:
        """`cst btrfs watch` as the user, with the installed btrfs-stall-report
        and an empty recording folder (a recorder that wrote nothing yet):
        usage errors exit 2 and `--list` says no runs, without the report
        program it exits 1 and records nothing; a run passes on the
        command's exit status (3), keeps its output in workload.log (and on
        the terminal), saves the report of its window (NO DATA here, after the
        wait for the recorder runs out) as report.txt and report.json, and
        adds a line to runs.tsv that `--list` shows."""
        work = "/var/tmp/e2e-watch"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"""
            if [ -e {work}/made-report ]; then rm -f /usr/local/bin/btrfs-stall-report; fi
            if [ -e {work}/made-log ]; then rmdir /var/log/btrfs-stall-monitor; fi
            rm -rf {work}
            """,
            check=False,
        )
        exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            rm -rf {work} && install -d -m 0755 {work}
            if [ ! -e /usr/local/bin/btrfs-stall-report ]; then
                touch {work}/made-report
                /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install btrfs/btrfs-stall-report >/dev/null
            fi
            if [ ! -d /var/log/btrfs-stall-monitor ]; then
                touch {work}/made-log
                # As the unit's LogsDirectory= makes it (Group=wheel, mode 0750); tester is in wheel.
                install -d -m 0750 -o root -g wheel /var/log/btrfs-stall-monitor
            fi
            install -d -m 0777 {work}/state
            """,
        )
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "test_26 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C XDG_STATE_HOME=/var/tmp/e2e-watch/state
            base=$XDG_STATE_HOME/btrfs-stall-watch
            cst=/opt/cst-rw/cst
            show() { sed "s/^/test_26 $1: /" <<<"$2" >&2; }   # the CI log
            fails() {   # fails RC CMD...: CMD exits RC; its output on stdout
                local want="$1" rc=0 out
                shift
                out="$("$@" 2>&1)" || rc=$?
                [[ "$rc" == "$want" ]] || { echo "exit $rc, not $want: $*" >&2; printf '%s\n' "$out" >&2; return 1; }
                printf '%s\n' "$out"
            }

            # 1. Usage errors and --list before any run: nothing recorded.
            grep -qF 'cst btrfs watch [-l LABEL] -- command [args...]' <<<"$(fails 2 "$cst" btrfs watch)"
            grep -qxF 'Unknown option: --bogus (use -- before the command)' <<<"$(fails 2 "$cst" btrfs watch --bogus)"
            grep -qxF 'Invalid duration: soon' <<<"$(fails 2 "$cst" btrfs watch --duration soon)"
            [[ "$("$cst" btrfs watch --list)" == "no runs yet" ]]
            # 2. Without the report program (not on PATH): exit 1, nothing recorded.
            out="$(fails 1 env PATH=/usr/bin:/bin "$cst" btrfs watch -- true)"
            grep -qxF "btrfs-stall-report not found: install with 'cst btrfs install' (on the host)" <<<"$out"
            [[ ! -e "$base" ]]

            # 3. A run.
            before="$(date +%s)"
            out="$(fails 3 "$cst" btrfs watch -l 'e2e label' -- sh -c 'echo e2e-out; echo e2e-err >&2; exit 3')"
            after="$(date +%s)"
            show run "$out"
            [[ "$(wc -l <"$base/runs.tsv")" == 2 ]]
            [[ "$(head -n1 "$base/runs.tsv")" == $'start\tend\texit\tlabel\tdir' ]]
            IFS=$'\t' read -r _ _ r_exit r_label r_dir < <(sed -n 2p "$base/runs.tsv")
            [[ "$r_exit" == 3 && "$r_label" == "e2e label" ]]
            [[ "$r_dir" == "$base"/* && -d "$r_dir" ]]
            grep -qxF e2e-out "$r_dir/workload.log"
            grep -qxF e2e-err "$r_dir/workload.log"
            grep -qxF e2e-out <<<"$out"
            grep -qxF "=== btrfs-stall-report for 'e2e label' ($r_dir) ===" <<<"$out"
            grep -q '^verdict  NO DATA for this window' "$r_dir/report.txt"
            python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
w, c = d["window"], d["coverage"]
lo, hi = float(sys.argv[2]), float(sys.argv[3])
assert lo <= w["since"] <= hi, w
assert w["since"] < w["until"] <= hi + 1, w
assert c["last_sample"] is None and d["samples"] == 0, c
' "$r_dir/report.json" "$before" "$after"
            list="$("$cst" btrfs watch --list)"
            show list "$list"
            grep -qF 'e2e label' <<<"$list"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_35_disable_timers_one_missing_unit_does_not_keep_the_others(self) -> None:
        """btrfs-setup.sh's disable_timers (the snapshots, NAS and docker-gc
        disable paths, and uninstall) with the real systemd: a Persistent=
        timer and a service, both enabled and running, and a unit that does
        not exist in between. Both are disabled and stopped, the timer's
        last run (its stamp) is forgotten, the missing unit says nothing, and
        it returns 0, also when run again. A control first runs the one
        `systemctl disable --now` for all three that it made before: it must
        fail and leave a real unit enabled or running (the problem fixed)."""
        units = "/etc/systemd/system"
        names = ("e2e-dt.timer", "e2e-dt.service", "e2e-dt-other.service")
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"systemctl disable --now {' '.join(names)} 2>/dev/null || true; "
            f"rm -f {' '.join(f'{units}/{n}' for n in names)} /var/lib/systemd/timers/stamp-e2e-dt.timer; "
            "systemctl daemon-reload",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "test_35 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            U=/etc/systemd/system
            stamp=/var/lib/systemd/timers/stamp-e2e-dt.timer
            printf '%s\n' '[Unit]' 'Description=e2e disable_timers' '[Service]' 'Type=oneshot' 'ExecStart=/bin/true' \
                >"$U/e2e-dt.service"
            printf '%s\n' '[Unit]' 'Description=e2e disable_timers' '[Timer]' 'OnCalendar=yearly' 'Persistent=true' \
                '[Install]' 'WantedBy=timers.target' >"$U/e2e-dt.timer"
            printf '%s\n' '[Unit]' 'Description=e2e disable_timers' '[Service]' 'ExecStart=/bin/sleep infinity' \
                '[Install]' 'WantedBy=multi-user.target' >"$U/e2e-dt-other.service"
            systemctl daemon-reload
            systemctl enable --now e2e-dt.timer e2e-dt-other.service
            systemctl is-active -q e2e-dt.timer
            systemctl is-active -q e2e-dt-other.service
            if [[ ! -e "$stamp" ]]; then
                echo "no $stamp after the timer started: the check below would prove nothing" >&2
                exit 1
            fi
            # The two functions as btrfs-setup.sh defines them.
            eval "$(sed -n '/^forget_timer_state() {/,/^}/p; /^disable_timers() {/,/^}/p' /opt/cst-rw/btrfs-setup.sh)"
            declare -F disable_timers forget_timer_state >/dev/null
            UNIT_DIR="$U"
            # Control: the one call for all three that disable_timers made before.
            if systemctl disable --now e2e-dt.timer e2e-dt-missing.timer e2e-dt-other.service 2>/dev/null; then
                echo "one disable for all three did not fail on the missing unit" >&2
                exit 1
            fi
            left=0
            for u in e2e-dt.timer e2e-dt-other.service; do
                if [[ "$(systemctl is-enabled "$u" || true)" == enabled ]] || systemctl is-active -q "$u"; then left=1; fi
            done
            (( left )) || { echo "one disable for all three still disabled and stopped both: no old problem" >&2; exit 1; }
            systemctl enable --now e2e-dt.timer e2e-dt-other.service
            err="$(disable_timers e2e-dt.timer e2e-dt-missing.timer e2e-dt-other.service 2>&1 >/dev/null)"
            [[ -z "$err" ]] || { echo "disable_timers said: $err" >&2; exit 1; }
            for u in e2e-dt.timer e2e-dt-other.service; do
                state="$(systemctl is-enabled "$u" || true)"
                [[ "$state" == disabled ]] || { echo "$u is $state" >&2; exit 1; }
                if systemctl is-active -q "$u"; then echo "$u still active" >&2; exit 1; fi
            done
            [[ ! -e "$stamp" ]] || { echo "the timer's last run was not forgotten" >&2; exit 1; }
            # A second run: units already disabled are no failure either.
            err="$(disable_timers e2e-dt.timer e2e-dt-missing.timer e2e-dt-other.service 2>&1 >/dev/null)"
            [[ -z "$err" ]] || { echo "a second disable_timers said: $err" >&2; exit 1; }
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_80_bluetooth_second_apply_and_le_audio_cycle(self) -> None:
        """bluetooth as tester with the real user manager, WirePlumber and
        libwireplumber. `apply --no-restart` writes main.conf, the LDAC rule,
        the LE Audio drop-in and the LDAC watcher; `plan` then has nothing to
        do (exit 0). A second `apply` (restarts allowed) changes nothing: the
        same inode, mtime and content of each file, no new main.conf backup,
        no restart of WirePlumber. `le-audio` alone only shows the setting.
        `le-audio on` writes LE_AUDIO=on (the other lines kept), removes the
        drop-in and restarts WirePlumber, whose roles are then the PipeWire
        defaults; nothing else changes and `plan` exits 0. A second
        `le-audio on` writes nothing and restarts nothing. `le-audio off`
        puts the same drop-in back, restarts WirePlumber and the roles are the
        classic ones again. `revert` afterwards."""
        self.addCleanup(
            as_tester_bash,
            self.SYSTEMD_CONTAINER,
            "/opt/cst-rw/cst bluetooth revert --yes --no-restart >/dev/null 2>&1 || true; "
            "rm -f /home/tester/.config/cst/bluetooth.conf",
            check=False,
        )
        exec_bash(
            self.SYSTEMD_CONTAINER,
            "mkdir -p /etc/bluetooth && printf '[General]\\n# Name = BlueZ\\n' >/etc/bluetooth/main.conf",
        )
        as_tester_bash(
            self.SYSTEMD_CONTAINER,
            "mkdir -p /home/tester/.config/cst && printf 'DEVICE=00:11:22:33:44:55\\nLDAC_QUALITY=auto\\n"
            "LDAC_RATE=48000\\nLE_AUDIO=off\\n' >/home/tester/.config/cst/bluetooth.conf",
        )
        out = as_tester_bash(self.SYSTEMD_CONTAINER, BT_SECOND_APPLY_SCRIPT).stdout
        self.assertIn("E2E-OK", out)

    def test_73_setup_scripts_route_as_real_root(self) -> None:
        """vm-setup.sh and toolchains-setup.sh started as root (as sudo starts them), from a
        scratch copy with fake programs: vm runs its program directly as root with CST_PROG;
        toolchains steps down to SUDO_USER through the real runuser (tester's HOME, USER,
        XDG_RUNTIME_DIR; SUDO_USER and XDG_CONFIG_HOME dropped); without a SUDO_USER (or
        with root) it refuses with exit 1, and with an unknown SUDO_USER too; vm's root commands
        refuse in a toolbox; the help of both works as root."""
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            d=$(mktemp -d /var/tmp/e2e-route.XXXXXX)
            trap 'rm -rf "$d"' EXIT
            cp /opt/cst-rw/vm-setup.sh /opt/cst-rw/toolchains-setup.sh "$d/"
            cp -a /opt/cst-rw/cst_core "$d/"
            find "$d/cst_core" -name __pycache__ -prune -exec rm -rf {} +
            mkdir "$d/vm" "$d/toolchains"
            fake='import os, sys
e = os.environ
print("RAN", os.geteuid(), e.get("HOME"), e.get("USER"), e.get("LOGNAME"), e.get("XDG_RUNTIME_DIR"),
      e.get("CST_PROG"), "SUDO_USER" in e, "XDG_CONFIG_HOME" in e, *sys.argv[1:], sep="|")'
            printf '%s\n' "$fake" >"$d/vm/cst-vm"
            printf '%s\n' "$fake" >"$d/toolchains/cst-toolchains"
            chmod -R a+rX "$d"
            cd /
            uid=$(id -u tester)
            got=$(env -u USER -u LOGNAME -u XDG_RUNTIME_DIR HOME=/root CST_PROG="cst vm" CST_VM_CONTAINER=0 \
                bash "$d/vm-setup.sh" backup w --full)
            [[ "$got" == "RAN|0|/root|None|None|None|cst vm|False|False|backup|w|--full" ]] \
                || { echo "vm as root: $got" >&2; exit 1; }
            got=$(SUDO_USER=tester XDG_CONFIG_HOME=/root/.config CST_PROG="cst toolchains" \
                bash "$d/toolchains-setup.sh" status --brief)
            [[ "$got" == "RAN|$uid|/home/tester|tester|tester|/run/user/$uid|cst toolchains|False|False|status|--brief" ]] \
                || { echo "toolchains as root: $got" >&2; exit 1; }
            got=$(env -u CST_PROG SUDO_USER=tester bash "$d/toolchains-setup.sh" status)
            [[ "$got" == "RAN|$uid|/home/tester|tester|tester|/run/user/$uid|$d/toolchains-setup.sh|False|False|status" ]] \
                || { echo "toolchains as root, no CST_PROG: $got" >&2; exit 1; }
            for who in "" root; do
                rc=0
                got=$(SUDO_USER="$who" CST_PROG="cst toolchains" bash "$d/toolchains-setup.sh" status 2>&1) || rc=$?
                [[ $rc == 1 && "$got" == "cst toolchains: ERROR: run it from your user account (needs SUDO_USER): toolchain caches and settings belong to your user" ]] \
                    || { echo "toolchains, SUDO_USER='$who': exit $rc: $got" >&2; exit 1; }
            done
            rc=0
            got=$(CST_PROG="cst vm" CST_VM_CONTAINER=1 bash "$d/vm-setup.sh" backup w 2>&1) || rc=$?
            [[ $rc == 1 && "$got" == *"'cst vm backup w' must run on the host, not inside a toolbox/container."* ]] \
                || { echo "vm in a toolbox: exit $rc: $got" >&2; exit 1; }
            rc=0
            got=$(SUDO_USER=e2e-no-such-user CST_PROG="cst toolchains" bash "$d/toolchains-setup.sh" status 2>&1) || rc=$?
            [[ $rc == 1 && "$got" == "cst toolchains: ERROR: SUDO_USER=e2e-no-such-user: no such user" ]] \
                || { echo "toolchains, unknown SUDO_USER: exit $rc: $got" >&2; exit 1; }
            for m in vm toolchains; do
                rc=0
                got=$(CST_PROG="cst $m" bash "$d/$m-setup.sh" help) || rc=$?
                [[ $rc == 0 && "$got" == *"Usage: cst $m <command> [args]" ]] \
                    || { echo "$m help as root: exit $rc: $got" >&2; exit 1; }
            done
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_74_vm_root_commands_refuse_in_a_real_container(self) -> None:
        """In a container with /.dockerenv (no override), cst vm's commands that change the
        host refuse before sudo, with the host command to type (cst is not on PATH there:
        it names itself /opt/cst/cst)."""
        for subcmd in ("backup w --nas", "tune apply w", "nocow apply", "backup-timer on w"):
            p = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *self.SECURITY_OPTS,
                    "-e",
                    "E2E_KEEP_DOCKERENV=1",
                    "-v",
                    f"{self.CST_ROOT_MOUNT}:/opt/cst:ro",
                    "--entrypoint",
                    "bash",
                    self.IMAGE,
                    "-c",
                    f"/opt/cst/cst vm {subcmd}",
                ],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertEqual(p.stdout, "")
            self.assertEqual(
                p.stderr,
                f"'/opt/cst/cst vm {subcmd}' must run on the host, not inside a toolbox/container.\n"
                f"In a host terminal: /opt/cst/cst vm {subcmd}\n",
            )
