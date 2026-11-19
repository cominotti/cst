# SPDX-License-Identifier: MPL-2.0
"""Docker Layer 1 E2E suite: unprivileged systemd PID 1 + user manager + SFTP sidecar."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import shlex
import subprocess
from typing import ClassVar, override
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
    r"""export E2E_CSUM=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee

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
    r"""D="/ostree/deploy/e2e/deploy/$E2E_CSUM.0"
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


# tests/e2e test_88, test_90: cst swap as root on the container's real /etc and
# systemd, with the kernel's side in a scratch tree (CST_SWAP_PROC, CST_SWAP_SYS:
# the container's /proc/sys and /sys are read-only, and they are the host
# kernel's), stand-ins for the commands that touch a disk or the kernel
# (blockdev, blkid, modprobe, sysctl, swapon, zramctl) and device nodes that no
# disk has (mknod: a container may create them, not open them). /proc/swaps
# says the mapper is in use (2 MiB), so apply never starts the real
# cryptsetup and swap units. systemctl and systemd-escape are the real ones.
SWAP_E2E_DIR = "/var/tmp/e2e-swap"
SWAP_E2E_LIB = (
    f"W={SWAP_E2E_DIR}\n"
    r"""PU=0e2e5a5a-1111-4222-8333-944455556666
SIZE=1073741824
CST=/opt/cst-rw/cst
SRC=/opt/cst-rw/swap
DROPIN_DIR=/etc/systemd/system/machine.slice.d
REC=/var/lib/cst/installed.json
export PATH="$W/bin:$PATH" CST_SWAP_PROC="$W/proc" CST_SWAP_SYS="$W/sys"

# swap_setup works in three parts, and swap_cleanup (the caller's EXIT trap, set before it) undoes
# only what the parts that ran changed: (1) the checks that can fail, before anything outside $W
# changes; (2) the originals saved into $W/orig, $W/orig/.saved last; (3) the changes. Without
# .saved, swap_cleanup removes $W and nothing else.
swap_setup() {
    cpu_dropin_added=""
    rm -rf "$W"
    mkdir -p "$W/bin" "$W/orig" "$W/proc/sys/vm" "$W/proc/pressure" "$W/sys/module/zswap/parameters" \
        "$W/sys/class/block/e2e-swap0" "$W/sys/class/block/e2e-dm0/dm"
    # 1. The checks.
    [[ ! -e /dev/e2e-swap0 && ! -e /dev/e2e-dm0 && ! -e /dev/mapper/swap0 ]]
    [[ ! -e /etc/cst/swap.conf ]]
    # 2. What swap_cleanup puts back.
    for f in /etc/crypttab /etc/fstab "$REC"; do
        if [[ -e "$f" ]]; then cp -a "$f" "$W/orig/"; fi
    done
    if [[ -e /etc/systemd/zram-generator.conf || -L /etc/systemd/zram-generator.conf ]]; then
        cp -a /etc/systemd/zram-generator.conf "$W/orig/"
    fi
    systemctl is-active machine.slice >"$W/orig/slice-state" || true
    : >"$W/orig/.saved"
    # 3. The changes.
    printf 'name         : zstd\ndriver       : zstd-generic\n' >"$W/proc/crypto"
    printf 'MemTotal:        8000000 kB\nMemAvailable:    6000000 kB\nZswap:                 0 kB\nZswapped:              0 kB\n' >"$W/proc/meminfo"
    printf 'zswpout 0\nzswpin 0\nzswpwb 0\npswpout 0\npswpin 0\n' >"$W/proc/vmstat"
    printf 'Filename\tType\tSize\tUsed\tPriority\n/dev/e2e-dm0 partition 1048572 2048 -2\n' >"$W/proc/swaps"
    echo 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0' >"$W/proc/pressure/memory"
    local p k
    for p in $(sed -n 's|^w /sys/module/zswap/parameters/\([^ ]*\) .*|\1|p' "$SRC/tmpfiles/zswap.conf"); do
        echo N >"$W/sys/module/zswap/parameters/$p"
    done
    for k in swappiness page-cluster watermark_boost_factor watermark_scale_factor; do echo 7 >"$W/proc/sys/vm/$k"; done
    echo $((SIZE / 512)) >"$W/sys/class/block/e2e-swap0/size"
    echo swap0 >"$W/sys/class/block/e2e-dm0/dm/name"
    mkdir -p /dev/disk/by-partuuid /dev/mapper
    mknod /dev/e2e-swap0 b 7 1040
    mknod /dev/e2e-dm0 b 7 1041
    ln -s ../../e2e-swap0 "/dev/disk/by-partuuid/$PU"
    ln -s ../e2e-dm0 /dev/mapper/swap0
    cat >"$W/bin/blockdev" <<EOF
#!/bin/bash
echo "blockdev \$*" >>$W/calls
[[ "\$*" == "--getsize64 /dev/disk/by-partuuid/$PU" ]] && echo $SIZE
EOF
    cat >"$W/bin/sysctl" <<EOF
#!/bin/bash
# sysctl -q -p FILE|- into the scratch /proc/sys
echo "sysctl \$*" >>$W/calls
f="\${@: -1}"
[[ "\$f" == - ]] && f=/dev/stdin
while IFS= read -r line; do
    line="\${line%%#*}"
    [[ "\$line" == *=* ]] || continue
    k="\${line%%=*}"; v="\${line#*=}"
    k="\${k//[[:space:]]/}"; v="\$(echo \$v)"
    printf '%s\n' "\$v" >"$W/proc/sys/\${k//.//}"
done <"\$f"
EOF
    printf '#!/bin/bash\necho "$(basename "$0") $*" >>%s/calls\n' "$W" >"$W/bin/modprobe"
    cp "$W/bin/modprobe" "$W/bin/blkid"
    cp "$W/bin/modprobe" "$W/bin/zramctl"
    printf '#!/bin/bash\necho "NAME TYPE SIZE USED PRIO"\necho "/dev/e2e-dm0 partition 1024M 2M -2"\n' >"$W/bin/swapon"
    chmod 0755 "$W"/bin/*
    # Stopped, so that every swap test starts from the same machine.slice (apply's daemon-reload puts
    # a new MemoryLow into a running slice's memory.low too: test_88 step 7).
    if [[ "$(cat "$W/orig/slice-state")" == active ]]; then systemctl stop machine.slice; fi
    # A cgroup folder an inactive machine.slice left in the container: removed when it is empty, as
    # systemd removes it at a stop, so every swap test starts from the same tree (test_88 plants one).
    if [[ -d /sys/fs/cgroup/machine.slice ]] && ! systemctl -q is-active machine.slice; then
        rmdir /sys/fs/cgroup/machine.slice || { slice_facts; exit 1; }
    fi
    if [[ ! -e "$DROPIN_DIR/10-cpu-setup.conf" ]]; then
        mkdir -p "$DROPIN_DIR"
        cp /opt/cst-rw/cpu/units/machine.slice.d-10-cpu-setup.conf "$DROPIN_DIR/10-cpu-setup.conf"
        cpu_dropin_added=1
    fi
}

swap_cleanup() {
    local f
    if [[ ! -e "$W/orig/.saved" ]]; then
        rm -rf "$W"
        return 0
    fi
    for f in crypttab fstab; do
        rm -f "/etc/$f.pre-swap-config.bak"
        if [[ -e "$W/orig/$f" ]]; then cp -a "$W/orig/$f" "/etc/$f"; else rm -f "/etc/$f"; fi
    done
    if [[ -e "$W/orig/installed.json" ]]; then cp -a "$W/orig/installed.json" "$REC"; else rm -f "$REC"; fi
    rm -f /etc/systemd/zram-generator.conf
    if [[ -e "$W/orig/zram-generator.conf" || -L "$W/orig/zram-generator.conf" ]]; then
        cp -a "$W/orig/zram-generator.conf" /etc/systemd/
    fi
    rm -f /etc/tmpfiles.d/zswap.conf /etc/sysctl.d/99-swap-tuning.conf /etc/cst/swap.conf \
        "$DROPIN_DIR/10-swap-memory-low.conf" "$DROPIN_DIR/50-memory-low.conf" \
        /run/systemd/system.control/machine.slice.d/50-MemoryLow.conf
    rmdir /etc/cst 2>/dev/null || true
    if [[ -n "${cpu_dropin_added:-}" ]]; then rm -f "$DROPIN_DIR/10-cpu-setup.conf"; fi
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    rm -f /dev/mapper/swap0 "/dev/disk/by-partuuid/$PU" /dev/e2e-swap0 /dev/e2e-dm0
    systemctl stop e2e-swap-vm.service 2>/dev/null || true
    rm -rf /var/tmp/e2e-swap-cst40g
    if [[ "$(cat "$W/orig/slice-state" 2>/dev/null)" == active ]]; then
        systemctl start machine.slice || true
    else
        systemctl stop machine.slice || true
    fi
    systemctl daemon-reload
    rm -rf "$W"
}

# Every file and link under /etc and the scratch tree: content, link target, mode, owner.
snap() {
    find /etc "$W" -path "$W/calls" -prune -o -path "$W/out" -prune -o -path "$W/err" -prune -o -type l -printf 'L %p -> %l\n' | sort
    find /etc "$W" -path "$W/calls" -prune -o -path "$W/out" -prune -o -path "$W/err" -prune -o -type f -print0 | sort -z | xargs -0 sha256sum
    find /etc "$W" -path "$W/calls" -prune -o -path "$W/out" -prune -o -path "$W/err" -prune -o -printf '%p %m %u\n' | sort
}

# What systemd and the cgroup tree say about machine.slice (printed when a step fails).
slice_facts() {
    echo "--- machine.slice"
    systemctl show -p ActiveState -p MemoryLow -p CPUWeight -p DropInPaths -p ControlGroup machine.slice || true
    cat /sys/fs/cgroup/machine.slice/memory.low 2>&1 || true
    find /sys/fs/cgroup/machine.slice -mindepth 1 -maxdepth 1 -type d 2>&1 || true
    cat /sys/fs/cgroup/machine.slice/cgroup.procs 2>&1 || true
    cat /sys/fs/cgroup/cgroup.subtree_control 2>&1 || true
    ls -la /etc/systemd/system/machine.slice.d /run/systemd/system.control/machine.slice.d \
        /run/systemd/system/machine.slice.d 2>&1 || true
}

# With a MemoryLow set at run time on machine.slice (set-property --runtime, then a reload): status
# and apply exit 0 with an "over" row naming the runtime drop-in; apply writes nothing and leaves it.
check_runtime_over() {
    local rt=/run/systemd/system.control/machine.slice.d/50-MemoryLow.conf s
    [[ "$(systemctl show -P MemoryLow machine.slice)" == 1073741824 && -f "$rt" ]] || fail "no runtime MemoryLow"
    swap_run status
    [[ $rc == 0 ]] || fail "status with a runtime MemoryLow"
    grep -qF "  over    machine.slice MemoryLow      1.0 GiB   (desired 48.0 GiB)" <<<"$out" || fail "status over row"
    grep -qF "  over: $rt sets machine.slice's MemoryLow" <<<"$out" || fail "status over note"
    [[ "$out" != *"  DIFF "* ]] || fail "status DIFF with a runtime MemoryLow"
    s="$(snap)"
    swap_run apply
    [[ $rc == 0 ]] || fail "apply with a runtime MemoryLow"
    grep -qF "  over    machine.slice MemoryLow      1.0 GiB   (desired 48.0 GiB)" <<<"$out" || fail "apply over row"
    [[ "$out" != *"written:"* && "$out" != *"removed:"* && -z "$err" ]] || fail "apply with a runtime MemoryLow wrote"
    [[ "$(snap)" == "$s" ]] || fail "apply with a runtime MemoryLow changed something"
    [[ "$(systemctl show -P MemoryLow machine.slice)" == 1073741824 && -f "$rt" ]] || fail "apply touched the runtime value"
}

# Run a command as tester, with the scratch kernel tree and the stand-ins.
as_user() {
    runuser -u tester -- env HOME=/home/tester USER=tester LOGNAME=tester PATH="$PATH" \
        CST_SWAP_PROC="$W/proc" CST_SWAP_SYS="$W/sys" "$@"
}

# Run cst swap ARGS as root: its stdout in $out, stderr in $err, exit status in $rc.
swap_run() {
    rc=0
    "$CST" swap "$@" >"$W/out" 2>"$W/err" || rc=$?
    out="$(cat "$W/out")"
    err="$(cat "$W/err")"
}
"""
)


# A stand-in for btrfs-stall-monitord as the main process of a Type=notify unit: READY=1 after
# argv[1] seconds, then WATCHDOG=1 every 10 s (the shipped WatchdogSec= is 2 min). Installed at
# NOTIFY_STUB_PATH (not under /tmp or /var/tmp: the unit has PrivateTmp=yes).
NOTIFY_STUB_PATH = "/usr/local/libexec/e2e-notify-stub"
NOTIFY_STUB = """\
import os, socket, sys, time
time.sleep(float(sys.argv[1]))
a = os.environ["NOTIFY_SOCKET"]
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
a = "\\0" + a[1:] if a.startswith("@") else a
s.sendto(b"READY=1", a)
while True:
    time.sleep(10)
    s.sendto(b"WATCHDOG=1", a)
"""


def stall_monitor_e2e_unit(
    shipped: str, exec_start: str, *, folders: str | None = None, timeout_start: str = "30s"
) -> str:
    """btrfs-stall-monitor.service as shipped, with ExecStart= replaced by exec_start and Nice=-5
    dropped: a negative nice needs CAP_SYS_NICE, which the tier 1 container does not have (the unit
    would fail with 201/NICE). TimeoutStartSec= becomes timeout_start: a start that fails here fails
    in seconds, not after the shipped 5 min. With folders, LogsDirectory= and RuntimeDirectory= are
    renamed to it, so that a test running the real daemon keeps out of the real folders. Every other
    line stays, Type=notify, WatchdogSec= and the RuntimeDirectory= options included."""
    out: list[str] = []
    seen = dict.fromkeys(
        ("ExecStart=", "Nice=", "Type=notify", "TimeoutStartSec=", "LogsDirectory=", "RuntimeDirectory="), 0
    )
    for line in shipped.splitlines():
        key = next((k for k in seen if line == k or (k.endswith("=") and line.startswith(k))), None)
        if key is not None:
            seen[key] += 1
        if key == "ExecStart=":
            out.append(f"ExecStart={exec_start}")
        elif key == "Nice=":
            out.append(f"# {line}: dropped, no CAP_SYS_NICE in the container")
        elif key == "TimeoutStartSec=":
            out.append(f"TimeoutStartSec={timeout_start}")
        elif key in ("LogsDirectory=", "RuntimeDirectory=") and folders is not None:
            out.append(f"{key}{folders}")
        else:
            out.append(line)
    if set(seen.values()) != {1}:
        raise AssertionError(f"the shipped unit changed shape ({seen}):\n{shipped}")
    return "\n".join(out) + "\n"


# main's btrfs-stall-monitord (1c43979): old_tree DIR makes a copy of the checkout with the files of
# tests/fixtures/legacy put back in their places (tests/e2e/README.md, Mixed-version tests).
STALL_OLD_TREE = r"""
fx=/opt/cst-rw/tests/fixtures/legacy
old_tree() {
    rm -rf "$1" && cp -a /opt/cst-rw "$1"
    cp "$fx/btrfs-stall_monitord-adapters-monitor.py@1c43979" "$1/btrfs/stall_monitord/adapters/monitor.py"
    cp "$fx/btrfs-stall_monitord-cli.py@1c43979" "$1/btrfs/stall_monitord/cli.py"
    cp "$fx/btrfs-units-btrfs-stall-monitor.service@1c43979" "$1/btrfs/units/btrfs-stall-monitor.service"
}
"""

# Steps of the stall monitor tests, on the unit $u (file $path) in test $t: use NAME puts $w/NAME.service
# in place; show PROP is one property of $u; starts counts the daemon's start lines in $u's journal and
# started_since N waits up to 10 s for more than N. On a failure: the line, the unit's status and journal.
STALL_UNIT_STEPS = r"""
set -E
diag() { systemctl status --no-pager "$u" >&2 || true; journalctl -q --no-pager -u "$u" -n 40 >&2 || true; }
trap 'echo "$t FAILED at line $LINENO: $BASH_COMMAND" >&2; diag' ERR
export LC_ALL=C
use() { cp "$w/$1.service" "$path"; systemctl daemon-reload; }
show() { systemctl show -p "$1" --value "$u"; }
starts() { journalctl --sync; journalctl -q --no-pager -o cat -u "$u" | grep -c '^started: ' || true; }
started_since() {
    for _ in $(seq 1 40); do (( $(starts) > $1 )) && return 0; sleep 0.25; done
    echo "no new start line of the daemon in the journal of $u" >&2
    return 1
}
"""


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
# The helpers of test_80 and test_101, which run `cst bluetooth` as tester: TEST names the test
# in the CI log; bt runs this checkout's cst bluetooth.
BT_STEPS = r"""
bt() { /opt/cst-rw/cst bluetooth "$@"; }
show() { sed "s/^/$TEST $1: /" <<<"$2" >&2; }   # the CI log
verify_failed_only() {   # verify_failed_only LABEL RC OUT: the live checks failed, nothing else
    if [[ "$2" != 1 ]] || ! grep -qF "ERROR: verification found problems (see above)." <<<"$3" \
        || ! grep -qF "no Bluetooth adapter in org.bluez" <<<"$3"; then
        echo "$TEST $1: exit $2, not only the live checks failing" >&2
        return 1
    fi
}
# step LABEL ARGS...: bt ARGS into $out, shown in the CI log, ending with only the live checks failing.
step() {
    local rc=0
    out="$(bt "${@:2}" 2>&1)" || rc=$?
    show "$1" "$out"
    verify_failed_only "$1" "$rc" "$out"
}
no_writes() {   # no_writes LABEL OUT: apply's report has no line of a change (every verb it uses)
    if grep -E '^(adopted|backup|enabled|reconnect|removed|restart|restarted|restored|set|started|written): ' <<<"$2"; then
        echo "$TEST $1 changed something" >&2
        return 1
    fi
}
"""


BT_SECOND_APPLY_SCRIPT = (
    BT_STEPS
    + r"""
set -E
TEST=test_80
trap 'echo "test_80 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
export LC_ALL=C
H=/home/tester
mac=00:11:22:33:44:55
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
roles() { python3 -I /opt/cst-rw/bluetooth/bt-helper wp-roles | cut -d'|' -f1; }
wp_pid() { systemctl --user show -p MainPID --value wireplumber.service; }
systemctl --user is-active wireplumber.service

owed_file="$H/.local/state/cst/bluetooth-restart-pending.json"

# 1. apply --no-restart: the files are written and WirePlumber's restart stays owed
# (bluetoothd does not run here: it owes nothing, it reads main.conf when it starts).
pid="$(wp_pid)"
step apply apply --yes --no-restart
for f in "$rule" "$le" "$unit" "$watch"; do [[ -f "$f" ]]; done
grep -qxF "Experimental = true" /etc/bluetooth/main.conf
[[ "$(wp_pid)" == "$pid" ]]
[[ -f "$owed_file" ]]
rc=0; out="$(bt plan 2>&1)" || rc=$?
show "plan after apply --no-restart" "$out"
[[ "$rc" == 10 ]]
grep -qE '^  ok +bluetooth.service +no restart needed$' <<<"$out"
grep -qE '^  restart +wireplumber \(your session\) ' <<<"$out"
grep -qF "(still owed: an earlier run did not restart it)" <<<"$out"
s1="$(files)"
le_text="$(cat "$le")"

# 2. apply: no file written, no backup; the owed WirePlumber restart is made, then forgotten.
step "apply after --no-restart" apply --yes
wp_restart_line="restart:   wireplumber (your session; audio stops for a moment)"
grep -qxF "$wp_restart_line" <<<"$out"
no_writes "apply after --no-restart" "$(grep -vxF "$wp_restart_line" <<<"$out" || true)"
same "apply after --no-restart" "$s1"
[[ "$(wp_pid)" != "$pid" ]]
systemctl --user is-active wireplumber.service
[[ ! -e "$owed_file" ]]
rc=0; out="$(bt plan 2>&1)" || rc=$?
show "plan after the restart" "$out"
[[ "$rc" == 0 ]]
grep -qxF "Nothing to do." <<<"$out"

# 3. a second apply changes nothing: no file written, no backup, no restart.
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

# 4. le-audio without on/off only shows the setting.
out="$(bt le-audio)"
grep -qxF "LE Audio in PipeWire: off (~/.config/cst/bluetooth.conf)" <<<"$out"
same "le-audio (show)" "$s1"

# 5. le-audio on: the setting written (other lines kept), the drop-in removed,
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

# 6. le-audio on again: nothing written, nothing restarted.
step "le-audio on again" le-audio on --yes
grep -qxF "unchanged: ~/.config/cst/bluetooth.conf already has LE_AUDIO=on" <<<"$out"
grep -qxF "unchanged: LE Audio on (no drop-in)" <<<"$out"
no_writes "le-audio on again" "$out"
same "le-audio on again" "$s2"
[[ "$(wp_pid)" == "$pid" ]]
out="$(bt le-audio)"
grep -qxF "LE Audio in PipeWire: on (~/.config/cst/bluetooth.conf)" <<<"$out"
same "le-audio (show, on)" "$s2"

# 7. le-audio off: the same drop-in back, the classic roles again.
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
)


# test_103: docker-gc run --dry-run of the program at argv[1] with its settings file at argv[2], against
# the fake docker and systemctl of btrfs/tests/test_docker_gc_program.py and its scenario (an old image of
# its protected registry, registry.example.com/team/db:1, and old unprotected ones), with the recorder
# running and a fresh record: a protected image is "held back", the others are listed for removal.
# legacy_cpu_tree: argv[2], this checkout's cst_core/domain/install.py, with the install manifest table of
# argv[1] (the previous version's install.py) in place of its own. The previous version's whole module
# would not import next to this checkout's cst_core (its types are not the ones record.py now uses).
LEGACY_MANIFEST = r"""
import sys
Q = '"' * 3
def table(text):
    start = text.index("r" + Q, text.index("INSTALL_MANIFEST: ")) + 4
    return start, text.index(Q, start)
with open(sys.argv[1]) as f:
    old = f.read()
with open(sys.argv[2]) as f:
    new = f.read()
(a, b), (c, d) = table(old), table(new)
with open(sys.argv[2], "w") as f:
    f.write(new[:c] + old[a:b] + new[d:])
"""


GC_DRY_RUN = r"""
import json, os, subprocess, sys, tempfile, time
sys.path.insert(0, "/opt/cst-rw")
from btrfs.tests import test_docker_gc_program as t
prog, conf = sys.argv[1], sys.argv[2]
tmp = tempfile.mkdtemp(prefix="e2e-gc-dry-", dir="/var/tmp")  # /tmp is noexec here: the fakes run
fakes = os.path.join(tmp, "bin")
os.mkdir(fakes)
for name, text in (("docker", t.FAKE_DOCKER), ("systemctl", t.FAKE_SYSTEMCTL)):
    path = os.path.join(fakes, name)
    with open(path, "w") as f:
        f.write(text.replace("@PYTHON@", sys.executable))
    os.chmod(path, 0o755)
now = time.time()
with open(os.path.join(tmp, "docker.json"), "w") as f:
    json.dump(t.scenario(now), f)
state = os.path.join(tmp, "state")
os.mkdir(state)
with open(os.path.join(state, "last-used.json"), "w") as f:
    json.dump({"since": t.OLD + 30 * t.DAY, "images": {t.sha("8"): now - t.DAY}, "heartbeat": now}, f)
env = {
    "PATH": fakes + ":/usr/bin:/bin", "HOME": tmp, "DOCKER_GC_STATE": state, "DOCKER_GC_RUNTIME": os.path.join(tmp, "run"),
    "DOCKER_GC_CONF": conf, "DOCKER_GC_INSTALLED": prog,
    "FAKE_DOCKER_STATE": os.path.join(tmp, "docker.json"), "FAKE_DOCKER_LOG": os.path.join(tmp, "calls.log"),
}
r = subprocess.run([prog, "run", "--dry-run"], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, cwd="/")
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
"""


# test_112: the program at argv[1] with its settings file at argv[2] against the same fake docker and
# scenario as test_103's, with one container record that lacks "Image" (its image would look unused):
# run --dry-run and status (which plan as the weekly run does), twice each, stop with "no Image;
# nothing done", and nothing is removed.
GC_HOLE_RUN = r"""
import json, os, subprocess, sys, tempfile, time
sys.path.insert(0, "/opt/cst-rw")
from btrfs.tests import test_docker_gc_program as t
prog, conf = sys.argv[1], sys.argv[2]
tmp = tempfile.mkdtemp(prefix="e2e-gc-hole-", dir="/var/tmp")  # /tmp is noexec here: the fakes run
fakes = os.path.join(tmp, "bin")
os.mkdir(fakes)
for name, text in (("docker", t.FAKE_DOCKER), ("systemctl", t.FAKE_SYSTEMCTL)):
    path = os.path.join(fakes, name)
    with open(path, "w") as f:
        f.write(text.replace("@PYTHON@", sys.executable))
    os.chmod(path, 0o755)
now = time.time()
docker = t.scenario(now)
hole = docker["containers"][0]
del hole["Image"]
with open(os.path.join(tmp, "docker.json"), "w") as f:
    json.dump(docker, f)
state = os.path.join(tmp, "state")
os.mkdir(state)
last_used = os.path.join(state, "last-used.json")
with open(last_used, "w") as f:
    json.dump({"since": t.OLD + 30 * t.DAY, "images": {t.sha("8"): now - t.DAY}, "heartbeat": now}, f)
with open(last_used, "rb") as f:
    before = f.read()
env = {
    "PATH": fakes + ":/usr/bin:/bin", "HOME": tmp, "DOCKER_GC_STATE": state, "DOCKER_GC_RUNTIME": os.path.join(tmp, "run"),
    "DOCKER_GC_CONF": conf, "DOCKER_GC_INSTALLED": prog,
    "FAKE_DOCKER_STATE": os.path.join(tmp, "docker.json"), "FAKE_DOCKER_LOG": os.path.join(tmp, "calls.log"),
}
want = f"<3>docker-gc: container inspect: {hole['Id']}: no Image; nothing done"
for args in (["run", "--dry-run"], ["status"], ["run", "--dry-run"], ["status"]):  # run: skipped right after boot
    r = subprocess.run([prog, *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, cwd="/")
    sys.stderr.write(f"test_112 {' '.join(args)}: rc={r.returncode} {r.stdout}{r.stderr}")
    if r.returncode != 76 or want not in r.stdout + r.stderr:  # 76: the recorder unit does not restart for it
        sys.exit(f"test_112: {args}: expected exit 76 and {want!r}")
with open(os.path.join(tmp, "calls.log")) as f:
    calls = [json.loads(line) for line in f]
removing = [c for c in calls if "rm" in c or "prune" in c]
if removing:
    sys.exit(f"test_112: docker was asked to remove {removing}")
with open(os.path.join(tmp, "docker.json")) as f:
    if json.load(f) != docker:
        sys.exit("test_112: the fake docker's objects changed")
with open(last_used, "rb") as f:
    if f.read() != before:
        sys.exit("test_112: last-used.json changed")
print("E2E-OK")
"""


# test_101: run as tester once the test has written main.conf. First the machine as main's CST
# (535aea1) left it when it set the headset by default: its LDAC rule (the vendored fixture, for an
# invented headset) and a settings file without DEVICE. Then a machine with no headset at all.
BT_HEADSET_SCRIPT = (
    BT_STEPS
    + r"""
set -E
TEST=test_101
trap 'echo "test_101 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
export LC_ALL=C
H=/home/tester
mac=0A:1B:2C:3D:4E:5F
wp="$H/.config/wireplumber/wireplumber.conf.d"
rule="$wp/50-bluez-0A_1B_2C_3D_4E_5F.conf"
conf="$H/.config/cst/bluetooth.conf"
sum() { if [[ -e "$1" ]]; then echo "$(stat -c '%i %Y' "$1") $(sha256sum <"$1")"; else echo absent; fi; }
rules() { find "$wp" -maxdepth 1 -name '50-bluez-*' 2>/dev/null | sort; }

# 1. What main's install left.
mkdir -p "$wp" "$(dirname "$conf")"
cp /opt/cst-rw/tests/fixtures/legacy/50-bluez-0A_1B_2C_3D_4E_5F.conf@535aea1 "$rule"
printf '# CST bluetooth settings (override bluetooth/bluetooth.conf in the repo)\nLE_AUDIO=off\n' >"$conf"
settings="$(cat "$conf")"
c0="$(sum "$conf")"; r0="$(sum "$rule")"

# 2. plan: the headset of the rule, which apply would save as DEVICE; plan writes nothing.
rc=0; out="$(bt plan 2>&1)" || rc=$?
show plan "$out"
[[ "$rc" == 10 ]]
grep -qF "Headset $mac (from CST's LDAC rule; apply saves it as DEVICE in ~/.config/cst/bluetooth.conf)" <<<"$out"
grep -qE "^  set +~/.config/cst/bluetooth.conf +DEVICE=$mac: the headset of CST's LDAC rule" <<<"$out"
grep -qE "^  ok +wireplumber.conf.d/50-bluez-0A_1B_2C_3D_4E_5F.conf +up to date" <<<"$out"
[[ "$(sum "$conf")" == "$c0" && "$(sum "$rule")" == "$r0" ]]

# 3. apply --no-restart (bluetoothd does not run here; WirePlumber's restart stays owed):
# DEVICE saved (the other lines kept), the rule as it was.
step apply apply --yes --no-restart
grep -qxF "written:   ~/.config/cst/bluetooth.conf: DEVICE=$mac" <<<"$out"
grep -qxF "unchanged: $rule" <<<"$out"
[[ "$(cat "$conf")" == "$settings"$'\n'"DEVICE=$mac" ]]
[[ "$(sum "$rule")" == "$r0" ]]
c1="$(sum "$conf")"
# apply: only the owed WirePlumber restart.
wp_restart_line="restart:   wireplumber (your session; audio stops for a moment)"
step "apply after --no-restart" apply --yes
grep -qxF "$wp_restart_line" <<<"$out"
no_writes "apply after --no-restart" "$(grep -vxF "$wp_restart_line" <<<"$out" || true)"

# 4. A second apply changes nothing; plan has nothing to do, the headset now from the settings.
step "second apply" apply --yes
no_writes "second apply" "$out"
[[ "$(sum "$conf")" == "$c1" && "$(sum "$rule")" == "$r0" ]]
rc=0; out="$(bt plan 2>&1)" || rc=$?
show "plan after apply" "$out"
[[ "$rc" == 0 ]]
grep -qF "Headset $mac (settings)" <<<"$out"

# 5. revert removes the rule; the saved DEVICE stays (it is your setting now).
out="$(bt revert --yes --no-restart 2>&1)"
show revert "$out"
grep -qxF "removed:   $rule" <<<"$out"
[[ -z "$(rules)" ]]
grep -qxF "DEVICE=$mac" "$conf"

# 5b. Before any apply saved it, the rule is the only place that names the headset: revert saves it as
# DEVICE first, then removes the rule.
cp /opt/cst-rw/tests/fixtures/legacy/50-bluez-0A_1B_2C_3D_4E_5F.conf@535aea1 "$rule"
printf '%s\n' "$settings" >"$conf"
out="$(bt revert --yes --no-restart 2>&1)"
show "revert before apply" "$out"
saved="$(grep -nxF "written:   ~/.config/cst/bluetooth.conf: DEVICE=$mac" <<<"$out" | cut -d: -f1)"
gone="$(grep -nxF "removed:   $rule" <<<"$out" | cut -d: -f1)"
[[ -n "$saved" && -n "$gone" && "$saved" -lt "$gone" ]]
[[ "$(cat "$conf")" == "$settings"$'\n'"DEVICE=$mac" && -z "$(rules)" ]]

# 6. No headset at all: plan, apply and a second apply skip its parts and write nothing for one;
# the old script's first rule name, which only a headset's rule replaces, stays.
rm -f "$conf"
legacy="$wp/50-jbl-live-beam-4.conf"
cp /opt/cst-rw/tests/fixtures/legacy/50-bluez-0A_1B_2C_3D_4E_5F.conf@535aea1 "$legacy"
l0="$(sum "$legacy")"
rc=0; out="$(bt plan 2>&1)" || rc=$?
show "plan, no headset" "$out"
[[ "$rc" == 10 ]]   # main.conf and the LE Audio drop-in, which need no headset
grep -qF "Headset none (no DEVICE set)" <<<"$out"
grep -qE "^  skip +the headset's LDAC rule +no headset set: set DEVICE=AA:BB:CC:DD:EE:FF in ~/.config/cst/bluetooth.conf" <<<"$out"
step "apply, no headset" apply --yes --no-restart
grep -qF "skipped:   no headset set: " <<<"$out"
grep -qE "^  skip +headset +no headset set: " <<<"$out"
grep -qxF "Experimental = true" /etc/bluetooth/main.conf
[[ -f "$wp/90-cst-le-audio-off.conf" && ! -e "$conf" && -z "$(rules)" ]]
step "apply after --no-restart, no headset" apply --yes
grep -qxF "$wp_restart_line" <<<"$out"
no_writes "apply after --no-restart, no headset" "$(grep -vxF "$wp_restart_line" <<<"$out" || true)"
step "second apply, no headset" apply --yes
no_writes "second apply, no headset" "$out"
[[ ! -e "$conf" && -z "$(rules)" && "$(sum "$legacy")" == "$l0" ]]
rc=0; out="$(bt plan 2>&1)" || rc=$?
show "plan after apply, no headset" "$out"
[[ "$rc" == 0 ]]
out="$(bt revert --yes --no-restart 2>&1)"
show "revert, no headset" "$out"
grep -qF "unchanged: no headset set: " <<<"$out"
[[ "$(sum "$legacy")" == "$l0" ]]
rm -f "$legacy"
echo E2E-OK
"""
)


class Tier1SystemdE2ETest(unittest.TestCase):
    SYSTEMD_CONTAINER = os.environ["CST_E2E_SYSTEMD_CONTAINER"]
    NAS_CONTAINER = os.environ["CST_E2E_NAS_CONTAINER"]
    IMAGE = os.environ["CST_E2E_SYSTEMD_IMAGE"]
    CST_ROOT_MOUNT = os.environ["CST_E2E_REPO_ROOT"]
    # run.py's host_lsm_security_opts (label=disable where SELinux is on), for the containers test_01 starts.
    SECURITY_OPTS = os.environ.get("CST_E2E_SECURITY_OPTS", "").split()

    @classmethod
    @override
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
            "python3 -I -B /opt/cst-rw/btrfs/cst-pending-alerts --list",
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
            zz="$(cd /opt/cst-rw && python3 -I -B -c 'import sys; sys.path.insert(0, "."); from cpu.game.domain import GAME_DROPIN; print(GAME_DROPIN)')"
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

    def test_88_swap_plan_apply_status_second_apply_on_the_real_etc_and_systemd(self) -> None:
        """cst swap as root on the container's /etc and systemd (the kernel's side in a
        scratch tree, see SWAP_E2E_LIB). Not configured: plan says so and apply refuses,
        both changing nothing. With /etc/cst/swap.conf: plan changes nothing and shows the
        scratch partition; apply writes crypttab, fstab, the three files, the zram mask and
        the install record; the real systemd merges swap's 10-swap-memory-low.conf with
        cpu's 10-cpu-setup.conf on machine.slice; status has nothing to do; a second apply
        changes nothing; with a VM stand-in running, a changed MemoryLow reaches the running
        slice through apply's daemon-reload; a MemoryLow set at run time wins over CST's after
        a daemon-reload."""
        r = exec_bash(
            self.SYSTEMD_CONTAINER,
            SWAP_E2E_LIB
            + r"""
trap swap_cleanup EXIT
swap_setup
trap 'echo "FAILED line $LINENO: $BASH_COMMAND"' ERR
fail() { echo "FAILED: $1"; echo "--- stdout"; echo "$out"; echo "--- stderr"; echo "$err"; slice_facts; exit 1; }

# 1. Nothing configured: plan says so, apply refuses; neither changes anything.
s0="$(snap)"
swap_run plan
[[ $rc == 0 ]] || fail "plan, not configured"
grep -qF "  REFUSE    cst/swap.conf                          no swap partition configured: set SWAP_PARTUUID and SWAP_SIZE_BYTES" <<<"$out" || fail "plan row"
[[ "$out" != *"crypttab:"* ]] || fail "crypttab row without a partition"
swap_run apply
[[ $rc == 1 ]] || fail "apply, not configured"
grep -qF "ERROR: /etc/cst/swap.conf: no swap partition configured" <<<"$err" || fail "apply message"
[[ "$(snap)" == "$s0" ]] || fail "a refused apply changed something"

# 2. Configured: plan shows the scratch partition and changes nothing.
mkdir -p /etc/cst
printf 'SWAP_PARTUUID=%s\nSWAP_SIZE_BYTES=%s\n' "$PU" "$SIZE" >/etc/cst/swap.conf
s1="$(snap)"
swap_run plan
[[ $rc == 0 ]] || fail "plan"
grep -qF "  ok        cst/swap.conf                          partition $PU, $SIZE bytes" <<<"$out" || fail "settings row"
grep -qF "  write     crypttab: swap0 line" <<<"$out" || fail "crypttab row"
grep -qF "  ok        partition 0e2e5a5a...                  /dev/e2e-swap0, $SIZE bytes as expected" <<<"$out" || fail "partition row"
grep -qF "  later     swap0" <<<"$out" || fail "swap0 row"
[[ "$(snap)" == "$s1" ]] || fail "plan changed something"
# A cgroup folder of machine.slice while systemd reports the slice inactive (as one left behind; its
# memory.low reads 0): plan and apply do not read it as the slice's floor.
! systemctl -q is-active machine.slice || fail "machine.slice active before apply"
mkdir /sys/fs/cgroup/machine.slice || fail "cannot plant a cgroup folder for the inactive machine.slice"
swap_run plan
[[ $rc == 0 ]] || fail "plan with a stale cgroup folder"
grep -qF "  -       machine.slice memory.low     slice not active (no VM running)" <<<"$out" || fail "plan, stale cgroup folder"
[[ "$out" != *"  DIFF    machine.slice"* ]] || fail "plan DIFF with a stale cgroup folder"

# 3. apply (the stale cgroup folder still there).
swap_run apply
[[ $rc == 0 ]] || fail "apply"
grep -qF "written:   /etc/crypttab (swap0 entry)" <<<"$out" || fail "crypttab written"
grep -qF "entry changed but 2 MiB are in use" <<<"$err" || fail "in-use warning"
grep -qF "Done. Details:" <<<"$out" || fail "done"
grep -qxF "swap0 /dev/disk/by-partuuid/$PU /dev/urandom swap,cipher=aes-xts-plain64,size=512,sector-size=4096,discard # managed-by: swap-setup.sh (CST)" /etc/crypttab
[[ "$(stat -c %a /etc/crypttab)" == 600 ]]
grep -qxF "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: swap-setup.sh (CST)" /etc/fstab
cmp "$SRC/tmpfiles/zswap.conf" /etc/tmpfiles.d/zswap.conf
cmp "$SRC/sysctl/99-swap-tuning.conf" /etc/sysctl.d/99-swap-tuning.conf
cmp "$SRC/units/machine.slice.d-10-swap-memory-low.conf" "$DROPIN_DIR/10-swap-memory-low.conf"
[[ "$(readlink /etc/systemd/zram-generator.conf)" == /dev/null ]]
[[ "$(cat "$W/sys/module/zswap/parameters/compressor")" == zstd ]]
[[ "$(cat "$W/proc/sys/vm/swappiness")" == 100 ]]
python3 -I - "$REC" <<'PY'
import json, sys
rec = json.load(open(sys.argv[1]))
d = "/etc/systemd/system/machine.slice.d/"
want = {"/etc/sysctl.d/99-swap-tuning.conf", d + "10-swap-memory-low.conf", "/etc/tmpfiles.d/zswap.conf"}
assert want <= set(rec["files"]), sorted(rec["files"])
assert d + "50-memory-low.conf" not in rec["files"]
assert rec["components"]["swap:core"] == "done", rec["components"]
PY
rmdir /sys/fs/cgroup/machine.slice || fail "the stale cgroup folder"

# 4. The real systemd merges both 10- drop-ins of machine.slice, and a start puts the floor
# into the cgroup (where the memory controller is there).
systemctl start machine.slice
[[ "$(systemctl show -P MemoryLow machine.slice)" == 51539607552 ]] || fail "MemoryLow"
[[ "$(systemctl show -P CPUWeight machine.slice)" == 50 ]] || fail "CPUWeight"
low=/sys/fs/cgroup/machine.slice/memory.low
[[ ! -e "$low" || "$(cat "$low")" == 51539607552 ]] || fail "systemd did not set memory.low on the started slice"

# 5. status: nothing differs (memory.low as systemd set it, where the controller is there).
swap_run status
[[ $rc == 0 ]] || fail "status"
for row in "crypttab: swap0 line" "fstab: /dev/mapper/swap0 line" "machine.slice.d/10-swap-memory-low.conf" \
    "tmpfiles.d/zswap.conf" "sysctl.d/99-swap-tuning.conf" "swap0 " "zram0 "; do
    grep -qF "  ok        $row" <<<"$out" || fail "status row $row"
done
[[ "$out" != *"  DIFF "* && "$out" != *"  REFUSE"* ]] || fail "status differs"
if [[ -e /sys/fs/cgroup/machine.slice/memory.low ]]; then
    grep -qE "^  ok +machine.slice memory.low +48.0 GiB" <<<"$out" || fail "memory.low"
fi

# 6. A second apply changes nothing.
s2="$(snap)"
swap_run apply
[[ $rc == 0 ]] || fail "second apply"
[[ "$out" != *"written:"* && "$out" != *"removed:"* && "$out" != *"masked:"* && -z "$err" ]] || fail "second apply wrote"
[[ "$(snap)" == "$s2" ]] || fail "second apply changed something"

# 7. A changed MemoryLow reaches a running slice through apply's daemon-reload alone: a stand-in VM
# in machine.slice, and a copy of the checkout whose drop-in says 40G (outside $W, which snap hashes).
systemd-run --quiet --unit=e2e-swap-vm --slice=machine.slice sleep 600
systemctl -q is-active e2e-swap-vm || fail "the stand-in VM"
c40=/var/tmp/e2e-swap-cst40g
rm -rf "$c40"
cp -a /opt/cst-rw "$c40"
sed -i 's/^MemoryLow=48G$/MemoryLow=40G/' "$c40/swap/units/machine.slice.d-10-swap-memory-low.conf"
grep -qx MemoryLow=40G "$c40/swap/units/machine.slice.d-10-swap-memory-low.conf"
for want in 42949672960 51539607552; do
    cst_real="$CST"
    if [[ $want == 42949672960 ]]; then CST="$c40/cst"; fi
    swap_run apply
    CST="$cst_real"
    [[ $rc == 0 && -z "$err" ]] || fail "apply of MemoryLow $want with a VM running"
    grep -qF "written:   $DROPIN_DIR/10-swap-memory-low.conf" <<<"$out" || fail "apply of $want: drop-in not written"
    [[ "$out" != *"  DIFF "* ]] || fail "apply of $want: DIFF"
    systemctl -q is-active e2e-swap-vm || fail "apply of $want stopped the stand-in VM"
    [[ "$(systemctl show -P MemoryLow machine.slice)" == "$want" ]] || fail "apply of $want: MemoryLow"
    [[ ! -e "$low" || "$(cat "$low")" == "$want" ]] || fail "apply of $want: the running slice's memory.low"
done
systemctl stop e2e-swap-vm
rm -rf "$c40"

# 8. A MemoryLow set at run time wins over CST's 48G after a daemon-reload.
systemctl set-property --runtime machine.slice MemoryLow=1G
systemctl daemon-reload
[[ "$(systemctl show -P MemoryLow machine.slice)" == 1073741824 ]]
check_runtime_over
rm -f /run/systemd/system.control/machine.slice.d/50-MemoryLow.conf
systemctl daemon-reload
[[ "$(systemctl show -P MemoryLow machine.slice)" == 51539607552 ]]
echo E2E-OK
""",
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("E2E-OK", r.stdout)

    def test_90_swap_upgrade_from_an_install_with_the_partition_in_the_code(self) -> None:
        """cst swap apply on what an older CST's apply left: its marked crypttab and fstab
        lines, the drop-in under its old name 50-memory-low.conf (and its install-record
        line), no /etc/cst/swap.conf. There a runtime MemoryLow loses the reload (the bug).
        plan as root shows the settings it will write, the removal and the 10- file, and
        changes nothing; as the user it says crypttab needs root; doctor warns. apply writes
        /etc/cst/swap.conf from the crypttab line and the partition, removes the old
        drop-in and its record line; a runtime MemoryLow then wins; a second apply changes
        nothing; doctor is ok."""
        r = exec_bash(
            self.SYSTEMD_CONTAINER,
            SWAP_E2E_LIB
            + r"""
trap swap_cleanup EXIT
swap_setup
trap 'echo "FAILED line $LINENO: $BASH_COMMAND"' ERR
fail() { echo "FAILED: $1"; echo "--- stdout"; echo "$out"; echo "--- stderr"; echo "$err"; slice_facts; exit 1; }

# What an older CST left: set up with today's apply, then turned back.
mkdir -p /etc/cst
printf 'SWAP_PARTUUID=%s\nSWAP_SIZE_BYTES=%s\n' "$PU" "$SIZE" >/etc/cst/swap.conf
swap_run apply
[[ $rc == 0 ]] || fail "setup apply"
rm /etc/cst/swap.conf "$DROPIN_DIR/10-swap-memory-low.conf"
old="$DROPIN_DIR/50-memory-low.conf"
{
    printf '# SPDX-%s\n' 'License-Identifier: MPL-2.0'
    printf '%s\n' "# Managed by swap-setup.sh (CST): protect libvirt guests from reclaim." \
        "# Soft floor: reclaim prefers unprotected cgroups while they have anything" \
        "# reclaimable; ignored when they don't. Propagates to the VM scope via the" \
        "# memory_recursiveprot cgroup2 mount option. 48G matches the win11-devbox" \
        "# allocation; raise it if the VM grows." "[Slice]" "MemoryLow=48G"
} >"$old"
python3 -I - "$REC" "$old" <<'PY'
import hashlib, json, sys
path, old = sys.argv[1], sys.argv[2]
with open(path) as f:
    rec = json.load(f)
d = "/etc/systemd/system/machine.slice.d/"
entry = rec["files"].pop(d + "10-swap-memory-low.conf")
entry["src"] = "swap/units/machine.slice.d-50-memory-low.conf"
with open(old, "rb") as f:
    entry["sha256"] = hashlib.sha256(f.read()).hexdigest()
rec["files"][d + "50-memory-low.conf"] = entry
with open(path, "w") as f:
    json.dump(rec, f, indent=2)
PY
systemctl daemon-reload
systemctl start machine.slice
# The bug of the old name: a MemoryLow set at run time loses the reload.
systemctl set-property --runtime machine.slice MemoryLow=1G
systemctl daemon-reload
[[ "$(systemctl show -P MemoryLow machine.slice)" == 51539607552 ]] || fail "old name: runtime value won"
rm -f /run/systemd/system.control/machine.slice.d/50-MemoryLow.conf
systemctl daemon-reload
# A stop and a start give the steps below a slice started with the 48G (the reload already put it
# back into memory.low).
systemctl stop machine.slice
systemctl start machine.slice

# plan as root: what apply will do, nothing changed.
s0="$(snap)"
swap_run plan
[[ $rc == 0 ]] || fail "plan"
grep -qF "  write     cst/swap.conf                          from the swap0 line an older CST wrote in crypttab: partition $PU, $SIZE bytes (its size now)" <<<"$out" || fail "settings row"
grep -qF "  remove    machine.slice.d/50-memory-low.conf" <<<"$out" || fail "remove row"
grep -qF "  write     machine.slice.d/10-swap-memory-low.conf" <<<"$out" || fail "drop-in row"
grep -qF "  ok        crypttab: swap0 line" <<<"$out" || fail "crypttab row"
[[ "$(snap)" == "$s0" ]] || fail "plan changed something"
# As the user /etc/crypttab (0600) cannot be read: the marked fstab line tells.
user_plan="$(as_user "$CST" swap plan)"
grep -qF "  ?         cst/swap.conf                          not written yet; an older CST set up swap0 (fstab)" <<<"$user_plan" || { out="$user_plan"; fail "user plan"; }
doc="$(as_user "$CST" doctor 2>&1 || true)"
grep -qF "swap partition: /etc/cst/swap.conf not written yet; an older CST set up swap0" <<<"$doc" || { out="$doc"; fail "doctor before"; }
[[ "$(snap)" == "$s0" ]] || fail "the user's plan or doctor changed something"

# A swap0 line written by hand in crypttab: apply refuses before it writes anything, the settings too.
cp -a /etc/crypttab "$W/crypttab.before"
echo "swap0 /dev/nvme0n1p5 none luks" >>/etc/crypttab
s_bad="$(snap)"
swap_run apply
[[ $rc == 1 ]] || fail "apply with a hand-written crypttab line"
grep -qF "/etc/crypttab has an unmarked entry for swap0: remove it by hand" <<<"$err" || fail "refusal message"
[[ ! -e /etc/cst/swap.conf ]] || fail "a refused apply wrote the settings"
[[ "$(snap)" == "$s_bad" ]] || fail "a refused apply changed /etc"
cp -a "$W/crypttab.before" /etc/crypttab

# apply: the settings from the installed state, the drop-in under its new name.
swap_run apply
[[ $rc == 0 ]] || fail "apply"
grep -qF "written:   /etc/cst/swap.conf (SWAP_PARTUUID=$PU, SWAP_SIZE_BYTES=$SIZE:" <<<"$out" || fail "settings written"
grep -qF "removed:   $old (the old name of 10-swap-memory-low.conf)" <<<"$out" || fail "old drop-in removed"
grep -qF "unchanged: /etc/crypttab (swap0 entry)" <<<"$out" || fail "crypttab kept"
[[ ! -e "$old" ]]
cmp "$SRC/units/machine.slice.d-10-swap-memory-low.conf" "$DROPIN_DIR/10-swap-memory-low.conf"
grep -qxF "SWAP_PARTUUID=$PU" /etc/cst/swap.conf
grep -qxF "SWAP_SIZE_BYTES=$SIZE" /etc/cst/swap.conf
grep -qxF "SWAP_NAME=swap0" /etc/cst/swap.conf
[[ "$(stat -c '%a %U' /etc/cst/swap.conf)" == "644 root" ]]
python3 -I - "$REC" <<'PY'
import json, sys
rec = json.load(open(sys.argv[1]))
d = "/etc/systemd/system/machine.slice.d/"
assert d + "50-memory-low.conf" not in rec["files"], sorted(rec["files"])
assert rec["files"][d + "10-swap-memory-low.conf"]["src"] == "swap/units/machine.slice.d-10-swap-memory-low.conf"
PY
doc="$(as_user "$CST" doctor 2>&1 || true)"
grep -qF "ok     swap partition: $PU ($SIZE bytes, /etc/cst/swap.conf)" <<<"$doc" || { out="$doc"; fail "doctor after"; }

# A second apply changes nothing.
s1="$(snap)"
swap_run apply
[[ $rc == 0 ]] || fail "second apply"
[[ "$out" != *"written:"* && "$out" != *"removed:"* && -z "$err" ]] || fail "second apply wrote"
[[ "$(snap)" == "$s1" ]] || fail "second apply changed something"

# A record line of 50-memory-low.conf whose file is gone, then one whose file is not CST's: apply
# drops the line and leaves the file as it is; doctor then asks for nothing.
record_old_name() {
    python3 -I - "$REC" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    rec = json.load(f)
d = "/etc/systemd/system/machine.slice.d/"
entry = dict(rec["files"][d + "10-swap-memory-low.conf"])
entry["src"] = "swap/units/machine.slice.d-50-memory-low.conf"
rec["files"][d + "50-memory-low.conf"] = entry
with open(path, "w") as f:
    json.dump(rec, f, indent=2)
PY
}
old_name_recorded() {
    python3 -I -c 'import json, sys; sys.exit(0 if sys.argv[2] in json.load(open(sys.argv[1]))["files"] else 1)' "$REC" "$old"
}
record_old_name
old_name_recorded || fail "the record line was not planted"
[[ ! -e "$old" ]]
doc="$(as_user "$CST" doctor 2>&1 || true)"
[[ "$doc" == *"$old"* ]] || { out="$doc"; fail "doctor did not see the record line without its file"; }
swap_run apply
[[ $rc == 0 && -z "$err" ]] || fail "apply, a record line without its file"
! old_name_recorded || fail "apply kept the record line of a file that is gone"
[[ ! -e "$old" && "$out" != *"written:"* ]] || fail "apply wrote with a record line without its file"
doc="$(as_user "$CST" doctor 2>&1 || true)"
[[ "$doc" != *"$old"* ]] || { out="$doc"; fail "doctor, a record line without its file"; }
printf '[Slice]\nMemoryLow=48G\n' >"$old"
systemctl daemon-reload
record_old_name
foreign="$(sha256sum "$old")"
doc="$(as_user "$CST" doctor 2>&1 || true)"
[[ "$doc" == *"$old"* ]] || { out="$doc"; fail "doctor did not see the record line of a file not CST's"; }
swap_run apply
[[ $rc == 0 && -z "$err" ]] || fail "apply, a record line of a file not CST's"
! old_name_recorded || fail "apply kept the record line of a file not CST's"
[[ "$(sha256sum "$old")" == "$foreign" && "$out" != *"removed:"* ]] || fail "apply touched a file not CST's"
doc="$(as_user "$CST" doctor 2>&1 || true)"
[[ "$doc" != *"$old"* ]] || { out="$doc"; fail "doctor, a record line of a file not CST's"; }
rm "$old"
systemctl daemon-reload

# Now a MemoryLow set at run time wins the reload, and status and apply leave it.
systemctl set-property --runtime machine.slice MemoryLow=1G
systemctl daemon-reload
[[ "$(systemctl show -P MemoryLow machine.slice)" == 1073741824 ]]
check_runtime_over
echo E2E-OK
""",
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("E2E-OK", r.stdout)

    def test_89_swap_dropin_loses_to_a_runtime_memory_low_after_reload(self) -> None:
        """systemd's real drop-in order for swap's machine.slice floor, next to cpu's
        10-cpu-setup.conf (both shipped files, on a slice of their own): both
        settings merge, and a `set-property --runtime MemoryLow` still wins after
        a daemon-reload because 10-swap-memory-low.conf sorts before
        50-MemoryLow.conf. Control: under the old name, 50-memory-low.conf, CST's
        48G wins again at the reload (the bug the rename fixed)."""
        exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            swap_src=/opt/cst-rw/swap/units/machine.slice.d-10-swap-memory-low.conf
            cpu_src="$(ls /opt/cst-rw/cpu/units/machine.slice.d-*-cpu-setup.conf)"
            [[ "$(sed -n 's|^swap/units/machine.slice.d-10-swap-memory-low.conf *\(/etc/[^ ]*\) .*|\1|p' \
                /opt/cst-rw/cst_core/domain/install.py)" == /etc/systemd/system/machine.slice.d/10-swap-memory-low.conf ]]
            d=/etc/systemd/system/e2e-swaporder.slice.d
            ctl=/run/systemd/system.control/e2e-swaporder.slice.d
            cleanup() {
                systemctl stop e2e-swaporder.slice || true
                rm -rf /etc/systemd/system/e2e-swaporder.slice "$d" "$ctl"
                systemctl daemon-reload
            }
            trap cleanup EXIT
            printf '[Unit]\nDescription=CST E2E swap drop-in order\n' >/etc/systemd/system/e2e-swaporder.slice
            mkdir -p "$d"
            cp "$swap_src" "$d/10-swap-memory-low.conf"
            cp "$cpu_src" "$d/${cpu_src##*/machine.slice.d-}"
            systemctl daemon-reload
            systemctl start e2e-swaporder.slice
            [[ "$(systemctl show -P MemoryLow e2e-swaporder.slice)" == 51539607552 ]]
            [[ "$(systemctl show -P CPUWeight e2e-swaporder.slice)" == 50 ]]
            systemctl set-property --runtime e2e-swaporder.slice MemoryLow=1G
            [[ -f "$ctl/50-MemoryLow.conf" ]]
            systemctl daemon-reload
            [[ "$(systemctl show -P MemoryLow e2e-swaporder.slice)" == 1073741824 ]]
            [[ "$(systemctl show -P CPUWeight e2e-swaporder.slice)" == 50 ]]
            # The old name sorts after 50-MemoryLow.conf ('M' < 'm'): CST's 48G wins the reload.
            mv "$d/10-swap-memory-low.conf" "$d/50-memory-low.conf"
            systemctl daemon-reload
            [[ "$(systemctl show -P MemoryLow e2e-swaporder.slice)" == 51539607552 ]]
            """,
        )

    def test_125_swap_e2e_setup_that_fails_before_saving_leaves_etc_alone(self) -> None:
        """The swap E2E harness itself (SWAP_E2E_LIB, as test_88 and test_90 use it: the cleanup
        trap set before swap_setup). A swap_setup whose own check fails, here on an
        /etc/cst/swap.conf already there, stops before it saves or changes anything, and the
        trap then removes only its scratch folder: swap.conf, crypttab, fstab, the install
        record and machine.slice's state stay as they were."""
        state = r"""
            for f in /etc/cst/swap.conf /etc/crypttab /etc/fstab /var/lib/cst/installed.json; do
                if [[ -e "$f" ]]; then sha256sum "$f"; else echo "absent $f"; fi
            done
            echo "machine.slice $(systemctl is-active machine.slice || true)"
            if [[ -e /var/tmp/e2e-swap ]]; then echo "scratch folder there"; fi
            """
        planted = "/var/tmp/e2e-swap125-planted"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"p={planted}\n"
            r"""
            if [[ -f "$p/files" ]]; then xargs -r rm -f <"$p/files"; fi
            if [[ -f "$p/dirs" ]]; then xargs -r rmdir <"$p/dirs" || true; fi
            rm -rf "$p"
            """,
        )
        exec_bash(
            self.SYSTEMD_CONTAINER,
            f"p={planted}\n"
            r"""
            [[ ! -e /etc/cst/swap.conf && ! -e /var/tmp/e2e-swap ]]
            rm -rf "$p"
            mkdir -p "$p"
            for f in /etc/crypttab /etc/fstab; do
                if [[ ! -e "$f" ]]; then echo "$f" >>"$p/files"; printf '# e2e test_125\n' >"$f"; fi
            done
            if [[ ! -d /etc/cst ]]; then echo /etc/cst >>"$p/dirs"; mkdir /etc/cst; fi
            echo /etc/cst/swap.conf >>"$p/files"
            printf 'SWAP_PARTUUID=0e2e5a5a-1111-4222-8333-944455556666\nSWAP_SIZE_BYTES=1073741824\n' >/etc/cst/swap.conf
            """,
        )
        before = exec_bash(self.SYSTEMD_CONTAINER, state).stdout
        self.assertNotIn("scratch folder there", before)
        r = exec_bash(
            self.SYSTEMD_CONTAINER, SWAP_E2E_LIB + "trap swap_cleanup EXIT\nswap_setup\necho SETUP-RAN\n", check=False
        )
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("SETUP-RAN", r.stdout)
        self.assertEqual(exec_bash(self.SYSTEMD_CONTAINER, state).stdout, before, r.stdout + r.stderr)

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
                gp() {{ python3 -I -B /opt/cst-rw/cpu/game-profile "$@"; }}
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
                python3 -I -B /opt/cst-rw/cpu/game-profile off || true
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
            # The file again (the install record still lists it, as installed): the record checks below
            # compare every recorded destination with the stage.
            runuser -u tester -- {installer} install --root {stage} --home /home/tester --owner user >/dev/null
            [[ -f "$d" && ! -L "$d" ]] || {{ echo "the user file was not installed again" >&2; exit 1; }}
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
            # Captured first: under pipefail, `install | grep -q` fails when grep stops reading at the
            # match and the installer, still printing, ends on SIGPIPE (141).
            restore="$({installer} install --root {stage} --owner root {sets})"
            grep -qxF "written:   $f" <<<"$restore" ||
                {{ echo "a second install did not restore the edited file: $restore" >&2; exit 1; }}
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
            "/usr/local/sbin/btrfs-snapshot-runtime": ("--help", 2),
            "/usr/local/sbin/btrfs-stall-monitord": ("--help", 0),
            "/usr/local/bin/btrfs-stall-report": ("--help", 0),
            # env -i below sets no HOME, which it needs before reading its arguments (as the bash did): exit 1.
            "/usr/local/bin/cst-pending-alerts": ("--help", 1),
            "/usr/local/sbin/docker-gc": ("--help", 2),
            "/usr/local/sbin/docker-volumes-nocow": ("--help", 2),
            "/usr/local/bin/cpu-watch": ("--help", 2),
            "/usr/local/sbin/cpu-vm-pin": ("--help", 2),
            "/usr/local/sbin/cpu-game-profile": ("--help", 2),
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
    # Settings a component's install needs, written first (and removed after) when they are not there:
    # over a docker-gc it does not know (the plain-script copy below) without its settings file, the
    # installer refuses (it cannot tell what that copy protects).
    COMPONENT_FILES: Mapping[str, Mapping[str, str]] = {
        "btrfs:docker-gc": {"/etc/cst/docker-gc.conf": "DOCKER_GC_PROTECT=\n"}
    }

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
        for path, text in self.COMPONENT_FILES.get(comp, {}).items():
            q = shlex.quote(path)
            made_file = exec_bash(
                c,
                f'[[ -e {q} ]] || {{ install -d -m 0755 "$(dirname {q})"; printf %s {shlex.quote(text)} >{q}; echo made; }}',
            ).stdout
            if "made" in made_file:
                self.addCleanup(exec_bash, c, f"rm -f {q}", check=False)
        made_needs: list[str] = []
        for need in self.COMPONENT_NEEDS.get(comp, ()):
            # Installed (and removed after) only when none of its files is there: another test's stay.
            present = f"$({installer} dests --component {need} | while read -r d; do [[ ! -e $d ]] || echo $d; done)"
            made = exec_bash(
                c, f'if [[ -z "{present}" ]]; then {installer} install --component {need} >/dev/null; echo made; fi'
            ).stdout
            if "made" in made:
                made_needs.append(need)
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
        # Removed now, not at the end of the test: the component's own cycle (a later subTest) needs it gone.
        for need in made_needs:
            exec_bash(c, f"{installer} remove --component {need} >/dev/null")

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
        # The install writes docker-gc's settings first (none installed: none protected); removed after.
        if not exec_bash(c, "[[ ! -e /etc/cst/docker-gc.conf ]] || echo there").stdout.strip():
            self.addCleanup(exec_bash, c, "rm -f /etc/cst/docker-gc.conf", check=False)
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
        in "verdict: OK"; without the next deployment's boot entry plan and
        install refuse and change nothing, status says NO BOOT ENTRY and
        files-only still runs; a control: the real verify reads the units
        from the work folder's copies, not from /etc (a broken record unit and
        service template in /etc do not stop an install, which writes the good
        ones back; this also held when verify ran after the writes); a typo in
        a unit (an unknown key in a drop-in of the record unit, which verify
        reads with the copy) stops plan and an install at their
        `systemd-analyze verify`, before they write anything (a hand edit of
        the timer stays) and before any rpm-ostree change; uninstall removes
        everything and untracks it. rpm-ostree, findmnt, btrfs, lsblk, cpio and
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

        # Without the next deployment's boot entry (as when /boot is not
        # mounted) install cannot find the overlay to compare: plan and
        # install refuse and change nothing, and status says so. files-only
        # leaves the initramfs alone and still runs.
        entry = "/boot/loader/entries/ostree-1-e2e.conf"
        hint = "in /boot/loader/entries (is /boot mounted?)"
        with self.subTest("no boot entry for the next deployment"):
            step(f"mv {entry} {w}/entry.conf")
            try:
                n = len(calls())
                for mode in ("plan", "install"):
                    r = exec_bash(c, f". {w}/fakes.bash\n/opt/cst-rw/cst btrfs degraded-boot {mode}", check=False)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    out = r.stdout + r.stderr
                    self.assertIn("ERROR: no boot entry for the next deployment /ostree/deploy/e2e/deploy/", out)
                    self.assertIn(f" {hint}. Nothing was changed.\n", out)
                    self.assertNotIn("written:", r.stdout)
                status = step("/opt/cst-rw/cst btrfs degraded-boot status")
                self.assertIn(
                    f"next-boot initramfs overlay: unknown - no boot entry for the next deployment {hint}\n", status
                )
                self.assertIn(f"next boot:        NO BOOT ENTRY for the next deployment {hint}\n", status)
                only = step("/opt/cst-rw/cst btrfs degraded-boot files-only")
                self.assertNotIn("written:", only)
                self.assertTrue(only.endswith("--files-only: initramfs overlay not updated\n"), only)
                self.assertEqual(step(sums), before)
                self.assertEqual([x for x in calls()[n:] if x.startswith("rpm-ostree initramfs-etc")], [])
            finally:
                step(f"mv {w}/entry.conf {entry}")

        # A control: the units named on verify's command line are the work
        # folder's copies (the templates under their instance names, the
        # record unit under its own), so broken ones in /etc are not what
        # verify reads. (The old order, writes first, passed this too.)
        with self.subTest("broken installed units are replaced, not verified"):
            broken = (record, files[1])
            step(" && ".join(f"printf 'BogusKeyE2E=1\\n' >>{f}" for f in broken))
            fixed = step("/opt/cst-rw/cst btrfs degraded-boot")
            for f in broken:
                self.assertIn(f"written:   {f}\n", fixed)
                self.assertNotIn("BogusKeyE2E", step(f"cat {f}"))
        # verify reads a unit's drop-ins with it; an unknown key there is only
        # a warning unless the script passes --recursive-errors=no. The check
        # runs before anything is written: a hand edit of the timer stays.
        timer = files[2]
        with self.subTest("a typo in a unit stops plan and install before they write"):
            step(f"mkdir -p {typo} && printf '[Service]\\nTimeoutStratSec=5\\n' >{typo}/e2e-typo.conf")
            step(f"printf '# e2e local edit\\n' >>{timer}")
            n = len(calls())
            for mode in ("plan", "install"):
                bad = exec_bash(c, f". {w}/fakes.bash\n/opt/cst-rw/cst btrfs degraded-boot {mode}", check=False)
                self.assertNotEqual(bad.returncode, 0, bad.stdout)
                self.assertNotIn("written:", bad.stdout)
                self.assertNotIn("would write:", bad.stdout)
                self.assertEqual(step(f"tail -n 1 {timer}"), "# e2e local edit\n")
                self.assertIn("Unknown key 'TimeoutStratSec'", bad.stdout + bad.stderr)
                self.assertIn(
                    "ERROR: systemd-analyze verify failed for the generated units; nothing was written",
                    bad.stdout + bad.stderr,
                )
                self.assertNotIn("self-test:", bad.stdout)
            self.assertEqual(
                [x for x in calls()[n:] if x.startswith(("rpm-ostree initramfs-etc", "udevadm control"))], []
            )
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
    SECURITY_CEILINGS: ClassVar[dict[str, int]] = {
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
        # 23, not 21: no NoNewPrivileges= (it blocks the SELinux move into the
        # Docker domain on Fedora; see btrfs/README.md and test_unit_contract).
        "docker-gc-recorder.service": 23,
        "docker-gc.service": 23,
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
        its daemon swapped for a stand-in that says READY=1 (NOTIFY_STUB) and
        Nice=-5 dropped (see stall_monitor_e2e_unit): a file in its RuntimeDirectory, where the
        daemon queues alerts for btrfs-stall-alert.service, survives
        `systemctl restart` (RuntimeDirectoryPreserve=restart; with the default
        a restart deleted it), and `stop` removes the folder."""
        c = self.SYSTEMD_CONTAINER
        shipped = docker_exec(c, ["cat", "/opt/cst/btrfs/units/btrfs-stall-monitor.service"]).stdout
        stub = stall_monitor_e2e_unit(shipped, f"/usr/bin/python3 -I {NOTIFY_STUB_PATH} 0")
        unit = "e2e-stall-monitor.service"
        path = f"/etc/systemd/system/{unit}"
        self.addCleanup(
            exec_bash,
            c,
            f"systemctl stop {unit}; rm -f {path} {NOTIFY_STUB_PATH}; systemctl daemon-reload; systemctl reset-failed {unit}",
            check=False,
        )
        docker_exec(
            c,
            ["bash", "-c", f"install -d {os.path.dirname(NOTIFY_STUB_PATH)} && cat >{NOTIFY_STUB_PATH}"],
            input_text=NOTIFY_STUB,
        )
        docker_exec(c, ["bash", "-c", f"cat >{path}"], input_text=stub)
        out = exec_bash(
            c,
            f"""
            diag() {{ systemctl status --no-pager {unit} >&2 || true; journalctl -q --no-pager -u {unit} -n 30 >&2 || true; }}
            d=/run/btrfs-stall-monitor
            systemctl daemon-reload
            # Type=notify: start returns once the stand-in said READY=1. Running, and never
            # restarted: a failing stub would also keep the folder (Restart=on-failure + Preserve).
            running() {{
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

    def test_104_stall_monitor_unit_is_active_only_after_ready(self) -> None:
        """btrfs-stall-monitor.service as shipped (Type=notify, WatchdogSec=), as e2e-stall-notify.service
        with Nice=-5 dropped and folders of its own (stall_monitor_e2e_unit), and three main processes:
        (1) a stand-in that says READY=1 after 4 s (NOTIFY_STUB): `start --no-block` leaves the unit
        activating until then, and it is active only 4 s after the process started; (2) main's daemon
        (1c43979, from tests/fixtures/legacy; it never says READY=1) starts and logs its start line, but
        the unit never becomes active: start fails at TimeoutStartSec= (5 s here). That is why an install
        writes the program before the unit (test_106). (3) This checkout's daemon, as the installer builds
        it: start returns once it says READY=1, the unit is active, and its WATCHDOG=1 pings move
        WatchdogTimestamp on, with no restart. The shipped unit waits 5 min for READY=1 (a start during a
        stall reads Python from the stalled disk); the derived ones 30 s, so a failure here is quick."""
        c = self.SYSTEMD_CONTAINER
        w = "/var/lib/e2e-stall-notify"  # not /var/tmp: the unit has PrivateTmp=yes
        unit = "e2e-stall-notify.service"
        path, dropin = f"/etc/systemd/system/{unit}", f"/etc/systemd/system/{unit}.d"
        self.addCleanup(
            exec_bash,
            c,
            f"systemctl stop {unit}; rm -rf {path} {dropin} {w} {NOTIFY_STUB_PATH} /var/log/e2e-stall-notify; "
            f"systemctl daemon-reload; systemctl reset-failed {unit}",
            check=False,
        )
        self._stall_daemons(c, w)
        shipped = docker_exec(c, ["cat", "/opt/cst/btrfs/units/btrfs-stall-monitor.service"]).stdout
        self.assertIn("\nTimeoutStartSec=5min\n", shipped)
        for name, exec_start in (
            ("stub", f"/usr/bin/python3 -I {NOTIFY_STUB_PATH} 4"),
            ("old", f"/usr/bin/python3 -I {w}/old/usr/local/sbin/btrfs-stall-monitord"),
            ("new", f"/usr/bin/python3 -I {w}/new/usr/local/sbin/btrfs-stall-monitord"),
        ):
            text = stall_monitor_e2e_unit(shipped, exec_start, folders="e2e-stall-notify")
            docker_exec(c, ["bash", "-c", f"cat >{w}/{name}.service"], input_text=text)
        out = exec_bash(
            c,
            STALL_UNIT_STEPS
            + f"""
            u={unit} path={path} dropin={dropin} w={w} t=test_104
            # 1. The stand-in says READY=1 after 4 s: activating until then.
            use stub
            systemctl start --no-block $u
            sleep 1.5
            [[ "$(show ActiveState)" == activating ]]
            for _ in $(seq 1 40); do [[ "$(show ActiveState)" == active ]] && break; sleep 0.25; done
            [[ "$(show ActiveState)" == active ]]
            (( $(show ActiveEnterTimestampMonotonic) - $(show ExecMainStartTimestampMonotonic) >= 3500000 ))
            systemctl stop $u

            # 2. main's daemon under this unit: it runs (its start line) but never says READY=1.
            use old
            install -d $dropin
            printf '[Service]\nTimeoutStartSec=5s\n' >$dropin/e2e.conf
            systemctl daemon-reload
            before="$(starts)"
            rc=0; systemctl start $u 2>/dev/null || rc=$?
            [[ "$rc" != 0 && "$(show Result)" == timeout ]]
            started_since "$before"
            systemctl stop $u; rm -rf $dropin; systemctl daemon-reload; systemctl reset-failed $u || true

            # 3. This checkout's daemon: active once it said READY=1, then its pings.
            use new
            before="$(starts)"
            systemctl start $u
            started_since "$before"
            [[ "$(show ActiveState) $(show Type) $(show WatchdogUSec)" == "active notify 2min" ]]
            w1="$(show WatchdogTimestampMonotonic)"
            sleep 3
            (( w1 > 0 && $(show WatchdogTimestampMonotonic) > w1 ))
            [[ "$(show ActiveState) $(show NRestarts)" == "active 0" ]]
            systemctl stop $u
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_105_stall_monitor_is_restarted_when_its_pings_stop(self) -> None:
        """This checkout's btrfs-stall-monitord under the shipped unit (as in test_104), with a drop-in
        that shortens WatchdogSec= to 3 s and RestartSec= to 1 s: its pings keep it running past two
        such periods with no restart; once its main process is stopped (SIGSTOP: a loop that no longer
        passes, as a deadlock or a read stuck for minutes), systemd logs "Watchdog timeout", kills it and
        Restart=on-failure starts a new process, which is active again (NRestarts 1). The shipped
        LimitCORE=0 holds: the process runs with a core size limit of 0, which systemd-coredump honours,
        so the SIGABRT leaves no core (in a container it would go to the machine that runs it:
        core_pattern is not per container)."""
        c = self.SYSTEMD_CONTAINER
        w = "/var/lib/e2e-stall-watchdog"
        unit = "e2e-stall-watchdog.service"
        path, dropin = f"/etc/systemd/system/{unit}", f"/etc/systemd/system/{unit}.d"
        self.addCleanup(
            exec_bash,
            c,
            f"systemctl stop {unit}; rm -rf {path} {dropin} {w} /var/log/e2e-stall-watchdog; "
            f"systemctl daemon-reload; systemctl reset-failed {unit}",
            check=False,
        )
        self._stall_daemons(c, w, old=False)
        shipped = docker_exec(c, ["cat", "/opt/cst/btrfs/units/btrfs-stall-monitor.service"]).stdout
        text = stall_monitor_e2e_unit(
            shipped, f"/usr/bin/python3 -I {w}/new/usr/local/sbin/btrfs-stall-monitord", folders="e2e-stall-watchdog"
        )
        docker_exec(c, ["bash", "-c", f"cat >{w}/new.service"], input_text=text)
        out = exec_bash(
            c,
            STALL_UNIT_STEPS
            + f"""
            u={unit} path={path} dropin={dropin} w={w} t=test_105
            use new
            install -d $dropin
            printf '[Service]\nWatchdogSec=3s\nRestartSec=1s\n' >$dropin/e2e.conf
            systemctl daemon-reload
            systemctl start $u
            sleep 8
            [[ "$(show ActiveState) $(show NRestarts) $(show WatchdogUSec) $(show LimitCORE)" == "active 0 3s 0" ]]
            pid="$(show MainPID)"
            grep -Eq '^Max core file size +0 +0 ' /proc/$pid/limits  # what systemd-coredump honours
            kill -STOP "$pid"
            for _ in $(seq 1 60); do
                [[ "$(show NRestarts) $(show ActiveState)" == "1 active" ]] && break
                sleep 0.5
            done
            [[ "$(show NRestarts) $(show ActiveState)" == "1 active" ]]
            [[ "$(show MainPID)" != "$pid" ]]
            if kill -0 "$pid" 2>/dev/null; then echo "the stopped process $pid is still there" >&2; exit 1; fi
            journalctl --sync
            journalctl -q --no-pager -o cat -u $u | grep -q 'Watchdog timeout'
            systemctl stop $u
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_112_stall_monitor_with_a_duration_of_zero_stops_at_once(self) -> None:
        """This checkout's btrfs-stall-monitord, as its installer builds it, with --duration 0 ("stop after
        0 seconds"): it ends before its first sample with exit 0. The daemon before took 0 as no limit and
        ran on until the timeout killed it (124)."""
        c = self.SYSTEMD_CONTAINER
        w = "/var/lib/e2e-stall-duration"
        self.addCleanup(exec_bash, c, f"rm -rf {w}", check=False)
        self._stall_daemons(c, w, old=False)
        out = exec_bash(
            c,
            f"""
            install -d {w}/log {w}/run
            rc=0
            timeout 20 /usr/bin/python3 -I {w}/new/usr/local/sbin/btrfs-stall-monitord \\
                --duration 0 --log-dir {w}/log --run-dir {w}/run >{w}/out 2>&1 || rc=$?
            [[ "$rc" == 0 ]] || {{ echo "exit $rc" >&2; cat {w}/out >&2; exit 1; }}
            grep -q 'stopped' {w}/out || {{ cat {w}/out >&2; exit 1; }}
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_106_stall_monitor_upgrade_installs_the_program_before_the_unit(self) -> None:
        """The upgrade from main (1c43979: tests/fixtures/legacy put back in a copy of the checkout)
        with the real installers on the container's /, btrfs-stall-monitord and its unit only, and a
        drop-in with Nice=0 (no CAP_SYS_NICE here), TimeoutStartSec=30s (the shipped 5 min only slows a failure
        down here) and a log folder of its own: (1) main's install, old daemon and old unit, runs as
        Type=simple; (2) this checkout's install writes the program before the unit (its output), so
        no start can meet the new unit with the old daemon (test_104 shows that one never becomes
        active); after the daemon-reload, the old process keeps running, unwatched (systemd keeps the
        watchdog a process started with, none here: with a drop-in of WatchdogSec=2s in that step, it is
        still there 6 s later), until the restart that
        btrfs-setup.sh install does next, and after it the new daemon is active as Type=notify with its
        pings, never restarted; (3) a second install writes nothing; (4) the new daemon under main's unit
        (what an install leaves when the unit's verify refuses it, after the program) also runs, as
        Type=simple."""
        c = self.SYSTEMD_CONTAINER
        w = "/var/lib/e2e-stall-upgrade"
        unit = "btrfs-stall-monitor.service"
        path, dropin = f"/etc/systemd/system/{unit}", f"/etc/systemd/system/{unit}.d"
        prog, rec = "/usr/local/sbin/btrfs-stall-monitord", "/var/lib/cst/installed.json"
        logs = "/var/log/e2e-stall-upgrade"  # the drop-in's LogsDirectory=: the real folder is left alone
        exec_bash(
            c,
            f"""
            [[ ! -e {prog} && ! -e {path} && ! -e {dropin} ]] || {{ echo "a stall monitor is installed already" >&2; exit 1; }}
            rm -rf {w} && install -d {w}/orig
            if [[ -e {rec} ]]; then cp -a {rec} {w}/orig/installed.json; fi
            """,
        )
        self.addCleanup(
            exec_bash,
            c,
            f"""
            systemctl stop {unit}; rm -rf {prog} {path} {dropin}
            if [[ -e {w}/orig/installed.json ]]; then cp -a {w}/orig/installed.json {rec}; else rm -f {rec}; fi
            rm -rf {w} {logs}; systemctl daemon-reload; systemctl reset-failed {unit}
            """,
            check=False,
        )
        out = exec_bash(
            c,
            STALL_UNIT_STEPS
            + STALL_OLD_TREE
            + f"""
            u={unit} path={path} dropin={dropin} w={w} t=test_106
            srcs="btrfs/btrfs-stall-monitord btrfs/units/btrfs-stall-monitor.service"
            line_of() {{ grep -nxF "$1" <<<"$2" | cut -d: -f1; }}
            install -d $dropin
            # TimeoutStartSec=30s: a start that fails here fails in seconds, not after the shipped 5 min.
            printf '[Service]\nNice=0\nTimeoutStartSec=30s\nLogsDirectory=\nLogsDirectory=e2e-stall-upgrade\n' >$dropin/e2e.conf

            # 1. main's install: old daemon, old unit.
            old_tree $w/old-tree
            /usr/bin/python3 -I -B $w/old-tree/cst_core/installer.py install $srcs >/dev/null
            systemctl daemon-reload
            before="$(starts)"
            systemctl start $u
            started_since "$before"
            sleep 2
            [[ "$(show ActiveState) $(show Type) $(show NRestarts)" == "active simple 0" ]]
            pid="$(show MainPID)"

            # 2. This checkout's install: the program first, then the unit.
            out="$(/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install $srcs)"
            sed "s/^/$t install: /" <<<"$out" >&2
            p="$(line_of "written:   {prog}" "$out")"; n="$(line_of "written:   {path}" "$out")"
            [[ -n "$p" && -n "$n" && "$p" -lt "$n" ]]
            grep -qx 'TimeoutStartSec=5min' $path
            # WatchdogSec=2s for this step only (the shipped 2 min could not fire within the test): had the
            # reload armed it on the old process, which never pings, systemd would kill it within 2 s.
            printf '[Service]\nWatchdogSec=2s\n' >$dropin/e2e-watchdog.conf
            systemctl daemon-reload
            [[ "$(show TimeoutStartUSec)" == 30s ]]
            # systemd keeps the watchdog the old process started with (none: WatchdogUSec 0), and no
            # timeout comes later either (6 s: three such periods).
            [[ "$(show ActiveState) $(show MainPID) $(show WatchdogUSec)" == "active $pid 0" ]]
            sleep 6
            [[ "$(show ActiveState) $(show MainPID) $(show NRestarts) $(show Result)" == "active $pid 0 success" ]]
            rm $dropin/e2e-watchdog.conf
            systemctl daemon-reload
            before="$(starts)"
            systemctl restart $u
            started_since "$before"
            [[ "$(show ActiveState) $(show Type) $(show WatchdogUSec)" == "active notify 2min" ]]
            w1="$(show WatchdogTimestampMonotonic)"
            sleep 3
            (( w1 > 0 && $(show WatchdogTimestampMonotonic) > w1 ))
            [[ "$(show ActiveState) $(show NRestarts)" == "active 0" ]]

            # 3. A second install writes nothing.
            again="$(/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install $srcs)"
            if grep -v '^unchanged: ' <<<"$again"; then echo "a second install changed files" >&2; exit 1; fi

            # 4. The new daemon under main's unit (a unit the verify refused stays the old one).
            cp "$fx/btrfs-units-btrfs-stall-monitor.service@1c43979" $path
            systemctl daemon-reload
            before="$(starts)"
            systemctl restart $u
            started_since "$before"
            sleep 3
            [[ "$(show ActiveState) $(show Type) $(show NRestarts)" == "active simple 0" ]]
            systemctl stop $u
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def _stall_daemons(self, c: str, w: str, *, old: bool = True) -> None:
        """This checkout's btrfs-stall-monitord in w/new (the zipapp its installer builds, staged with
        --root); with old, also main's in w/old and NOTIFY_STUB at NOTIFY_STUB_PATH."""
        old_steps = f"""
            old_tree {w}/old-tree
            /usr/bin/python3 -I -B {w}/old-tree/cst_core/installer.py install --root {w}/old btrfs/btrfs-stall-monitord >/dev/null
            install -d {os.path.dirname(NOTIFY_STUB_PATH)}
            """
        exec_bash(
            c,
            STALL_OLD_TREE
            + f"""
            rm -rf {w} && install -d {w}
            /usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py install --root {w}/new btrfs/btrfs-stall-monitord >/dev/null
            """
            + (old_steps if old else ""),
        )
        if old:
            docker_exec(c, ["bash", "-c", f"cat >{NOTIFY_STUB_PATH}"], input_text=NOTIFY_STUB)

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
        the LE Audio drop-in and the LDAC watcher and restarts nothing;
        `plan` then still owes WirePlumber's restart (exit 10; bluetoothd
        does not run here, so it owes none). `apply` makes that restart and
        writes nothing; `plan` then has nothing to do (exit 0). A second
        `apply` changes nothing: the same inode, mtime and content of each
        file, no new main.conf backup, no restart of WirePlumber. `le-audio` alone only shows the setting.
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

    def test_101_bluetooth_keeps_the_headset_of_an_older_setup_and_works_without_one(self) -> None:
        """CST ships no headset (DEVICE in bluetooth/bluetooth.conf is empty). The upgrade from
        main's install (535aea1), as tester with the real user manager and WirePlumber: its LDAC
        rule for the headset it set by default (tests/fixtures/legacy, an invented address: main
        wrote this text for it) and a settings file without DEVICE. plan names that headset and
        that apply saves it, and writes nothing; apply --no-restart saves DEVICE (the other lines
        kept) and leaves the rule, apply then makes only the owed WirePlumber restart (bluetoothd
        does not run here, as in test_80); a second apply changes nothing and plan has nothing to
        do; revert removes the rule and keeps DEVICE. A revert before any apply saved DEVICE saves
        it first, then removes the rule. Then no headset at all: plan, apply and a second apply skip
        the headset's LDAC rule and checks, write main.conf and the LE Audio drop-in, and no
        headset's rule or setting, and the old script's first rule name (only a headset's rule
        replaces it) stays; revert says there is no rule to remove."""
        self.addCleanup(
            as_tester_bash,
            self.SYSTEMD_CONTAINER,
            "/opt/cst-rw/cst bluetooth revert --yes --no-restart >/dev/null 2>&1 || true; "
            "rm -f /home/tester/.config/cst/bluetooth.conf "
            "/home/tester/.config/wireplumber/wireplumber.conf.d/50-bluez-0A_1B_2C_3D_4E_5F.conf "
            "/home/tester/.config/wireplumber/wireplumber.conf.d/50-jbl-live-beam-4.conf",
            check=False,
        )
        exec_bash(
            self.SYSTEMD_CONTAINER,
            "mkdir -p /etc/bluetooth && printf '[General]\\n# Name = BlueZ\\n' >/etc/bluetooth/main.conf",
        )
        out = as_tester_bash(self.SYSTEMD_CONTAINER, BT_HEADSET_SCRIPT).stdout
        self.assertIn("E2E-OK", out)

    def test_96_bluetooth_as_root_drops_the_test_overrides(self) -> None:
        """Run as root with SUDO_USER, `cst bluetooth` runs again as that user without the
        unit tests' overrides CST_BT_ROOT, CST_BT_CONTAINER and CST_BT_LIBWP (sudo -E or a root
        shell could pass them on): plan reads the real /etc/bluetooth/main.conf, not the one
        under the folder CST_BT_ROOT names, and does not take itself for a run inside a toolbox
        (/run/host/etc); status reads WirePlumber's roles through the real libwireplumber, not
        the missing library CST_BT_LIBWP names. The unit twin (StepDownTest in
        bluetooth/tests/test_setup.py) runs under unshare -r with a fake runuser."""
        self.addCleanup(exec_bash, self.SYSTEMD_CONTAINER, "rm -rf /tmp/bt96", check=False)
        exec_bash(
            self.SYSTEMD_CONTAINER,
            """
            mkdir -p /etc/bluetooth /tmp/bt96/etc/bluetooth
            printf '[General]\\n# Name = BlueZ\\n' >/etc/bluetooth/main.conf
            printf '[General]\\n# elsewhere\\n' >/tmp/bt96/etc/bluetooth/main.conf
            """,
        )
        overrides = "SUDO_USER=tester CST_BT_ROOT=/tmp/bt96 CST_BT_CONTAINER=1 CST_BT_LIBWP=/tmp/bt96/no-such.so"
        r = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"rc=0; {overrides} /opt/cst-rw/cst bluetooth plan --device 00:11:22:33:44:55 || rc=$?; echo rc=$rc",
            check=False,
        )
        self.assertIn("; host /etc in /etc\n", r.stdout, r.stdout + r.stderr)
        self.assertIn("rc=10", r.stdout, r.stdout + r.stderr)
        r = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"{overrides} /opt/cst-rw/cst bluetooth status --device 00:11:22:33:44:55 2>&1 || true",
            check=False,
        )
        roles = [line for line in r.stdout.splitlines() if "WirePlumber bluez5.roles" in line]
        self.assertEqual(len(roles), 1, r.stdout)
        self.assertNotIn("could not be read", roles[0], r.stdout)

    def test_102_docker_gc_enable_keeps_the_registry_of_mains_install(self) -> None:
        """docker-gc's protected registries move from the program into /etc/cst/docker-gc.conf.
        The upgrade from main's install (654bbda): /usr/local/sbin/docker-gc a zipapp whose
        btrfs/docker_gc/domain.py is main's (tests/fixtures/legacy; its built-in registry replaced by
        an invented one), no settings file. This checkout's `docker-gc settings` shows that registry
        as built into the installed program and that it would write the file (exit 10), and writes
        nothing; `cst btrfs docker-gc enable` writes the file with it (root, 0644) before it installs
        the new program, whose `settings` then reads it from the file (exit 0); a second `enable`
        leaves the file; `disable --purge` keeps it; `enable` after the purge reads it again and
        leaves it. Then the upgrade again (main's program, no settings file) and `disable --purge`
        before any `enable`: the purge writes the file with that registry before it removes the
        program. No Docker in this container: a stub docker.service (not started) when there is
        none, so the recorder is not started."""
        c = self.SYSTEMD_CONTAINER
        self.addCleanup(
            exec_bash,
            c,
            """
            s=/var/tmp/e2e-gc-upgrade
            SUDO_USER=tester /opt/cst-rw/cst btrfs docker-gc disable --purge >/dev/null 2>&1 || true
            rm -f /etc/cst/docker-gc.conf
            if [ -e "$s/made-docker" ]; then rm -f /run/systemd/system/docker.service; fi
            rm -rf "$s"
            systemctl daemon-reload
            """,
            check=False,
        )
        out = exec_bash(
            c,
            r"""
            set -E
            trap 'echo "test_102 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            s=/var/tmp/e2e-gc-upgrade
            bin=/usr/local/sbin/docker-gc
            conf=/etc/cst/docker-gc.conf
            reg=registry.example.com
            cst() { SUDO_USER=tester /opt/cst-rw/cst "$@"; }
            show() { sed "s/^/test_102 $1: /" <<<"$2" >&2; }   # the CI log
            sum() { echo "$(stat -c '%i %Y %a %U' "$1") $(sha256sum <"$1")"; }
            line_of() { grep -nxF "$1" <<<"$2" | cut -d: -f1; }
            rm -rf "$s"; mkdir -p "$s"
            [[ ! -e "$bin" && ! -e "$conf" ]] || { echo "a docker-gc or its settings are already there" >&2; exit 1; }
            if ! systemctl cat docker.service >/dev/null 2>&1; then
                printf '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/true\n' >/run/systemd/system/docker.service
                touch "$s/made-docker"
                systemctl daemon-reload
            fi

            # main's install: its zipapp, with main's domain.py.
            old_install() {
                python3 -I -c 'import sys, zipfile; z = zipfile.ZipFile(sys.argv[2], "w"); z.writestr("__main__.py", "raise SystemExit(1)\n"); z.write(sys.argv[1], "btrfs/docker_gc/domain.py"); z.close()' \
                    "/opt/cst-rw/tests/fixtures/legacy/btrfs-docker_gc-domain.py@654bbda" "$bin"
                chmod 0755 "$bin"
            }

            # 1. main's install.
            old_install

            # 2. The dry run: the registry built into the installed program; nothing written.
            rc=0; out="$(python3 -I -B /opt/cst-rw/btrfs/docker-gc settings)" || rc=$?
            show settings "$out"
            [[ "$rc" == 10 ]]
            grep -qF "protected registries: $reg (built into the installed $bin, while $conf is missing" <<<"$out"
            grep -qxF "would write: $conf (DOCKER_GC_PROTECT=$reg)" <<<"$out"
            [[ ! -e "$conf" ]]

            # 3. enable: the file first, then the new program.
            out="$(cst btrfs docker-gc enable 2>&1)" || { show enable "$out"; exit 1; }
            show enable "$out"
            first="$(line_of "written:   $conf (DOCKER_GC_PROTECT=$reg)" "$out")"
            prog="$(line_of "written:   $bin" "$out")"
            [[ -n "$first" && -n "$prog" && "$first" -lt "$prog" ]]
            [[ "$(stat -c '%a %U' "$conf")" == "644 root" ]]
            [[ "$(tail -n 1 "$conf")" == "DOCKER_GC_PROTECT=$reg" ]]
            rc=0; out="$("$bin" settings)" || rc=$?
            show "installed settings" "$out"
            [[ "$rc" == 0 ]]
            grep -qxF "protected registries: $reg (DOCKER_GC_PROTECT in $conf)" <<<"$out"
            c1="$(sum "$conf")"

            # 4. A second enable leaves the file.
            out="$(cst btrfs docker-gc enable 2>&1)" || { show "second enable" "$out"; exit 1; }
            show "second enable" "$out"
            grep -qxF "unchanged: $conf" <<<"$out"
            if grep -q '^written:' <<<"$out"; then echo "the second enable wrote a file" >&2; exit 1; fi
            [[ "$(sum "$conf")" == "$c1" ]]

            # 5. disable --purge keeps the settings; enable afterwards reads them again.
            out="$(cst btrfs docker-gc disable --purge 2>&1)" || { show purge "$out"; exit 1; }
            show purge "$out"
            grep -qxF "kept:      $conf (the settings: which registries are protected)" <<<"$out"
            [[ ! -e "$bin" && "$(sum "$conf")" == "$c1" ]]
            out="$(cst btrfs docker-gc enable 2>&1)" || { show "enable after purge" "$out"; exit 1; }
            show "enable after purge" "$out"
            grep -qxF "unchanged: $conf" <<<"$out"
            grep -qxF "written:   $bin" <<<"$out"
            [[ "$(sum "$conf")" == "$c1" ]]

            # 6. main's install again, no settings file, and a purge before any enable: the purge
            # writes the registry into the file before it removes the program.
            out="$(cst btrfs docker-gc disable --purge 2>&1)" || { show "purge" "$out"; exit 1; }
            rm -f "$conf"
            old_install
            out="$(cst btrfs docker-gc disable --purge 2>&1)" || { show "purge before enable" "$out"; exit 1; }
            show "purge before enable" "$out"
            grep -qxF "written:   $conf (DOCKER_GC_PROTECT=$reg)" <<<"$out"
            grep -qxF "kept:      $conf (the settings: which registries are protected)" <<<"$out"
            [[ ! -e "$bin" && "$(tail -n 1 "$conf")" == "DOCKER_GC_PROTECT=$reg" ]]
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_103_docker_gc_installer_keeps_the_registry_of_mains_install(self) -> None:
        """Every install path writes docker-gc's settings before it replaces the program, not only
        `enable`. Staged under /var/tmp/e2e-gc-manifest: main's install (654bbda; its zipapp with the
        domain.py of tests/fixtures/legacy, a registry built in) and no /etc/cst/docker-gc.conf. The
        installer's plan names the settings file and writes nothing; `installer.py install --owner
        root` (the whole manifest of root) writes the file with that registry, before the new program;
        that installed docker-gc's `run --dry-run` (a fake docker with an old image of the registry and
        old unprotected ones, the recorder running) still holds the registry's image back ("protected
        registry") and lists the others; a second install changes nothing. Fail closed: with the file
        removed, the same dry run exits 78 and asks docker nothing."""
        c = self.SYSTEMD_CONTAINER
        stage = "/var/tmp/e2e-gc-manifest"
        self.addCleanup(exec_bash, c, f"rm -rf {stage} /var/tmp/e2e-gc-dry-*", check=False)
        installer = "/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
        sets = (
            "--set UUID=00000000-0000-0000-0000-000000000000 --set OPTIONS=subvolid=5 --set USER=tester "
            "--set ALLOWED=AllowedCPUs=0"
        )
        out = exec_bash(
            c,
            f"""
            set -E
            trap 'echo "test_103 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            stage={stage}
            prog="$stage/usr/local/sbin/docker-gc"
            conf="$stage/etc/cst/docker-gc.conf"
            reg=registry.example.com
            show() {{ sed "s/^/test_103 $1: /" <<<"$2" >&2; }}   # the CI log
            rm -rf "$stage" && install -d -m 0755 "$stage" "$(dirname "$prog")"
            dry_run() {{ /usr/bin/python3 -I -c {shlex.quote(GC_DRY_RUN)} "$prog" "$conf"; }}

            # 1. main's install: its zipapp, with main's domain.py; no settings file.
            python3 -I -c 'import sys, zipfile; z = zipfile.ZipFile(sys.argv[2], "w"); z.writestr("__main__.py", "raise SystemExit(1)\\n"); z.write(sys.argv[1], "btrfs/docker_gc/domain.py"); z.close()' \
                /opt/cst-rw/tests/fixtures/legacy/btrfs-docker_gc-domain.py@654bbda "$prog"
            chmod 0755 "$prog"

            # 2. The plan names the settings file, first, and writes nothing.
            plan="$({installer} plan --root "$stage" --owner root {sets})"
            show plan "$(head -n 3 <<<"$plan")"
            [[ "$(head -n 1 <<<"$plan")" == "would write: $conf (from $prog, before it is replaced)" ]]
            [[ ! -e "$conf" ]]

            # 3. The whole manifest of root: the settings first, with the registry, then the new program.
            out="$({installer} install --root "$stage" --owner root {sets})"
            show install "$(head -n 3 <<<"$out")"
            [[ "$(head -n 1 <<<"$out")" == "written:   $conf (DOCKER_GC_PROTECT=$reg)" ]]
            grep -qxF "written:   $prog" <<<"$out"
            [[ "$(tail -n 1 "$conf")" == "DOCKER_GC_PROTECT=$reg" && "$(stat -c '%a %U' "$conf")" == "644 root" ]]
            /usr/bin/python3 -I -c 'import sys, zipfile; sys.exit("btrfs/docker_gc/application.py" not in zipfile.ZipFile(sys.argv[1]).namelist())' "$prog"

            # 4. The installed docker-gc still holds the registry's image back; the others are listed.
            rc=0; out="$(dry_run 2>&1)" || rc=$?
            show "dry run" "$out"
            [[ "$rc" == 0 ]]
            grep -qxF "held back: 1 image(s) (protected registry: cleanup-now only)" <<<"$out"
            grep -qF "  alpine:old  (last use " <<<"$out"
            if grep -qF "$reg/team/db" <<<"$out"; then echo "the protected image is listed for removal" >&2; exit 1; fi
            grep -qxF "(dry run: nothing removed)" <<<"$out"

            # 5. A second install changes nothing (no settings step: the file is there).
            again="$({installer} install --root "$stage" --owner root {sets})"
            if grep -v '^unchanged: ' <<<"$again"; then echo "a second install changed files" >&2; exit 1; fi

            # 6. Fail closed: this docker-gc has no registry of its own; without the file it removes nothing.
            rm -f "$conf"
            rc=0; out="$(dry_run 2>&1)" || rc=$?
            show "dry run, no settings" "$out"
            [[ "$rc" == 78 ]]
            grep -qF "$conf is missing, and the installed $prog reads them from that file" <<<"$out"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_112_docker_gc_stops_on_a_record_without_a_key_the_plan_needs(self) -> None:
        """The installed docker-gc (the whole manifest of root, staged under /var/tmp/e2e-gc-hole) with
        a fake docker whose container record lacks "Image": run --dry-run and status exit 76 naming the
        record and the key ("nothing done"), twice; docker is asked to remove nothing, its objects and
        last-used.json are unchanged. (A plain run plans the same way, but is skipped in the first
        10 minutes after boot: the unit tests cover it.)"""
        c = self.SYSTEMD_CONTAINER
        stage = "/var/tmp/e2e-gc-hole"
        self.addCleanup(exec_bash, c, f"rm -rf {stage} /var/tmp/e2e-gc-hole-*", check=False)
        installer = "/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
        sets = (
            "--set UUID=00000000-0000-0000-0000-000000000000 --set OPTIONS=subvolid=5 --set USER=tester "
            "--set ALLOWED=AllowedCPUs=0"
        )
        out = exec_bash(
            c,
            f"""
            set -E
            trap 'echo "test_112 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            stage={stage}
            rm -rf "$stage" && install -d -m 0755 "$stage"
            {installer} install --root "$stage" --owner root {sets} >/dev/null
            /usr/bin/python3 -I -c {shlex.quote(GC_HOLE_RUN)} "$stage/usr/local/sbin/docker-gc" "$stage/etc/cst/docker-gc.conf"
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_113_cst_vm_refuses_a_damaged_index_and_leaves_it(self) -> None:
        """cst vm (the checkout's program; the index a scratch file, CST_VM_INDEX) with an index that is
        not JSON, then one without a "backups" list: list exits 1 naming the file and the way out, twice,
        and the file keeps its bytes. A backup and status need libvirt: their refusal is a unit test."""
        w = "/var/tmp/e2e-vm-index"
        self.addCleanup(exec_bash, self.SYSTEMD_CONTAINER, f"rm -rf {w}", check=False)
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            set -E
            trap 'echo "test_113 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            w={w}
            rm -rf "$w" && mkdir -p "$w"
            export CST_PROG="cst vm" CST_VM_INDEX="$w/vm-backups.json"
            for text in '{{"backups": [' '{{"version": 1}}'; do
                printf '%s' "$text" >"$CST_VM_INDEX"
                before="$(sha256sum <"$CST_VM_INDEX")"
                for args in list list; do
                    rc=0; err="$(/usr/bin/python3 -I /opt/cst-rw/vm/cst-vm $args 2>&1 >/dev/null)" || rc=$?
                    echo "test_113 $args: rc=$rc $err" >&2
                    [[ "$rc" == 1 ]]
                    grep -qF "$CST_VM_INDEX cannot be read as cst vm's backup index" <<<"$err"
                    grep -qF "move it aside (mv $CST_VM_INDEX $CST_VM_INDEX.bad) to start a new index" <<<"$err"
                done
                [[ "$(sha256sum <"$CST_VM_INDEX")" == "$before" ]]
            done
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_114_docker_volumes_nocow_waits_for_a_daemon_json_it_cannot_read(self) -> None:
        """docker-volumes-nocow (the checkout's program, as root in the container) with a migration
        requested and an /etc/docker/daemon.json that is not JSON: migrate changes nothing, records
        the boot as postponed with the file's reason, and status names the file; mark exits 2 and marks
        nothing. With the file fixed, the reason is gone from the next run. The check comes before
        any btrfs call, so the container's file system is enough."""
        c = self.SYSTEMD_CONTAINER
        self.addCleanup(
            exec_bash,
            c,
            "rm -rf /etc/docker/daemon.json /etc/btrfs-setup/docker-nocow-pending /var/lib/docker-volumes-nocow "
            "/var/tmp/e2e-nocow; [ -e /var/tmp/e2e-nocow-docker ] && rm -rf /etc/docker || true; rm -f /var/tmp/e2e-nocow-docker",
            check=False,
        )
        out = exec_bash(
            c,
            """
            set -E
            trap 'echo "test_114 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            export LC_ALL=C
            prog="/usr/bin/python3 -I /opt/cst-rw/btrfs/docker-volumes-nocow"
            [ -e /etc/docker ] || { mkdir -p /etc/docker && touch /var/tmp/e2e-nocow-docker; }
            [ ! -e /etc/docker/daemon.json ] || { echo "a daemon.json is there already" >&2; exit 1; }
            printf '{' >/etc/docker/daemon.json
            install -d -m 0755 /etc/btrfs-setup && touch /etc/btrfs-setup/docker-nocow-pending
            mkdir -p /var/tmp/e2e-nocow && ls -la /var/lib/docker 2>/dev/null >/var/tmp/e2e-nocow/before || true
            rc=0; out="$($prog migrate 2>&1)" || rc=$?
            echo "test_114 migrate: rc=$rc $out" >&2
            [[ "$rc" == 0 ]]
            grep -qF "/etc/docker/daemon.json: not JSON, so the volumes folder Docker uses is unknown: migration left for the next boot" <<<"$out"
            grep -qF '"reason": "/etc/docker/daemon.json: not JSON, so the volumes folder Docker uses is unknown"' \
                /var/lib/docker-volumes-nocow/postponed
            ls -la /var/lib/docker 2>/dev/null >/var/tmp/e2e-nocow/after || true
            cmp -s /var/tmp/e2e-nocow/before /var/tmp/e2e-nocow/after
            [[ -e /etc/btrfs-setup/docker-nocow-pending ]]
            status="$($prog status 2>&1)" || true
            grep -qF "/etc/docker/daemon.json: not JSON: the boot's migration waits until it is fixed" <<<"$status"
            rc=0; out="$($prog mark 2>&1)" || rc=$?
            [[ "$rc" == 2 ]]
            grep -qF "nothing marked (fix the file, then run it again)" <<<"$out"
            printf '{}' >/etc/docker/daemon.json
            out="$($prog migrate 2>&1)" || true
            echo "test_114 fixed: $out" >&2
            if grep -qF "daemon.json" <<<"$out"; then echo "the fixed file still holds the migration back" >&2; exit 1; fi
            if grep -qF "daemon.json" /var/lib/docker-volumes-nocow/postponed 2>/dev/null; then
                echo "the record still names the fixed file" >&2; exit 1
            fi
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_81_snapshots_setup_turns_on_the_toolchains_timer_as_the_user(self) -> None:
        """setup's user steps (snap_user_configs of btrfs/setup-snapshots.bash), run as root
        the way `snapshots setup` runs them, with what sudo can hand root (its own
        XDG_RUNTIME_DIR and bus, which runuser keeps): `toolchains auto on --unless-off`
        reaches tester's real user manager, the timer is enabled and active, and tester's
        install record says toolchains:auto is done. The same variables make systemctl
        --user fail when nothing replaces them (checked first, else the test proves
        nothing). Afterwards the earlier state is put back."""
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "test_81 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            H=/home/tester
            off=$H/.config/cst/toolchains-auto.off
            was_off=0; [[ -e "$off" ]] && was_off=1
            rm -f "$off"
            as_tester() { runuser -u tester -- env HOME=/home/tester USER=tester LOGNAME=tester \
                XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "$@"; }
            restore() {
                as_tester /opt/cst-rw/cst toolchains auto off >/dev/null 2>&1 || true
                (( was_off )) || rm -f "$off"
            }
            trap restore EXIT
            export XDG_RUNTIME_DIR=/run/user/0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus
            if runuser -u tester -- systemctl --user show-environment >/dev/null 2>&1; then
                echo "root's variables still reach tester's manager: the test would prove nothing" >&2
                exit 1
            fi
            [[ -S /run/user/1000/systemd/private ]]
            out="$(SRC=/opt/cst-rw/btrfs PROG="cst btrfs" target_user=tester bash -euo pipefail -c '
                . "$SRC/setup-snapshots.bash"; snap_user_configs /home/tester' 2>&1)"
            sed 's/^/test_81: /' <<<"$out" >&2
            if grep -qF NOTE: <<<"$out"; then echo "setup's user steps reported a problem" >&2; exit 1; fi
            grep -qF "enabled:   cst-toolchains-prune.timer" <<<"$out"
            as_tester systemctl --user is-enabled cst-toolchains-prune.timer
            as_tester systemctl --user is-active cst-toolchains-prune.timer
            python3 -I -c 'import json, sys; sys.exit(json.load(open(sys.argv[1]))["components"].get("toolchains:auto") != "done")' \
                "$H/.local/state/cst/installed.json"
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_82_toolchains_auto_on_reaches_the_users_manager_with_roots_variables(self) -> None:
        """`cst toolchains auto on` run as tester directly (not through snapshots setup), with
        the XDG_RUNTIME_DIR and bus of root that sudo and runuser can pass on: the program's
        own systemctl --user calls use tester's /run/user/1000 and bus instead, so the timer
        is enabled and active. The same variables make systemctl --user fail as they are
        (checked first, else the test proves nothing). Afterwards the earlier state is put back."""
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "test_82 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
            H=/home/tester
            off=$H/.config/cst/toolchains-auto.off
            was_off=0; [[ -e "$off" ]] && was_off=1
            as_tester() { runuser -u tester -- env HOME=/home/tester USER=tester LOGNAME=tester "$@"; }
            mine=(XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus)
            roots=(XDG_RUNTIME_DIR=/run/user/0 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus)
            restore() {
                as_tester "${mine[@]}" /opt/cst-rw/cst toolchains auto off >/dev/null 2>&1 || true
                (( was_off )) || rm -f "$off"
            }
            trap restore EXIT
            if as_tester "${roots[@]}" systemctl --user show-environment >/dev/null 2>&1; then
                echo "root's variables still reach tester's manager: the test would prove nothing" >&2
                exit 1
            fi
            as_tester "${mine[@]}" /opt/cst-rw/cst toolchains auto off >/dev/null 2>&1 || true   # the check below is what counts
            if as_tester "${mine[@]}" systemctl --user is-enabled cst-toolchains-prune.timer >/dev/null 2>&1; then
                echo "auto off left the timer enabled: the test would prove nothing" >&2
                exit 1
            fi
            out="$(as_tester "${roots[@]}" /opt/cst-rw/cst toolchains auto on 2>&1)"
            sed 's/^/test_82: /' <<<"$out" >&2
            grep -qF "enabled:   cst-toolchains-prune.timer" <<<"$out"
            as_tester "${mine[@]}" systemctl --user is-enabled cst-toolchains-prune.timer
            as_tester "${mine[@]}" systemctl --user is-active cst-toolchains-prune.timer
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_95_bluetooth_python_program_with_the_bash_one_runuser_and_le_audio(self) -> None:
        """`cst bluetooth` after its port to Python (bluetooth/bt-setup), with the real user
        manager and WirePlumber. Mixed versions share the record of restarts still owed and
        the files, both ways: the previous bash program (the fixture
        tests/fixtures/legacy/bluetooth-setup.sh@d4d8c91, in a copy of the checkout outside the
        noexec /tmp) applies
        without restarting and the Python one's plan still owes WirePlumber's restart (exit
        10), its apply makes it, and the bash one's plan has nothing to do (exit 0); then the
        other way round with another LDAC quality. Run as root with SUDO_USER, the program runs
        again as that user (runuser: the user's settings are read); without SUDO_USER it
        refuses (exit 1). `le-audio on` writes the setting and runs apply in the same process
        (exec), which writes the files; `le-audio off` likewise."""
        self.addCleanup(
            as_tester_bash,
            self.SYSTEMD_CONTAINER,
            "/opt/cst-rw/cst bluetooth revert --yes --no-restart >/dev/null 2>&1 || true; "
            "rm -f /home/tester/.config/cst/bluetooth.conf",
            check=False,
        )
        # Not under /tmp: Docker's --tmpfs /tmp is noexec, and the old program must run.
        self.addCleanup(exec_bash, self.SYSTEMD_CONTAINER, "rm -rf /opt/cst-old", check=False)
        exec_bash(
            self.SYSTEMD_CONTAINER,
            """
            mkdir -p /etc/bluetooth && printf '[General]\\n# Name = BlueZ\\n' >/etc/bluetooth/main.conf
            rm -rf /opt/cst-old && cp -a /opt/cst-rw /opt/cst-old
            install -m 0755 '/opt/cst-rw/tests/fixtures/legacy/bluetooth-setup.sh@d4d8c91' /opt/cst-old/bluetooth-setup.sh
            chown -R tester: /opt/cst-old
            """,
        )
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "test_95 FAILED at line $LINENO: $BASH_COMMAND" >&2; echo "--- bt95.out:" >&2; cat /tmp/bt95.out >&2' ERR
            NEW=/opt/cst-rw/bluetooth-setup.sh
            OLD=/opt/cst-old/bluetooth-setup.sh
            # apply PROGRAM LINE ARGS...: exit 1 (its checks find no Bluetooth adapter in the
            # container), after the step that prints LINE.
            apply() {
                local prog="$1" line="$2"
                shift 2
                rc=0; "$prog" apply "$@" >/tmp/bt95.out 2>&1 || rc=$?
                [[ $rc == 1 ]]
                grep -qF 'no Bluetooth adapter in org.bluez' /tmp/bt95.out
                grep -qF "$line" /tmp/bt95.out
            }
            grep -q 'bluetooth/bt-setup' "$NEW"
            if grep -q 'bluetooth/bt-setup' "$OLD"; then echo "the previous program is the new one" >&2; exit 1; fi
            mkdir -p /home/tester/.config/cst
            printf 'DEVICE=00:11:22:33:44:55\nLDAC_QUALITY=sq\n' >/home/tester/.config/cst/bluetooth.conf
            # The bash program skips the restart; the Python one still owes it, makes it; bash: nothing to do.
            apply "$OLD" 'Not restarted (--no-restart)' --yes --no-restart
            rc=0; "$NEW" plan >/tmp/bt95.out 2>&1 || rc=$?
            [[ $rc == 10 ]]
            grep -qF '(still owed: an earlier run did not restart it)' /tmp/bt95.out
            apply "$NEW" 'restart:   wireplumber (your session; audio stops for a moment)' --yes
            rc=0; "$OLD" plan >/tmp/bt95.out 2>&1 || rc=$?
            [[ $rc == 0 ]]
            grep -qx 'Nothing to do.' /tmp/bt95.out
            # The other way round, with another quality.
            apply "$NEW" 'Not restarted (--no-restart)' --yes --no-restart --ldac-quality hq
            rc=0; "$OLD" plan --ldac-quality hq >/tmp/bt95.out 2>&1 || rc=$?
            [[ $rc == 10 ]]
            grep -qF '(still owed: an earlier run did not restart it)' /tmp/bt95.out
            apply "$OLD" 'restart:   wireplumber (your session; audio stops for a moment)' --yes --ldac-quality hq
            rc=0; "$NEW" plan --ldac-quality hq >/tmp/bt95.out 2>&1 || rc=$?
            [[ $rc == 0 ]]
            # le-audio on: the setting, then apply in the same process (its files written).
            out="$(/opt/cst-rw/cst bluetooth le-audio on --yes --no-restart 2>&1 || true)"
            grep -qF 'written:   ~/.config/cst/bluetooth.conf: LE_AUDIO=on' <<<"$out"
            # cst names itself by the path it was run as when it is not the cst on PATH.
            grep -qE '^Now: (/opt/cst-rw/)?cst bluetooth apply --yes --no-restart$' <<<"$out"
            grep -qF '== BlueZ: /etc/bluetooth/main.conf' <<<"$out"
            grep -qx 'LE_AUDIO=on' /home/tester/.config/cst/bluetooth.conf
            test ! -e /home/tester/.config/wireplumber/wireplumber.conf.d/90-cst-le-audio-off.conf
            out="$(/opt/cst-rw/cst bluetooth le-audio off --yes --no-restart 2>&1 || true)"
            grep -qF 'written:   ~/.config/cst/bluetooth.conf: LE_AUDIO=off' <<<"$out"
            test -e /home/tester/.config/wireplumber/wireplumber.conf.d/90-cst-le-audio-off.conf
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)
        # As root: with SUDO_USER it runs again as that user (tester's settings: quality sq).
        root = exec_bash(
            self.SYSTEMD_CONTAINER,
            "rc=0; SUDO_USER=tester /opt/cst-rw/cst bluetooth plan || rc=$?; echo rc=$rc",
            check=False,
        )
        self.assertIn("Headset 00:11:22:33:44:55 (settings), LDAC quality sq", root.stdout, root.stdout + root.stderr)
        self.assertIn("rc=10", root.stdout)
        refused = exec_bash(self.SYSTEMD_CONTAINER, "env -u SUDO_USER /opt/cst-rw/cst bluetooth plan", check=False)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("ERROR: run it from your user account (needs SUDO_USER)", refused.stderr)

    def test_83_sccache_server_starts_in_its_own_scope_from_a_capped_compile(self) -> None:
        """The real rustc wrapper (installed by `cst toolchains config apply
        --sccache`), real cargo and the real user manager, with a fake sccache
        that daemonises like the real one (its server stays in the cgroup it
        starts in, and the socket comes 0.3 s after the start; unlike the real
        one it runs rustc in the client's cgroup). The toolbox is a stand-in
        (CST_SCCACHE_CONTAINERENV names "e2ebox"), so this is the toolbox
        branch; the host one (app.slice) has unit tests only. The first build
        runs in a transient scope with MemoryMax, like a test harness's capped
        lane: the wrapper must start the one server in
        cst-sccache-e2ebox.scope in the inner user.slice (MemoryMax infinity),
        not in the capped scope. cargo's first rustc call (its version probe)
        comes alone, so the start is not contended here; the unit tests cover
        the lock. What a fake cannot prove: where systemd puts a --user --scope
        started from inside another unit, that the scope's folder is where the
        wrapper looks, and that the scope goes away when the server stops.
        Also: a second build starts nothing; with a server an older wrapper
        left in a capped scope, the next build starts one in the scope (the old
        one runs on); a start that systemd refuses (a unit of that name in
        another slice) is tried once, and the server the client then starts
        is used without another try; --no-sccache undoes the cargo setting."""
        self.addCleanup(
            as_tester_bash,
            self.SYSTEMD_CONTAINER,
            """
            for f in /home/tester/work/scc-scope/state/*.pid; do
                [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true
            done
            systemctl --user stop 'e2e-scc-*' cst-sccache-e2ebox.scope 2>/dev/null || true
            /opt/cst-rw/cst toolchains config apply --no-sccache >/dev/null 2>&1 || true
            rm -rf /home/tester/work/scc-scope /run/user/1000/cst-sccache-e2ebox.sock \
                /run/user/1000/cst-sccache-e2ebox.sock.lock /run/user/1000/cst-sccache-e2ebox.sock.failed
            """,
            check=False,
        )
        out = as_tester_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            set -E
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            cst=/opt/cst-rw/cst
            w=/home/tester/work/scc-scope
            st=$w/state
            ucg=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service
            scope=cst-sccache-e2ebox.scope
            want="0::/user.slice/user-1000.slice/user@1000.service/user.slice/$scope"
            sock=/run/user/1000/cst-sccache-e2ebox.sock
            rm -rf "$w" "$sock" "$sock.lock"
            mkdir -p "$w/bin" "$w/crate/src" "$st"
            printf 'name="e2ebox"\n' >"$w/containerenv"
            # The fake sccache. --start-server: a daemon (setsid, stdio closed)
            # that records its cgroup, then the socket 0.3 s later. A compile
            # with no live server starts one in its own cgroup, as the real
            # client does.
            cat >"$w/bin/sccache" <<'EOF'
#!/bin/sh
st=$SCCACHE_E2E_STATE
serve() {
    setsid sh -c 'cat /proc/self/cgroup >"$1/server-$$.cgroup"; echo $$ >"$1/server-$$.pid"; echo $$ >"$1/live"; exec sleep 300' \
        sh "$st" </dev/null >/dev/null 2>&1 &
    for _ in $(seq 50); do [ -s "$st/live" ] && break; sleep 0.1; done
}
sock() { python3 -I -c 'import os, stat, sys; os.mknod(sys.argv[1], stat.S_IFSOCK | 0o600)' "$SCCACHE_SERVER_UDS"; }
live() { [ -s "$st/live" ] && kill -0 "$(cat "$st/live")" 2>/dev/null; }
if [ "${1:-}" = --start-server ]; then
    echo start >>"$st/starts"
    rm -f "$st/live"
    serve
    sleep 0.3
    [ -e "$SCCACHE_SERVER_UDS" ] || sock
    exit 0
fi
if ! live || [ ! -e "$SCCACHE_SERVER_UDS" ]; then
    echo auto >>"$st/starts"
    rm -f "$st/live"
    serve
    [ -e "$SCCACHE_SERVER_UDS" ] || sock
fi
cat /proc/self/cgroup >>"$st/compiles"
exec "$@"
EOF
            chmod 755 "$w/bin/sccache"
            # A crate with six path dependencies: cargo compiles them in parallel.
            printf '[package]\nname = "e2escc"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n' \
                >"$w/crate/Cargo.toml"
            for i in 1 2 3 4 5 6; do
                mkdir -p "$w/crate/d$i/src"
                printf '[package]\nname = "d%s"\nversion = "0.1.0"\nedition = "2021"\n' "$i" >"$w/crate/d$i/Cargo.toml"
                echo "pub fn f() -> u32 { $i }" >"$w/crate/d$i/src/lib.rs"
                printf 'd%s = { path = "d%s" }\n' "$i" "$i" >>"$w/crate/Cargo.toml"
            done
            echo 'pub fn all() -> u32 { d1::f() + d2::f() + d3::f() + d4::f() + d5::f() + d6::f() }' \
                >"$w/crate/src/lib.rs"

            "$cst" toolchains config apply --sccache >/dev/null
            wrapper=/home/tester/.local/libexec/cst/rustc-wrapper
            grep -q "^rustc-wrapper = \"$wrapper\"" /home/tester/.cargo/config.toml

            # build N: cargo build from scratch inside a capped transient scope
            # (MemoryMax=1G), started from a user service: a --user --scope
            # needs a caller that the user manager may move.
            build() {
                local n=$1 jobs=${2:-8}
                systemd-run --user --wait --pipe --quiet --collect --unit="e2e-scc-launch-$n" \
                    -E PATH="$w/bin:$PATH" -E SCCACHE_E2E_STATE="$st" -E CST_SCCACHE_CONTAINERENV="$w/containerenv" \
                    -E CARGO_INCREMENTAL=0 -E CARGO_TARGET_DIR="$w/t$n" -E CARGO_BUILD_BUILD_DIR="$w/b$n" \
                    -E DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus --working-directory="$w/crate" \
                    systemd-run --user --scope --quiet --collect --unit="e2e-scc-cap-$n.scope" -p MemoryMax=1G \
                    cargo build --offline -j"$jobs" </dev/null >"$w/cargo-$n.out" 2>&1 || { cat "$w/cargo-$n.out" >&2; exit 1; }
            }
            starts() { tr '\n' ' ' <"$st/starts"; }

            build 1
            # One start, by the wrapper; no compile found the server missing.
            [ "$(starts)" = "start " ] || { echo "e2e: starts: '$(starts)'" >&2; exit 1; }
            # The compiles ran in the capped scope; the server is in its own one.
            [ "$(grep -c 'e2e-scc-cap-1.scope' "$st/compiles")" -ge 7 ]
            if grep -v 'e2e-scc-cap-1.scope' "$st/compiles"; then exit 1; fi
            server=$(cat "$st/live")
            [ "$(cat "$st/server-$server.cgroup")" = "$want" ]
            test -d "$ucg/user.slice/$scope"
            [ "$(systemctl --user show -P Slice "$scope")" = user.slice ]
            [ "$(systemctl --user show -P MemoryMax "$scope")" = infinity ]
            # The capped scope ended with its build: nothing of the server is in it.
            for _ in $(seq 50); do systemctl --user -q is-active e2e-scc-cap-1.scope || break; sleep 0.1; done
            if systemctl --user -q is-active e2e-scc-cap-1.scope; then exit 1; fi

            # A second build: the server is there, nothing starts.
            build 2
            [ "$(starts)" = "start " ]

            # The server stops: the scope goes away.
            kill "$server"
            for _ in $(seq 50); do [ -d "$ucg/user.slice/$scope" ] || break; sleep 0.1; done
            test ! -d "$ucg/user.slice/$scope"
            [ "$(systemctl --user show -P LoadState "$scope")" = not-found ]

            # Upgrade: a server that an older wrapper left in a capped scope (it
            # answers on the socket). The next build starts one in the scope.
            : >"$st/starts"
            rm -f "$sock"
            systemd-run --user --wait --quiet --collect --unit=e2e-scc-launch-3 -E SCCACHE_E2E_STATE="$st" \
                -E SCCACHE_SERVER_UDS="$sock" \
                systemd-run --user --scope --quiet --unit=e2e-scc-old.scope -p MemoryMax=1G "$w/bin/sccache" true
            old=$(cat "$st/live")
            grep -q e2e-scc-old.scope "$st/server-$old.cgroup"
            build 4
            [ "$(starts)" = "auto start " ] || { echo "e2e: starts: '$(starts)'" >&2; exit 1; }
            [ "$(cat "$st/server-$(cat "$st/live").cgroup")" = "$want" ]
            kill -0 "$old"   # the old one runs on until it stops by itself (killed here)
            kill "$old" "$(cat "$st/live")"
            for _ in $(seq 50); do [ -d "$ucg/user.slice/$scope" ] || break; sleep 0.1; done

            # A start that systemd refuses: a unit of the scope's name is already
            # there, in another slice, so the scope's folder never appears. One
            # try (cargo's first rustc call), then a marker; the server the
            # client starts is used by every later compile (one at a time here:
            # -j1), and no compile removes its socket to try again.
            : >"$st/starts"
            rm -f "$sock" "$sock.failed"
            systemd-run --user --quiet --collect --unit=e2e-scc-block \
                systemd-run --user --scope --quiet --unit="$scope" --slice=app.slice sleep 300
            for _ in $(seq 50); do systemctl --user -q is-active "$scope" && break; sleep 0.1; done
            systemctl --user -q is-active "$scope"
            [ "$(systemctl --user show -P Slice "$scope")" = app.slice ]
            build 5 1
            [ "$(starts)" = "auto " ] || { echo "e2e: starts: '$(starts)'" >&2; exit 1; }
            [ -s "$sock.failed" ]
            test -S "$sock"
            grep -q e2e-scc-cap-5.scope "$st/server-$(cat "$st/live").cgroup"
            kill "$(cat "$st/live")"
            systemctl --user stop e2e-scc-block "$scope" 2>/dev/null || true

            # Undo: cargo no longer names the wrapper.
            "$cst" toolchains config apply --no-sccache >/dev/null
            if grep -q '^rustc-wrapper' /home/tester/.cargo/config.toml; then exit 1; fi
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    # The previous version of the cpu programs (the bash cpu-setup.sh and game-profile of cfb496a, the
    # last commit before the port), vendored in tests/fixtures/legacy (tests/e2e/README.md,
    # "Mixed-version tests"): test_84 installs it as the version an upgraded machine has, test_85 runs
    # its game profile next to this one.
    LEGACY = "/opt/cst-rw/tests/fixtures/legacy"
    LEGACY_CPU_DIR = "/var/tmp/e2e-cst-legacy-cpu"

    def legacy_cpu_tree(self) -> str:
        """A copy of this checkout with the previous version's cpu-setup.sh, game-profile and install
        manifest (the files the port changed that the old install reads) put back. The manifest is
        only the table of the previous install.py, put into this checkout's (LEGACY_MANIFEST)."""
        d, src = self.LEGACY_CPU_DIR, self.LEGACY
        exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            rm -rf {d} && cp -a /opt/cst-rw {d}
            install -m 0644 {src}/cpu-setup.sh@cfb496a {d}/cpu-setup.sh
            install -m 0644 {src}/game-profile@cfb496a {d}/cpu/game-profile
            /usr/bin/python3 -I -c {shlex.quote(LEGACY_MANIFEST)} {src}/install.py@cfb496a {d}/cst_core/domain/install.py
            find {d} -name __pycache__ -prune -exec rm -rf {{}} +
            chmod -R a+rX {d}
            """,
        )
        return d

    def test_84_cpu_install_cycle_over_the_bash_version(self) -> None:
        """`cst cpu install` as root for tester with the real systemd, over what the bash
        cpu-setup.sh (main before the port) installed: only the programs are written again (the game
        profile becomes a zipapp; the other zipapps carry the new cst_core), every unit, drop-in, rule
        and setting stays as it was; `status` runs; a second install writes nothing; `uninstall`
        removes the units, programs, rule and drop-ins (and their record lines) and stops the timer; a
        second uninstall works too. The bash version's uninstall then finds nothing left."""
        old = self.legacy_cpu_tree()
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"""
            SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 /opt/cst-rw/cst cpu uninstall >/dev/null 2>&1
            rm -f /etc/cpu-setup/cpu-setup.conf
            if [[ -e /tmp/e2e-84-conf.bak ]]; then mv /tmp/e2e-84-conf.bak /etc/cpu-setup/cpu-setup.conf; fi
            rm -rf {old}
            systemctl daemon-reload
            """,
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            rf"""
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            [[ ! -e /etc/cpu-setup/cpu-setup.conf ]] || cp -a /etc/cpu-setup/cpu-setup.conf /tmp/e2e-84-conf.bak
            rm -f /etc/cpu-setup/cpu-setup.conf
            as_root() {{ SUDO_USER=tester SUDO_UID=1000 SUDO_GID=1000 "$@"; }}
            as_root bash {old}/cpu-setup.sh install >/tmp/e2e-84-old.txt
            [[ "$(head -n1 /usr/local/sbin/cpu-game-profile)" == *bash* ]]
            # This version over it: the programs again (zipapps), nothing else.
            as_root /opt/cst-rw/cst cpu install >/tmp/e2e-84-new.txt
            written=$(sed -n 's/^written:   //p' /tmp/e2e-84-new.txt | sort | tr '
' ' ')
            # The game profile becomes a zipapp; cpu-watch and cpu-vm-pin carry this version's cst_core.
            [[ "$written" == "/usr/local/bin/cpu-watch /usr/local/sbin/cpu-game-profile /usr/local/sbin/cpu-vm-pin " ]] \
                || {{ echo "written over the previous version's install: $written" >&2; cat /tmp/e2e-84-new.txt >&2; exit 1; }}
            grep -q '^game profile: off' <<<"$(/usr/bin/python3 -I /usr/local/sbin/cpu-game-profile state)"  # no pipe: grep -q would cut it
            grep -q 'unchanged: /etc/systemd/system/cpu-game.service' /tmp/e2e-84-new.txt
            grep -q 'unchanged: /home/tester/.config/systemd/user/user.slice.d/10-cpu-setup.conf' /tmp/e2e-84-new.txt
            systemctl is-active cpu-game-watchdog.timer
            grep -q 'cpu-game-profile' /var/lib/cst/installed.json
            # status (root): every section, exit 0 with a target user.
            st=$(as_root /opt/cst-rw/cst cpu status)
            for s in "game profile" GameMode "baseline weights" VM Docker "memory experiments" "last measurements"; do
                grep -qx "== $s" <<<"$st" || {{ echo "status without '$s'" >&2; exit 1; }}
            done
            # A second install writes nothing.
            as_root /opt/cst-rw/cst cpu install >/tmp/e2e-84-again.txt
            if grep -q '^written:' /tmp/e2e-84-again.txt; then cat /tmp/e2e-84-again.txt >&2; exit 1; fi
            # uninstall, twice.
            as_root /opt/cst-rw/cst cpu uninstall
            for f in /usr/local/sbin/cpu-game-profile /usr/local/sbin/cpu-vm-pin /etc/systemd/system/cpu-game.service                     /etc/systemd/system/machine.slice.d/10-cpu-setup.conf /etc/polkit-1/rules.d/50-cpu-setup-game.rules                     /home/tester/.config/systemd/user/user.slice.d/10-cpu-setup.conf; do
                [[ ! -e "$f" ]] || {{ echo "uninstall left $f" >&2; exit 1; }}
            done
            if systemctl -q is-active cpu-game-watchdog.timer; then echo "the watchdog still runs" >&2; exit 1; fi
            if grep -q 'cpu-game-profile' /var/lib/cst/installed.json; then echo "the record still lists the game profile" >&2; exit 1; fi
            as_root /opt/cst-rw/cst cpu uninstall >/dev/null
            as_root bash {old}/cpu-setup.sh uninstall >/dev/null
            rm -f /etc/cpu-setup/cpu-setup.conf
            [[ ! -e /tmp/e2e-84-conf.bak ]] || mv /tmp/e2e-84-conf.bak /etc/cpu-setup/cpu-setup.conf
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_85_cpu_game_profile_bash_and_python_copies_share_their_state_and_lock(self) -> None:
        """The game profile of an older CST (bash) and this one (Python) with the real system and
        user managers, both ways: one switches it on, the other's tick adds nothing and its state
        reads it, and the other switches it off, which restores machine.slice's weight and removes
        game.state and the zz- drop-ins. Each waits for the other's run lock (flock(2) on
        /run/cpu-setup/lock)."""
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            """
            python3 -I -B /opt/cst-rw/cpu/game-profile off || true
            rm -rf /run/cpu-setup
            rm -f /etc/cpu-setup/cpu-setup.conf
            if [[ -e /tmp/e2e-85-conf.bak ]]; then mv /tmp/e2e-85-conf.bak /etc/cpu-setup/cpu-setup.conf; fi
            rmdir /etc/cpu-setup 2>/dev/null || true
            systemctl daemon-reload
            """,
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            rf"""
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            [[ ! -e /etc/cpu-setup/cpu-setup.conf ]] || cp -a /etc/cpu-setup/cpu-setup.conf /tmp/e2e-85-conf.bak
            install -d /etc/cpu-setup /run/cpu-setup
            printf '%s
' "TARGET_USER='tester'" "BUILD_CPUS='0'" "P_CORES='0'" "VM_WEIGHT=10"                 "GAME_MEMORY_LOW='16G'" "GAME_ZSWAP_WRITEBACK='yes'" >/etc/cpu-setup/cpu-setup.conf
            touch /run/cpu-setup/manual   # no GameMode client here: tick must not switch off
            bashgp() {{ bash {self.LEGACY}/game-profile@cfb496a "$@"; }}
            pygp() {{ python3 -I -B /opt/cst-rw/cpu/game-profile "$@"; }}
            base=$(systemctl show -P CPUWeight machine.slice)
            for pair in "bashgp pygp" "pygp bashgp"; do
                read -r first second <<<"$pair"
                touch /run/cpu-setup/manual
                $first on
                [[ "$(systemctl show -P CPUWeight machine.slice)" == 10 ]]
                # This version's game.intended names its format; the bash copy skips that line.
                if [[ $first == pygp ]]; then [[ "$(head -n1 /run/cpu-setup/game.intended)" == "cst-game-intended 1" ]]; fi
                n1=$(wc -l </run/cpu-setup/game.state)
                $second tick
                [[ "$(wc -l </run/cpu-setup/game.state)" == "$n1" ]] || {{ echo "$second tick added records" >&2; exit 1; }}
                st=$($second state)
                grep -q '^game profile: ON (by hand)' <<<"$st"
                grep -q 'drift: none' <<<"$st" || {{ echo "$second state: $st" >&2; exit 1; }}
                # Each copy reads the records of the other's game.intended (past the format line): a
                # drift is seen by its state and set again by its tick.
                systemctl set-property --runtime machine.slice CPUWeight=77
                st=$($second state)
                grep -qF "DRIFT: machine.slice CPUWeight is '77', the profile set '10'" <<<"$st" \
                    || {{ echo "$second state saw no drift: $st" >&2; exit 1; }}
                $second tick
                [[ "$(systemctl show -P CPUWeight machine.slice)" == 10 ]] || {{ echo "$second tick did not repair" >&2; exit 1; }}
                $second off
                test ! -e /run/cpu-setup/game.state
                test -z "$(ls /run/systemd/system/*.d/zz-cst-game.conf 2>/dev/null)"
                [[ "$(systemctl show -P CPUWeight machine.slice)" == "$base" ]]
            done
            # The lock: a holder of either kind keeps the other waiting.
            (flock /run/cpu-setup/lock sleep 3) &
            sleep 0.5
            t0=$(date +%s); pygp tick; t1=$(date +%s)
            (( t1 - t0 >= 2 )) || {{ echo "the Python copy did not wait for flock's lock" >&2; exit 1; }}
            wait
            python3 -I -c 'import fcntl, os, time; fd = os.open("/run/cpu-setup/lock", os.O_WRONLY); fcntl.flock(fd, fcntl.LOCK_EX); time.sleep(3)' &
            sleep 0.5
            t0=$(date +%s); bashgp tick; t1=$(date +%s)
            (( t1 - t0 >= 2 )) || {{ echo "the bash copy did not wait for the Python lock" >&2; exit 1; }}
            wait
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_107_cpu_game_profile_on_with_a_full_run_folder_leaves_the_unit_active(self) -> None:
        """'on' with /run/cpu-setup full (a tmpfs mounted over it, filled to its last page): the
        records cannot be written, so 'on' changes nothing it could not undo, logs why and exits 0.
        cpu-game.service (the shipped unit, its programs pointed at this checkout) is then active,
        not failed, so the watchdog's tick, with no GameMode client left, stops it and its
        ExecStop ('off') runs: game.state is gone and machine.slice's weight is as it was. Before,
        'on' ended in a traceback, the oneshot unit stayed failed, its ExecStop never ran, and
        game.state kept the profile "on" until a reboot."""
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            """
            systemctl stop cpu-game.service 2>/dev/null || true
            systemctl reset-failed cpu-game.service 2>/dev/null || true
            umount /run/cpu-setup 2>/dev/null || true
            rm -rf /run/cpu-setup /run/systemd/system/cpu-game.service
            systemctl daemon-reload
            rm -f /etc/cpu-setup/cpu-setup.conf
            if [[ -e /tmp/e2e-107-conf.bak ]]; then mv /tmp/e2e-107-conf.bak /etc/cpu-setup/cpu-setup.conf; fi
            """,
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            [[ ! -e /etc/cpu-setup/cpu-setup.conf ]] || cp -a /etc/cpu-setup/cpu-setup.conf /tmp/e2e-107-conf.bak
            install -d /etc/cpu-setup
            printf '%s\n' "TARGET_USER='tester'" "BUILD_CPUS='0'" "P_CORES='0'" >/etc/cpu-setup/cpu-setup.conf
            gp="/usr/bin/python3 -I -B /opt/cst-rw/cpu/game-profile"
            unit=/run/systemd/system/cpu-game.service
            sed "s|/usr/local/sbin/cpu-game-profile|$gp|" /opt/cst-rw/cpu/units/cpu-game.service >"$unit"
            systemctl daemon-reload
            [[ "$(systemctl show -P FragmentPath cpu-game.service)" == "$unit" ]]
            base=$(systemctl show -P CPUWeight machine.slice)
            # /run/cpu-setup: one page of tmpfs, already used up.
            install -d -m 0755 /run/cpu-setup
            mount -t tmpfs -o size=4k,mode=0755 tmpfs /run/cpu-setup
            head -c 4096 /dev/zero >/run/cpu-setup/filler
            if head -c 1 /dev/zero >>/run/cpu-setup/filler 2>/dev/null; then echo "the tmpfs is not full" >&2; exit 1; fi
            systemctl start cpu-game.service
            [[ "$(systemctl show -P ActiveState cpu-game.service)" == active ]]                 || { systemctl status cpu-game.service >&2 || true; exit 1; }
            log=$(journalctl -u cpu-game.service -b --no-pager -o cat)
            grep -q 'not done, its record could not be written to /run/cpu-setup/game.state' <<<"$log"                 || { echo "$log" >&2; exit 1; }
            if grep -q Traceback <<<"$log"; then echo "$log" >&2; exit 1; fi
            [[ "$(systemctl show -P CPUWeight machine.slice)" == "$base" ]]   # nothing changed unrecorded
            test -e /run/cpu-setup/game.state
            # The watchdog, no GameMode client: it stops the unit, whose ExecStop switches the profile off.
            grep -q 'no GameMode client left: switching off' <<<"$($gp tick)"
            for _ in $(seq 100); do [[ -e /run/cpu-setup/game.state ]] || break; sleep 0.1; done
            test ! -e /run/cpu-setup/game.state
            [[ "$(systemctl show -P ActiveState cpu-game.service)" == inactive ]]
            [[ "$(systemctl show -P Result cpu-game.service)" == success ]]
            [[ "$(systemctl show -P CPUWeight machine.slice)" == "$base" ]]
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_86_cpu_game_profile_refuses_a_settings_line_bash_would_run(self) -> None:
        """With a line in cpu-setup.conf that bash would expand (`BUILD_CPUS=$(nproc)`), the real
        game profile does not switch on: exit 1, the line in its output, no game.state, machine.slice's
        weight as it was. The watchdog's tick, run as a unit with a GameMode client running (a process
        that maps a file named libgamemode), does not start cpu-game.service (a stub that records its
        starts): its unit ends in success, the line is named once, a second tick says nothing. `off`
        still works; with the line fixed, the tick starts the unit again, and `on` and `off` work."""
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            """
            python3 -I -B /opt/cst-rw/cpu/game-profile off || true
            kill "$(cat /tmp/e2e-86-client.pid 2>/dev/null)" 2>/dev/null || true
            systemctl stop cpu-game.service 2>/dev/null || true
            rm -f /run/systemd/system/cpu-game.service
            rm -f /etc/cpu-setup/cpu-setup.conf
            if [[ -e /tmp/e2e-86-conf.bak ]]; then mv /tmp/e2e-86-conf.bak /etc/cpu-setup/cpu-setup.conf; fi
            rmdir /etc/cpu-setup 2>/dev/null || true
            rm -rf /run/cpu-setup /tmp/e2e-86-client.pid /tmp/e2e-86-ready /tmp/e2e-86-starts /tmp/e2e-86-libgamemode.so
            systemctl daemon-reload || true
            """,
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            [[ ! -e /etc/cpu-setup/cpu-setup.conf ]] || cp -a /etc/cpu-setup/cpu-setup.conf /tmp/e2e-86-conf.bak
            install -d /etc/cpu-setup
            printf '%s\n' "TARGET_USER='tester'" 'BUILD_CPUS=$(nproc)' "P_CORES='0'" >/etc/cpu-setup/cpu-setup.conf
            gp() { python3 -I -B /opt/cst-rw/cpu/game-profile "$@"; }
            base=$(systemctl show -P CPUWeight machine.slice)
            rc=0; got=$(gp on) || rc=$?
            [[ $rc == 1 ]] || { echo "on with a bad line: exit $rc" >&2; exit 1; }
            grep -q 'cpu-setup.conf line 2: BUILD_CPUS=$(nproc): ' <<<"$got"
            grep -q 'not switched on' <<<"$got"
            test ! -e /run/cpu-setup/game.state
            [[ "$(systemctl show -P CPUWeight machine.slice)" == "$base" ]]
            grep -q 'settings: /etc/cpu-setup/cpu-setup.conf line 2' <<<"$(gp state)"
            # The watchdog, a game running, the profile off: cpu-game.service is not started.
            stub=/run/systemd/system/cpu-game.service
            printf '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/sh -c "echo started >>/tmp/e2e-86-starts"\n' >"$stub"
            systemctl daemon-reload
            [[ "$(systemctl show -P FragmentPath cpu-game.service)" == "$stub" ]]
            python3 -I -c 'import mmap, sys, time; f = open(sys.argv[1], "w+b"); f.write(bytes(4096)); f.flush(); m = mmap.mmap(f.fileno(), 4096); open(sys.argv[2], "w").close(); time.sleep(300)' \
                /tmp/e2e-86-libgamemode.so /tmp/e2e-86-ready </dev/null >/dev/null 2>&1 &
            echo $! >/tmp/e2e-86-client.pid
            for _ in $(seq 50); do [[ -e /tmp/e2e-86-ready ]] && break; sleep 0.1; done
            grep -q libgamemode "/proc/$(cat /tmp/e2e-86-client.pid)/maps"
            tick() { systemd-run --wait --pipe --quiet --collect --unit="e2e-86-tick-$1" python3 -I -B /opt/cst-rw/cpu/game-profile tick; }
            first=$(tick 1)   # exit 0: the unit does not fail
            grep -q 'GameMode client running, profile not switched on: a line of /etc/cpu-setup/cpu-setup.conf sets nothing' <<<"$first"                 || { echo "first tick: $first" >&2; exit 1; }
            second=$(tick 2)
            [[ -z "$second" ]] || { echo "second tick: $second" >&2; exit 1; }
            sleep 1
            test ! -e /tmp/e2e-86-starts
            [[ "$(systemctl show -P ActiveState cpu-game.service)" == inactive ]]
            test ! -e /run/cpu-setup/game.state
            gp off
            sed -i "s/^BUILD_CPUS=.*/BUILD_CPUS='0'/" /etc/cpu-setup/cpu-setup.conf
            # Fixed: the watchdog starts the unit again.
            grep -q 'switching on' <<<"$(tick 3)"
            for _ in $(seq 50); do [[ -e /tmp/e2e-86-starts ]] && break; sleep 0.1; done
            test -e /tmp/e2e-86-starts
            kill "$(cat /tmp/e2e-86-client.pid)"
            systemctl stop cpu-game.service
            rm -f "$stub"
            systemctl daemon-reload
            touch /run/cpu-setup/manual
            gp on
            [[ "$(systemctl show -P CPUWeight machine.slice)" == 10 ]]
            gp off
            [[ "$(systemctl show -P CPUWeight machine.slice)" == "$base" ]]
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_87_cpu_game_profile_off_skips_a_cut_record_and_acts_for_the_user_on_acted_for(self) -> None:
        """'off' with the real system and user managers, after 'on' for tester: a last line of
        game.state cut short (no newline, "existed 1") is named and not undone (machine.slice's
        IOWeight goes back to its own value, not 1), and with TARGET_USER changed to a line that sets
        nothing (`TARGET_USER="$(id -un tester)"`) the user manager's side is undone for tester, the
        uid game.state names: user.slice's CPUWeight back, its override file gone."""
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            """
            printf '%s\n' "TARGET_USER='tester'" "BUILD_CPUS='0'" "P_CORES='0'" >/etc/cpu-setup/cpu-setup.conf || true
            python3 -I -B /opt/cst-rw/cpu/game-profile off || true
            rm -f /etc/cpu-setup/cpu-setup.conf
            if [[ -e /tmp/e2e-87-conf.bak ]]; then mv /tmp/e2e-87-conf.bak /etc/cpu-setup/cpu-setup.conf; fi
            rmdir /etc/cpu-setup 2>/dev/null || true
            rm -rf /run/cpu-setup
            systemctl daemon-reload || true
            """,
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            r"""
            trap 'echo "e2e: failed at line $LINENO: $BASH_COMMAND" >&2' ERR
            [[ ! -e /etc/cpu-setup/cpu-setup.conf ]] || cp -a /etc/cpu-setup/cpu-setup.conf /tmp/e2e-87-conf.bak
            install -d /etc/cpu-setup /run/cpu-setup
            printf '%s\n' "TARGET_USER='tester'" "BUILD_CPUS='0'" "P_CORES='0'" >/etc/cpu-setup/cpu-setup.conf
            gp() { python3 -I -B /opt/cst-rw/cpu/game-profile "$@"; }
            uid=$(id -u tester)
            io=$(systemctl show -P IOWeight machine.slice)
            uw=$(systemctl --user -M tester@ show -P CPUWeight user.slice)
            touch /run/cpu-setup/manual
            gp on
            [[ "$(systemctl --user -M tester@ show -P CPUWeight user.slice)" == idle ]]
            override=/run/user/$uid/systemd/user.control/user.slice.d/50-CPUWeight.conf
            test -e "$override"
            grep -q "^prop sys user@$uid.service MemoryLow " /run/cpu-setup/game.state
            printf 'prop sys machine.slice IOWeight existed 1' >>/run/cpu-setup/game.state   # cut short
            n=$(( $(wc -l </run/cpu-setup/game.state) + 1 ))
            printf '%s\n' 'TARGET_USER="$(id -un tester)"' "BUILD_CPUS='0'" "P_CORES='0'" >/etc/cpu-setup/cpu-setup.conf
            got=$(gp off)
            grep -qF "game.state line $n: no record 'off' can read, skipped: prop sys machine.slice IOWeight existed 1" <<<"$got" \
                || { echo "off: $got" >&2; exit 1; }
            grep -qF "TARGET_USER not set in /etc/cpu-setup/cpu-setup.conf: undoing for tester (uid $uid)" <<<"$got" \
                || { echo "off: $got" >&2; exit 1; }
            test ! -e /run/cpu-setup/game.state
            [[ "$(systemctl show -P IOWeight machine.slice)" == "$io" ]] || { echo "IOWeight: not $io" >&2; exit 1; }
            test ! -e "$override"
            [[ "$(systemctl --user -M tester@ show -P CPUWeight user.slice)" == "$uw" ]] \
                || { echo "user.slice CPUWeight: not $uw" >&2; exit 1; }
            echo E2E-OK
            """,
        ).stdout
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

    # ---- btrfs-bulk-boot.service under the real systemd (test_76, test_77) ----------------------
    # The unit as the manifest installs it (copied to /etc when the container has none), running this
    # checkout's btrfs-bulk through a drop-in in /run; the settings, journals, boot flag, btrfs-notify
    # and new pending alerts are put back by the cleanup (btrfs-notify is moved aside, never removed).
    BULK_BOOT_SETUP = r"""
s=/var/tmp/e2e-bulk-boot
rm -rf "$s"; mkdir -p "$s/aside"
if [ ! -e /etc/systemd/system/btrfs-bulk-boot.service ]; then
    cp /opt/cst-rw/btrfs/units/btrfs-bulk-boot.service /etc/systemd/system/
    touch "$s/made-unit"
fi
mkdir -p /run/systemd/system/btrfs-bulk-boot.service.d
# ExecStopPost: the exit status and result, read after the oneshot is gone (an unreferenced
# inactive unit may be unloaded, and systemctl show would then answer for a fresh one).
printf '[Service]\nExecStart=\nExecStart=/usr/bin/python3 -I /opt/cst-rw/btrfs/btrfs-bulk boot\nExecStopPost=/bin/sh -c %s\n' \
    "'echo \"\$\$EXIT_STATUS \$\$SERVICE_RESULT\" >/var/tmp/e2e-bulk-boot/exit'" \
    >/run/systemd/system/btrfs-bulk-boot.service.d/50-e2e.conf
for p in /etc/btrfs-setup/bulk.conf /etc/btrfs-setup/bulk-at-boot /var/lib/btrfs-bulk /usr/local/sbin/btrfs-notify; do
    if [ -e "$p" ] || [ -L "$p" ]; then mkdir -p "$s/aside$(dirname "$p")"; mv "$p" "$s/aside$p"; fi
done
[ -d /var/lib/cst-alerts ] || touch "$s/made-alerts"
[ -d /var/lib/cst-alerts/pending ] || touch "$s/made-pending"
[ -e /var/lib/cst-alerts/.lock ] || touch "$s/made-lock"
ls -1 /var/lib/cst-alerts/pending 2>/dev/null | sort >"$s/pending-before" || true
id cste2ebulk >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /var/tmp/cst-e2e-bulk-home cste2ebulk
install -d -o cste2ebulk -m 0755 /var/tmp/cst-e2e-bulk-home
[ -d /etc/btrfs-setup ] || touch "$s/made-etc"
mkdir -p /etc/btrfs-setup
systemctl daemon-reload
# boot_unit: start it as at boot; exit 1 ("needs a look") counts as success (SuccessExitStatus).
boot_unit() {
    rm -f "$s/exit"
    systemctl start btrfs-bulk-boot.service || { systemctl status --no-pager btrfs-bulk-boot.service >&2; return 1; }
    for _ in $(seq 1 20); do [ -s "$s/exit" ] && break; sleep 0.25; done   # ExecStopPost may lag the job
    [ "$(cat "$s/exit" 2>/dev/null)" = "1 success" ] ||
        { echo "exit status and result: $(cat "$s/exit" 2>/dev/null)" >&2; return 1; }
}
# unit_log_has TEXT: the unit's journal since the test started has TEXT (waits for journald).
unit_log_has() {
    for _ in $(seq 1 60); do
        journalctl --sync 2>/dev/null || true
        if journalctl -q --no-pager -o cat -u btrfs-bulk-boot.service --since "@$start" | grep -qF -- "$1"; then return 0; fi
        sleep 0.5
    done
    echo "not in the unit's journal: $1" >&2
    journalctl -q --no-pager -o cat -u btrfs-bulk-boot.service --since "@$start" >&2 || true
    return 1
}
start="$(date +%s)"
"""
    BULK_BOOT_CLEANUP = r"""
s=/var/tmp/e2e-bulk-boot
systemctl reset-failed btrfs-bulk-boot.service 2>/dev/null || true
rm -f /run/systemd/system/btrfs-bulk-boot.service.d/50-e2e.conf
rmdir /run/systemd/system/btrfs-bulk-boot.service.d 2>/dev/null || true
if [ -e "$s/made-unit" ]; then rm -f /etc/systemd/system/btrfs-bulk-boot.service; fi
rm -rf /etc/btrfs-setup/bulk.conf /etc/btrfs-setup/bulk-at-boot /var/lib/btrfs-bulk
if [ -e "$s/made-alerts" ]; then
    rm -rf /var/lib/cst-alerts
else
    if [ -e "$s/made-pending" ]; then
        rm -rf /var/lib/cst-alerts/pending
    elif [ -d /var/lib/cst-alerts/pending ]; then
        ls -1 /var/lib/cst-alerts/pending | sort | comm -13 "$s/pending-before" - |
            while read -r n; do rm -f "/var/lib/cst-alerts/pending/$n"; done
    fi
    if [ -e "$s/made-lock" ]; then rm -f /var/lib/cst-alerts/.lock; fi
fi
for p in /etc/btrfs-setup/bulk.conf /etc/btrfs-setup/bulk-at-boot /var/lib/btrfs-bulk /usr/local/sbin/btrfs-notify; do
    if [ -e "$s/aside$p" ] || [ -L "$s/aside$p" ]; then mv "$s/aside$p" "$p"; fi
done
if [ -e "$s/made-etc" ]; then rmdir /etc/btrfs-setup 2>/dev/null || true; fi
userdel cste2ebulk 2>/dev/null || true
rm -rf /var/tmp/cst-e2e-bulk-home "$s"
systemctl daemon-reload
"""

    def bulk_boot(self, script: str) -> str:
        """script after BULK_BOOT_SETUP, as root in the systemd container; its output."""
        self.addCleanup(exec_bash, self.SYSTEMD_CONTAINER, self.BULK_BOOT_CLEANUP, check=False)
        return exec_bash(self.SYSTEMD_CONTAINER, self.BULK_BOOT_SETUP + script).stdout

    def test_76_bulk_boot_leaves_a_journal_of_a_newer_btrfs_bulk_alone(self) -> None:
        """btrfs-bulk-boot.service (real systemd) finds a journal of an interrupted move written by a
        newer btrfs-bulk ("v" above this checkout's bulk-journal format): the unit runs (the glob
        condition), counts "needs a look" (exit 1) as success, names the format in its journal, and
        leaves the journal byte for byte as it was, at that boot and the next one."""
        out = self.bulk_boot(
            r"""
h=/var/tmp/cst-e2e-bulk-home
printf 'BULK_USER=cste2ebulk\n' >/etc/btrfs-setup/bulk.conf
install -d -m 0755 /var/lib/btrfs-bulk
j=/var/lib/btrfs-bulk/journal-apps.json
printf '{"kind": "copy", "src": "%s/Programs", "target": "%s/Bulk/apps", "new": "%s/Bulk/apps.bulk-new-1", "old": "%s/Programs.bulk-old-1", "stamp": 1, "link": "%s/Programs", "v": 99}' \
    "$h" "$h" "$h" "$h" "$h" >"$j"
cp "$j" "$s/journal.before"
boot_unit
unit_log_has "NEEDS A LOOK: apps: $j is format 99 of the bulk-journal (this CST reads format 1 and older): written by a newer CST; left as it is"
cmp "$s/journal.before" "$j"
boot_unit
cmp "$s/journal.before" "$j"
echo E2E-OK
"""
        )
        self.assertIn("E2E-OK", out)

    def test_77_bulk_boot_refusal_reaches_doctor_without_btrfs_notify(self) -> None:
        """A bad bulk.conf line at boot with btrfs-notify missing (moved aside): the refusal's alert
        is kept in /var/lib/cst-alerts/pending in btrfs-notify's format (the unit's journal says so),
        and cst doctor, as the user, shows it and warns that the installed unit alerts through a
        missing btrfs-notify; cst-pending-alerts lists it for the next login."""
        out = self.bulk_boot(
            r"""
printf 'BULK_USER=$USER\n' >/etc/btrfs-setup/bulk.conf
printf 'apps\n' >/etc/btrfs-setup/bulk-at-boot
boot_unit
unit_log_has "alert not delivered (/usr/local/sbin/btrfs-notify is not installed): ~/Bulk: nothing moved at boot; kept in /var/lib/cst-alerts/pending/"
f="$(grep -lxF 'title=~/Bulk: nothing moved at boot' /var/lib/cst-alerts/pending/*.alert)"
[ "$(head -n 1 "$f")" = "cst-alert 1" ]
grep -qxF 'urgency=critical' "$f"
grep -qxF 'app=btrfs-bulk' "$f"
[ "$(stat -c '%a %U' "$f")" = "644 root" ]
[ "$(stat -c '%a' /var/lib/cst-alerts/pending)" = 755 ]
[ -e /etc/btrfs-setup/bulk-at-boot ]   # the items wait for the next boot
echo E2E-OK
"""
        )
        self.assertIn("E2E-OK", out)
        doctor = as_tester_bash(self.SYSTEMD_CONTAINER, "/opt/cst-rw/cst doctor", check=False).stdout
        self.assertIn("~/Bulk: nothing moved at boot", doctor)
        self.assertIn("btrfs-bulk-boot.service", doctor)
        self.assertIn("alert through /usr/local/sbin/btrfs-notify, which is missing", doctor)
        listed = as_tester_bash(
            self.SYSTEMD_CONTAINER, "python3 -I -B /opt/cst-rw/btrfs/cst-pending-alerts --list"
        ).stdout
        self.assertIn("btrfs-bulk: ~/Bulk: nothing moved at boot", listed)

    def test_78_docker_gc_recorder_stays_failed_on_a_state_of_a_newer_docker_gc(self) -> None:
        """docker-gc-recorder.service as shipped (read from the checkout; only ExecStart points at this
        checkout's docker-gc, under a name of its own; a stub docker.service when the container has
        none, for its BindsTo=), with a last-used.json of a newer docker-gc ("v": 99): the recorder exits
        65 before any docker call, and RestartPreventExitStatus= keeps systemd from starting it again
        after RestartSec=10: the unit is failed (cst doctor lists it), never restarted, its journal
        names the format once, and the file is byte for byte as it was. The same unit ending with exit
        76 (an inspect record without a key docker-gc relies on) is not restarted either. Control: the
        same unit ending with exit 1 is restarted."""
        shipped = docker_exec(
            self.SYSTEMD_CONTAINER, ["cat", "/opt/cst-rw/btrfs/units/docker-gc-recorder.service"]
        ).stdout
        self.assertEqual(shipped.count("\nExecStart="), 1, "the shipped recorder has one ExecStart= line")
        unit = "e2e-gc-recorder.service"
        path = f"/run/systemd/system/{unit}"

        def derived(exec_start: str) -> str:
            return "".join(
                f"ExecStart={exec_start}\n" if line.startswith("ExecStart=") else line
                for line in shipped.splitlines(keepends=True)
            )

        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"""
            s=/var/tmp/e2e-gc-recorder
            systemctl stop {unit} 2>/dev/null || true
            systemctl reset-failed {unit} 2>/dev/null || true
            rm -f {path}
            if [ -e "$s/made-docker" ]; then systemctl stop docker.service 2>/dev/null || true; rm -f /run/systemd/system/docker.service; fi
            if [ -e "$s/made-state" ]; then rm -rf /var/lib/docker-gc; fi
            if [ -e "$s/state-aside" ]; then mv "$s/state-aside" /var/lib/docker-gc; fi
            rm -rf "$s"
            systemctl daemon-reload
            """,
            check=False,
        )
        docker_exec(
            self.SYSTEMD_CONTAINER,
            ["bash", "-c", f"cat >{path}"],
            input_text=derived("/usr/bin/python3 -I /opt/cst-rw/btrfs/docker-gc record"),
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            s=/var/tmp/e2e-gc-recorder
            rm -rf "$s"; mkdir -p "$s"
            diag() {{ systemctl status --no-pager {unit} >&2 || true; journalctl -q --no-pager -u {unit} -n 30 >&2 || true; }}
            show() {{ systemctl show -p "$1" --value {unit}; }}
            if [ -e /var/lib/docker-gc ]; then mv /var/lib/docker-gc "$s/state-aside"; fi
            if ! systemctl cat docker.service >/dev/null 2>&1; then
                printf '[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/true\n' >/run/systemd/system/docker.service
                touch "$s/made-docker"
            fi
            install -d -m 0755 /var/lib/docker-gc
            touch "$s/made-state"   # the cleanup removes this folder only then
            printf '{{"since": 1, "images": {{}}, "heartbeat": 2, "v": 99}}' >/var/lib/docker-gc/last-used.json
            cp /var/lib/docker-gc/last-used.json "$s/before"
            systemctl daemon-reload
            start="$(date +%s)"
            systemctl start {unit} || {{ diag; exit 1; }}
            sleep 14   # past RestartSec=10: a restart would have happened by now
            [ "$(show ActiveState)" = failed ] || {{ echo "state: $(show ActiveState)" >&2; diag; exit 1; }}
            [ "$(show NRestarts)" = 0 ] || {{ echo "restarted $(show NRestarts) time(s)" >&2; diag; exit 1; }}
            [ "$(show ExecMainStatus)" = 65 ] || {{ echo "exit status $(show ExecMainStatus)" >&2; diag; exit 1; }}
            cmp "$s/before" /var/lib/docker-gc/last-used.json
            journalctl --sync 2>/dev/null || true
            n="$(journalctl -q --no-pager -o cat -u {unit} --since "@$start" | grep -cF 'last-used.json is format 99 of the docker-gc-state' || true)"
            [ "$n" = 1 ] || {{ echo "the format line was logged $n time(s)" >&2; diag; exit 1; }}
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)
        # Control: the restart policy is in force; only exit 65 is exempt from it.
        docker_exec(self.SYSTEMD_CONTAINER, ["bash", "-c", f"cat >{path}"], input_text=derived("/bin/sh -c 'exit 1'"))
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            systemctl stop {unit} 2>/dev/null || true
            systemctl reset-failed {unit} 2>/dev/null || true
            systemctl daemon-reload
            systemctl start {unit}
            sleep 14
            n="$(systemctl show -p NRestarts --value {unit})"
            systemctl stop {unit} || true
            [ "$n" -ge 1 ] || {{ echo "an exit 1 was not restarted (NRestarts=$n)" >&2; exit 1; }}
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)
        # Exit 76 (IncompleteRecordError: docker answers the same at every start) is exempt as well.
        docker_exec(self.SYSTEMD_CONTAINER, ["bash", "-c", f"cat >{path}"], input_text=derived("/bin/sh -c 'exit 76'"))
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            f"""
            show() {{ systemctl show -p "$1" --value {unit}; }}
            systemctl stop {unit} 2>/dev/null || true
            systemctl reset-failed {unit} 2>/dev/null || true
            systemctl daemon-reload
            systemctl start {unit} || true
            sleep 14   # past RestartSec=10
            state="$(show ActiveState)" n="$(show NRestarts)" rc="$(show ExecMainStatus)"
            systemctl stop {unit} || true
            [ "$state" = failed ] && [ "$n" = 0 ] && [ "$rc" = 76 ] ||
                {{ echo "exit 76: state $state, NRestarts $n, status $rc" >&2; exit 1; }}
            echo E2E-OK
            """,
        ).stdout
        self.assertIn("E2E-OK", out)

    def test_108_btrfs_snapshot_as_root_ignores_its_test_seam_and_runs_no_settings_line(self) -> None:
        """btrfs/btrfs-snapshot as install writes it, run as real root: CST_SNAPSHOT_ROOT (the test seam
        of its unit tests) is ignored, so the state folder is the real /var/lib/btrfs-snapshot and
        nothing appears below the seam's folder; snapshots.conf is read as data, so a line that is a
        command does not run (a warning on stderr names it) while a plain setting still applies. The
        unit test of the seam runs under unshare -r, which CI's container does not allow."""
        d = "/var/tmp/e2e-snapshot-seam"
        conf = "/etc/btrfs-setup/snapshots.conf"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"rm -rf {d} /var/lib/btrfs-snapshot; if [ -e {d}.conf ]; then mv -f {d}.conf {conf}; else rm -f {conf}; fi",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            rf"""
d={d} conf={conf}
rm -rf "$d" /var/lib/btrfs-snapshot
install -d -m 0755 "$d" "$d/seam" /etc/btrfs-setup
[ -e "$conf" ] && cp -a "$conf" "$d.conf"
/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py payload btrfs/btrfs-snapshot >"$d/btrfs-snapshot"
chmod 0755 "$d/btrfs-snapshot"
printf '%s\n' "HOME_DIR='/home/tester'" "touch $d/ran" 'NAS_PATH=$(touch '"$d"'/ran)' "PLAN_X='y'" >"$conf"
rc=0; CST_SNAPSHOT_ROOT="$d/seam" "$d/btrfs-snapshot" planned-left >"$d/out" 2>"$d/err" || rc=$?
[ "$rc" = 0 ]
[ ! -s "$d/out" ]
[ ! -e "$d/ran" ]
[ -d /var/lib/btrfs-snapshot ]
[ -z "$(find "$d/seam" -mindepth 1 -print -quit)" ]
grep -qxF "<4>btrfs-snapshot: $conf line 2: touch $d/ran: not a KEY=VALUE line; ignored" "$d/err"
grep -qF "$conf line 3: " "$d/err"
[ "$(grep -c "^<4>btrfs-snapshot: $conf line " "$d/err")" = 2 ]
rc=0; CST_SNAPSHOT_ROOT="$d/seam" "$d/btrfs-snapshot" bogus 2>/dev/null || rc=$?
[ "$rc" = 2 ]
[ -z "$(find "$d/seam" -mindepth 1 -print -quit)" ]
echo test_108-ok
""",
        ).stdout
        self.assertIn("test_108-ok", out)

    def test_109_btrfs_snapshot_runtime_installed_runs_as_root(self) -> None:
        """btrfs-snapshot and btrfs-snapshot-runtime as install writes them (the runtime a zipapp in
        /usr/local/sbin, next to the bash front), run as real root, as tier 2's test_03, test_03a and
        test_08 run them on btrfs: the front hands each command to the runtime of its folder and the
        runtime runs the engine of its folder under the lock it hands on (convert-planned --lock-held);
        what can be checked without btrfs: configure, not set up, nas off, purge without a snapshot
        folder, hourly up to the free-space reading, a usage error, a lock another process holds, and
        the installer writing the runtime first and removing it last."""
        d = "/var/tmp/e2e-snapshot-runtime"
        conf = "/etc/btrfs-setup/snapshots.conf"
        inst = "/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
        files = "btrfs/btrfs-snapshot-runtime btrfs/btrfs-snapshot btrfs/btrfs-snapshot-core"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"{inst} remove {files} >/dev/null 2>&1; rm -rf {d} /var/lib/btrfs-snapshot /run/btrfs-snapshot.lock; "
            f"if [ -e {d}.conf ]; then mv -f {d}.conf {conf}; else rm -f {conf}; fi",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            rf"""
d={d} conf={conf}
rm -rf "$d" /var/lib/btrfs-snapshot /run/btrfs-snapshot.lock
install -d -m 0755 "$d" /etc/btrfs-setup
if [ -e "$conf" ]; then cp -a "$conf" "$d.conf"; fi
written="$({inst} install {files})"
printf '%s\n' "$written"
w_rt="$(grep -n 'btrfs-snapshot-runtime$' <<<"$written" | cut -d: -f1)"
w_front="$(grep -n 'sbin/btrfs-snapshot$' <<<"$written" | cut -d: -f1)"
[ -n "$w_rt" ]
[ -n "$w_front" ]
[ "$w_rt" -lt "$w_front" ]
S=/usr/local/sbin/btrfs-snapshot
[ "$(head -c 2 /usr/local/sbin/btrfs-snapshot-runtime)" = "#!" ]
printf '%s\n' "NAS_RESTIC_HOST=e2e" >"$conf"
rc=0; "$S" configure >"$d/out" 2>&1 || rc=$?
[ "$rc" = 0 ]
[ ! -s "$d/out" ]
printf '%s\n' "HOME_MOUNT=/home" "NAS_RESTIC_HOST=e2e" >"$conf"
rc=0; "$S" list >"$d/out" 2>"$d/err" || rc=$?
[ "$rc" = 1 ]
[ ! -s "$d/out" ]
grep -qxF "not set up; run 'cst btrfs snapshots setup'" "$d/err"
printf '%s\n' "HOME_DIR=/home/tester" "HOME_MOUNT=/home" "TOP=$d/top" "NAS_ENABLED=0" "NAS_RESTIC_HOST=e2e" >"$conf"
rc=0; "$S" nas >"$d/out" 2>&1 || rc=$?
[ "$rc" = 0 ]
grep -qxF "<6>btrfs-snapshot: NAS backup not enabled" "$d/out"
rc=0; "$S" purge >"$d/out" 2>&1 || rc=$?
[ "$rc" = 0 ]
grep -qxF "No snapshot directory: nothing to delete." "$d/out"
[ "$(stat -c %a /run/btrfs-snapshot.lock)" = 644 ]
# hourly: the engine's step under the lock handed on, then the free space (no btrfs here).
rc=0; "$S" hourly >"$d/out" 2>"$d/err" || rc=$?
cat "$d/out" "$d/err"
[ "$rc" = 1 ]
grep -qxF "<3>btrfs-snapshot: cannot read free space" "$d/out"
if grep -q "convert-planned" "$d/out" "$d/err"; then exit 1; fi
rc=0; "$S" prune --x >"$d/out" 2>"$d/err" || rc=$?
[ "$rc" = 2 ]
grep -qxF "usage: btrfs-snapshot prune [--dry-run] [--allow-mass-delete]" "$d/err"
# Another process holds the lock: hourly skips.
flock /run/btrfs-snapshot.lock sleep 5 &
holder=$!
sleep 1
rc=0; "$S" hourly >"$d/out" 2>&1 || rc=$?
kill "$holder" 2>/dev/null || true
wait "$holder" 2>/dev/null || true
[ "$rc" = 0 ]
grep -qxF "<5>btrfs-snapshot: another conversion/snapshot run is active; skipped" "$d/out"
removed="$({inst} remove {files})"
printf '%s\n' "$removed"
r_rt="$(grep -n 'btrfs-snapshot-runtime$' <<<"$removed" | cut -d: -f1)"
r_front="$(grep -n 'sbin/btrfs-snapshot$' <<<"$removed" | cut -d: -f1)"
[ -n "$r_rt" ]
[ -n "$r_front" ]
[ "$r_front" -lt "$r_rt" ]
echo test_109-ok
""",
        ).stdout
        self.assertIn("test_109-ok", out)

    def test_110_btrfs_snapshot_hourly_as_a_unit_hands_sigpipe_on_and_keeps_its_alert_as_root(self) -> None:
        """btrfs-snapshot and its runtime as install writes them, run as real root with a fake
        btrfs (10 GiB free: the emergency purge) and a fake btrfs-snapshot-core that records its own
        SIGPIPE disposition, and no btrfs-notify. As a systemd unit (which starts it with SIGPIPE
        ignored) the core gets SIGPIPE ignored, as bash's children did (CST_SIGPIPE_IGNORED, handed
        over by the front); from a shell with SIGPIPE ignored too, and at its default from a shell
        with it at its default. The emergency alert, which btrfs-notify cannot take, is kept by
        cst_core's Notifier in the real /var/lib/cst-alerts/pending (root's, 0644 in a 0755 folder,
        the spool's format) and sets the day's stamp, so the next run keeps no second copy. Then
        convert-planned --lock-held against the real /run lock (/proc/self/fdinfo): an fd that holds
        the flock is taken; another open of the lock file while that one holds it, an fd of another
        file and an fd whose lock was released are refused (exit 2, nothing converted)."""
        d = "/var/tmp/e2e-snapshot-unit"
        conf = "/etc/btrfs-setup/snapshots.conf"
        inst = "/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
        files = "btrfs/btrfs-snapshot-runtime btrfs/btrfs-snapshot"
        core = "/usr/local/sbin/btrfs-snapshot-core"
        notify = "/usr/local/sbin/btrfs-notify"
        pending = "/var/lib/cst-alerts/pending"
        # Before the cleanup is registered: on a container where another test left a core, the
        # cleanup below (which removes the core, the install, its state, the settings file and
        # the alerts folder it did not back up) must not run at all.
        found = exec_bash(self.SYSTEMD_CONTAINER, f"[ ! -e {core} ]", check=False)
        self.assertEqual(found.returncode, 0, f"a btrfs-snapshot-core is already installed ({core}) {found.stderr}")
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"{inst} remove {files} >/dev/null 2>&1; rm -f {core}; "
            f"if [ -e {d}/notify.bak ]; then mv -f {d}/notify.bak {notify}; fi; "
            f"rm -rf {pending}; if [ -e {d}/pending.bak ]; then mv {d}/pending.bak {pending}; fi; "
            f"rm -rf /var/lib/btrfs-snapshot /run/btrfs-snapshot.lock; "
            f"if [ -e {d}/conf.bak ]; then mv -f {d}/conf.bak {conf}; else rm -f {conf}; fi; rm -rf {d}",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            rf"""
set -E
trap 'echo "test_110 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
d={d} conf={conf} core={core} notify={notify} pending={pending}
S=/usr/local/sbin/btrfs-snapshot
rm -rf "$d" /var/lib/btrfs-snapshot /run/btrfs-snapshot.lock
install -d -m 0755 "$d" "$d/bin" /etc/btrfs-setup
if [ -e "$conf" ]; then cp -a "$conf" "$d/conf.bak"; fi
if [ -e "$notify" ]; then mv "$notify" "$d/notify.bak"; fi
if [ -e "$pending" ]; then mv "$pending" "$d/pending.bak"; fi
{inst} install {files} >/dev/null
# The fake core: its own SigIgn, then success.
printf '%s\n' '#!/bin/bash' "grep '^SigIgn:' /proc/\$\$/status >>$d/core-sigign" 'exit 0' >"$core"
chmod 0755 "$core"
# The fake btrfs: 10 GiB free (at or below EMERGENCY_AT_OR_BELOW_GIB), and subvolume sync.
printf '%s\n' '#!/bin/bash' 'if [ "$1 $2" = "filesystem usage" ]; then echo "    Free (estimated):     10737418240      (min: 10737418240)"; fi' 'exit 0' \
    >"$d/bin/btrfs"
chmod 0755 "$d/bin/btrfs"
printf '%s\n' "HOME_DIR=/home/tester" "HOME_MOUNT=/home" "TOP=$d/top" "NAS_RESTIC_HOST=e2e" >"$conf"
pipe_ignored() {{ local mask; mask=$((16#$(tail -n 1 "$d/core-sigign" | awk '{{print $2}}'))); (( mask & (1 << 12) )); }}
P="$d/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"

# 1. As a unit: SIGPIPE ignored reaches the core; the alert is kept and stamped.
rc=0; systemd-run --quiet --wait --collect --unit=e2e-110-hourly -p Environment=PATH="$P" "$S" hourly || rc=$?
journalctl --sync
journalctl -q --no-pager -o cat -u e2e-110-hourly.service >"$d/unit.log" || true
cat "$d/unit.log"
[ "$rc" = 0 ]
[ "$(wc -l <"$d/core-sigign")" = 1 ]
pipe_ignored
[ "$(stat -c '%a %U' "$pending")" = "755 root" ]
alerts=("$pending"/*.alert)
[ "${{#alerts[@]}}" = 1 ]
[ "$(stat -c '%a %U' "${{alerts[0]}}")" = "644 root" ]
head -n 3 "${{alerts[0]}}" | diff - <(printf '%s\n' "cst-alert 1" "urgency=critical" "app=btrfs-snapshot")
grep -qxF "count=1" "${{alerts[0]}}"
grep -qxF "title=Snapshots: emergency purge" "${{alerts[0]}}"
grep -qF "alert not delivered ($notify is not installed): Snapshots: emergency purge; kept in ${{alerts[0]}}" "$d/unit.log"
[ -e /var/lib/btrfs-snapshot/alerted-emergency ]

# 2. From a shell with SIGPIPE ignored: ignored in the core too; the stamp keeps a second copy out.
rc=0; (trap '' PIPE; PATH="$P" "$S" hourly) >"$d/out" 2>&1 || rc=$?
cat "$d/out"
[ "$rc" = 0 ]
pipe_ignored
if grep -q "alert not delivered" "$d/out"; then exit 1; fi
grep -qxF "count=1" "${{alerts[0]}}"

# 3. From a shell with SIGPIPE at its default: at its default in the core.
rc=0; (trap - PIPE; PATH="$P" "$S" hourly) >"$d/out" 2>&1 || rc=$?
[ "$rc" = 0 ]
[ "$(wc -l <"$d/core-sigign")" = 3 ]
if pipe_ignored; then exit 1; fi

# 4. convert-planned --lock-held: only an fd that holds the real lock.
lock=/run/btrfs-snapshot.lock
refused() {{ grep -qxF "<3>btrfs-snapshot: convert-planned: fd $1 is not a held $lock; nothing converted" "$d/out"; }}
exec 9>"$lock"
flock 9
rc=0; "$S" convert-planned --lock-held 9 >"$d/out" 2>&1 || rc=$?
cat "$d/out"
[ "$rc" = 0 ]
[ ! -s "$d/out" ]
exec 8<"$lock"
rc=0; "$S" convert-planned --lock-held 8 >"$d/out" 2>&1 || rc=$?
[ "$rc" = 2 ]
refused 8
exec 7<"$conf"
rc=0; "$S" convert-planned --lock-held 7 >"$d/out" 2>&1 || rc=$?
[ "$rc" = 2 ]
refused 7
flock -u 9
rc=0; "$S" convert-planned --lock-held 9 >"$d/out" 2>&1 || rc=$?
[ "$rc" = 2 ]
refused 9
exec 9>&- 8<&- 7<&-
echo test_110-ok
""",
        ).stdout
        self.assertIn("test_110-ok", out)

    def test_111_snapshots_status_reads_snapshots_conf_through_settings_lines(self) -> None:
        """cst btrfs snapshots status as root on the container's root, with a snapshots.conf that has
        a command line and a $(...) value next to plain settings: it reads the file through
        btrfs-snapshot settings-lines, as the units do (never sourced): the plain setting shows
        (SNAPSHOT_EXCLUDE_CACHES=no), neither command runs, and a warning on stderr names each of the
        two lines."""
        d = "/var/tmp/e2e-snapshot-status"
        conf = "/etc/btrfs-setup/snapshots.conf"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"if [ -e {d}/conf.bak ]; then mv -f {d}/conf.bak {conf}; else rm -f {conf}; fi; rm -rf {d}",
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            rf"""
set -E
trap 'echo "test_111 FAILED at line $LINENO: $BASH_COMMAND" >&2' ERR
d={d} conf={conf}
rm -rf "$d"
# Writable by everyone: a command in the file would leave its mark whoever runs it.
install -d -m 1777 "$d"
install -d -m 0755 /etc/btrfs-setup
if [ -e "$conf" ]; then cp -a "$conf" "$d/conf.bak"; fi
printf '%s\n' "HOME_DIR='/home/tester'" "touch $d/ran" "NAS_HOST=\"\$(touch $d/ran2; echo nas)\"" \
    "SNAPSHOT_EXCLUDE_CACHES=no" >"$conf"
rc=0; SUDO_USER=tester /opt/cst-rw/cst btrfs snapshots status >"$d/out" 2>"$d/err" || rc=$?
cat "$d/out" "$d/err"
[ "$rc" = 0 ]
[ ! -e "$d/ran" ]
[ ! -e "$d/ran2" ]
grep -qxF "  cache folders (CACHEDIR.TAG): kept in new snapshots (SNAPSHOT_EXCLUDE_CACHES=no)" "$d/out"
grep -qxF "<4>btrfs-snapshot: $conf line 2: touch $d/ran: not a KEY=VALUE line; ignored" "$d/err"
grep -qF "<4>btrfs-snapshot: $conf line 3: NAS_HOST=" "$d/err"
echo test_111-ok
""",
        ).stdout
        self.assertIn("test_111-ok", out)

    def test_99_installed_health_check_and_pending_alerts_zipapps(self) -> None:
        """The installed forms of the two Python ports, under real systemd: btrfs-health-check and
        cst-pending-alerts as install writes them (zipapps from installer.py payload). The health
        check runs from the shipped unit (only ExecStart points at the zipapp, under its installed
        name, plus the test root): healthy, the unit succeeds and its OK line reaches the journal at
        priority 6 under the identifier btrfs-health-check; with a degraded-boot flag, exit 1, the
        problem at priority 2, and the real btrfs-notify (no desktop session) keeps the alert, which
        the cst-pending-alerts zipapp, run as the user, lists for the next login. A second healthy run
        reads the kernel log from the cursor the first one recorded."""
        d = "/var/tmp/e2e-hc"
        self.addCleanup(
            exec_bash,
            self.SYSTEMD_CONTAINER,
            f"""
            systemctl reset-failed cst-e2e-health-check.service 2>/dev/null || true
            rm -f /run/systemd/system/cst-e2e-health-check.service
            systemctl daemon-reload
            rm -rf {d}
            """,
            check=False,
        )
        out = exec_bash(
            self.SYSTEMD_CONTAINER,
            rf"""
d={d}
rm -rf "$d"
install -d -m 0755 "$d" "$d/sbin" "$d/bin" "$d/root" "$d/root/usr/local/sbin" "$d/alerts" "$d/alerts/pending" "$d/run-user"
inst="/usr/bin/python3 -I -B /opt/cst-rw/cst_core/installer.py"
$inst payload btrfs/btrfs-health-check >"$d/sbin/btrfs-health-check"
$inst payload btrfs/cst-pending-alerts >"$d/sbin/cst-pending-alerts"
$inst payload btrfs/btrfs-notify >"$d/root/usr/local/sbin/btrfs-notify"
chmod 0755 "$d/sbin/btrfs-health-check" "$d/sbin/cst-pending-alerts" "$d/root/usr/local/sbin/btrfs-notify"
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).getinfo("__main__.py")' "$d/sbin/btrfs-health-check"
# No btrfs in the container, and the kernel log is the host's: both answer nothing.
printf '#!/bin/sh\nexit 0\n' >"$d/bin/findmnt"
cat >"$d/bin/journalctl" <<'EOF'
#!/bin/sh
for a in "$@"; do [ "$a" = -k ] && k=1; done
if [ -n "$k" ]; then
    for a in "$@"; do
        case "$a" in --cursor-file=*) f="${{a#--cursor-file=}}"; echo "$*" >>"$f.calls"; echo s=e2e >"$f";; esac
    done
    exit 0
fi
exec /usr/bin/journalctl "$@"
EOF
chmod 0755 "$d/bin/findmnt" "$d/bin/journalctl"
sed "s|^ExecStart=.*|ExecStart=$d/sbin/btrfs-health-check|" /opt/cst-rw/btrfs/units/btrfs-health-check.service \
    >/run/systemd/system/cst-e2e-health-check.service
cat >>/run/systemd/system/cst-e2e-health-check.service <<EOF
Environment=CST_HEALTH_ROOT=$d/root CST_ALERTS_DIR=$d/alerts CST_RUN_USER=$d/run-user PATH=$d/bin:/usr/local/bin:/usr/bin:/bin
ExecStopPost=/bin/sh -c 'echo "\$\$EXIT_STATUS \$\$SERVICE_RESULT" >$d/exit'
EOF
systemctl daemon-reload
run_unit() {{
    rm -f "$d/exit"
    systemctl start cst-e2e-health-check.service 2>/dev/null || true
    for _ in $(seq 1 40); do [ -s "$d/exit" ] && break; sleep 0.25; done
    cat "$d/exit" 2>/dev/null || true
}}
# journal_has MESSAGE PRIORITY: the unit logged MESSAGE under btrfs-health-check at PRIORITY.
journal_has() {{
    for _ in $(seq 1 60); do
        journalctl --sync 2>/dev/null || true
        if journalctl -q --no-pager -o json -u cst-e2e-health-check.service --since "@$start" | python3 -I -c '
import json, sys
# All of it first: leaving early would end journalctl with SIGPIPE, and pipefail would fail the match.
for line in sys.stdin.read().splitlines():
    e = json.loads(line)
    if (e.get("MESSAGE"), e.get("SYSLOG_IDENTIFIER"), e.get("PRIORITY")) == (sys.argv[1], "btrfs-health-check", sys.argv[2]):
        sys.exit(0)
sys.exit(1)' "$1" "$2"; then return 0; fi
        sleep 0.5
    done
    echo "not in the journal at priority $2: $1" >&2
    journalctl -q --no-pager -o verbose -u cst-e2e-health-check.service --since "@$start" >&2 || true
    return 1
}}
start="$(date +%s)"
r="$(run_unit)"
[ "$r" = "0 success" ] || {{ echo "healthy run: $r" >&2; exit 1; }}
journal_has "btrfs-health-check: OK (; disks: )" 6
[ "$(cat "$d/root/var/lib/btrfs-health-check/kernel.cursor")" = s=e2e ]
mkdir -p "$d/root/run/btrfs-allow-degraded" && touch "$d/root/run/btrfs-allow-degraded/e2e-uuid"
start="$(date +%s)"
r="$(run_unit)"
[ "$r" = "1 exit-code" ] || {{ echo "run with a problem: $r" >&2; exit 1; }}
journal_has "btrfs-health-check: btrfs e2e-uuid: this boot is DEGRADED (btrfs-last-resort: member disk missing at boot)" 2
f="$(grep -lxF 'title=Disk health: problems detected' "$d"/alerts/pending/*.alert)"
grep -qxF 'app=btrfs-health-check' "$f"
grep -qxF 'urgency=critical' "$f"
grep -qxF 'btrfs e2e-uuid: this boot is DEGRADED (btrfs-last-resort: member disk missing at boot)' "$f"
rm -f "$d/root/run/btrfs-allow-degraded/e2e-uuid" "$d/root/var/lib/btrfs-health-check/kernel.cursor.calls"
start="$(date +%s)"
r="$(run_unit)"
[ "$r" = "0 success" ] || {{ echo "second healthy run: $r" >&2; exit 1; }}
# The second run read from the cursor: one call, with no --since and no new cursor recorded.
calls="$(cat "$d/root/var/lib/btrfs-health-check/kernel.cursor.calls")"
case "$calls" in *--since*|*" -n 1 "*) echo "second run: $calls" >&2; exit 1;; esac
[ "$(printf '%s\n' "$calls" | wc -l)" = 1 ] || {{ echo "second run: $calls" >&2; exit 1; }}
echo E2E-OK
""",
        ).stdout
        self.assertIn("E2E-OK", out)
        listed = as_tester_bash(
            self.SYSTEMD_CONTAINER, f"CST_ALERTS_DIR={d}/alerts CST_ALERTS_OWNER=0 {d}/sbin/cst-pending-alerts --list"
        ).stdout
        self.assertRegex(
            listed,
            r"(?m)^.*  critical btrfs-health-check: Disk health: problems detected  \[NOT shown to you yet: next login\]$",
        )

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
