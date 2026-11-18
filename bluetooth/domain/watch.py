# SPDX-License-Identifier: MPL-2.0
"""The rules of the LDAC rate watcher (bt-ldac-watch): what it sees of the
headset in pw-dump, the pactl events and card profiles that matter, and the
one-attempt-per-connection rule (Tracker). No I/O.
"""

from collections.abc import Iterable, Sequence
import re
from typing import Final, TypedDict

from bluetooth.domain import PactlCard, PwObject, PwProps, node_rate


# ---- the LDAC rate watcher (bt-ldac-watch): what it sees and what it decides ---------------
# Seconds. SETTLE: how long the wrong rate must last before acting (the
# headset and WirePlumber are often still busy just after a connection).
SETTLE: Final[float] = 5.0
COOLDOWN: Final[float] = 600.0
RELAPSE: Final[float] = 300.0  # wrong rate again this soon after a fix: the fix did not hold
MAX_TRIES: Final[int] = 2  # attempts per connection, whatever happens


class Snapshot(TypedDict):
    """What the watcher sees of the headset in pw-dump (see snapshot)."""

    card: int | None
    profile: str | None
    out: int | None
    codec: str | None
    rate: int | None
    inputs: list[int | None]
    mic: bool


def serial(props: PwProps) -> int | None:
    """Parse PipeWire `object.serial` into an int, or None if unavailable."""
    s = props.get("object.serial")
    if s is None:
        return None
    try:
        return int(s)
    except TypeError, ValueError:
        return None


def snapshot(dev: str, objs: Iterable[PwObject]) -> Snapshot:
    """What matters of headset DEV (MAC with "_") in pw-dump's objects, as a
    dict: card (object serial, None = not in PipeWire), profile (active card
    profile), out (output node serial), codec, rate, mic (the headset's input
    node is running). Missing parts are None."""
    mac = dev.replace("_", ":")
    inputs: list[int | None] = []
    snap: Snapshot = {
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


def decide(snap: Snapshot | None, rate: int) -> tuple[str, str]:
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

    def observe(self, snap: Snapshot | None, now: float) -> tuple[str, str | float]:
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

    def own_node(self, snap: Snapshot | None) -> None:
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


def profiles(dev: str, cards: Sequence[PactlCard]) -> tuple[str | None, str | None, str | None]:
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
