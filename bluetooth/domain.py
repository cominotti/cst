# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic, value parsing, and rendering for the bluetooth module."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
import posixpath
import re
from typing import Any, Final

from cst_core.domain.files import effective_lines
from cst_core.domain.text import SettingsProblem, parse_shell_assignments

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
    lines = re.split(r"\r?\n", text)
    final = lines[-1] == ""
    if final:
        lines.pop()
    return lines, nl, final


def join_lines(lines: Sequence[str], nl: str, final: bool = True) -> str:
    """Rejoin lines with `nl`, keeping trailing newline iff `final`."""
    return nl.join(lines) + (nl if final else "") if lines else ""


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
    out: dict[str, str | None] = {k: None for k in KEYS}
    for key in KEYS:
        active, _ = key_lines(lines, ranges, key)
        if active:
            m = key_pattern(key).match(lines[active[-1]])
            if m is not None:
                out[key] = m.group(2)
    return out


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
        if active:
            m = key_pattern(key).match(lines[active[-1]])
            if m is not None and satisfied(key, m.group(2)):
                continue
        changed = True
        if active:
            lines[active[-1]] = f"{key} = {val}"
            for j in active[:-1]:
                lines[j] = "#" + lines[j].lstrip()
        elif commented:
            lines[commented[-1]] = f"{key} = {val}"
        else:
            lines.insert(ranges[0][0] + 1, f"{key} = {val}")
    return join_lines(lines, nl, final), changed


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
        want: str | None = None
        bact, bcom = key_lines(blines, branges, key)
        if bact or bcom:
            want = blines[(bact or bcom)[-1]]
        ranges = general_ranges(lines)
        active, _ = key_lines(lines, ranges, key)
        if want is None:
            for j in active:
                lines[j] = "#" + lines[j].lstrip()
        elif active:
            lines[active[-1]] = want
            for j in active[:-1]:
                lines[j] = "#" + lines[j].lstrip()
        else:
            m = key_pattern(key).match(want)
            if m is not None and not m.group(1):
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


def wp_state(existing: str | None, rendered: str) -> str:
    """ok (equal), adopt (only comments differ: apply renews them, no
    restart), change (ours or the old script's, settings differ), conflict
    (a file nobody of ours wrote, with other settings: apply refuses),
    missing (EXISTING is None)."""
    if existing is None:
        return "missing"
    if existing == rendered:
        return "ok"
    if effective(existing) == effective(rendered):
        return "adopt"
    if MARKER in existing or any(m in existing for m in OLD_MARKERS):
        return "change"
    return "conflict"


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


def unit_state(existing: str | None, rendered: str) -> str:
    """ok, change (ours, differs), conflict (not ours), missing."""
    if existing is None:
        return "missing"
    if existing == rendered:
        return "ok"
    return "change" if MARKER in existing else "conflict"


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


def roles_state(value: str | None) -> str:
    """classic | le | default for the effective bluez5.roles VALUE (None =
    not set: PipeWire's own default, which has the LE Audio roles)."""
    if value is None:
        return "default"
    roles = re.findall(r"[A-Za-z0-9_]+", value)
    return "le" if any(r.startswith("bap_") for r in roles) else "classic"


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


_LINE_KEY: Final = re.compile(r"\s*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def line_key(line: str) -> str | None:
    """The key one line sets, or tries to: also `export KEY=...` and a problem line
    (`KEY=$X`, which sets nothing), so settings_set replaces it; None for a
    comment or any other line."""
    m = _LINE_KEY.match(line)
    return m.group(1) if m else None


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
    out = {k: "" for k in SETTING_KEYS}
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
def adapter_path(objects: Mapping[str, Any]) -> str | None:
    """The first object path with org.bluez.Adapter1, or None."""
    data = objects.get("data") or [{}]
    for path, ifaces in sorted(data[0].items()):
        if "org.bluez.Adapter1" in ifaces:
            return str(path)
    return None


def card_state(
    dev: str,
    cards: Sequence[Mapping[str, Any]],
    srcs: Sequence[Mapping[str, Any]],
    outs: Sequence[Mapping[str, Any]],
) -> tuple[str, bool, bool] | None:
    """(active profile, a2dp-sink offered, headset mic in use), or None when
    the headset has no card in PipeWire."""
    card = next((c for c in cards if c.get("name") == "bluez_card." + dev), None)
    if card is None:
        return None
    a2dp = bool(card.get("profiles", {}).get("a2dp-sink", {}).get("available", False))
    bt_src = {s.get("index") for s in srcs if str(s.get("name", "")).startswith("bluez_input." + dev)}
    mic = any(o.get("source") in bt_src for o in outs)
    return str(card.get("active_profile") or ""), a2dp, mic


def pw_state(
    dev: str, objs: Sequence[Mapping[str, Any]]
) -> tuple[str | None, str | None, str | None, str | None, str | None] | None:
    """(profiles, rule quality, node codec, node quality, node rate) of the headset, or
    None when it is not in PipeWire. Missing parts are None."""
    prof: str | None = None
    rule: str | None = None
    codec: str | None = None
    quality: str | None = None
    rate: int | None = None
    found = False
    for o in objs:
        info = o.get("info") or {}
        p = info.get("props") or {}
        params = info.get("params") or {}
        kind = str(o.get("type", ""))
        if kind.endswith("Device") and p.get("device.name") == "bluez_card." + dev:
            found = True
            prof = " ".join(str(pr.get("name", "?")) for pr in params.get("Profile", []) or [])
            raw_rule = p.get("bluez5.a2dp.ldac.quality")
            rule = None if raw_rule is None else str(raw_rule)
        if (
            kind.endswith("Node")
            and str(p.get("node.name", "")).startswith("bluez_output." + dev)
            and p.get("api.bluez5.codec")
        ):
            codec = str(p["api.bluez5.codec"])
            rate = node_rate(info)
            for pr in params.get("Props", []) or []:
                if "quality" in pr:
                    q = pr["quality"]
                    quality = QUALITY_NUM.get(q, str(q)) if isinstance(q, int) else str(q)
    if not found:
        return None
    return prof or None, rule, codec, quality, None if rate is None else str(rate)


def node_rate(info: Mapping[str, Any]) -> int | None:
    """The node's negotiated Format rate, else the rate its EnumFormat offers
    (a suspended node has an empty Format)."""
    params = info.get("params") or {}
    for key in ("Format", "EnumFormat"):
        for fmt in params.get(key) or []:
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
        when, ident, msg = m.groups()
        if msg.startswith("fixed: "):
            last = None
        elif msg.startswith("failed: "):
            last = (when, msg[len("failed: ") :], prev_detail.get(ident, ""))
        elif msg.startswith("act: "):
            prev_detail[ident] = ""
        elif not KEYWORD.match(msg) and ident.startswith("python3"):
            prev_detail[ident] = msg
    return last


# ---- the LDAC rate watcher (bt-ldac-watch): what it sees and what it decides ---------------
# Seconds. SETTLE: how long the wrong rate must last before acting (the
# headset and WirePlumber are often still busy just after a connection).
SETTLE: Final[float] = 5.0
COOLDOWN: Final[float] = 600.0
RELAPSE: Final[float] = 300.0  # wrong rate again this soon after a fix: the fix did not hold
MAX_TRIES: Final[int] = 2  # attempts per connection, whatever happens


def serial(props: Mapping[str, Any]) -> int | None:
    """Parse PipeWire `object.serial` into an int, or None if unavailable."""
    s = props.get("object.serial")
    if s is None:
        return None
    try:
        return int(s)
    except TypeError, ValueError:
        return None


def snapshot(dev: str, objs: Iterable[Any]) -> dict[str, Any]:
    """What matters of headset DEV (MAC with "_") in pw-dump's objects, as a
    dict: card (object serial, None = not in PipeWire), profile (active card
    profile), out (output node serial), codec, rate, mic (the headset's input
    node is running). Missing parts are None."""
    mac = dev.replace("_", ":")
    inputs: list[int | None] = []
    snap: dict[str, Any] = {
        "card": None,
        "profile": None,
        "out": None,
        "codec": None,
        "rate": None,
        "inputs": inputs,
        "mic": False,
    }
    for o in objs:
        if not isinstance(o, dict):
            continue
        info = o.get("info") or {}
        p = info.get("props") or {}
        kind = str(o.get("type", ""))
        if kind.endswith("Device") and p.get("device.name") == "bluez_card." + dev:
            snap["card"] = serial(p)
            prof = (info.get("params") or {}).get("Profile") or []
            if prof and isinstance(prof[0], dict):
                snap["profile"] = prof[0].get("name")
        if not kind.endswith("Node"):
            continue
        name = str(p.get("node.name", ""))
        if name.startswith("bluez_output." + dev) and p.get("api.bluez5.codec"):
            snap["out"] = serial(p)
            snap["codec"] = p["api.bluez5.codec"]
            snap["rate"] = node_rate(info)
        elif name.startswith(("bluez_input." + dev, "bluez_input." + mac)):
            inputs.append(serial(p))
            if info.get("state") == "running":
                snap["mic"] = True
    return snap


def decide(snap: Mapping[str, Any] | None, rate: int) -> tuple[str, str]:
    """(what, why) for one snapshot: what is "none" (nothing wrong),
    "skip" (wrong rate, but not now) or "act" (wrong rate, may be fixed)."""
    if snap is None or snap["card"] is None:
        return "none", "headset not in PipeWire"
    if snap["codec"] != "ldac":
        return "none", f"codec {snap['codec'] or 'none'}"
    if snap["rate"] is None:
        return "none", "LDAC, rate unknown"
    if snap["rate"] == rate:
        return "none", f"LDAC at {rate} Hz"
    if not str(snap["profile"] or "").startswith("a2dp-sink"):
        return "skip", f"LDAC at {snap['rate']} Hz, but the card profile is {snap['profile']}"
    if snap["mic"]:
        return "skip", f"LDAC at {snap['rate']} Hz, but the headset's microphone is in use"
    return "act", f"LDAC at {snap['rate']} Hz (want {rate})"


class Tracker:
    """The one-attempt-per-connection rule (at most MAX_TRIES, and none
    after a failure or a fix that did not hold), the settle time and the
    cooldown. observe() takes a snapshot and the time and says what to do:
      ("none"|"skip", why)   nothing to do now
      ("wait", seconds)      wrong rate: look again after SECONDS
      ("act", why)           do the bounce now (then call attempted/result)
      ("done", why)          wrong rate, but this connection had its attempt(s)
      ("cooldown", seconds)  wrong rate, the last attempt was too recent"""

    def __init__(
        self,
        rate: int,
        settle: float = SETTLE,
        cooldown: float = COOLDOWN,
        relapse: float = RELAPSE,
        max_tries: int = MAX_TRIES,
    ) -> None:
        self.rate = rate
        self.settle = settle
        self.cooldown = cooldown
        self.relapse = relapse
        self.max_tries = max_tries
        self.card: int | None = None
        self.out: int | None = None
        self.tried: bool = False
        self.failed: str | None = None  # why this connection gets no more attempts
        self.tries: int = 0  # attempts on this connection
        self.fixed: float | None = None  # time of the last attempt that worked, this connection
        self.last: float | None = None  # time of the last attempt (any connection)
        self.since: float | None = None  # wrong rate seen first at

    def _new_connection(self) -> None:
        self.tried = False
        self.failed = None
        self.tries = 0
        self.fixed = None
        self.since = None

    def observe(self, snap: Mapping[str, Any] | None, now: float) -> tuple[str, str | float]:
        """Evaluate a PipeWire snapshot at monotonic timestamp `now`."""
        card: int | None = snap["card"] if snap else None
        out: int | None = snap["out"] if snap else None
        if card != self.card:
            # Gone, or connected again (a new card object): a new connection.
            self._new_connection()
        elif out != self.out and self.out is not None:
            # The output node went away or was replaced, not by us (own
            # bounces are recorded with own_node): one more attempt, unless
            # the last one failed or the connection had all its attempts.
            self.tried = False
            self.since = None
        self.card, self.out = card, out
        what, why = decide(snap, self.rate)
        if what != "act":
            self.since = None
            return what, why
        assert snap is not None
        if not self.failed and self.fixed is not None and now - self.fixed < self.relapse:
            # The wrong rate is back soon after a fix, on the same connection:
            # the headset set the stream up again by itself. Another bounce
            # would not hold either and would only repeat the dropout.
            self.failed = f"the headset went back to {snap['rate']} Hz by itself {now - self.fixed:.0f} s after the fix"
        if self.failed:
            return "done", f"{self.failed}: given up until it connects again"
        if self.tries >= self.max_tries:
            return "done", f"{self.tries} attempts on this connection already: given up until it connects again"
        if self.tried:
            return "done", "already tried once on this connection"
        if self.last is not None and now - self.last < self.cooldown:
            return "cooldown", self.cooldown - (now - self.last)
        if self.since is None:
            self.since = now
        if now - self.since < self.settle:
            return "wait", self.settle - (now - self.since)
        return "act", why

    def attempted(self, now: float) -> None:
        """Record that a bounce attempt started at monotonic timestamp `now`."""
        self.tried = True
        self.tries += 1
        self.last = now
        self.since = None

    def own_node(self, snap: Mapping[str, Any] | None) -> None:
        """After our own bounce: its new output node is not a reason to try again."""
        if snap and snap["card"] == self.card:
            self.out = snap["out"]

    def result(self, ok: bool) -> None:
        """Record whether the latest bounce attempt succeeded."""
        if ok:
            self.fixed = self.last
        else:
            self.failed = "an attempt failed on this connection"


EVENT: Final[re.Pattern[str]] = re.compile(r"^Event '(new|change|remove)' on (card|sink|source) #(\d+)\s*$")


def relevant(line: str, known: set[int]) -> bool:
    """Whether a `pactl subscribe` line can matter: a card or sink that comes
    or goes, or a change of one of the headset's objects (KNOWN: their
    indexes, which are the PipeWire object serials)."""
    m = EVENT.match(line.strip())
    if not m:
        return False
    event, kind, idx = m.group(1), m.group(2), int(m.group(3))
    if event in ("new", "remove") and kind in ("card", "sink"):
        return True
    return idx in known


A2DP_DESC: Final[re.Pattern[str]] = re.compile(r"\(A2DP Sink, codec ([^)]+)\)")


def profiles(dev: str, cards: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None, str | None]:
    """(card's active profile, LDAC profile, profile to bounce through) from
    `pactl -f json list cards`. Found by the codec in each profile's
    description, else by PipeWire's usual names. The bounce profile is SBC,
    else another available A2DP codec (SBC-XQ, AAC, ...); None when the card
    or a profile is missing."""
    card = next((c for c in cards if c.get("name") == "bluez_card." + dev), None)
    if card is None:
        return None, None, None
    by_codec: dict[str, str] = {}
    for name, p in (card.get("profiles") or {}).items():
        if not p.get("available", False):
            continue
        m = A2DP_DESC.search(str(p.get("description", "")))
        if m:
            by_codec[m.group(1).strip().upper()] = str(name)
    have = {str(n) for n, p in (card.get("profiles") or {}).items() if p.get("available", False)}
    ldac = by_codec.get("LDAC") or ("a2dp-sink" if "a2dp-sink" in have else None)
    alt = by_codec.get("SBC") or ("a2dp-sink-sbc" if "a2dp-sink-sbc" in have else None)
    if alt is None:
        others = sorted(n for c, n in by_codec.items() if c != "LDAC" and n != ldac)
        alt = others[0] if others else None
    active = card.get("active_profile")
    return None if active is None else str(active), ldac, alt
