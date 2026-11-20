# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic, value parsing, and rendering for the bluetooth module.

The rules of the LDAC rate watcher (bt-ldac-watch) are in bluetooth.domain.watch.
"""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
import json
import posixpath
import re
from typing import Final, Literal, TypedDict

from cst_core.domain.files import OwnedState, effective_lines
from cst_core.domain.shapes import (
    ShapeError,
    first_item,
    has_item,
    items_of,
    json_object,
    object_of,
    object_or_empty,
    parse_json,
)
from cst_core.domain.text import enum_member, parse_shell_assignments, SettingsProblem, group, split
from cst_core.domain.values import Json

# The kernel's experimental "ISO socket" feature: needed for LE Audio
# (connected isochronous streams, LC3). Naming only this UUID, rather than
# "true", leaves out the other experimental kernel features (codec offload,
# Bluetooth Quality Report) that "true" would also switch on.
ISO_UUID: Final[str] = "6fbaf188-05e0-496a-9885-d6ddfdb4e03e"
KEYS: Final[tuple[str, str]] = ("Experimental", "KernelExperimental")
WANTED: Final[dict[str, str]] = {"Experimental": "true", "KernelExperimental": ISO_UUID}

MARKER: Final[str] = "# managed-by: bluetooth-setup.sh (CST)"
# What the standalone script this module replaces wrote into its file.
OLD_MARKERS: Final[tuple[str, ...]] = ("Gerado por apply-le-audio.sh",)
QUALITIES: Final[tuple[str, ...]] = ("auto", "hq", "sq", "mq")
# PipeWire's numeric LDAC quality on the node (Props) -> the rule's word.
QUALITY_NUM: Final[dict[int, str]] = {-1: "auto", 0: "hq", 1: "sq", 2: "mq"}
SETTING_KEYS: Final[tuple[str, ...]] = ("DEVICE", "LDAC_QUALITY", "BEARER", "LDAC_RATE", "LE_AUDIO")
# With LE_AUDIO=off PipeWire registers only these Bluetooth roles with BlueZ:
# the default list of PipeWire 1.6 (man pipewire-props, bluez5.roles) without
# the LE Audio ones (bap_sink, bap_source, bap_bcast_sink, bap_bcast_source).
CLASSIC_ROLES: Final[tuple[str, ...]] = ("a2dp_sink", "a2dp_source", "hfp_hf", "hfp_ag")
LE_CONF_NAME: Final[str] = "90-cst-le-audio-off.conf"
LE_SECTION: Final[str] = "monitor.bluez.properties"
# The LDAC rate watcher: its user unit, and where apply installs the program
# (unit-relative: %h is the home folder).
LDAC_UNIT: Final[str] = "cst-bt-ldac48.service"
LDAC_PROG: Final[str] = "%h/.local/lib/cst/bt-ldac-watch"
# The two services apply restarts: BlueZ (system) and WirePlumber (the user's session).
BLUETOOTH_UNIT: Final[str] = "bluetooth.service"
WIREPLUMBER_UNIT: Final[str] = "wireplumber.service"

# A section header of a WirePlumber config file: `monitor.bluez.properties = {`,
# also quoted, with `:` or without a separator, with or without `override.`.
SECTION_RE: Final[re.Pattern[str]] = re.compile(r'^\s*"?(override\.)?' + re.escape(LE_SECTION) + r'"?\s*[=:]?\s*(\{|$)')

# journalctl -o short-iso: "TIME HOST IDENT[PID]: MESSAGE". The watcher's
# outcome lines start with "fixed:" or "failed:"; its other lines with a word
# and a colon ("act:", "wait:", "none:", "cooldown:"); a line without such a
# word (from its bounce) says what went wrong.
JOURNAL_LINE: Final[re.Pattern[str]] = re.compile(r"^(\S+) \S+ ([^:\s]+): (.*)$")
KEYWORD: Final[re.Pattern[str]] = re.compile(r"^[a-z]+: ")
SETTINGS_VALUE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:-]*")


# ---- /etc/bluetooth/main.conf -------------------------------------------------------------
# bluetoothd reads it with GLib's key file parser: a [General] header that
# shows up again continues the same group, the last line of a key in the group
# wins, and '#' starts a comment line. Only [General] is touched; everything
# else, comments included, is kept as it is.
def split_lines(text: str) -> tuple[list[str], str, bool]:
    """(lines, newline, final) of TEXT: the lines without the empty string
    after the last newline, and whether TEXT ended with a newline."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = split(r"\r?\n", text)
    final = lines[-1] == ""
    if final:
        lines.pop()
    return lines, nl, final


def join_lines(lines: Sequence[str], nl: str, final: bool = True) -> str:
    """Rejoin lines with `nl`, keeping trailing newline iff `final`."""
    if not lines:
        return ""
    return nl.join(lines) + (nl if final else "")


def general_ranges(lines: Sequence[str]) -> list[tuple[int, int]]:
    """[(start, end), ...] of every [General] block, in file order: START is
    the header's index, END the index of the next section header (or
    len(lines)). Empty without one."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if start is not None:
                out.append((start, i))
                start = None
            if s == "[General]":
                start = i
    if start is not None:
        out.append((start, len(lines)))
    return out


def key_pattern(key: str) -> re.Pattern[str]:
    """Matches `KEY = value` and `#KEY = value`; group 1 is the '#'s, group 2 the value."""
    return re.compile(r"^\s*(#*)\s*" + re.escape(key) + r"\s*=\s*(.*?)\s*$")


def key_lines(lines: Sequence[str], ranges: Sequence[tuple[int, int]], key: str) -> tuple[list[int], list[int]]:
    """(active, commented) line indexes of KEY in all the [General] blocks."""
    pat = key_pattern(key)
    active: list[int] = []
    commented: list[int] = []
    for start, end in ranges:
        for i in range(start + 1, end):
            m = pat.match(lines[i])
            if m:
                (commented if m.group(1) else active).append(i)
    return active, commented


def satisfied(key: str, value: str | None) -> bool:
    """Whether VALUE already does what we want. A KernelExperimental list
    that contains the ISO UUID among others is fine as it is."""
    if value is None:
        return False
    v = value.strip()
    if key == "KernelExperimental":
        return ISO_UUID in v.lower()
    return v == "true"


def current_values(text: str) -> dict[str, str | None]:
    """{key: value of the line that counts, or None} for KEYS in [General]."""
    lines = split_lines(text)[0]
    ranges = general_ranges(lines)
    out: dict[str, str | None] = {}
    for key in KEYS:
        out[key] = None
        active, _ = key_lines(lines, ranges, key)
        if active:
            m = key_pattern(key).match(lines[active[-1]])
            if m is not None:
                out[key] = m.group(2)
    return out


def _already_set(lines: Sequence[str], active: Sequence[int], key: str) -> bool:
    """Whether the line of KEY that counts (the last of ACTIVE) already has a value we accept."""
    if not active:
        return False
    m = key_pattern(key).match(lines[active[-1]])
    return m is not None and satisfied(key, m.group(2))


def _comment_out(lines: list[str], idxs: Iterable[int]) -> None:
    """Comment out the lines at IDXS, in place."""
    for j in idxs:
        lines[j] = "#" + lines[j].lstrip()


def edit_general(text: str) -> tuple[str, bool]:
    """(new text, changed). The line of a key that counts (the last active
    one, in any [General] block) gets our value (an old
    `KernelExperimental = true` becomes the ISO UUID); earlier active lines
    of the same key are commented out; without an active line, the last
    commented one (the vendor's `#Key = false` example) is replaced in place;
    without any, the key goes right under the first [General]. No [General]
    at all: one is put at the top."""
    lines, nl, final = split_lines(text)
    if not general_ranges(lines):
        lines = ["[General]"] + [f"{k} = {v}" for k, v in WANTED.items()] + [""] + lines
        return join_lines(lines, nl, final or not text), True
    changed = False
    for key, val in WANTED.items():
        ranges = general_ranges(lines)  # again: an insert moves the lines
        active, commented = key_lines(lines, ranges, key)
        if _already_set(lines, active, key):
            continue
        changed = True
        if active:
            lines[active[-1]] = f"{key} = {val}"
            _comment_out(lines, active[:-1])
        elif commented:
            lines[commented[-1]] = f"{key} = {val}"
        else:
            lines.insert(ranges[0][0] + 1, f"{key} = {val}")
    return join_lines(lines, nl, final), changed


def _backup_line(blines: Sequence[str], branges: Sequence[tuple[int, int]], key: str) -> str | None:
    """The backup's line for KEY: its active line, else its commented example; None without either."""
    bact, bcom = key_lines(blines, branges, key)
    return blines[(bact or bcom)[-1]] if bact or bcom else None


def _is_active_line(key: str, line: str) -> bool:
    """LINE sets KEY and is not commented out."""
    m = key_pattern(key).match(line)
    return m is not None and not m.group(1)


def revert_general(text: str, backup: str) -> tuple[str, bool]:
    """(new text, changed): the two keys back to what BACKUP had, everything
    else in TEXT kept (later edits by hand survive). The backup's line for a
    key is its active line, else its commented example; that line replaces
    the one that counts now. A key the backup did not have at all is
    commented out (bluetoothd's default, false). CHANGED compares lines, so
    a missing newline at the end of the file alone is no change."""
    lines, nl, final = split_lines(text)
    before = list(lines)
    blines = split_lines(backup)[0]
    if not general_ranges(lines):
        return text, False
    branges = general_ranges(blines)
    for key in KEYS:
        want = _backup_line(blines, branges, key)
        ranges = general_ranges(lines)
        active, _ = key_lines(lines, ranges, key)
        if want is None:
            _comment_out(lines, active)
        elif active:
            lines[active[-1]] = want
            _comment_out(lines, active[:-1])
        elif _is_active_line(key, want):
            # The backup had it on, the file has no active line now.
            lines.insert(ranges[0][0] + 1, want)
    if lines == before:
        return text, False
    return join_lines(lines, nl, final), True


# ---- the WirePlumber rule -----------------------------------------------------------------
def device_id(mac: str) -> str:
    """Convert a Bluetooth MAC address into PipeWire's uppercase underscore card suffix."""
    return mac.upper().replace(":", "_")


def render_wp(mac: str, quality: str) -> str:
    """The rule file ~/.config/wireplumber/wireplumber.conf.d/50-bluez-<ID>.conf."""
    mac = mac.upper()
    return f"""\
{MARKER}: change the settings with `cst bluetooth apply`, not in this file.
# Headset {mac}: LDAC quality "{quality}" when it is connected over A2DP.
# "auto" = adaptive bitrate (990, 660 or 330 kbps, lower when the radio loses
# packets); a fixed "hq" (990 kbps) made the JBL Live Beam 4 stall and play in bursts.
# LE Audio (BAP/LC3) comes from BlueZ (Experimental/KernelExperimental in
# /etc/bluetooth/main.conf); the bap-* profiles show up when the headset connects over LE.
monitor.bluez.rules = [
  {{
    matches = [ {{ device.name = "bluez_card.{device_id(mac)}" }} ]
    actions = {{
      update-props = {{
        bluez5.a2dp.ldac.quality = "{quality}"
      }}
    }}
  }}
]
"""


def effective(text: str) -> list[str]:
    """The lines WirePlumber reads (no comments, no blank lines, no trailing blanks)."""
    return [line.rstrip() for line in effective_lines(text)]


# The states of a whole file CST writes (wp_state, unit_state): the five OwnedState words WpState
# and LeState copy, so WpState(state) and LeState(state) always name a member.
type WholeFileState = Literal[
    OwnedState.OK, OwnedState.ADOPT, OwnedState.CHANGE, OwnedState.MISSING, OwnedState.CONFLICT
]


def wp_state(existing: str | None, rendered: str) -> WholeFileState:
    """ok (equal), adopt (only comments differ: apply renews them, no
    restart), change (ours or the old script's, settings differ), conflict
    (a file nobody of ours wrote, with other settings: apply refuses),
    missing (EXISTING is None)."""
    if existing is None:
        return OwnedState.MISSING
    if existing == rendered:
        return OwnedState.OK
    if effective(existing) == effective(rendered):
        return OwnedState.ADOPT
    if MARKER in existing or any(m in existing for m in OLD_MARKERS):
        return OwnedState.CHANGE
    return OwnedState.CONFLICT


# ---- the LDAC rate watcher's user unit ------------------------------------------------------
def render_unit(mac: str, rate: str | int) -> str:
    """~/.config/systemd/user/cst-bt-ldac48.service."""
    mac = mac.upper()
    return f"""\
{MARKER}: change the settings with `cst bluetooth apply`, not in this file.
# Puts the LDAC stream of headset {mac} back to {rate} Hz when the headset
# itself configured another rate (96000 Hz) on reconnecting: one SBC <-> LDAC
# profile switch, the link stays up. Why and how: bluetooth/README.md in CST.
[Unit]
Description=CST: LDAC of headset {mac} at {rate} Hz
After=pipewire.service pipewire-pulse.service wireplumber.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {LDAC_PROG} --device {mac} --rate {rate}
Restart=on-failure
RestartSec=30
# A stop during a profile switch waits for the switch back to LDAC (up to
# about 20 s): the stop signal goes to the program only, not to its pactl.
KillMode=mixed
TimeoutStopSec=60
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""


def unit_state(existing: str | None, rendered: str) -> WholeFileState:
    """ok, change (ours, differs), conflict (not ours), missing."""
    if existing is None:
        return OwnedState.MISSING
    if existing == rendered:
        return OwnedState.OK
    return OwnedState.CHANGE if MARKER in existing else OwnedState.CONFLICT


# ---- LE Audio off: the WirePlumber drop-in --------------------------------------------------
def render_le_off() -> str:
    """~/.config/wireplumber/wireplumber.conf.d/90-cst-le-audio-off.conf.
    The `override.` goes on the SECTION, with a plain key inside. WirePlumber
    0.5 (checked on 0.5.19 with its own libwireplumber) strips an `override.`
    prefix only while it merges into a section that an earlier file already
    defines; with no such file, `override.bluez5.roles` inside a plain section
    reaches the bluez5 monitor under that literal name, is ignored, and LE
    Audio stays on. `override.monitor.bluez.properties` always yields a plain
    `bluez5.roles`, and replaces the list instead of joining it with the
    lists of earlier files. The price: other monitor.bluez.properties keys
    of earlier files are dropped too (plan and status name such files)."""
    roles = " ".join(CLASSIC_ROLES)
    return f"""\
{MARKER}: change the settings with `cst bluetooth apply`, not in this file.
# LE Audio off (LE_AUDIO=off, the CST default). For ALL Bluetooth devices:
# PipeWire offers BlueZ only the classic roles, so headsets play over A2DP
# (LDAC, AAC, SBC) and HFP, never over LE Audio (no bap-* profiles).
# Why: the JBL Live Beam 4 connected over BR/EDR and LE at once, PipeWire
# picked LE (bap-duplex) and the audio dropped in a loop; LE stereo to
# true-wireless earbuds does not work on this Intel BE200 yet (upstream BlueZ
# is working on it). Turn LE Audio on: `cst bluetooth le-audio on`
# (or LE_AUDIO=on in ~/.config/cst/bluetooth.conf, then `cst bluetooth apply`).
# "override." replaces the whole section of earlier files. Details:
# bluetooth/README.md in CST, "LE Audio: off by default".
override.{LE_SECTION} = {{
  bluez5.roles = [ {roles} ]
}}
"""


# ---- LE Audio off: what WirePlumber really reads ------------------------------------------
def defines_section(text: str, section: str = LE_SECTION) -> bool:
    """Whether TEXT (a WirePlumber config file) defines monitor.bluez.properties."""
    pat = (
        SECTION_RE
        if section == LE_SECTION
        else re.compile(r'^\s*"?(override\.)?' + re.escape(section) + r'"?\s*[=:]?\s*(\{|$)')
    )
    return any(pat.match(line) for line in effective(text))


def classify_other_fragments(
    ours: str, dir_files: Sequence[tuple[str, Sequence[tuple[str, str]]]]
) -> list[tuple[str, str]]:
    """[(before|after, path)] of WirePlumber fragments other than OURS that define
    monitor.bluez.properties, in the order WirePlumber reads them: by file name,
    over DIR_FILES (highest priority first: a name found earlier hides the same
    name in later entries)."""
    found: dict[str, tuple[str, str]] = {}
    for directory, entries in dir_files:
        for name, text in entries:
            if name.endswith(".conf") and name not in found:
                found[name] = (posixpath.join(directory, name), text)
    out: list[tuple[str, str]] = []
    for name in sorted(found):
        if name == ours:
            continue
        path, text = found[name]
        if defines_section(text):
            out.append(("before" if name < ours else "after", path))
    return out


class RolesState(StrEnum):
    """What bluez5.roles gives BlueZ (bt-helper wp-roles prints it before the |)."""

    CLASSIC = "classic"  # only the classic roles (A2DP, HFP)
    LE = "le"  # the LE Audio roles too
    DEFAULT = "default"  # not set: PipeWire's own default, which has the LE Audio roles


def roles_state(value: str | None) -> RolesState:
    """classic | le | default for the effective bluez5.roles VALUE (None =
    not set: PipeWire's own default, which has the LE Audio roles)."""
    if value is None:
        return RolesState.DEFAULT
    roles = re.findall(r"\w+", value, flags=re.ASCII)
    return RolesState.LE if any(r.startswith("bap_") for r in roles) else RolesState.CLASSIC


def parse_roles_state(word: str) -> RolesState | None:
    """The RolesState bt-helper wp-roles printed, or None for any other word."""
    return enum_member(RolesState, word)


# ---- settings -------------------------------------------------------------------------------
def parse_settings(text: str) -> dict[str, str]:
    """{KEY: value} of the known keys, through cst_core's one settings reader
    (a file only Python reads: blanks around =, '#' comments also after a
    value, quotes dropped); unknown keys are ignored. A line that is not an
    assignment sets nothing (settings_problems lists them)."""
    return {k: v for k, v in parse_shell_assignments(text, bash_sourced=False).values.items() if k in SETTING_KEYS}


def settings_problems(text: str) -> tuple[SettingsProblem, ...]:
    """The lines of a settings file that are not plain assignments."""
    return parse_shell_assignments(text, bash_sourced=False).problems


_LINE_KEY: Final = re.compile(r"\s*(?:export[ \t]+)?([A-Za-z_](?a:\w*))\s*=")  # (?a:): \w in ASCII only


def line_key(line: str) -> str | None:
    """The key one line sets, or tries to: also `export KEY=...` and a problem line
    (`KEY=$X`, which sets nothing), so settings_set replaces it; None for a
    comment or any other line."""
    m = _LINE_KEY.match(line)
    return group(m, 1) if m else None


def set_keys(text: str) -> list[str]:
    """The known keys TEXT sets, in file order, each once."""
    return list(dict.fromkeys(parse_settings_order(text)))


def parse_settings_order(text: str) -> Iterator[str]:
    """Yield known setting keys in the order they appear in TEXT."""
    for a in parse_shell_assignments(text, bash_sourced=False).entries:
        if a.key in SETTING_KEYS:
            yield a.key


def settings_set(text: str | None, key: str, value: str) -> tuple[str, bool]:
    """(new text, changed): KEY=VALUE in a settings file's TEXT (None = the
    file does not exist yet). The line that counts (the last active one) is
    replaced in place, earlier active lines of KEY are commented out; without
    one the line is appended, on a line of its own even when TEXT does not
    end with a newline. Everything else is kept."""
    want = f"{key}={value}"
    if text is None:
        return f"# CST bluetooth settings (override bluetooth/bluetooth.conf in the repo)\n{want}\n", True
    lines, nl, final = split_lines(text)
    idx = [i for i, line in enumerate(lines) if line_key(line) == key]
    before = list(lines)
    if idx:
        if parse_settings(lines[idx[-1]]).get(key) != value:
            lines[idx[-1]] = want
        for i in idx[:-1]:
            lines[i] = "# " + lines[i].lstrip()
    else:
        lines.append(want)
        final = True
    if lines == before:
        return text, False
    return join_lines(lines, nl, final), True


def merged_settings(texts: Iterable[str]) -> dict[str, str]:
    """Merge settings texts in priority order (later entries win)."""
    out = dict.fromkeys(SETTING_KEYS, "")
    for t in texts:
        out.update(parse_settings(t))
    return out


def settings_report(files: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """What `bt-helper settings` prints for (path, text) files in priority
    order: (KEY=value lines of the merged settings, for stdout; one warning
    per line that is not an assignment, for stderr)."""
    shown = "".join(f"{k}={v}\n" for k, v in merged_settings(t for _, t in files).items())
    warnings = "".join(
        f"warning: {path} {p} (the line is ignored)\n" for path, t in files for p in settings_problems(t)
    )
    return shown, warnings


# ---- live state (parsed from the output of busctl, pactl and pw-dump) ----------------------
# PipeWire's names of the headset's card and its input nodes (the prefix, then DEV), and the
# property of an output node that names its codec.
CARD_PREFIX: Final = "bluez_card."
INPUT_PREFIX: Final = "bluez_input."
CODEC_KEY: Final = "api.bluez5.codec"
# The parts of their JSON the rules read. total=False: the tools give more keys and may leave one
# out. Nothing checks these shapes at run time; the functions keep their own defaults and
# isinstance tests where a part can be missing or of another type.
PwProps = TypedDict(
    "PwProps",
    {
        "object.serial": int | str,
        "device.name": str,
        "node.name": str,
        "api.bluez5.codec": str,
        "api.bluez5.profile": str,
        "bluez5.a2dp.ldac.quality": str,
    },
    total=False,
)


class PwFormat(TypedDict, total=False):
    """One entry of a node's Format or EnumFormat param."""

    mediaType: str
    format: Json  # a name, or {"default": name} in EnumFormat
    rate: Json  # a number, or a choice ({"default": n, "min": ..., "max": ...}) in EnumFormat
    channels: int


class PwProfileParam(TypedDict, total=False):
    """One entry of a device's Profile param."""

    index: int
    name: str


class PwPropsParam(TypedDict, total=False):
    """One entry of a node's Props param (LDAC's quality: a number, or a word)."""

    volume: float
    quality: int | str


class PwParams(TypedDict, total=False):
    """The params of a pw-dump object that the rules read."""

    Profile: list[PwProfileParam]
    Props: list[PwPropsParam]
    Format: list[PwFormat]
    EnumFormat: list[PwFormat]


class PwInfo(TypedDict, total=False):
    """The info of a pw-dump object."""

    state: str
    props: PwProps
    params: PwParams


class PwObject(TypedDict, total=False):
    """One object of `pw-dump`."""

    id: int
    type: str
    info: PwInfo


class PactlProfile(TypedDict, total=False):
    """One profile of a card in `pactl -f json list cards`."""

    description: str
    available: bool


class PactlCard(TypedDict, total=False):
    """One card of `pactl -f json list cards`."""

    name: str
    index: int
    active_profile: str
    profiles: dict[str, PactlProfile]


class PactlSource(TypedDict, total=False):
    """One source of `pactl -f json list short sources`."""

    index: int
    name: str


class PactlSourceOutput(TypedDict, total=False):
    """One source output of `pactl -f json list source-outputs`."""

    source: int


class BusctlObjects(TypedDict, total=False):
    """`busctl -j call ... GetManagedObjects`: data holds one map of object path -> interfaces."""

    type: str
    data: list[dict[str, dict[str, Json]]]


def adapter_path(objects: object) -> str | None:
    """The first object path with org.bluez.Adapter1 in `busctl -j ... GetManagedObjects`'s
    JSON (BusctlObjects), or None. ShapeError for JSON of another shape."""
    data = object_of(objects).get("data") or [{}]
    for path, ifaces in sorted(object_of(first_item(data)).items()):
        if has_item(ifaces, "org.bluez.Adapter1"):
            return str(path)
    return None


# The pactl outputs card_state reads: cards, short sources, source outputs.
PACTL_OUTPUTS: Final = 3


def card_state_of(dev: str, outputs: object) -> tuple[str, bool, bool] | None:
    """card_state of the three pactl outputs in one JSON value (`[cards, sources, outputs]`)."""
    parts = items_of(outputs)
    if len(parts) != PACTL_OUTPUTS:  # card_state(dev, *outputs) raised TypeError
        raise ShapeError(f"{len(parts)} pactl outputs, not {PACTL_OUTPUTS}")
    return card_state(dev, parts[0], parts[1], parts[2])


def card_state(dev: str, cards: object, srcs: object, outs: object) -> tuple[str, bool, bool] | None:
    """(active profile, a2dp-sink offered, headset mic in use), or None when the headset has no
    card in PipeWire. CARDS, SRCS and OUTS: the JSON of `pactl -f json list cards` (PactlCard),
    `list short sources` (PactlSource) and `list source-outputs` (PactlSourceOutput);
    ShapeError where a part this reads is of another shape."""
    card = next((c for c in map(object_of, items_of(cards)) if c.get("name") == CARD_PREFIX + dev), None)
    if card is None:
        return None
    a2dp = bool(object_of(object_of(card.get("profiles", {})).get("a2dp-sink", {})).get("available", False))
    bt_src = {
        _hashable(s.get("index"))
        for s in map(object_of, items_of(srcs))
        if str(s.get("name", "")).startswith(INPUT_PREFIX + dev)
    }
    mic = any(_hashable(o.get("source")) in bt_src for o in map(object_of, items_of(outs)))
    return str(card.get("active_profile") or ""), a2dp, mic


def _hashable(value: object) -> object:
    """value, a member of a set; ShapeError for a JSON list or object (unhashable)."""
    if isinstance(value, list | dict):
        raise ShapeError(f"unhashable: {type(value).__name__}")
    return value


def _device_profiles(params: object, raw_rule: object) -> tuple[str, str | None]:
    """(the card's profiles, the LDAC rule's quality) of the headset's Device object (pw_state)."""
    prof = " ".join(str(object_of(pr).get("name", "?")) for pr in items_of(object_of(params).get("Profile", []) or []))
    return prof, None if raw_rule is None else str(raw_rule)


def _node_parts(codec: object, info: object, params: object, quality: str | None) -> tuple[str, str | None, str | None]:
    """(codec, rate, quality) of the headset's output Node (pw_state); QUALITY, an earlier node's,
    stays when its Props name none."""
    rate = node_rate(info)
    for pr in items_of(object_of(params).get("Props", []) or []):
        if has_item(pr, "quality"):
            q = object_of(pr)["quality"]
            quality = QUALITY_NUM.get(q, str(q)) if isinstance(q, int) else str(q)
    return str(codec), None if rate is None else str(rate), quality


def pw_state(dev: str, objs: object) -> tuple[str | None, str | None, str | None, str | None, str | None] | None:
    """(profiles, rule quality, node codec, node quality, node rate) of the headset in pw-dump's
    JSON (a list of PwObject), or None when it is not in PipeWire. Missing parts are None;
    ShapeError where a part this reads is of another shape."""
    prof: str | None = None
    rule: str | None = None
    codec: str | None = None
    quality: str | None = None
    rate: str | None = None
    found = False
    for o in map(object_of, items_of(objs)):
        info = object_or_empty(o.get("info"))
        kind = str(o.get("type", ""))
        # Read only where the object's type needs them: another type's props and params are
        # never looked into.
        p = object_of(info.get("props") or {}) if kind.endswith(("Device", "Node")) else {}
        params: object = info.get("params") or {}
        if kind.endswith("Device") and p.get("device.name") == CARD_PREFIX + dev:
            found = True
            prof, rule = _device_profiles(params, p.get("bluez5.a2dp.ldac.quality"))
        if (
            kind.endswith("Node")
            and str(p.get("node.name", "")).startswith("bluez_output." + dev)
            and p.get(CODEC_KEY, "")
        ):
            codec, rate, quality = _node_parts(p[CODEC_KEY], info, params, quality)
    if not found:
        return None
    return prof or None, rule, codec, quality, rate


def node_rate(info: object) -> int | None:
    """The node's negotiated Format rate, else the rate its EnumFormat offers
    (a suspended node has an empty Format). INFO: a pw-dump object's info (PwInfo);
    ShapeError where a part this reads is of another shape."""
    params = object_or_empty(object_of(info).get("params"))
    for key in ("Format", "EnumFormat"):
        for fmt in items_of(params.get(key) or []):
            rate = fmt.get("rate") if isinstance(fmt, dict) else None
            if isinstance(rate, int):
                return rate
    return None


# ---- the LDAC rate watcher's journal --------------------------------------------------------
def lw_failure(text: str) -> tuple[str, str, str] | None:
    """(time, reason, detail) of the watcher's last outcome when that was a
    failure, else None. DETAIL is the line of the same process just before
    it that is not a keyword line ("" if none)."""
    last: tuple[str, str, str] | None = None
    prev_detail: dict[str, str] = {}
    for line in text.splitlines():
        m = JOURNAL_LINE.match(line)
        if not m:
            continue
        when, ident, msg = group(m, 1), group(m, 2), group(m, 3)
        if msg.startswith("fixed: "):
            last = None
        elif msg.startswith("failed: "):
            last = (when, msg[len("failed: ") :], prev_detail.get(ident, ""))
        elif msg.startswith("act: "):
            prev_detail[ident] = ""
        elif not KEYWORD.match(msg) and ident.startswith("python3"):
            prev_detail[ident] = msg
    return last


# ---- restarts skipped (--no-restart, a cancelled question, a failed restart) ----------------
# The units whose restart apply may owe, as bt-helper restart-pending takes their invocation IDs.
RESTART_UNITS: Final[tuple[str, str]] = (BLUETOOTH_UNIT, WIREPLUMBER_UNIT)


def parse_pending(text: str | None) -> dict[str, str]:
    """unit -> invocation ID from the pending-restart file (JSON); {} when absent or unreadable,
    and only the known units with a non-empty ID."""
    try:
        data = parse_json(text or "{}")
    except ValueError:
        return {}
    if not json_object(data):
        return {}
    return {u: i for u, i in data.items() if u in RESTART_UNITS and isinstance(i, str) and i}


def still_pending(recorded: Mapping[str, str], current: Mapping[str, str], skipped: Iterable[str]) -> dict[str, str]:
    """The restarts still owed: a recorded one while its unit runs with the invocation it had
    when the restart was skipped (a restart, a stop or a reboot gives another one, or none),
    and each skipped unit that runs now. A unit that does not run reads the new files when
    it starts: nothing is owed for it."""
    owed = {u: i for u, i in recorded.items() if current.get(u) == i}
    owed.update({u: current[u] for u in skipped if u in RESTART_UNITS and current.get(u, "")})
    return owed


def render_pending(owed: Mapping[str, str]) -> str:
    """The pending-restart file's text ('' when nothing is owed: the file goes)."""
    return json.dumps(dict(sorted(owed.items())), indent=1) + "\n" if owed else ""
