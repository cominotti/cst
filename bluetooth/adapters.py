# SPDX-License-Identifier: MPL-2.0
"""Concrete host adapters for the `bluetooth` bounded context: the files and libwireplumber that
bt-helper reads, and the LDAC rate watcher's pw-dump and pactl calls and its loop."""

from collections.abc import Callable, Sequence
import contextlib
import ctypes
import io
import json
import os
import select
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import IO, Final, Protocol, override

from bluetooth.domain import (
    CARD_PREFIX,
    LE_SECTION,
    PactlCard,
    RESTART_UNITS,
    classify_other_fragments,
    parse_pending,
    render_pending,
    still_pending,
)
from bluetooth.domain.watch import Snapshot, Tracker, WatchNote, WatchVerdict, profiles, relevant, snapshot
from bluetooth.ports import LiveAudioPort
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.files import ensure_dir, write_atomic
from cst_core.adapters.journal import log
from cst_core.ports import CommandFault, CommandRunnerPort


def one_line_errors[**P](main: Callable[P, int]) -> Callable[P, int]:
    """bt-helper's main: a file it cannot read or decode (OSError, ValueError) ends it with one
    line on its stderr (the stderr= argument, else sys.stderr), "bt-helper: " and the error,
    and exit status 1, instead of a traceback."""

    def run(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return main(*args, **kwargs)
        except (OSError, ValueError) as e:
            given = kwargs.get("stderr")
            print(f"bt-helper: {e}", file=given if isinstance(given, io.TextIOBase) else sys.stderr)
            return 1

    return run


def read(path: str) -> str:
    """Read a UTF-8 text file."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_or_none(path: str) -> str | None:
    """Read a UTF-8 text file, or return None if it does not exist."""
    try:
        return read(path)
    except FileNotFoundError:
        return None


def other_fragments(ours: str, dirs: Sequence[str]) -> list[tuple[str, str]]:
    """[(before|after, path)] of the WirePlumber fragments other than OURS
    (a file name) that define monitor.bluez.properties, in the order
    WirePlumber reads them: by file name, over DIRS (highest priority
    first: a name found in an earlier dir hides the same name in later ones)."""
    seen_names: set[str] = set()
    dir_files: list[tuple[str, list[tuple[str, str]]]] = []
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        entries: list[tuple[str, str]] = []
        for n in names:
            if not n.endswith(".conf") or n in seen_names:
                continue
            seen_names.add(n)
            if n == ours:
                continue
            try:
                text = read(os.path.join(d, n))
            except OSError, UnicodeDecodeError:
                continue
            entries.append((n, text))
        dir_files.append((d, entries))
    return classify_other_fragments(ours, dir_files)


def wp_effective_roles() -> tuple[bool, str | None]:
    """bluez5.roles as WirePlumber resolves its config right now (all its
    files, merged by libwireplumber itself, read-only: nothing is started or
    connected). Returns (True, value_or_None) on success, or (False, error)
    when libwireplumber cannot be loaded or the config cannot be opened."""
    try:
        lib = ctypes.CDLL(os.environ.get("CST_BT_LIBWP") or "libwireplumber-0.5.so.0")
        lib.wp_init.argtypes = [ctypes.c_int]
        lib.wp_init(0)
        lib.wp_conf_new_open.restype = ctypes.c_void_p
        lib.wp_conf_new_open.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.wp_properties_new_empty.restype = ctypes.c_void_p
        lib.wp_conf_section_update_props.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        lib.wp_properties_get.restype = ctypes.c_char_p
        lib.wp_properties_get.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        err = ctypes.c_void_p()
        conf = lib.wp_conf_new_open(b"wireplumber.conf", None, ctypes.byref(err))
        if not conf:
            return False, "WirePlumber could not open wireplumber.conf"
        props = lib.wp_properties_new_empty()
        lib.wp_conf_section_update_props(conf, LE_SECTION.encode(), props)
        v: bytes | None = lib.wp_properties_get(props, b"bluez5.roles")
        return True, None if v is None else v.decode("utf-8", "replace")
    except (OSError, AttributeError) as exc:
        return False, str(exc)


class LocalBluetoothHostAdapter:
    """Default adapter reading from the local filesystem and libwireplumber."""

    def read(self, path: str) -> str:
        """Read a UTF-8 file from disk."""
        return read(path)

    def read_or_none(self, path: str) -> str | None:
        """Read a UTF-8 file from disk, or None when missing."""
        return read_or_none(path)

    def other_fragments(self, ours: str, dirs: Sequence[str]) -> list[tuple[str, str]]:
        """List other WirePlumber fragments defining monitor.bluez.properties."""
        return other_fragments(ours, dirs)

    def wp_effective_roles(self) -> tuple[bool, str | None]:
        """Resolve effective WirePlumber bluez5.roles via libwireplumber."""
        return wp_effective_roles()

    def read_stdin(self) -> str:
        """Read all text from sys.stdin."""
        return sys.stdin.read()

    def restart_pending(self, path: str, ids: tuple[str, str], skipped: Sequence[str], *, save: bool) -> str:
        """The restarts still owed, one per line; with save, the file at path holds them (atomically), or is removed.
        A record that cannot be read (not UTF-8, not a file) counts as none, as parse_pending treats one that is not JSON."""
        try:
            text = read_or_none(path)
        except OSError, ValueError:
            text = None
        owed = still_pending(parse_pending(text), dict(zip(RESTART_UNITS, ids, strict=True)), skipped)
        if save and owed:
            ensure_dir(os.path.dirname(path), 0o755)
            write_atomic(path, render_pending(owed), mode=0o644, relabel=False)
        elif save:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        return "".join(f"{u}\n" for u in sorted(owed))


# ---- the LDAC rate watcher: pw-dump and pactl ------------------------------------------------
RUNNER: CommandRunnerPort = SubprocessCommandRunner()


def run(cmd: Sequence[str], timeout: float = 15) -> tuple[int, str]:
    """(exit status, stdout) of CMD; 127 when it does not exist, 124 on timeout.
    stdin is ours; bytes that are not UTF-8 become U+FFFD; a command that
    cannot start raises (as subprocess.run did)."""
    r = RUNNER.run(cmd, timeout=timeout, stdin="inherit")
    if r.fault in (CommandFault.MISSING, CommandFault.TIMEOUT):
        return r.returncode, ""
    if r.cause is not None:
        raise r.cause
    return r.returncode, r.stdout


class Live(LiveAudioPort):
    """The real system: pw-dump and pactl."""

    def __init__(self, dev: str) -> None:
        self.dev = dev

    @override
    def snap(self) -> Snapshot | None:
        """Query `pw-dump` and return this device's parsed snapshot, or None."""
        rc, out = run(["pw-dump"], timeout=10)
        if rc != 0:
            return None
        try:
            return snapshot(self.dev, json.loads(out))
        except ValueError:
            return None

    @override
    def cards(self) -> list[PactlCard] | None:
        """Query `pactl -f json list cards`, or return None on failure."""
        rc, out = run(["pactl", "-f", "json", "list", "cards"])
        if rc != 0:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    @override
    def set_profile(self, prof: str) -> bool:
        """Switch `bluez_card.<dev>` to profile `prof` via `pactl`."""
        return run(["pactl", "set-card-profile", CARD_PREFIX + self.dev, prof])[0] == 0


# ---- the LDAC rate watcher's loop (bt-ldac-watch): it listens to `pactl subscribe`, looks at the
# headset, and bounces the card's profile LDAC -> SBC -> LDAC when bluetooth.domain.watch says so.
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


def bounce(live: LiveAudioPort, ldac: str, alt: str, rate: int) -> tuple[bool, str, Snapshot | None]:
    """LDAC -> ALT -> LDAC. (ok, message, last snapshot)."""
    if not live.set_profile(alt):
        return False, f"pactl could not switch to {alt}", live.snap()

    def on_alt() -> bool:
        c = live.cards()
        return c is not None and profiles(live.dev, c)[0] == alt

    if wait_until(on_alt, SBC_WAIT) is None:
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

    def ldac_back() -> Snapshot | None:
        s = live.snap()
        return s if s and s["codec"] == "ldac" and s["rate"] is not None and s["rate"] != 0 else None

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
        self.said: tuple[WatchVerdict | WatchNote, str | float | None] | None = None

    def say(self, what: WatchVerdict | WatchNote, why: str | float, level: int = 6) -> None:
        """Logs a verdict only when it differs from the last one (no line
        every fallback look)."""
        key: tuple[WatchVerdict | WatchNote, str | float | None] = (
            what,
            why if what not in (WatchVerdict.WAIT, WatchVerdict.COOLDOWN) else None,
        )
        if key != self.said:
            self.said = key
            text = why if isinstance(why, str) else f"{why:.0f} s"
            log(level, f"{what}: {text}")

    def remember(self, snap: Snapshot | None) -> None:
        """Remember known PipeWire object serials for event filtering."""
        self.known = set()
        if snap:
            self.known = {int(x) for x in [snap["card"], snap["out"], *snap["inputs"]] if x is not None}

    def look(self) -> float | None:
        """One evaluation; returns seconds until the next forced look, or None."""
        snap = self.live.snap()
        if snap is None:
            # pw-dump failed (PipeWire restarting): not a disconnection.
            self.say(WatchVerdict.NONE, "pw-dump gave no answer")
            return None
        self.remember(snap)
        what, why = self.tracker.observe(snap, time.monotonic())
        if what in (WatchVerdict.WAIT, WatchVerdict.COOLDOWN) and not isinstance(why, str):
            self.say(what, why)
            return float(why)
        if what != WatchVerdict.ACT:
            self.say(what, why, 4 if what == WatchVerdict.DONE else 6)
            return None
        cards = self.live.cards()
        active, ldac, alt = profiles(self.dev, cards or [])
        if cards is None or ldac is None or active != ldac:
            self.say(
                WatchVerdict.SKIP, f"{why}, but the LDAC profile is not the active one (active {active}, LDAC {ldac})"
            )
            return None
        if alt is None:
            self.tracker.attempted(time.monotonic())
            self.tracker.result(False)
            self.say(
                WatchNote.GIVE_UP, f"{why}, but the card offers no SBC (or other A2DP) profile to switch through", 4
            )
            return None
        if self.dry:
            self.tracker.attempted(time.monotonic())
            self.say(WatchNote.DRY_RUN, f"{why}: would switch {ldac} -> {alt} -> {ldac}")
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


def stdin_snapshot(dev: str) -> Snapshot | None:
    """`bt-ldac-watch --snapshot`: what the watcher sees of headset DEV in the pw-dump on
    standard input; None when that is not JSON."""
    try:
        return snapshot(dev, json.load(sys.stdin))
    except ValueError:
        return None


def run_watcher(dev: str, rate: int, *, dry_run: bool) -> int:
    """Run the watcher until a stop signal: Stop's SystemExit(0) goes on up after the "stopped"
    line, so the program's exit status is 0; see Stop for a stop during a bounce. SIGINT is Stop's
    as well, so no KeyboardInterrupt comes here."""
    signal.signal(signal.SIGTERM, Stop.handler)
    signal.signal(signal.SIGINT, Stop.handler)
    os.environ.setdefault("LC_ALL", "C")  # pactl's event lines in English
    try:
        Watcher(dev, rate, dry_run).run()
    except SystemExit:  # Stop's exit, status 0
        log(6, "stopped")
        raise
    return 0
