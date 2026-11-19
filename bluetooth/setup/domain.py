# SPDX-License-Identifier: MPL-2.0
"""The rules of `cst bluetooth` (bluetooth-setup.sh): its command line, its settings,
where its files are, and the words of its plan and status tables. No I/O.

bluetooth/setup/application/ runs the commands over the machine; bt-helper's
rules (main.conf, the WirePlumber files, the watcher's unit) are in
bluetooth.domain.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import os.path
import re
from typing import Final

from bluetooth.domain import LDAC_PROG, LDAC_UNIT, LE_CONF_NAME, OLD_MARKERS, QUALITIES
from cst_core.domain.dispatch import HELP_WORDS
from cst_core.domain.text import enum_member


# The marker of the rule apply-le-audio.sh wrote (revert removes that rule too).
OLD_WP_MARKER: Final = OLD_MARKERS[0]
# A2DP Audio Sink: the one profile reconnected after a WirePlumber restart.
A2DP_SINK_UUID: Final = "0000110b-0000-1000-8000-00805f9b34fb"
BEARERS: Final = ("", "le", "bredr", "last-used")
VALUED: Final = ("--device", "--ldac-quality", "--bearer", "--le-audio")
MAC_RE: Final = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
TIMEOUT_RE: Final = re.compile(r"[0-9]{1,9}")
# Which command takes which option (bluetooth-setup.sh's allowed()).
_ALLOWED: Final = frozenset(
    {
        ("plan", "--ldac-quality"),
        ("apply", "--ldac-quality"),
        ("status", "--ldac-quality"),
        ("plan", "--bearer"),
        ("apply", "--bearer"),
        ("plan", "--le-audio"),
        ("apply", "--le-audio"),
        ("status", "--le-audio"),
        ("apply", "--force"),
        ("apply", "--yes"),
        ("revert", "--yes"),
        ("apply", "--no-restart"),
        ("revert", "--no-restart"),
        ("revert", "--check"),
        ("le-audio", "--yes"),
        ("le-audio", "--no-restart"),
    }
)


class BluetoothCommand(StrEnum):
    """The commands of `cst bluetooth`, in the order its usage lists them."""

    PLAN = "plan"
    APPLY = "apply"
    STATUS = "status"
    REVERT = "revert"
    LE_AUDIO = "le-audio"


class WpState(StrEnum):
    """The headset's LDAC rule in the plan: bt-helper's state of the file (OwnedState's words), or none (no headset)."""

    OK = "ok"
    ADOPT = "adopt"
    CHANGE = "change"
    MISSING = "missing"
    CONFLICT = "conflict"
    NONE = "none"


class LeState(StrEnum):
    """The LE Audio drop-in in the plan: with LE Audio off, bt-helper's state of the file (OwnedState's
    words); with it on, absent, remove (CST's: apply removes it) or conflict."""

    OK = "ok"
    ADOPT = "adopt"
    CHANGE = "change"
    MISSING = "missing"
    CONFLICT = "conflict"
    ABSENT = "absent"
    REMOVE = "remove"


class BearerState(StrEnum):
    """The preferred bearer in the plan."""

    NONE = "none"  # no BEARER set
    NOHEADSET = "noheadset"
    UNKNOWN = "unknown"  # bluetoothctl not found
    UNPAIRED = "unpaired"
    OK = "ok"  # already the one set
    SET = "set"  # apply sets it


class BapState(StrEnum):
    """PipeWire's BAP LC3 endpoint, by the last BAPSink event of the running bluetoothd."""

    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    NO_RECORD = "no-record"
    UNKNOWN = "unknown"  # no bluetoothd or WirePlumber found


@dataclass(frozen=True, slots=True)
class Options:
    """The command line of one run."""

    cmd: BluetoothCommand
    args: tuple[str, ...]  # the command and its words, as given (for the messages and the re-runs)
    device: str = ""
    quality: str = ""
    bearer: str = ""
    bearer_given: bool = False
    le: str = ""
    le_set: str = ""  # le-audio on|off
    force: bool = False
    yes: bool = False
    no_restart: bool = False
    check: bool = False


@dataclass(frozen=True, slots=True)
class Exit:
    """A run that ends before it looks at the machine: its stdout, stderr and exit status."""

    status: int
    out: str = ""
    err: str = ""


def allowed(cmd: BluetoothCommand, opt: str) -> bool:
    """Whether the command takes the option."""
    if opt == "--device":
        return cmd != BluetoothCommand.LE_AUDIO
    return (cmd, opt) in _ALLOWED


def parse_command_line(argv: Sequence[str], prog: str, usage: str) -> Options | Exit:
    """The options, or the help (usage: its text) or a usage error, as bluetooth-setup.sh read them."""
    word0 = argv[0] if argv and argv[0] else "help"  # ${1:-help}: an empty word is help too
    rest = list(argv[1:])
    words = (word0, *rest)
    if word0 in HELP_WORDS:
        return Exit(0, out=usage)
    cmd = enum_member(BluetoothCommand, word0)
    if cmd is None:
        return Exit(2, err=f"Unknown command: {word0}\n{usage}")
    le_set = ""
    if cmd == BluetoothCommand.LE_AUDIO and rest and not rest[0].startswith("--"):
        le_set = rest.pop(0)
        if le_set not in ("on", "off"):
            return Exit(2, err=f"{prog} le-audio: say on or off (got '{le_set}')\n")
    values: dict[str, str] = {}
    flags: set[str] = set()
    while rest:
        word = rest[0]
        opt = word.split("=", 1)[0]
        if not allowed(cmd, opt):
            return Exit(2, err=f"{prog} {cmd}: unknown option '{word}' (see {prog} help)\n")
        if word in VALUED:
            if len(rest) < 2 or rest[1].startswith("--"):
                return Exit(2, err=f"{prog} {cmd}: {word} needs a value\n")
            val = rest[1]
            rest.pop(0)
        elif word.startswith("--") and "=" in word:
            val = word.split("=", 1)[1]
        else:
            val = ""
        if opt in VALUED:
            values[opt] = val
        else:
            flags.add(opt)
        rest.pop(0)
    return Options(
        cmd=cmd,
        args=words,
        device=values.get("--device", ""),
        quality=values.get("--ldac-quality", ""),
        bearer=values.get("--bearer", ""),
        bearer_given="--bearer" in values,
        le=values.get("--le-audio", ""),
        le_set=le_set,
        force="--force" in flags,
        yes="--yes" in flags,
        no_restart="--no-restart" in flags,
        check="--check" in flags,
    )


def changes_machine(opts: Options) -> bool:
    """The commands that must run on the host (revert --check and le-audio alone only read)."""
    return (
        opts.cmd == BluetoothCommand.APPLY
        or (opts.cmd == BluetoothCommand.REVERT and not opts.check)
        or (opts.cmd == BluetoothCommand.LE_AUDIO and bool(opts.le_set))
    )


def bt_timeout(raw: str | None) -> int | Exit:
    """BT_TIMEOUT: whole seconds, read as base 10 (5 when unset or empty)."""
    value = raw or "5"
    if not TIMEOUT_RE.fullmatch(value):
        return Exit(2, err=f"BT_TIMEOUT must be a whole number of seconds (got '{value}')\n")
    return int(value, 10)


def read_settings_output(shown: str) -> dict[str, str]:
    """KEY=value lines (bt-helper settings) as `while IFS='=' read -r k v` read them."""
    out: dict[str, str] = {}
    for line in shown.split("\n"):
        if not line:
            continue
        key, sep, val = line.partition("=")
        if sep and val.endswith("=") and val.count("=") == 1:
            val = val[:-1]  # read drops a lone delimiter at the end of the last field
        out[key] = val
    return out


def home_tilde(path: str, home: str) -> str:
    """${PATH/#"${HOME}"/\\~}: home at the start written as ~."""
    return "~" + path[len(home) :] if home and path.startswith(home) else path


@dataclass(frozen=True, slots=True)
class Paths:
    """Where the program's files are, for one user and one host root."""

    home: str
    config: str  # XDG_CONFIG_HOME or ~/.config
    etc: str
    usr_share: str
    checkout: str  # the CST folder

    @property
    def main_conf(self) -> str:
        return f"{self.etc}/bluetooth/main.conf"

    @property
    def user_settings(self) -> str:
        return f"{self.config}/cst/bluetooth.conf"

    @property
    def default_settings(self) -> str:
        return f"{self.checkout}/bluetooth/bluetooth.conf"

    @property
    def helper(self) -> str:
        return f"{self.checkout}/bluetooth/bt-helper"

    @property
    def installer(self) -> str:
        return f"{self.checkout}/cst_core/installer.py"

    @property
    def lw_src(self) -> str:
        return f"{self.checkout}/bluetooth/bt-ldac-watch"

    @property
    def self_script(self) -> str:
        return f"{self.checkout}/bluetooth-setup.sh"

    @property
    def wp_dir(self) -> str:
        return f"{self.config}/wireplumber/wireplumber.conf.d"

    def wp_conf(self, device_id: str) -> str:
        return f"{self.wp_dir}/50-bluez-{device_id}.conf"

    @property
    def legacy_wp_conf(self) -> str:
        """The very first name of apply-le-audio.sh's rule: removed when found."""
        return f"{self.wp_dir}/50-jbl-live-beam-4.conf"

    @property
    def le_conf(self) -> str:
        return f"{self.wp_dir}/{LE_CONF_NAME}"

    @property
    def fragment_dirs(self) -> tuple[str, ...]:
        """Where WirePlumber looks for fragments, highest priority first."""
        return (
            self.wp_dir,
            f"{self.etc}/xdg/wireplumber/wireplumber.conf.d",
            f"{self.etc}/wireplumber/wireplumber.conf.d",
            f"{self.usr_share}/wireplumber/wireplumber.conf.d",
        )

    @property
    def lw_prog(self) -> str:
        """The watcher's program, where the unit's ExecStart (bluetooth.domain.LDAC_PROG, %h/...) names it."""
        return self.home + LDAC_PROG.removeprefix("%h")

    @property
    def lw_unit_file(self) -> str:
        return f"{self.config}/systemd/user/{LDAC_UNIT}"

    @property
    def pending(self) -> str:
        return f"{self.home}/.local/state/cst/bluetooth-restart-pending.json"


@dataclass(frozen=True, slots=True)
class Config:
    """The settings of one run: the options over the user's settings over CST's."""

    prog: str
    device: str  # upper case
    device_from: str
    quality: str
    bearer: str
    ldac_rate: str
    le_audio: str
    le_from: str
    user_keys: tuple[str, ...]

    @property
    def device_id(self) -> str:
        return self.device.replace(":", "_")


def resolve(
    opts: Options, settings: dict[str, str], user_keys: Sequence[str], paths: Paths, prog: str
) -> Config | Exit:
    """The settings of the run, or the usage error that stops it (exit 2)."""
    device = opts.device or settings.get("DEVICE", "")
    quality = opts.quality or settings.get("LDAC_QUALITY", "") or "auto"
    bearer = opts.bearer if opts.bearer_given else settings.get("BEARER", "")
    rate = settings.get("LDAC_RATE", "")
    le = opts.le or settings.get("LE_AUDIO", "") or "off"
    le_from = "the CST default"
    if "LE_AUDIO" in user_keys:
        le_from = home_tilde(paths.user_settings, paths.home)
    if opts.le:
        le_from = "--le-audio, this run only"
    if opts.le_set:
        le = opts.le_set

    def bad(msg: str) -> Exit:
        return Exit(2, err=f"{prog} {opts.cmd}: {msg}\n")

    if bearer not in BEARERS:
        return bad(f"the bearer must be le, bredr or last-used (got '{bearer}')")
    if rate not in ("", "48000"):
        return bad(f"LDAC_RATE must be empty (watcher off) or 48000 (got '{rate}' in the settings)")
    if le not in ("on", "off"):
        return bad(f"LE Audio must be on or off (got '{le}'; LE_AUDIO in the settings or --le-audio)")
    if quality not in QUALITIES:
        return bad(f"the LDAC quality must be auto, hq, sq or mq (got '{quality}')")
    # No headset at all is fine (CST ships none): the run skips its parts (see the Survey).
    if device and not MAC_RE.fullmatch(device):
        return bad(
            f"the device must be a MAC address like AA:BB:CC:DD:EE:FF (got '{device}'; set DEVICE in {paths.user_settings} or use --device)"
        )
    return Config(
        prog=prog,
        device=device.upper(),
        device_from=("--device" if opts.device else "settings") if device else "no DEVICE set",
        quality=quality,
        bearer=bearer,
        ldac_rate=rate,
        le_audio=le,
        le_from=le_from,
        user_keys=tuple(user_keys),
    )


# A WirePlumber rule CST writes for one headset: 50-bluez-<its address, _ for :>.conf, in capitals
# (as every version wrote it: Config.device is upper case).
RULE_NAME: Final = re.compile(r"50-bluez-((?:[0-9A-F]{2}_){5}[0-9A-F]{2})\.conf")


def rule_device(name: str) -> str | None:
    """The headset a rule's file name is for (AA:BB:...), or None (another name, a .bak copy, an
    address in lower case, which is not CST's name for it)."""
    m = RULE_NAME.fullmatch(name)
    return m.group(1).replace("_", ":") if m else None


def no_headset_note(rules: Sequence[str], settings: str) -> str:
    """What the parts of a headset say when none is set (settings: the user's file, as shown)."""
    if rules:
        return (
            f"no headset set, and CST has LDAC rules for several ({' '.join(rules)}): "
            f"set DEVICE=<one of them> in {settings}, or use --device MAC"
        )
    return (
        f"no headset set: set DEVICE=AA:BB:CC:DD:EE:FF in {settings} "
        "(bluetoothctl devices lists the paired ones), or use --device MAC"
    )


def pad(text: str, width: int) -> str:
    """text padded with spaces to width bytes, as printf's %-Ns pads."""
    return text + " " * max(0, width - len(text.encode("utf-8", "surrogateescape")))


def item(word: str, label: str, note: str = "") -> str:
    """One line of the plan's and the status's tables (52: the widest label still fits)."""
    return f"  {pad(word, 9)} {pad(label, 52)} {note}\n"


def wp_label(path: str) -> str:
    return "wireplumber.conf.d/" + os.path.basename(path)


def wp_note(state: str, quality: str, device: str) -> str:
    """What plan says about the WirePlumber rule in this state."""
    return {
        "ok": f"up to date (LDAC {quality} for {device})",
        "adopt": "up to date; written by apply-le-audio.sh or with older comments: apply only renews the comments, no restart",
        "change": f"differs (apply writes LDAC {quality}; the old file is kept as .bak)",
        "missing": "missing",
        "conflict": "not written by CST or the old script: apply refuses; move it away by hand",
    }.get(state, "")


def wp_word(state: str) -> str:
    """The first column of the rule's line."""
    return {"ok": "ok", "adopt": "adopt", "change": "update", "missing": "write", "conflict": "CONFLICT"}.get(state, "")


def bearer_matches(info: str, bearer: str) -> bool:
    """bluetoothctl info shows PreferredBearer: BEARER (grep -E on its lines)."""
    pattern = re.compile(rf"^[ \t\n\r\f\v]*PreferredBearer: {re.escape(bearer)}[ \t\n\r\f\v]*$")
    return any(pattern.search(line) for line in info.split("\n"))


def preferred_bearer(info: str) -> str:
    """The first PreferredBearer value in bluetoothctl info (sed's first match)."""
    for line in info.split("\n"):
        m = re.match(r"^[ \t\n\r\f\v]*PreferredBearer:[ \t\n\r\f\v]*([^ \t\n\r\f\v]*)", line)
        if m:
            return m.group(1)
    return ""


def matches_a_line(info: str, pattern: str) -> bool:
    """grep -Eq PATTERN on the lines of info."""
    rx = re.compile(pattern)
    return any(rx.search(line) for line in info.split("\n"))


def senders_of(bus_list: str, pid: str) -> list[str]:
    """busctl list --unique: the bus names of the process pid (awk '$2 == p {print $1}')."""
    out: list[str] = []
    for line in bus_list.split("\n"):
        f = line.split()
        if len(f) >= 2 and (f[1] == pid or (f[1].isdigit() and pid.isdigit() and int(f[1]) == int(pid))):
            out.append(f[0])
    return out


BAP_EVENT: Final = re.compile(r"Endpoint (un)?registered:.*MediaEndpointLE/BAPSink/lc3")


def bap_state(journal: str, senders: Sequence[str]) -> BapState:
    """registered | unregistered | no-record: the last BAPSink event of one of the senders."""
    events = [line for line in journal.split("\n") if BAP_EVENT.search(line)]
    mine = [e for e in events if any(f"sender={s} " in e for s in senders)]
    last = mine[-1] if mine else ""
    if last.startswith("Endpoint registered:"):
        return BapState.REGISTERED
    return BapState.NO_RECORD if not last else BapState.UNREGISTERED
