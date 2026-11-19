# SPDX-License-Identifier: MPL-2.0
"""The fake machine of the `cst bluetooth` (bluetooth-setup.sh) tests in test_setup.py.

Machine builds a scratch HOME, a scratch host root (CST_BT_ROOT, with its
/etc/bluetooth/main.conf) and a folder of stub commands on PATH: systemctl,
sudo, busctl, bluetoothctl, journalctl, pactl, pw-dump, flatpak-spawn,
selinuxenabled, sleep, python3 (which answers only bt-helper's wp-roles
and passes every other call to /usr/bin/python3) and runuser (only logged). The stubs keep their state
in files and log every call, so a test sees what the program ran, in order.
Nothing reaches the real machine: sudo runs its command as the caller (with
the root-only options of cp and install left out) and only inside the
scratch folders, and the unit tests never gain privileges
(tests/run_unit.py).
"""

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SETUP = os.path.join(REPO, "bluetooth-setup.sh")
MAC = "0A:1B:2C:3D:4E:5F"
DEV = "0A_1B_2C_3D_4E_5F"
ISO = "6fbaf188-05e0-496a-9885-d6ddfdb4e03e"
STUBS = (
    "flatpak-spawn",
    "sudo",
    "systemctl",
    "busctl",
    "bluetoothctl",
    "journalctl",
    "pactl",
    "pw-dump",
    "selinuxenabled",
    "sleep",
    "python3",
    "runuser",
)

FAKE = r"""#!/bin/bash
# The fake machine of bluetooth/tests/test_setup.py: one script for every
# command it stands in for (named by $0). Its state is files in $FAKE_STATE;
# every call but sleep's (Python's own sleep logs none) is logged there (log: the command line; env-log: the CST_, LC_
# and PYTHON variables it got, to catch a leak).
S="${FAKE_STATE:?}"
me="${0##*/}"
log() { printf '%s\n' "$*" >>"$S/log"; }
if [[ "$me" != sleep && ( "$me" != python3 || "$*" == *wp-roles* ) ]]; then
    log "$me $*"
    { printf '%s:' "$me"; env | grep -E '^(CST_|LC_|LANG|PYTHON)' | LC_ALL=C sort | tr '\n' ' '; echo; } >>"$S/env-log"
fi
unit() { # unit SCOPE NAME: the file prefix of a unit's state
    local n="${2%.service}"
    echo "$S/units/$1-$n"
}
new_id() { # new_id PREFIX: a new invocation ID for the unit
    local n
    n=$(( $(cat "$S/counter" 2>/dev/null || echo 0) + 1 ))
    echo "$n" >"$S/counter"
    echo "inv-${1##*/}-$n" >"$1.inv"
}
case "$me" in
    flatpak-spawn)
        [[ "$1" == --host ]] && shift
        if [[ "$1" == /usr/bin/python3 && "$*" == *socket* ]]; then
            [[ -e "$S/iso-socket" ]]
            exit
        fi
        exec "$@"
        ;;
    sudo)
        args=()
        skip=0
        for a in "$@"; do
            if (( skip )); then skip=0; continue; fi
            case "$1:$a" in
                install:-o|install:-g) skip=1; continue ;;
                install:-Z|cp:--preserve=context) continue ;;
            esac
            args+=("$a")
        done
        if [[ -e "$S/fail-sudo-$1" ]]; then
            how="$(cat "$S/fail-sudo-$1")"
            [[ "$how" == TERM ]] && kill -TERM $$  # killed by a signal
            [[ "$how" == PARENT-TERM ]] && { kill -TERM "$PPID"; exit 0; }  # the program gets SIGTERM
            [[ "$how" == PARENT-HUP ]] && { kill -HUP "$PPID"; exec "${args[@]}"; }  # SIGHUP, the step done
            exit "$how"
        fi
        exec "${args[@]}"
        ;;
    systemctl)
        scope=system
        if [[ "$1" == --user ]]; then scope=user; shift; fi
        mkdir -p "$S/units"
        case "$1" in
            is-active)
                if [[ "$2" == --quiet ]]; then [[ -e "$(unit "$scope" "$3").active" ]]; exit; fi
                if [[ -e "$(unit "$scope" "$2").active" ]]; then echo active; exit 0; fi
                echo inactive
                exit 3
                ;;
            is-enabled)
                if [[ -e "$(unit "$scope" "$2").enabled" ]]; then echo enabled; exit 0; fi
                echo disabled
                exit 1
                ;;
            show)
                u="$(unit "$scope" "${!#}")"
                case "$*" in
                    *InvocationID*) cat "$u.inv" 2>/dev/null; exit 0 ;;
                    *MainPID*) cat "$u.pid" 2>/dev/null || echo 0; exit 0 ;;
                esac
                exit 1
                ;;
            show-environment) [[ ! -e "$S/no-session" ]]; exit ;;
            daemon-reload) exit 0 ;;
            restart|start)
                u="$(unit "$scope" "$2")"
                [[ -e "$u.fail" ]] && { echo "Job for $2 failed." >&2; exit 1; }
                if [[ "$1" == restart || ! -e "$u.active" ]]; then new_id "$u"; fi
                touch "$u.active"
                exit 0
                ;;
            enable) touch "$(unit "$scope" "$2").enabled"; echo "Created symlink for $2." >&2; exit 0 ;;
            disable)
                u="$(unit "$scope" "${!#}")"
                rm -f "$u.enabled" "$u.active"
                echo "Removed $u." >&2
                exit 0
                ;;
        esac
        exit 1
        ;;
    busctl)
        case "$*" in
            *GetManagedObjects*) cat "$S/objects.json" 2>/dev/null; exit ;;
            "--system list --no-legend --unique") cat "$S/bus-names" 2>/dev/null; exit 0 ;;
            get-property*) cat "$S/prop-$4-$5" 2>/dev/null; exit ;;
        esac
        exit 1
        ;;
    bluetoothctl)
        via=direct
        [[ "$(tr '\0' ' ' <"/proc/$PPID/cmdline" 2>/dev/null)" == timeout* ]] && via="$(tr '\0' ' ' <"/proc/$PPID/cmdline")"
        log "  stdin=$(readlink /proc/self/fd/0) via=$via"
        case "$1" in
            info) cat "$S/bt-info" 2>/dev/null || { echo "Device $2 not available"; exit 1; } ;;
            bearer)
                [[ -e "$S/bearer-stuck" ]] && { echo "Failed to set bearer"; exit 1; }
                grep -v PreferredBearer "$S/bt-info" >"$S/bt-info.new" 2>/dev/null
                printf '\tPreferredBearer: %s\n' "$3" >>"$S/bt-info.new"
                mv "$S/bt-info.new" "$S/bt-info"
                echo "Changing PreferredBearer to $3 succeeded"
                ;;
            connect) touch "$S/a2dp-connected" ;;
        esac
        exit 0
        ;;
    journalctl)
        case "$*" in
            *_SYSTEMD_INVOCATION_ID=*) cat "$S/bt-journal" 2>/dev/null; exit 0 ;;
            *--user*) cat "$S/lw-journal" 2>/dev/null; exit 0 ;;
        esac
        exit 1
        ;;
    pactl)
        case "$*" in
            "-f json list cards") cat "$S/pactl-cards" 2>/dev/null; exit ;;
            "-f json list short sources") cat "$S/pactl-sources" 2>/dev/null || echo "[]"; exit 0 ;;
            "-f json list source-outputs") cat "$S/pactl-outputs" 2>/dev/null || echo "[]"; exit 0 ;;
            set-card-profile*) [[ ! -e "$S/no-profile-switch" ]]; exit ;;
        esac
        exit 1
        ;;
    pw-dump) cat "$S/pw-dump.json" 2>/dev/null; exit ;;
    selinuxenabled) [[ -e "$S/selinux" ]]; exit ;;
    sleep) exit 0 ;;
    runuser) exit 0 ;; # the run as SUDO_USER: its command line is in the log
    python3)
        if [[ "$*" == *wp-roles* ]]; then
            log "  XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-}"
            [[ -e "$S/wp-roles" ]] || exit 5
            cat "$S/wp-roles"
            exit 0
        fi
        exec /usr/bin/python3 "$@"
        ;;
esac
echo "fake: no such command: $me" >&2
exit 127
"""

# busctl -j GetManagedObjects with one adapter.
OBJECTS = {
    "type": "a{oa{sa{sv}}}",
    "data": [{"/org/bluez": {}, "/org/bluez/hci0": {"org.bluez.Adapter1": {}, "org.bluez.Media1": {}}}],
}


@dataclass(frozen=True)
class Run:
    """One run of the program: its exit status, output and the calls the stubs logged."""

    returncode: int
    stdout: str
    stderr: str
    calls: list[str]

    @property
    def text(self) -> str:
        """stdout and stderr, for assertion messages."""
        return self.stdout + self.stderr


class Machine:
    """A scratch machine for one test (removed at its end)."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bt-setup-test-")
        test.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "root")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.home, self.state, self.bin, os.path.join(self.root, "etc", "bluetooth")):
            os.makedirs(d)
        fake = os.path.join(self.bin, "fake")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        for name in STUBS:
            os.symlink("fake", os.path.join(self.bin, name))
        self.main_conf = os.path.join(self.root, "etc", "bluetooth", "main.conf")
        self.wp_dir = os.path.join(self.home, ".config", "wireplumber", "wireplumber.conf.d")
        self.wp_conf = os.path.join(self.wp_dir, f"50-bluez-{DEV}.conf")
        self.le_conf = os.path.join(self.wp_dir, "90-cst-le-audio-off.conf")
        self.settings = os.path.join(self.home, ".config", "cst", "bluetooth.conf")
        self.pending = os.path.join(self.home, ".local", "state", "cst", "bluetooth-restart-pending.json")
        self.lw_prog = os.path.join(self.home, ".local", "lib", "cst", "bt-ldac-watch")
        self.lw_unit = os.path.join(self.home, ".config", "systemd", "user", "cst-bt-ldac48.service")
        self.record = os.path.join(self.home, ".local", "state", "cst", "installed.json")

    # ---- the machine's state -------------------------------------------------------------
    def put(self, path: str, text: str, mode: int | None = None) -> str:
        """Write text at path (an absolute path, or one relative to the state folder)."""
        full = os.path.join(self.state, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(full, mode)
        return full

    def read(self, path: str) -> str:
        """The text at path (as put)."""
        with open(os.path.join(self.state, path), encoding="utf-8") as f:
            return f.read()

    def flag(self, name: str, *, on: bool = True) -> None:
        """A state flag of the stubs (a file in the state folder) set or cleared."""
        path = os.path.join(self.state, name)
        if on:
            self.put(name, "")
        elif os.path.exists(path):
            os.unlink(path)

    def unit(
        self, scope: str, name: str, *, active: bool = True, inv: str | None = None, pid: int | None = None
    ) -> None:
        """A unit of the fake systemd (scope system or user): running with invocation ID inv."""
        base = os.path.join("units", f"{scope}-{name}")
        self.flag(base + ".active", on=active)
        if inv is not None:
            self.put(base + ".inv", inv + "\n")
        if pid is not None:
            self.put(base + ".pid", f"{pid}\n")

    def unit_inv(self, scope: str, name: str) -> str:
        """The invocation ID the fake systemd has for a unit now."""
        return self.read(os.path.join("units", f"{scope}-{name}.inv")).strip()

    def has(self, name: str) -> bool:
        """A state file of the stubs exists."""
        return os.path.exists(os.path.join(self.state, name))

    def services(self) -> None:
        """bluetoothd and the session's WirePlumber running (with their bus name), the adapter powered."""
        self.unit("system", "bluetooth", inv="bt-0")
        self.unit("user", "wireplumber", inv="wp-0", pid=4242)
        self.put("bus-names", ":1.7 4242 wireplumber danilo :1.7 user@1000.service - -\n")
        # The adapter answers right after a restart (apply waits up to 15 s for it).
        self.put("prop-org.bluez.Adapter1-Powered", "b true\n")

    def adapter(self, *, iso: bool = True, bap: bool = True, listed: bool = True) -> None:
        """One Bluetooth adapter in org.bluez: its ISO feature on and BAP's PACs (as wanted).
        listed=False: only its properties answer (GetManagedObjects lists no adapter), so a run
        on the host never reaches the kernel ISO socket check, which opens a real Bluetooth
        socket with /usr/bin/python3 (no stub can stand in for it there; run() refuses)."""
        if listed:
            self.put("objects.json", json.dumps(OBJECTS))
        self.put("prop-org.bluez.Adapter1-ExperimentalFeatures", f'as 1 "{ISO}"\n' if iso else "as 0\n")
        self.put(
            "prop-org.bluez.Media1-SupportedUUIDs",
            'as 2 "00002bc9-0000-1000-8000-00805f9b34fb" "00002bcb-0000-1000-8000-00805f9b34fb"\n' if bap else "as 0\n",
        )
        self.put("prop-org.bluez.Adapter1-Powered", "b true\n")

    def user_settings(self, text: str) -> None:
        """~/.config/cst/bluetooth.conf."""
        self.put(self.settings, text)

    # ---- running it ----------------------------------------------------------------------
    def env(self, *, container: bool, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment of a run: the stubs first on PATH, the scratch HOME and host root."""
        env: dict[str, str] = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.home,
            "FAKE_STATE": self.state,
            "CST_BT_CONTAINER": "1" if container else "0",
            "CST_BT_ROOT": self.root,
        }
        env.update(extra or {})
        return env

    def run(
        self,
        *args: str,
        container: bool = False,
        env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
        timeout: float = 120,
        closed_stdout: bool = False,
        ignore_hup: bool = False,
        root: bool = False,
    ) -> Run:
        """bluetooth-setup.sh ARGS on this machine (container: as inside a toolbox; closed_stdout:
        its stdout a pipe nobody reads, as `| head` leaves it; root: as root in a user namespace,
        unshare -r, where this user is root with no privilege outside)."""
        if not container and self.has("objects.json"):
            raise AssertionError("an adapter listed on the host: the checks would open a real Bluetooth socket")
        log = os.path.join(self.state, "log")
        if os.path.exists(log):
            os.unlink(log)
        read_end, write_end = os.pipe()
        os.close(read_end)
        start = ["bash", "-c", 'trap "" HUP; exec bash "$0" "$@"'] if ignore_hup else ["bash"]
        r = subprocess.run(
            [*(["/usr/bin/unshare", "-r"] if root else []), *start, SETUP, *args],
            check=False,
            stdout=write_end if closed_stdout else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(container=container, extra=env),
            input=stdin_text,
            stdin=None if stdin_text is not None else subprocess.DEVNULL,
            timeout=timeout,
        )
        os.close(write_end)
        calls: list[str] = []
        if os.path.exists(log):
            with open(log, encoding="utf-8") as f:
                calls = f.read().splitlines()
        return Run(r.returncode, r.stdout or "", r.stderr, calls)

    def env_log(self) -> str:
        """What the stubs saw of the CST_, LC_, LANG and PYTHON variables, one line per call."""
        path = os.path.join(self.state, "env-log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()
