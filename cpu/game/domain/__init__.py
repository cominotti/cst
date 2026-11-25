# SPDX-License-Identifier: MPL-2.0
"""The game profile's rules (cpu-game-profile): its settings, its records and what they say. No I/O.

game.state holds one record per change 'on' made, written before the change,
so that 'off' can undo whatever was changed even when 'on' died halfway:
"prop MGR UNIT PROP existed OLD" (a runtime override was there, OLD its
value) or "prop MGR UNIT PROP new -", "cgw FILE OLD" (a cgroup file written
directly), "epp CPU OLD". game.intended holds what 'on' set, as systemd
showed it right after: "prop MGR UNIT PROP VALUE SHOWN" and "cgw FILE VALUE
SHOWN", for the drift repair. The fields are written as bash's printf %q
writes them, because an installed bash game-profile (an older CST) reads
these files with eval and greps them for its own quoted form.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import fnmatch
import posixpath
import re
from typing import Final, Literal

from cst_core.domain.contracts import CPU_SETTINGS, StateFormatKey, first_line_version, state_format
from cst_core.domain.text import enum_member, file_lines, group, printf_q, printf_q_words, strip_ascii_blanks
from cst_core.domain.values import MINUTE, parse_whole_number

# How long a writer under the run lock (/run/cpu-setup/lock: the game profile's on, off and tick,
# cpu-power's apply and revert) waits for it before it gives up.
RUN_LOCK_WAIT_S: Final = MINUTE

type Manager = Literal["sys", "usr"]
type Record = tuple[str, ...]


class GameRecordKind(StrEnum):
    """The first field of a record of game.state and game.intended."""

    PROP = "prop"  # a unit property (set-property --runtime)
    CGW = "cgw"  # a cgroup file written directly
    EPP = "epp"  # a CPU's energy/performance preference (game.state only)
    SYSFS = "sysfs"  # a generic sysfs file written directly and restored on off (game.state only)
    USER = "user"  # the uid the user manager's records after it are for (game.leftovers only)


_MANAGERS: Final[dict[str, Manager]] = {"sys": "sys", "usr": "usr"}


def manager_of(word: str) -> Manager | None:
    """The manager a record's MGR field names (sys or usr), None for any other word."""
    return _MANAGERS.get(word)


def _kind(rec: Sequence[str]) -> GameRecordKind | None:
    """A record's kind, None for an empty record or another word (the format line of game.intended)."""
    return enum_member(GameRecordKind, rec[0]) if rec else None


# The journal tag of every line (log in the adapters).
TAG: Final = "game-profile"
# The MemoryLow drop-ins 'on' writes into the system manager's runtime folder: zz- sorts after
# every other drop-in (uresourced's 50-MemoryLow.conf included), so it wins at a daemon-reload.
GAME_DROPIN: Final = "zz-cst-game.conf"
# The slices the profile and `cst cpu` act on by name: Docker's builds.slice (cpu docker-slice) in the
# system manager, the user manager's app.slice (the desktop apps and the game), and user.slice, which both
# managers have (the system's holds each user's session, the user manager's the toolboxes).
BUILDS_SLICE: Final = "builds.slice"
APP_SLICE: Final = "app.slice"
USER_SLICE: Final = "user.slice"
# The weight 'on' gives the boxes that run beside the game: the idle class.
IDLE_WEIGHT: Final = "CPUWeight=idle"
# Top-level slices of the user manager that have rules of their own: the game and the desktop
# apps (app.slice), gnome-shell (session.slice), the toolboxes (user.slice).
KNOWN_SLICES: Final = frozenset({APP_SLICE, "session.slice", USER_SLICE})
STEAM_SCOPES: Final = "app-flatpak-com.valvesoftware.Steam-*"
DOCKER_SCOPES: Final = "docker-*.scope"
USAGE: Final = "usage: game-profile on|off|tick|state"
# game.intended's first line names its format (cst_core.domain.contracts): "cst-game-intended 1".
INTENDED_MAGIC: Final = "cst-game-intended"
INTENDED_FORMAT: Final = state_format(StateFormatKey.GAME_INTENDED)


def _in(folder: str, name: str) -> str:
    """A file of the run folder."""
    return posixpath.join(folder, name)


def run_lock(run: str) -> str:
    """The run folder's lock: cpu-game-profile's on, off and tick, and cpu-power's apply and revert, hold
    it, so they never write GPU and C-state files at the same time."""
    return _in(run, "lock")


@dataclass(frozen=True, slots=True)
class GamePaths:
    """Where the profile reads and writes (tests point them into a folder of their own)."""

    conf: str = CPU_SETTINGS
    run: str = posixpath.dirname(state_format(StateFormatKey.GAME_STATE).path)
    cgroot: str = "/sys/fs/cgroup"
    # The system manager's runtime unit folder (the MemoryLow drop-ins).
    unit_runtime: str = "/run/systemd/system"
    # Where `set-property --runtime` writes: under /run/systemd (system manager) and
    # /run/user/UID/systemd (the user's).
    run_systemd: str = "/run/systemd"
    run_user: str = "/run/user"
    sys_cpu: str = "/sys/devices/system/cpu"
    sys_drm: str = "/sys/class/drm"
    proc: str = "/proc"

    @property
    def state(self) -> str:
        """game.state: exists exactly while the profile is on (other projects key on that)."""
        return posixpath.join(self.run, posixpath.basename(state_format(StateFormatKey.GAME_STATE).path))

    @property
    def intended(self) -> str:
        """game.intended: what 'on' set, for the drift repair."""
        return posixpath.join(self.run, posixpath.basename(state_format(StateFormatKey.GAME_INTENDED).path))

    @property
    def leftovers(self) -> str:
        """game.leftovers: the records 'off' could not undo, for the next 'off' and tick."""
        return _in(self.run, posixpath.basename(state_format(StateFormatKey.GAME_LEFTOVERS).path))

    @property
    def manual(self) -> str:
        """Switched on by hand: the watchdog leaves it on."""
        return _in(self.run, "manual")

    @property
    def suppress(self) -> str:
        """Switched off by hand: no automatic 'on' until the game ends."""
        return _in(self.run, "suppress")

    @property
    def lock(self) -> str:
        """on, off and tick never interleave (run_lock)."""
        return run_lock(self.run)

    def epp(self, cpu: str) -> str:
        """The energy/performance preference of one CPU."""
        return posixpath.join(self.sys_cpu, f"cpu{cpu}", "cpufreq", "energy_performance_preference")

    @property
    def settings_warned(self) -> str:
        """What the watchdog last said about settings 'on' refuses (once per boot): the settings file's
        problem lines, or a TARGET_USER that is unset or names no account."""
        return _in(self.run, "settings-warned")

    @property
    def drift_warned(self) -> str:
        """What the watchdog last said about drift it does not repair (once per boot)."""
        return _in(self.run, "drift-warned")


@dataclass(frozen=True, slots=True)
class GameSettings:
    """The profile's settings: /etc/cpu-setup/cpu-setup.conf over these defaults."""

    target_user: str = ""
    build_cpus: str = "12-23"  # VM, Docker and nothing of the game while it runs
    p_cores: str = "0-7"
    vm_weight: str = "10"
    game_memory_low: str = "16G"  # Dota 2 ~10 GB resident + steamwebhelper
    game_zswap_writeback: str = "no"  # app.slice during games: "no" = swapped pages stay in zswap
    gpu_pm_mode: str = "always"
    # "" when the settings file does not set it: cpu-power then applies boot_cstate_limit_us's default for
    # the host's cpuidle driver (200 us with intel_idle).
    cpu_cstate_max_latency_us: str = ""
    game_cstate_max_latency_us: str = "50"


_SETTING_KEYS: Final = {
    "TARGET_USER": "target_user",
    "BUILD_CPUS": "build_cpus",
    "P_CORES": "p_cores",
    "VM_WEIGHT": "vm_weight",
    "GAME_MEMORY_LOW": "game_memory_low",
    "GAME_ZSWAP_WRITEBACK": "game_zswap_writeback",
    "GPU_PM_MODE": "gpu_pm_mode",
    "CPU_CSTATE_MAX_LATENCY_US": "cpu_cstate_max_latency_us",
    "GAME_CSTATE_MAX_LATENCY_US": "game_cstate_max_latency_us",
}


_CSTATE_OFF_WORDS: Final = frozenset({"0", "off", "none"})
_AMD_PROFILE_ROW_RE: Final = re.compile(r"^\s*([0-9]+)\s+([A-Z0-9_]+)\s*(\*?)\s*(?::|$)")
_AMD_FULLSCREEN_PROFILE: Final = "3D_FULL_SCREEN"


def parse_cstate_limit_us(raw: str, *, default_us: int) -> tuple[int | None, str | None]:
    """Parse a C-state exit latency ceiling (`200`, `50`, or `off`/`0`); return `(limit_us_or_None, error)`.
    ASCII blanks around the value are dropped; a no-break space is part of it."""
    cleaned = strip_ascii_blanks(raw).lower()
    if not cleaned:
        return default_us, None
    if cleaned in _CSTATE_OFF_WORDS:
        return None, None
    # ASCII digits within int()'s limit (parse_whole_number): more digits are no number, as any other word.
    if (n := parse_whole_number(cleaned)) is not None and n > 0:
        return n, None
    return default_us, f"{raw!r} must be a positive microsecond integer or 'off'"


def parse_amd_profile_modes(text: str) -> tuple[str | None, str | None]:
    """Return `(current_index, 3D_FULL_SCREEN_index)` parsed from `pp_power_profile_mode`."""
    current_idx: str | None = None
    fullscreen_idx: str | None = None
    for line in text.splitlines():
        m = _AMD_PROFILE_ROW_RE.match(line)
        if m is None:
            continue
        idx, name, marker = group(m, 1), group(m, 2), group(m, 3)
        if marker == "*":
            current_idx = idx
        if name == _AMD_FULLSCREEN_PROFILE:
            fullscreen_idx = idx
    return current_idx, fullscreen_idx


def game_settings(values: Mapping[str, str]) -> GameSettings:
    """The settings a settings file's values give (keys it does not set keep their default)."""
    d = GameSettings()
    return GameSettings(**{attr: values.get(key, getattr(d, attr)) for key, attr in _SETTING_KEYS.items()})


def vm_io_weight(vm_weight: str) -> str:
    """machine.slice's IOWeight while the game runs: VM_WEIGHT when it is 1..10000 (systemd's
    IOWeight does not accept 'idle'), else 10."""
    if re.fullmatch(r"[0-9]+", vm_weight) and 1 <= int(vm_weight) <= 10000:
        return vm_weight
    return "10"


def expand_cpus(spec: str) -> list[str]:
    """ "0-3,6" -> ["0", "1", "2", "3", "6"]. A range whose ends are not whole numbers gives
    nothing, as `seq` did."""
    out: list[str] = []
    parts = spec.split(",")
    if parts and parts[-1] == "":
        parts.pop()  # `read -ra` drops a last empty field
    for part in parts:
        if "-" in part:
            lo, hi = part[: part.rfind("-")], part[part.find("-") + 1 :]
            if re.fullmatch(r"\s*\+?[0-9]+\s*", lo) and re.fullmatch(r"\s*\+?[0-9]+\s*", hi):
                out.extend(str(n) for n in range(int(lo), int(hi) + 1))
        else:
            out.append(part)
    return " ".join(out).split()


def transient_unit(unit: str) -> bool:
    """A unit systemd made at run time (scopes: Docker containers, Steam, apps; systemd-run's
    run-*.service): its runtime overrides live next to it, in the transient folder."""
    return fnmatch.fnmatchcase(unit, "*.scope") or fnmatch.fnmatchcase(unit, "run-*.service")


def ctl_file(paths: GamePaths, mgr: Manager, unit: str, prop: str, uid: str) -> str:
    """The file `systemctl set-property --runtime UNIT PROP=...` writes (uid: the user's, for usr)."""
    base = paths.run_systemd if mgr == "sys" else posixpath.join(paths.run_user, uid, "systemd")
    if transient_unit(unit):
        folder = "transient"
    elif mgr == "sys":
        folder = "system.control"
    else:
        folder = "user.control"
    return f"{base}/{folder}/{unit}.d/50-{prop}.conf"


def shown(stdout: str) -> str:
    """What `systemctl show -P PROP` printed, as the profile reads it: "[not set]" is empty, the
    last newlines go (a shell's command substitution)."""
    return "\n".join("" if line == "[not set]" else line for line in stdout.split("\n")).rstrip("\n")


def show_values(stdout: str) -> dict[str, str]:
    """{PROP: value} of `systemctl show -p A -p B UNIT` ("[not set]" is empty)."""
    vals: dict[str, str] = {}
    for line in stdout.split("\n"):
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        vals[key] = "" if value == "[not set]" else value
    return vals


def record_line(*fields: str) -> str:
    """One record: its fields as printf %q writes them, a blank between, and a newline."""
    return " ".join(printf_q(f) for f in fields) + "\n"


def intended_header() -> str:
    """The first line of a game.intended this version writes."""
    return record_line(INTENDED_MAGIC, str(INTENDED_FORMAT.version))


def newer_intended(path: str, text: str) -> str | None:
    """Why game.intended cannot be compared (a format newer than this version reads, or a first line
    that names no version), None when it can."""
    found = first_line_version(text, INTENDED_MAGIC)
    if found is None:
        return f"{path}: its first line names no format version"
    return INTENDED_FORMAT.newer_text(path, found) if found > INTENDED_FORMAT.version else None


def ends_cut_short(data: str | bytes) -> bool:
    """The file (its text or its bytes) ends in a line with no newline at its end: a record cut short.
    Every writer (this one, bash's printf) ends a record with a newline and writes it before the change
    it describes, so the change of such a line was never made, and a value cut short (an old value "1"
    of "100") must not be undone. An empty file is none."""
    if isinstance(data, str):
        return data != "" and not data.endswith("\n")
    return data != b"" and not data.endswith(b"\n")


def _cut_short(text: str, lines: Sequence[str], number: int) -> bool:
    """Line number (1-based) of a file is its last line, cut short (ends_cut_short)."""
    return number == len(lines) and ends_cut_short(text)


def cut_tail(data: bytes) -> tuple[int, bytes] | None:
    """A file's last line with no newline at its end (a record cut short, ends_cut_short): the length the
    file keeps without it (just after its last newline; 0 for a file of that line only) and the line's
    bytes. None for an empty file, or one that ends in a newline."""
    if not ends_cut_short(data):
        return None
    keep = data.rfind(b"\n") + 1
    return keep, data[keep:]


def _whole_lines(text: str) -> list[str]:
    """The lines of game.state or game.intended that can hold a record: every line but a last one cut
    short (no newline at its end, ends_cut_short)."""
    lines = file_lines(text)
    return lines[:-1] if ends_cut_short(text) else lines


def records(text: str) -> list[Record]:
    """The records of game.state or game.intended in file order; a line bash could not read as
    plain words, and a last line cut short (no newline at its end), are left out."""
    out: list[Record] = []
    for line in _whole_lines(text):
        words = printf_q_words(line)
        if words is not None:
            out.append(tuple(words))
    return out


def unreadable_lines(text: str, *, intended: bool) -> list[tuple[int, str]]:
    """The lines (number, text) of game.state, or of game.intended (intended), that hold no record the
    profile can use: bash could not read them as plain words (an unclosed quote), or they have fewer
    fields than their kind needs, or are the last line with no newline at its end (a record cut
    short). Blank lines are none. records() leaves such a line out; bash's eval stopped 'off' there
    instead (exit 1, game.state kept), and undid a last line cut short with its cut value."""
    out: list[tuple[int, str]] = []
    lines = file_lines(text)
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        words = printf_q_words(line)
        if words is None or _cut_short(text, lines, number):
            out.append((number, line))
            continue
        if not intended:
            if state_undo(words) is None:  # as 'off' reads it
                out.append((number, line))
            continue
        match _kind(words):
            case GameRecordKind.PROP:
                need = INTENDED_PROP_FIELDS
            case GameRecordKind.CGW:
                need = INTENDED_CGW_FIELDS
            case _:  # another kind (the format line) is no drift record
                need = 0
        if len(words) < need:
            out.append((number, line))
    return out


def state_head(*, on: bool, manual: bool, problem_lines: Sequence[str], why: str | None) -> list[str]:
    """The first lines of 'state': on (by hand) or off, then each problem line of the settings file and why
    'on' refuses for TARGET_USER (why: None when it does not)."""
    head = "game profile: ON (by hand)" if manual else "game profile: ON"
    lines = [head if on else "game profile: off"]
    lines += [
        f"  settings: {line} (sets nothing: 'on' refuses and the watchdog changes nothing until it is fixed)"
        for line in problem_lines
    ]
    if why is not None:
        lines.append(f"  settings: {why} ('on' refuses until it is fixed)")
    return lines


def unreadable_report(paths: GamePaths, state_text: str, intended_text: str, *, judged: bool) -> list[str]:
    """The lines of 'state' that name each line of game.state, and of game.intended when it is judged (not
    a newer format's: its lines may mean something else), that holds no record (unreadable_lines)."""
    files = [(paths.state, state_text, False)]
    if judged:
        files.append((paths.intended, intended_text, True))
    return [
        f"  unreadable: {path} line {number} (skipped): {line}"
        for path, text, intended in files
        for number, line in unreadable_lines(text, intended=intended)
    ]


# The fields of a drift record of game.intended: "prop MGR UNIT PROP VALUE SHOWN", "cgw FILE VALUE SHOWN".
INTENDED_PROP_FIELDS: Final = 6
INTENDED_CGW_FIELDS: Final = 4


def intended_records(text: str) -> list[Record]:
    """The drift records of game.intended (its text), read as the profile reads the file (records: a
    last line cut short and a line bash could not read are left out, fields as bash's eval gives them):
    ("prop", MGR, UNIT, PROP, VALUE, SHOWN) with MGR sys or usr, and ("cgw", FILE, VALUE, SHOWN), any
    further fields dropped. The format line and other kinds are none. The last record of a setting (MGR UNIT
    PROP, or FILE) wins, at its own place: 'on' writes a predicted record before its set-property and a
    second one when systemd then shows another form, and the drift repair one when its set shows another."""
    out: dict[Record, Record] = {}
    for r in records(text):
        match _kind(r):
            case GameRecordKind.PROP if len(r) >= INTENDED_PROP_FIELDS and manager_of(r[1]) is not None:
                rec, key = r[:INTENDED_PROP_FIELDS], r[:4]
            case GameRecordKind.CGW if len(r) >= INTENDED_CGW_FIELDS:
                rec, key = r[:INTENDED_CGW_FIELDS], r[:2]
            case _:
                continue
        out.pop(key, None)
        out[key] = rec
    return list(out.values())


def state_uid(recs: Sequence[Record]) -> str | None:
    """The uid of the user 'on' acted for, from game.state: 'on' records MemoryLow of the system
    manager's user@UID.service (its memory chain) first. None when no such record is there."""
    for r in recs:
        if len(r) >= 3 and r[0] == GameRecordKind.PROP and r[1] == "sys":
            m = re.fullmatch(r"user@([0-9]+)\.service", r[2])
            if m:
                return group(m, 1)
    return None


class UnitType(StrEnum):
    """The unit types of systemd (systemd.unit(5)): the part of a unit name after its last dot."""

    SERVICE = "service"
    SOCKET = "socket"
    DEVICE = "device"
    MOUNT = "mount"
    AUTOMOUNT = "automount"
    SWAP = "swap"
    TARGET = "target"
    PATH = "path"
    TIMER = "timer"
    SLICE = "slice"
    SCOPE = "scope"


UNIT_NAME_MAX: Final = 255  # systemd's UNIT_NAME_MAX (256) less the string's NUL
_UNIT_NAME_CHARS: Final = re.compile(r"[A-Za-z0-9:_.\\@-]+")


def unit_name_valid(name: str) -> bool:
    """A name systemd could give a unit, by the rules of its unit_name_is_valid (any form: plain,
    instance, template): at most 255 characters, all ASCII letters, digits, ':-_.@' or a backslash
    (systemd's escapes, such as x2d for a dash), a prefix that is not empty and does not start with
    '@', and a known type after the last dot. Permissive on purpose: a name it refused would leave a real box unhandled."""
    prefix, dot, suffix = name.rpartition(".")
    return (
        dot == "."
        and prefix != ""
        and not prefix.startswith("@")
        and len(name) <= UNIT_NAME_MAX
        and _UNIT_NAME_CHARS.fullmatch(prefix) is not None
        and enum_member(UnitType, suffix) is not None
    )


def escaped_unit(name: str) -> str | None:
    """The unit of a cgroup folder of the user manager next to the game, or None for one with a
    rule of its own, and for a folder whose name is no unit name (a blank, bytes that are not UTF-8:
    a folder someone made by hand; set-property could never act on it). systemd puts a '_' before a
    cgroup name that could clash with a controller file (cpu.slice -> _cpu.slice)."""
    unit = name.removeprefix("_")
    return None if unit in KNOWN_SLICES or not unit_name_valid(unit) else unit


@dataclass(frozen=True, slots=True)
class Drift:
    """A setting in game.intended that no longer holds.

    record: the intended record ("prop", MGR, UNIT, PROP, VALUE, SHOWN) or ("cgw", FILE, VALUE,
    SHOWN); now: what systemd shows (or the file holds) now.
    """

    record: Record
    now: str

    @property
    def is_cgroup_file(self) -> bool:
        """A cgroup file written directly (cgw), not a unit property."""
        return self.record[0] == GameRecordKind.CGW


def intended_props(recs: Sequence[Record]) -> dict[tuple[Manager, str], list[str]]:
    """{(MGR, UNIT): [PROP, ...]} of the prop records, in file order: one systemctl show each."""
    out: dict[tuple[Manager, str], list[str]] = {}
    for r in recs:
        mgr = manager_of(r[1]) if len(r) >= 4 and r[0] == GameRecordKind.PROP else None
        if mgr is not None:
            out.setdefault((mgr, r[2]), []).append(r[3])
    return out


def drifted(
    recs: Sequence[Record],
    now: Mapping[tuple[str, str], Mapping[str, str]],
    read_file: Callable[[str], str | None],
) -> list[Drift]:
    """The settings of game.intended that no longer hold, in file order.

    now: show_values of each loaded unit (a unit systemd could not be asked about, or has not
    loaded, is missing: nothing to compare). read_file: a cgroup file's text without its last
    newlines, None when it is gone. Skipped too: a record with an empty SHOWN.
    """
    out: list[Drift] = []
    for r in recs:
        match _kind(r):
            case GameRecordKind.PROP:
                found = _prop_drift(r, now)
            case GameRecordKind.CGW:
                found = _cgw_drift(r, read_file)
            case _:
                continue
        if found is not None:
            out.append(found)
    return out


def _prop_drift(r: Record, now: Mapping[tuple[str, str], Mapping[str, str]]) -> Drift | None:
    """A prop record's drift (drifted): what systemd shows now, None when it holds or cannot be compared."""
    if len(r) < INTENDED_PROP_FIELDS:
        return None
    vals = now.get((r[1], r[2]))
    if not r[5] or vals is None:
        return None
    value = vals.get(r[3], "")
    return Drift(r[:INTENDED_PROP_FIELDS], value) if value != r[5] else None


def _cgw_drift(r: Record, read_file: Callable[[str], str | None]) -> Drift | None:
    """A cgw record's drift (drifted): the cgroup file's text now, None when it holds or is gone."""
    if len(r) < INTENDED_CGW_FIELDS or not r[3]:
        return None
    value = read_file(r[1])
    if value is None or value == r[3]:
        return None
    return Drift(r[:INTENDED_CGW_FIELDS], value)


# The fields of a game.state record 'off' undoes: "prop MGR UNIT PROP MODE OLD", "cgw FILE OLD" and
# "epp CPU OLD".
STATE_PROP_FIELDS: Final = 6
STATE_FILE_FIELDS: Final = 3


@dataclass(frozen=True, slots=True)
class CgroupUndo:
    """A cgw record of game.state: 'off' writes old to the cgroup file again."""

    file: str
    old: str


@dataclass(frozen=True, slots=True)
class EppUndo:
    """An epp record of game.state: 'off' gives the CPU its old energy/performance preference."""

    cpu: str
    old: str


@dataclass(frozen=True, slots=True)
class SysfsUndo:
    """A sys record of game.state: 'off' writes old back into the sysfs file."""

    file: str
    old: str


@dataclass(frozen=True, slots=True)
class PropUndo:
    """A prop record of game.state: 'off' removes the override 'on' made (mode new) or sets old again."""

    mgr: Manager
    unit: str
    prop: str
    mode: str
    old: str


type StateUndo = CgroupUndo | EppUndo | PropUndo | SysfsUndo


def state_undo(rec: Sequence[str]) -> StateUndo | None:
    """What 'off' undoes for one record of game.state, None for one it cannot read: cgw, epp and sysfs need
    three fields; any other kind with six fields or more is a prop record (a manager other than usr is
    the system's)."""
    match _kind(rec):
        case GameRecordKind.CGW | GameRecordKind.EPP | GameRecordKind.SYSFS if len(rec) < STATE_FILE_FIELDS:
            return None
        case GameRecordKind.CGW:
            return CgroupUndo(rec[1], rec[2])
        case GameRecordKind.EPP:
            return EppUndo(rec[1], rec[2])
        case GameRecordKind.SYSFS:
            return SysfsUndo(rec[1], rec[2])
        case _ if len(rec) >= STATE_PROP_FIELDS:
            return PropUndo(manager_of(rec[1]) or "sys", rec[2], rec[3], rec[4], rec[5])
        case _:
            return None


def _state_keys(state_recs: Sequence[Record]) -> set[Record]:
    """The settings game.state holds a record of, as 'off' reads its records (state_undo)."""
    keys: set[Record] = set()
    for r in state_recs:
        match state_undo(r):
            case CgroupUndo(file=f):
                keys.add((GameRecordKind.CGW, f))
            case SysfsUndo(file=f):
                keys.add((GameRecordKind.SYSFS, f))
            case PropUndo(mgr=mgr, unit=unit, prop=prop):
                keys.add((GameRecordKind.PROP, mgr, unit, prop))
            case _:
                pass
    return keys


def has_prop_record(text: str, mgr: Manager, unit: str, prop: str) -> bool:
    """game.state (its text) holds a record of unit's prop under manager mgr that 'off' can undo: the
    fields of each record are compared, as 'off' reads them (records(), state_undo), so a line bash
    cannot read (an unclosed quote) or a last line cut short holds none, and a unit name is matched
    as it is, whatever printf %q made of it in the file (a backslash of systemd's \\x2d)."""
    return (GameRecordKind.PROP, mgr, unit, prop) in _state_keys(records(text))


def has_sys_record(text: str, path: str) -> bool:
    """Return True when `game.state` holds a whole `sysfs` undo record for `path`."""
    return (GameRecordKind.SYSFS, path) in _state_keys(records(text))


def held_sys_paths(text: str) -> frozenset[str]:
    """Return all sysfs file paths currently held by readable `sysfs` records in `game.state`."""
    return frozenset(k[1] for k in _state_keys(records(text)) if k[0] == GameRecordKind.SYSFS)


def _drift_key(d: Drift) -> Record:
    r = d.record
    return (GameRecordKind.CGW, r[1]) if d.is_cgroup_file else (GameRecordKind.PROP, r[1], r[2], r[3])


def unheld(drifts: Sequence[Drift], state_recs: Sequence[Record]) -> list[Drift]:
    """The drifts whose setting game.state (its records) holds no record of: 'off' could not undo
    them, so the drift repair does not set them again."""
    keys = _state_keys(state_recs)
    return [d for d in drifts if _drift_key(d) not in keys]


def unheld_text(d: Drift) -> str:
    """The journal line for a drift the tick does not set again (unheld)."""
    r = d.record
    what = r[1] if d.is_cgroup_file else f"{r[2]} {r[3]}"
    return f"drift not repaired: {what}: game.state holds no record of it, so 'off' could not undo it"


def repaired_text(d: Drift, *, ok: bool) -> tuple[int, str]:
    """The journal line (priority, text) for a drift the tick set again (ok) or could not."""
    r = d.record
    if d.is_cgroup_file:
        if ok:
            return 5, f"drift: {r[1]} was '{d.now}', wrote {r[2]} again"
        return 4, f"drift: {r[1]} is '{d.now}', could not write {r[2]} again"
    if ok:
        return 5, (
            f"drift: {r[2]} {r[3]} was '{d.now}' instead of '{r[5]}' (daemon-reload or uresourced?): "
            f"set {r[3]}={r[4]} again"
        )
    return 4, f"drift: {r[2]} {r[3]} is '{d.now}' instead of '{r[5]}': could not set it again"


def again_cmd(*, manual: bool) -> str:
    """The command that switches the profile off and on again in the same mode: 'off' drops the
    by-hand mark, so a restart of the unit alone would let the watchdog switch a by-hand profile
    off at the next tick when no game runs."""
    return "cst cpu game off && cst cpu game on" if manual else "sudo systemctl restart cpu-game.service"


def drift_lines(
    drifts: Sequence[Drift] | None, *, manual: bool, newer: str | None = None, not_held: Sequence[Drift] = ()
) -> list[str]:
    """The drift part of 'state' (drifts None: no game.intended, switched on by an older version; newer:
    why game.intended cannot be compared; not_held: the drifts the watchdog does not set again, unheld)."""
    if newer is not None:
        return [f"  drift: not checked ({newer})"]
    if drifts is None:
        return [
            "  drift: not checked (switched on by an older version, which kept no record of its values;",
            f"         {again_cmd(manual=manual)} switches it off and on again with one)",
        ]
    lines: list[str] = []
    for d in drifts:
        r = d.record
        suffix = " (not set again: game.state holds no record of it)" if d in not_held else ""
        if d.is_cgroup_file:
            lines.append(f"  DRIFT: {r[1]} is '{d.now}', the profile wrote '{r[3]}'{suffix}")
        else:
            lines.append(f"  DRIFT: {r[2]} {r[3]} is '{d.now}', the profile set '{r[5]}'{suffix}")
    if any(d not in not_held for d in drifts):
        lines.append("  (a daemon-reload or uresourced undid these; the watchdog sets them again within 30 s;")
        lines.append(f"   if they stay: {again_cmd(manual=manual)})")
    elif drifts:
        lines.append(f"  (the watchdog does not set these again; {again_cmd(manual=manual)} records them afresh)")
    else:
        lines.append("  drift: none (every setting the profile made still holds)")
    return lines


class BoxProp(StrEnum):
    """The properties the tick sets on the boxes it handles (Docker and Steam scopes, the boxes next to the
    game), which it reads back to judge whether they still hold (setting_holds)."""

    MEMORY_LOW = "MemoryLow"
    ALLOWED_CPUS = "AllowedCPUs"
    CPU_WEIGHT = "CPUWeight"


def escapes_in_state(recs: Sequence[Record]) -> str:
    """The boxes of the user manager the profile made idle, from game.state ("none" if none)."""
    out = [
        r[2]
        for r in recs
        if len(r) >= 4
        and r[0] == GameRecordKind.PROP
        and r[1] == "usr"
        and r[3] == BoxProp.CPU_WEIGHT
        and r[2] != USER_SLICE
    ]
    return " ".join(out) if out else "none"


def first_cpu(cpus: str) -> str:
    """The first CPU of a list such as 12-23 (bash: ${BUILD_CPUS%%-*})."""
    return cpus.split("-", 1)[0]


def dropin_text(unit: str, memory_low: str) -> str:
    """The MemoryLow drop-in 'on' writes for one unit of the memory chain."""
    section = "Service" if unit.endswith(".service") else "Slice"
    return (
        "# Written by cpu-game-profile (CST) while the game profile is on; 'off' removes it.\n"
        f"[{section}]\nMemoryLow={memory_low}\n"
    )


def on_text(s: GameSettings) -> str:
    """The journal line at the end of 'on'."""
    return (
        f"on: VM/Docker on {s.build_cpus}, toolboxes and other user boxes idle, game memory protected "
        f"({s.game_memory_low}), app zswap writeback {s.game_zswap_writeback}, P-cores EPP performance"
    )


def cpuset_in(subtree_control: str) -> bool:
    """cpuset is among the controllers (grep -w cpuset)."""
    return re.search(r"(?<![A-Za-z0-9_])cpuset(?![A-Za-z0-9_])", subtree_control) is not None
