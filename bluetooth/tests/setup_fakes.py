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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import difflib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from cst_core.ports import CommandResult

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SETUP = os.path.join(REPO, "bluetooth-setup.sh")
MAC = "0A:1B:2C:3D:4E:5F"
DEV = "0A_1B_2C_3D_4E_5F"
ISO = "6fbaf188-05e0-496a-9885-d6ddfdb4e03e"
type Answer = tuple[int, str, str]  # a command's exit, stdout and stderr
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
# every call but sleep's (Python's own sleep logs none) is logged there (log: the command line; env-log: the command and its
# first four arguments, a tab, then the CST_, LC_ and PYTHON variables it got, to catch a leak).
S="${FAKE_STATE:?}"
me="${0##*/}"
log() { printf '%s\n' "$*" >>"$S/log"; }
if [[ "$me" != sleep && ( "$me" != python3 || "$*" == *wp-roles* ) ]]; then
    log "$me $*"
    { printf '%s\t' "$me ${1:-} ${2:-} ${3:-} ${4:-}" | tr '\n' ' '; env | grep -E '^(CST_|LC_|LANG|PYTHON)' | LC_ALL=C sort | tr '\n' ' '; echo; } >>"$S/env-log"
fi
unit() { # unit SCOPE NAME: the file prefix of a unit's state
    local n="${2%.service}"
    echo "$S/units/$1-$n"
}
active_state() { # active_state PREFIX: the unit's ActiveState (PREFIX.state, else active or inactive)
    if [[ -e "$1.state" ]]; then cat "$1.state"; elif [[ -e "$1.active" ]]; then echo active; else echo inactive; fi
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
        while [[ "$1" == --env=* ]]; do export "${1#--env=}"; shift; done  # flatpak-spawn --env=K=V
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
        if [[ "$1" == cp && " ${args[*]} " == *" --update=none-fail "* ]]; then
            # GNU cp 9.5's --update=none-fail, whatever cp this machine has: a destination that is
            # there (a link too) is left alone, exit 1. race-backup: another run creates the
            # destination just before (once), as cp's O_EXCL open then finds it.
            kept=()
            for a in "${args[@]}"; do [[ "$a" == --update=none-fail ]] || kept+=("$a"); done
            args=("${kept[@]}")
            dst="${args[-1]}"
            if [[ -e "$S/race-backup" ]]; then
                rm -f "$S/race-backup"
                echo "another run's backup" >"$dst"
                echo "cp: cannot create regular file '$dst': File exists" >&2
                exit 1
            fi
            if [[ -e "$S/partial-backup" ]]; then  # a full disk: the start of the copy is left behind
                head -c 8 "${args[-2]}" >"$dst"
                echo "cp: error writing '$dst': No space left on device" >&2
                exit 1
            fi
            if [[ -e "$dst" || -L "$dst" ]]; then echo "cp: not replacing '$dst'" >&2; exit 1; fi
        fi
        exec "${args[@]}"
        ;;
    systemctl)
        scope=system
        if [[ "$1" == --user ]]; then scope=user; shift; fi
        mkdir -p "$S/units"
        case "$1" in
            is-active)
                if [[ "$2" == --quiet ]]; then
                    st="$(active_state "$(unit "$scope" "$3")")"
                    [[ "$st" == active || "$st" == reloading || "$st" == refreshing ]]
                    exit
                fi
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
                [[ -e "$S/hang-show" ]] && exec /usr/bin/sleep 30  # systemd does not answer
                u="$(unit "$scope" "${!#}")"
                case "$*" in
                    *ActiveState*)   # systemd prints ActiveState= before InvocationID=
                        echo "ActiveState=$(active_state "$u")"
                        echo "InvocationID=$(cat "$u.inv" 2>/dev/null)"
                        exit 0
                        ;;
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
            *GetManagedObjects*)  # fail-objects: its answer, then a failure
                cat "$S/objects.json" 2>/dev/null || exit
                [[ ! -e "$S/fail-objects" ]]
                exit
                ;;
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
            "-f json list cards")
                # After bluetoothctl connect: pactl-cards-connected, from the look after a2dp-delay looks.
                if [[ -e "$S/a2dp-connected" && -e "$S/pactl-cards-connected" ]]; then
                    n="$(cat "$S/a2dp-delay" 2>/dev/null || echo 0)"
                    if (( n > 0 )); then echo "$(( n - 1 ))" >"$S/a2dp-delay"; else cat "$S/pactl-cards-connected"; exit; fi
                fi
                cat "$S/pactl-cards" 2>/dev/null
                exit
                ;;
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

    def unit_active_state(self, scope: str, name: str, state: str) -> None:
        """The ActiveState word the fake systemctl gives for a unit (without it: active or inactive)."""
        self.put(os.path.join("units", f"{scope}-{name}.state"), state + "\n")

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


# ---- an in-process twin of FAKE ----------------------------------------------------------

BLUETOOTH_SERVICE = "bluetooth.service"
WIREPLUMBER_SERVICE = "wireplumber.service"


def _unified_diff(old: str, new: str) -> Answer:
    """diff -u OLD NEW as the real diff answers it: 0 for the same text, 1 and the hunks otherwise."""
    with open(old, encoding="utf-8") as f:
        a = f.read().splitlines(keepends=True)
    with open(new, encoding="utf-8") as f:
        b = f.read().splitlines(keepends=True)
    hunks = "".join(difflib.unified_diff(a, b, old, new))
    return (1 if hunks else 0), hunks, ""


def _text_or_none(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()
    except FileNotFoundError:
        return None


class SetupModel:
    """FAKE in process, for the commands apply and revert run (a CommandRunnerPort over the same
    state files of a Machine), with what each service read at its last (re)start: bluetoothd
    main.conf, WirePlumber the *.conf files of the user's wireplumber.conf.d. The fault tests run many
    applies; FAKE costs a fork of bash per command. A command it does not know raises
    AssertionError, so a new one shows up; test_setup_units' parity test keeps it in step with FAKE.

    restarts lists each restart that worked: (unit, whether it was needed then: the unit read other
    files than are on disk, or, for WirePlumber, bluetoothd restarted after it)."""

    def __init__(self, m: Machine) -> None:
        self.m = m
        self.calls: list[list[str]] = []
        self.restarts: list[tuple[str, bool]] = []
        self.loaded: dict[str, object] = {
            BLUETOOTH_SERVICE: self.disk(BLUETOOTH_SERVICE),
            WIREPLUMBER_SERVICE: self.disk(WIREPLUMBER_SERVICE),
        }
        self.started = {BLUETOOTH_SERVICE: 0, WIREPLUMBER_SERVICE: 0}  # the order of the last starts
        self.clock = 0

    # ---- what the services read -----------------------------------------------------------
    def disk(self, unit: str) -> object:
        """What the unit would read if it started now."""
        if unit == BLUETOOTH_SERVICE:
            return _text_or_none(self.m.main_conf)
        names = (
            sorted(n for n in os.listdir(self.m.wp_dir) if n.endswith(".conf")) if os.path.isdir(self.m.wp_dir) else []
        )
        return tuple((n, _text_or_none(os.path.join(self.m.wp_dir, n))) for n in names)

    def in_step(self, unit: str) -> bool:
        """The unit runs with what it would read now (WirePlumber: and started after bluetoothd)."""
        if self.loaded[unit] != self.disk(unit):
            return False
        return unit != WIREPLUMBER_SERVICE or self.started[WIREPLUMBER_SERVICE] >= self.started[BLUETOOTH_SERVICE]

    def inv(self, unit: str) -> str:
        scope = "system" if unit == BLUETOOTH_SERVICE else "user"
        return (_text_or_none(self.prefix(scope, unit) + ".inv") or "").strip()

    # ---- the state files, as FAKE keeps them --------------------------------------------------
    def state(self, name: str) -> str:
        return os.path.join(self.m.state, name)

    def has(self, name: str) -> bool:
        return os.path.exists(self.state(name))

    def cat(self, name: str) -> tuple[int, str]:
        text = _text_or_none(self.state(name))
        return (1, "") if text is None else (0, text)

    def prefix(self, scope: str, name: str) -> str:
        return self.state(os.path.join("units", f"{scope}-{name.removesuffix('.service')}"))

    def active_state(self, prefix: str) -> str:
        text = _text_or_none(prefix + ".state")
        if text is not None:
            return text.strip()
        return "active" if os.path.exists(prefix + ".active") else "inactive"

    def new_id(self, prefix: str) -> None:
        n = int((_text_or_none(self.state("counter")) or "0").strip()) + 1
        with open(self.state("counter"), "w", encoding="utf-8") as f:
            f.write(f"{n}\n")
        with open(prefix + ".inv", "w", encoding="utf-8") as f:
            f.write(f"inv-{os.path.basename(prefix)}-{n}\n")

    # ---- the commands -----------------------------------------------------------------------
    def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
        """CommandRunnerPort.run: each command FAKE answers that apply and revert run."""
        args = list(argv)
        self.calls.append(args)
        rc, out, err = self.answer(args)
        return CommandResult(rc, out, err, argv=tuple(args))

    def answer(self, args: list[str]) -> Answer:
        if args[:1] == ["timeout"]:
            args = args[2:]
        handler = self.handlers().get(args[0] if args else "")
        found = None if handler is None else handler(args)
        if found is None:
            raise AssertionError(f"SetupModel: no such command: {args}")
        return found

    def handlers(self) -> dict[str, Callable[[list[str]], Answer | None]]:
        """Each command by its first word; a handler answers None for a form FAKE does not know."""
        return {
            "mktemp": lambda _a: self.mktemp(),
            "sudo": lambda a: self.sudo(a[1:]),
            "systemctl": lambda a: self.systemctl(a[1:]),
            "busctl": lambda a: self.busctl(a[1:]),
            "bluetoothctl": lambda a: self.bluetoothctl(a[1:]),
            "journalctl": self.journalctl,
            "pw-dump": lambda _a: (*self.cat("pw-dump.json"), ""),
            "selinuxenabled": lambda _a: ((0 if self.has("selinux") else 1), "", ""),
            "diff": lambda a: _unified_diff(a[-2], a[-1]),
            "env": lambda a: self.wp_roles() if a[-1:] == ["wp-roles"] else None,
            "/usr/bin/python3": lambda a: (
                ((0 if self.has("iso-socket") else 1), "", "") if "socket" in " ".join(a) else None
            ),
        }

    def mktemp(self) -> Answer:
        fd, path = tempfile.mkstemp(prefix="tmp.", dir=self.m.tmp)
        os.close(fd)
        return 0, path + "\n", ""

    def journalctl(self, args: list[str]) -> Answer | None:
        if any(a.startswith("_SYSTEMD_INVOCATION_ID=") for a in args):
            return 0, self.cat("bt-journal")[1], ""
        if "--user" in args:
            return 0, self.cat("lw-journal")[1], ""
        return None

    def wp_roles(self) -> Answer:
        rc, text = self.cat("wp-roles")
        return (0, text, "") if rc == 0 else (5, "", "")

    def sudo(self, args: list[str]) -> Answer:
        how = _text_or_none(self.state(f"fail-sudo-{args[0]}"))
        if how is not None:
            if not how.strip().isdigit():
                raise AssertionError(f"SetupModel: fail-sudo-{args[0]} {how.strip()} is FAKE's alone")
            return int(how), "", ""
        if args[0] == "cp" and "--update=none-fail" in args:
            return self.cp_none_fail([a for a in args[1:] if a != "--update=none-fail"])
        kept: list[str] = []
        skip = False
        for a in args[1:]:
            if skip:
                skip = False
                continue
            if args[0] == "install" and a in ("-o", "-g"):
                skip = True
                continue
            if (args[0], a) in (("install", "-Z"), ("cp", "--preserve=context")):
                continue
            kept.append(a)
        match args[0], kept:
            case "cp", ["-p", src, dst]:
                shutil.copy2(src, dst)
            case "install", ["-m", mode, src, dst]:
                shutil.copyfile(src, dst)
                os.chmod(dst, int(mode, 8))
            case "mv", ["-f", src, dst]:
                os.replace(src, dst)
            case "systemctl", ["restart", "bluetooth"]:
                return self.systemctl(["restart", "bluetooth"])
            case _:
                raise AssertionError(f"SetupModel: no such sudo command: {args}")
        return 0, "", ""

    def cp_none_fail(self, args: list[str]) -> Answer:
        """sudo cp -p [--preserve=context] --update=none-fail SRC DST, as FAKE answers it."""
        dst = args[-1]
        if self.has("race-backup"):
            os.unlink(self.state("race-backup"))
            with open(dst, "w", encoding="utf-8") as f:
                f.write("another run's backup\n")
            return 1, "", f"cp: cannot create regular file '{dst}': File exists\n"
        if self.has("partial-backup"):
            with open(args[-2], encoding="utf-8") as src, open(dst, "w", encoding="utf-8") as f:
                f.write(src.read(8))
            return 1, "", f"cp: error writing '{dst}': No space left on device\n"
        if os.path.lexists(dst):
            return 1, "", f"cp: not replacing '{dst}'\n"
        match [a for a in args if a != "--preserve=context"]:
            case ["-p", src, _]:
                shutil.copy2(src, dst)
            case _:
                raise AssertionError(f"SetupModel: no such sudo cp: {args}")
        return 0, "", ""

    def systemctl(self, args: list[str]) -> Answer:
        scope = "system"
        if args[:1] == ["--user"]:
            scope, args = "user", args[1:]
        os.makedirs(self.state("units"), exist_ok=True)
        match args[0]:
            case "is-active":
                return self.is_active(scope, args)
            case "is-enabled":
                enabled = os.path.exists(self.prefix(scope, args[1]) + ".enabled")
                return (0, "enabled\n", "") if enabled else (1, "disabled\n", "")
            case "show":
                return self.show(scope, args)
            case "show-environment":
                return (1 if self.has("no-session") else 0), "", ""
            case "daemon-reload":
                return 0, "", ""
            case "restart" | "start":
                return self.start_unit(scope, args)
            case _:
                raise AssertionError(f"SetupModel: no such systemctl command: {args}")

    def is_active(self, scope: str, args: list[str]) -> Answer:
        if args[1] == "--quiet":
            running = self.active_state(self.prefix(scope, args[2])) in ("active", "reloading", "refreshing")
            return (0 if running else 3), "", ""
        active = os.path.exists(self.prefix(scope, args[1]) + ".active")
        return (0, "active\n", "") if active else (3, "inactive\n", "")

    def show(self, scope: str, args: list[str]) -> Answer:
        u = self.prefix(scope, args[-1])
        joined = " ".join(args)
        if "ActiveState" in joined:
            inv = (_text_or_none(u + ".inv") or "").strip()
            return 0, f"ActiveState={self.active_state(u)}\nInvocationID={inv}\n", ""
        if "InvocationID" in joined:
            return 0, _text_or_none(u + ".inv") or "", ""
        if "MainPID" in joined:
            return 0, _text_or_none(u + ".pid") or "0\n", ""
        return 1, "", ""

    def start_unit(self, scope: str, args: list[str]) -> Answer:
        u = self.prefix(scope, args[1])
        if os.path.exists(u + ".fail"):
            return 1, "", f"Job for {args[1]} failed.\n"
        if args[0] == "restart" or not os.path.exists(u + ".active"):
            self.new_id(u)
        with open(u + ".active", "w", encoding="utf-8"):
            pass
        self.started_now(f"{args[1].removesuffix('.service')}.service")
        return 0, "", ""

    def started_now(self, unit: str) -> None:
        if unit not in self.loaded:
            return
        self.restarts.append((unit, not self.in_step(unit)))
        self.clock += 1
        self.loaded[unit] = self.disk(unit)
        self.started[unit] = self.clock

    def busctl(self, args: list[str]) -> Answer:
        joined = " ".join(args)
        if "GetManagedObjects" in joined:
            rc, text = self.cat("objects.json")
            return (1 if rc == 0 and self.has("fail-objects") else rc), text, ""
        if joined == "--system list --no-legend --unique":
            return 0, self.cat("bus-names")[1], ""
        if args[:1] == ["get-property"]:
            return (*self.cat(f"prop-{args[3]}-{args[4]}"), "")
        raise AssertionError(f"SetupModel: no such busctl command: {args}")

    def bluetoothctl(self, args: list[str]) -> Answer:
        if args[:1] == ["info"]:
            rc, text = self.cat("bt-info")
            return (0, text, "") if rc == 0 else (1, f"Device {args[1]} not available\n", "")
        raise AssertionError(f"SetupModel: no such bluetoothctl command: {args}")
