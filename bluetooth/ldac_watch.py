"""The LDAC rate watcher's loop (bt-ldac-watch): it listens to `pactl
subscribe`, looks at the headset, and bounces the card's profile LDAC ->
SBC -> LDAC when the rules in bluetooth.domain say so. The program
bluetooth/bt-ldac-watch only hands its arguments to main().
"""

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import IO, Any, Final, Protocol

from bluetooth.adapters import Live
from bluetooth.domain import decide, device_id, profiles, relevant, snapshot, Tracker
from bluetooth.ports import LiveAudioPort
from cst_core.adapters.journal import log

DEBOUNCE: float = 1.0  # events come in bursts: one look after the burst
FALLBACK: Final[float] = 120.0  # a look now and then, should an event be missed
BACKOFF_MAX: Final[float] = 60.0  # pactl subscribe restarted after 1, 2, 4 ... 60 s
SBC_WAIT: Final[float] = 6.0  # how long the SBC profile may take to come up
SBC_HOLD: float = 2.0  # then kept this long, so the stream really closes
LDAC_WAIT: Final[float] = 10.0  # how long LDAC may take to come back with a rate


class _EventProc(Protocol):
    @property
    def stdout(self) -> IO[bytes] | None: ...


def wait_until[T](test: Callable[[], T], seconds: float, step: float = 0.5) -> T | None:
    """Calls TEST until it returns something true, at most SECONDS; returns that or None."""
    end = time.monotonic() + seconds
    while True:
        got = test()
        if got:
            return got
        if time.monotonic() >= end:
            return None
        time.sleep(step)


def bounce(live: LiveAudioPort, ldac: str, alt: str, rate: int) -> tuple[bool, str, dict[str, Any] | None]:
    """LDAC -> ALT -> LDAC. (ok, message, last snapshot)."""
    if not live.set_profile(alt):
        return False, f"pactl could not switch to {alt}", live.snap()

    def on_alt() -> bool:
        c = live.cards()
        return c is not None and profiles(live.dev, c)[0] == alt

    if not wait_until(on_alt, SBC_WAIT):
        log(4, f"{alt} did not become active within {SBC_WAIT:.0f} s")
    time.sleep(SBC_HOLD)
    # Look again before forcing LDAC back: in the meantime WirePlumber (its
    # switch to HFP when an app opens the headset's microphone) or the user
    # may have chosen another profile. That choice wins.
    snap = live.snap()
    c = live.cards()
    now = profiles(live.dev, c)[0] if c is not None else None
    hint = f"back to LDAC by hand: pactl set-card-profile bluez_card.{live.dev} {ldac}"
    if c is not None and now is None:
        return False, "the headset went away during the switch", snap
    if snap is not None and snap["mic"]:
        return False, f"the headset's microphone came into use during the switch: left on {now or alt} ({hint})", snap
    if now is not None and now not in (alt, ldac):
        return False, f"the card went to {now} during the switch (WirePlumber or the user): left there ({hint})", snap
    if not live.set_profile(ldac):
        time.sleep(1)
        if not live.set_profile(ldac):
            return (
                False,
                f"pactl could not switch back to {ldac}: the headset stays on {alt} "
                f"(pactl set-card-profile bluez_card.{live.dev} {ldac})",
                live.snap(),
            )

    def ldac_back() -> dict[str, Any] | None:
        s = live.snap()
        return s if s and s["codec"] == "ldac" and s["rate"] else None

    snap = wait_until(ldac_back, LDAC_WAIT, step=1.0)
    if snap is None:
        return False, f"LDAC did not come back within {LDAC_WAIT:.0f} s", live.snap()
    if snap["rate"] != rate:
        return False, f"LDAC came back at {snap['rate']} Hz again", snap
    return True, f"LDAC now at {snap['rate']} Hz", snap


class Stop:
    """SIGTERM/SIGINT: exit at once, except during a bounce. Between the two
    profile switches the card is on SBC, and WirePlumber keeps that as the
    headset's chosen profile; so the bounce first finishes (at most about
    20 s; systemd waits 90 s) and the program exits right after it."""

    busy: bool = False
    asked: bool = False

    @classmethod
    def handler(cls, signum: int, frame: FrameType | None) -> None:
        """Handle SIGTERM/SIGINT: defer exit while a profile bounce is active."""
        if cls.busy:
            cls.asked = True  # the sleeps and pactl calls carry on (PEP 475)
            return
        raise SystemExit(0)

    @classmethod
    def check(cls) -> None:
        """Exit if a stop signal arrived while busy."""
        if cls.asked:
            log(6, "stop asked during the profile switch: done switching, stopping now")
            raise SystemExit(0)


class Watcher:
    """Watch PulseAudio/PipeWire events and bounce LDAC back to the target rate."""

    def __init__(self, dev: str, rate: int, dry_run: bool = False) -> None:
        self.dev = dev
        self.rate = rate
        self.dry = dry_run
        self.live: LiveAudioPort = Live(dev)
        self.tracker = Tracker(rate)
        self.known: set[int] = set()
        self.said: tuple[str, str | float | None] | None = None

    def say(self, what: str, why: str | float, level: int = 6) -> None:
        """Logs a verdict only when it differs from the last one (no line
        every fallback look)."""
        key: tuple[str, str | float | None] = (what, why if what not in ("wait", "cooldown") else None)
        if key != self.said:
            self.said = key
            text = why if isinstance(why, str) else f"{why:.0f} s"
            log(level, f"{what}: {text}")

    def remember(self, snap: Mapping[str, Any] | None) -> None:
        """Remember known PipeWire object serials for event filtering."""
        self.known = set()
        if snap:
            self.known = {int(x) for x in [snap["card"], snap["out"], *snap["inputs"]] if x is not None}

    def look(self) -> float | None:
        """One evaluation; returns seconds until the next forced look, or None."""
        snap = self.live.snap()
        if snap is None:
            # pw-dump failed (PipeWire restarting): not a disconnection.
            self.say("none", "pw-dump gave no answer")
            return None
        self.remember(snap)
        what, why = self.tracker.observe(snap, time.monotonic())
        if what in ("wait", "cooldown") and not isinstance(why, str):
            self.say(what, why)
            return float(why)
        if what != "act":
            self.say(what, why, 4 if what == "done" else 6)
            return None
        cards = self.live.cards()
        active, ldac, alt = profiles(self.dev, cards or [])
        if cards is None or ldac is None or active != ldac:
            self.say("skip", f"{why}, but the LDAC profile is not the active one (active {active}, LDAC {ldac})")
            return None
        if alt is None:
            self.tracker.attempted(time.monotonic())
            self.tracker.result(False)
            self.say("give up", f"{why}, but the card offers no SBC (or other A2DP) profile to switch through", 4)
            return None
        if self.dry:
            self.tracker.attempted(time.monotonic())
            self.say("dry run", f"{why}: would switch {ldac} -> {alt} -> {ldac}")
            return None
        log(6, f"act: {why}: switching {ldac} -> {alt} -> {ldac} (about a second of silence)")
        self.tracker.attempted(time.monotonic())
        Stop.busy = True
        try:
            ok, msg, after = bounce(self.live, ldac, alt, self.rate)
        finally:
            Stop.busy = False
        self.tracker.own_node(after)
        self.tracker.result(ok)
        self.remember(after)
        if ok:
            log(6, f"fixed: {msg}")
        else:
            log(3, f"failed: {msg}; no further attempt until the headset connects again")
        self.said = None
        Stop.check()
        return None

    def run(self) -> None:
        """Subscribe to `pactl` events forever with exponential backoff on restarts."""
        log(6, f"watching bluez_card.{self.dev}: LDAC wanted at {self.rate} Hz{' (dry run)' if self.dry else ''}")
        backoff = 1.0
        while True:
            try:
                proc = subprocess.Popen(
                    ["pactl", "subscribe"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
                )
            except OSError as e:
                log(4, f"cannot start pactl subscribe ({e}); again in {backoff:.0f} s")
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            started = time.monotonic()
            self.listen(proc)
            proc.kill()
            proc.wait()
            if time.monotonic() - started > BACKOFF_MAX:
                backoff = 1.0
            log(4, f"pactl subscribe ended (PipeWire restarted?); again in {backoff:.0f} s")
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def listen(self, proc: _EventProc) -> None:
        """Reads events until pactl ends. A look right away (also after a
        reconnection), DEBOUNCE after a relevant event, and every FALLBACK."""
        now = time.monotonic()
        due: float | None = now  # next look
        fallback = now + FALLBACK
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        buf = b""  # read by hand: select() cannot see Python's buffer
        while True:
            now = time.monotonic()
            if due is not None and now >= due:
                due = None
                nxt = self.look()
                now = time.monotonic()
                fallback = now + FALLBACK
                if nxt is not None:
                    due = now + max(nxt, 0.5)
            if now >= fallback:
                due = now
                continue
            wait_until_ts = fallback if due is None else min(due, fallback)
            timeout = wait_until_ts - now
            r, _, _ = select.select([fd], [], [], max(timeout, 0))
            if not r:
                continue
            data = os.read(fd, 65536)
            if not data:
                return
            *lines, buf = (buf + data).split(b"\n")
            if due is None and any(relevant(x.decode("utf-8", "replace"), self.known) for x in lines):
                due = time.monotonic() + DEBOUNCE


def main(argv: Sequence[str], doc: str = "") -> int:
    """The command line of `bt-ldac-watch` (DOC: its help, the program's docstring)."""
    ap = argparse.ArgumentParser(prog="bt-ldac-watch", description=doc.split("\n\n")[0])
    ap.add_argument("--device", required=True)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", a.device):
        ap.error(f"--device must be a MAC address like AA:BB:CC:DD:EE:FF (got {a.device!r})")
    dev = device_id(a.device)
    if a.snapshot:
        try:
            snap = snapshot(dev, json.load(sys.stdin))
        except ValueError:
            return 3
        print(json.dumps(snap, sort_keys=True))
        what, why = decide(snap, a.rate)
        print(f"{what}: {why}")
        return 0
    signal.signal(signal.SIGTERM, Stop.handler)
    signal.signal(signal.SIGINT, Stop.handler)
    os.environ.setdefault("LC_ALL", "C")  # pactl's event lines in English
    try:
        Watcher(dev, a.rate, a.dry_run).run()
    except KeyboardInterrupt, SystemExit:
        log(6, "stopped")
    return 0
