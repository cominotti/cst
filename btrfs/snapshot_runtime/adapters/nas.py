# SPDX-License-Identifier: MPL-2.0
"""nas of btrfs-snapshot: the daily backup of the newest local snapshots to the NAS with restic over
SFTP, as btrfs/btrfs-snapshot (bash) did it.

Host, port, user, key and timeouts come from the managed Host block btrfs-setup-nas in
/root/.ssh/config; restic's own ssh picks it up. The snapshots are bind-mounted read-only at fixed
paths (NAS_MNT/home, NAS_MNT/Workspace), so restic's parent snapshot matches every day. /run/
btrfs-snapshot.lock is held only while choosing and pinning them; the NAS run's own lock for the
whole run. TERM, INT and HUP end the run after the command that is running (exit 143); the
mounts are undone at every end, as bash's EXIT trap did."""

from collections.abc import Sequence
import contextlib
import glob
import os
import shutil
import signal
import sys
from typing import TextIO

from btrfs.health_check.domain import bash_div
from btrfs.snapshot_runtime.adapters.hourly import RunStopped, clear_alert, send_alert
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun, Stopped
from cst_core.adapters.files import write_atomic
from cst_core.domain.contracts import Urgency
from cst_core.domain.values import DAY
from btrfs.snapshot_runtime.domain import (
    CHECK_FAILURES_ALERTED,
    EXIT_STOPPED,
    MIN_LEFT_S,
    NAS_LOCK_WAIT_S,
    NAS_SSH_ALIAS,
    RESTIC_DAMAGE_RE,
    RESTIC_INIT_S,
    RESTIC_SHORT_S,
    SFTP_TIMEOUT_S,
    SUMMARY_CHARS,
    AlertKind,
    CoreCommand,
    GuardError,
    ResticExit,
    restic_time,
    duration_s,
    features_type,
    guard_number,
    repo_id,
    restic_line,
    summary_text,
)

UMOUNT_TRIES = 5


class _Capture:
    """tee -a PATH: restic's journal lines appended to PATH. A file that cannot be opened or written
    is said once on stderr, as tee says it, and no longer written; the lines still reach the journal
    and the run goes on."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.file: TextIO | None = None
        try:
            self.file = open(path, "a", encoding="utf-8", errors="surrogateescape")
        except OSError as e:
            self.failed(e)

    def failed(self, e: OSError) -> None:
        print(f"tee: {self.path}: {e.strerror}", file=sys.stderr, flush=True)

    def add(self, line: str) -> None:
        if self.file is None:
            return
        try:
            self.file.write(line + "\n")
            self.file.flush()
        except OSError as e:
            self.failed(e)
            self.close()

    def close(self) -> None:
        f, self.file = self.file, None
        if f is not None:
            with contextlib.suppress(OSError):
                f.close()


NAS_RECORDS = ("nas-summary", "nas-last-prune", "nas-last-check", "nas-check-failures")


class Nas:
    """One NAS run: its deadline, the repository id and the snapshots it pinned."""

    def __init__(self, m: SnapshotRun) -> None:
        self.m = m
        self.deadline = 0
        self.repo_id = ""
        self.names: list[str] = []
        self.snaps: list[str] = []
        self.features: list[str] = []
        self.repository = f"sftp:{NAS_SSH_ALIAS}:{m.settings['NAS_PATH']}"

    # ---- numbers, as bash arithmetic read them -----------------------------------------------
    def number(self, key: str) -> int | None:
        """A numeric setting; None where bash's comparison failed (and was false)."""
        value = self.m.settings[key]
        n = guard_number(value)
        if n is GuardError.NOT_A_NUMBER:
            print(f"{self.m.front}: {key}={value!r} in {self.m.conf}: not a number", file=sys.stderr, flush=True)
            raise RunStopped
        if n is GuardError.BAD_NUMBER:
            print(f"{self.m.front}: ((: {key}={value!r}: not a number", file=sys.stderr, flush=True)
            return None
        return n

    def time_left(self) -> int:
        return self.deadline - self.m.now()

    def days_since(self, last: int) -> int:
        return bash_div(self.m.now() - last, DAY)

    def due(self, path: str, key: str) -> bool:
        """The epoch in PATH is missing, no number, or KEY days old."""
        last = self.m.read(path)
        if last is None or not last.isascii() or not last.isdigit():
            return True
        days, n = self.number(key), guard_number(last)
        if days is None or not isinstance(n, int):
            return False
        return self.m.now() - n >= days * DAY

    # ---- staleness -------------------------------------------------------------------------
    def since(self) -> int:
        """The last good backup, or when the backup was enabled if that is later; started now if
        neither is recorded."""
        best = 0
        for name in ("nas-last-ok", "nas-enabled-at"):
            v = self.m.read(f"{self.m.state}/{name}")
            if v is not None and v.isascii() and v.isdigit() and int(v) > best:
                best = int(v)
        if best == 0:
            best = self.m.now()
            self.m.write(f"{self.m.state}/nas-enabled-at", f"{best}\n")
        return best

    def stale_alert(self, msg: str) -> None:
        age_d = self.days_since(self.since())
        stale = self.number("NAS_STALE_DAYS")
        if stale is not None and age_d >= stale:
            send_alert(
                self.m,
                AlertKind.NAS,
                Urgency.NORMAL,
                f"NAS backup is {age_d} days old",
                f"{msg} Local snapshots are unaffected.",
            )

    # ---- commands ----------------------------------------------------------------------------
    def restic(self, limit: int, args: Sequence[str], *, env: Sequence[str] = (), capture: str = "") -> int:
        """restic bounded by LIMIT seconds (timeout --kill-after=5m), its output in the journal
        format line by line, and appended to CAPTURE if given. Its exit status (124: time limit)."""
        m = self.m
        sink = _Capture(capture) if capture else None

        def relay(line: str) -> None:
            out = restic_line(line)
            m.say(out)
            if sink is not None:
                sink.add(out)

        argv = ["timeout", "--kill-after=5m", str(limit), "env", *env, "restic", *args]
        extra = {"RESTIC_CAPTURE": capture} if capture else None
        try:
            return m.rc(m.run(argv, output="merge", on_line=relay, env=extra))
        finally:
            if sink is not None:
                sink.close()

    def reachable(self) -> bool:
        """Connection, key, host key and SFTP subsystem in one go (what restic uses)."""
        return self.m.ok(["timeout", str(SFTP_TIMEOUT_S), "sftp", "-q", "-b", "-", NAS_SSH_ALIAS], input_text="pwd\n")

    def cat_config(self) -> tuple[int, str]:
        res = self.m.run(["timeout", str(RESTIC_SHORT_S), "restic", "cat", "config"], output="merge")
        return self.m.rc(res), res.stdout.rstrip("\n")

    # ---- the repository ----------------------------------------------------------------------
    def repo_ready(self) -> bool:
        """The repository opens with our password; a missing one is created only if setup asked for
        it (nas-init-pending), never on its own. Sets repo_id."""
        m, pw = self.m, self.m.restic_password
        rc, out = self.cat_config()
        if rc == ResticExit.NO_REPOSITORY and os.path.exists(f"{m.state}/nas-init-pending"):
            m.log(5, f"NAS: creating the restic repository {self.repository} (requested by 'snapshots nas setup')")
            if self.restic(RESTIC_INIT_S, ["init"]) != 0:
                m.log(4, "NAS: restic init failed")
                self.stale_alert("Creating the repository on the NAS failed.")
                return False
            m.rm_f(f"{m.state}/nas-init-pending")
            send_alert(
                m,
                AlertKind.NAS_CREATED,
                Urgency.NORMAL,
                "NAS backup: repository created",
                f"Encrypted with the password in {pw}: store a copy in your password manager now (sudo cat {pw}); "
                "without it the backups cannot be read.",
            )
            rc, out = self.cat_config()
        match rc:
            case ResticExit.OK:
                m.rm_f(f"{m.state}/nas-init-pending")
                self.repo_id = repo_id(out)
                self.repo_records()
                return True
            case ResticExit.NO_REPOSITORY:
                m.log(
                    4,
                    f"NAS: no restic repository at {self.repository}; not creating one on its own "
                    "(re-run 'cst btrfs snapshots nas setup')",
                )
                self.stale_alert(f"No repository at {m.settings['NAS_PATH']} on the NAS (moved or deleted?).")
            case ResticExit.WRONG_PASSWORD:
                send_alert(
                    m,
                    AlertKind.NAS_PASSWORD,
                    Urgency.CRITICAL,
                    "NAS backup: wrong repository password",
                    f"{pw} does not open {self.repository}; put the saved password there.",
                )
            case _:
                m.log(4, f"NAS: cannot open the repository (restic rc={rc}): {out[:SUMMARY_CHARS]}")
                self.stale_alert(f"The repository on the NAS could not be opened (restic rc={rc}).")
        return False

    def repo_records(self) -> None:
        """What STATE records about the repository belongs to the repository it was recorded for:
        another one (or a missing id) starts over."""
        m, state = self.m, self.m.state
        found = m.read(f"{state}/nas-repo-id")
        known = found if found is not None else ""
        if self.repo_id and known == self.repo_id:
            return
        done = sorted(glob.glob(glob.escape(state) + "/nas-done-*"))
        if known:
            m.log(
                5,
                f"NAS: another repository than last time (id {known[:8]} -> {self.repo_id[:8]}); its records start over",
            )
        elif done or any(os.path.exists(f"{state}/{n}") for n in NAS_RECORDS):
            m.log(
                5,
                f"NAS: {state}/nas-repo-id is missing, so the records of the NAS backup are not known to be this "
                "repository's; they start over",
            )
        m.rm_f(*done, *(f"{state}/{n}" for n in NAS_RECORDS), f"{state}/nas-repo-id")
        if self.repo_id:
            m.write(f"{state}/nas-repo-id", f"{self.repo_id}\n")

    # ---- mounts and pins -------------------------------------------------------------------
    def unmount(self) -> bool:
        """Unmount NAS_MNT/* (a killed run can leave mounts stacked there); the pin file goes only
        once nothing is mounted. False if something stayed mounted (its snapshot stays pinned)."""
        m = self.m
        ok = True
        for mnt in sorted(glob.glob(glob.escape(m.nas_mnt) + "/*")):
            if not os.path.isdir(mnt):
                continue
            n = 0
            while m.ok(["mountpoint", "-q", mnt]):
                n += 1
                if n > UMOUNT_TRIES or m.rc(m.run(["umount", mnt])) != 0:
                    break
            if m.ok(["mountpoint", "-q", mnt]):
                ok = False
                m.log(
                    4,
                    f"NAS: cannot unmount {mnt}; the pinned snapshots stay pinned, and no backup starts until it "
                    "is unmounted",
                )
                continue
            with contextlib.suppress(OSError):
                os.rmdir(mnt)
        if ok:
            m.rm_f(m.pin_file)
        return ok

    def pin(self) -> bool:
        """Under the snapshot lock, so no hourly run deletes anything between choosing and pinning:
        the newest owned snapshot of each source, written to the pin file. False if none."""
        m = self.m
        fd = m.lock(m.lock_path, wait=True, timeout=NAS_LOCK_WAIT_S)
        if fd is None:
            m.log(5, f"NAS: the hourly run held {m.lock_path} for 30 minutes; skipped")
            return False
        try:
            src = m.sources()
            for name, path in zip(src.names, src.paths, strict=True):
                argv = [m.core, CoreCommand.LATEST, "--snapdir", m.snapdir, "--name", name, "--source-path", path]
                res = m.run(argv, output="stdout-show-stderr")
                snap = res.stdout.rstrip("\n")
                if res.fault is None and res.returncode == 0 and snap:
                    self.names.append(name)
                    self.snaps.append(snap)
                else:
                    m.log(4, f"NAS: no snapshot of {name} to back up")
            m.run(["install", "-d", "-m", "0755", m.run_dir])
            # printf '%s\n' with no snapshot writes one empty line.
            text = "".join(f"{s}\n" for s in self.snaps) or "\n"
            try:
                write_atomic(m.pin_file, text, mode="umask", durable=False, relabel=False)
            except OSError:
                m.log(3, f"NAS: cannot write {m.pin_file}")
                return False
        finally:
            m.unlock(fd)
        return bool(self.snaps)

    # ---- one backup --------------------------------------------------------------------------
    def backup(self, name: str, snap: str) -> bool:
        """One restic snapshot of SNAP, read at NAS_MNT/NAME, dated with the local snapshot's time."""
        m = self.m
        mnt = f"{m.nas_mnt}/{name}"
        entry = snap.rsplit("/", 1)[-1]
        done = f"{m.state}/nas-done-{name}"
        if self.repo_id and m.read(done) == f"{entry} {self.repo_id}":
            m.log(6, f"NAS: {entry} is already in the repository")
            return True
        when = restic_time(name, entry)
        left = self.time_left()
        timeout = m.settings["NAS_TIMEOUT"]
        if left <= MIN_LEFT_S:
            m.log(4, f"NAS: {name}: no time left in this run (NAS_TIMEOUT={timeout})")
            return False
        m.run(["install", "-d", "-m", "0700", mnt])
        # Never stack a mount on a leftover one: its snapshot would no longer be pinned.
        if m.ok(["mountpoint", "-q", mnt]):
            m.log(4, f"NAS: {name}: {mnt} is still mounted; skipped")
            return False
        if m.rc(m.run(["mount", "--bind", "-o", "ro,nosuid,nodev", snap, mnt])) != 0:
            m.log(4, f"NAS: {name}: cannot bind-mount {snap} at {mnt}")
            return False
        m.log(5, f"NAS: backing up {entry} (read at {mnt})")
        args = ["backup", "--host", m.settings["NAS_RESTIC_HOST"], "--tag", f"btrfs-setup,{name}", "--time", when]
        args += ["--one-file-system", "--exclude-caches", "--skip-if-unchanged", mnt]
        rc = self.restic(left, args, env=self.features)
        m.ok(["umount", mnt])  # unmount retries and keeps the pin if this failed
        match rc:
            case ResticExit.OK:
                pass
            case ResticExit.INCOMPLETE:
                m.log(4, f"NAS: {name}: some files could not be read; the rest was backed up")
            case ResticExit.TIMED_OUT | ResticExit.KILLED:
                m.log(
                    4,
                    f"NAS: {name}: stopped at the time limit (NAS_TIMEOUT={timeout}); the uploaded data is reused next run",
                )
                return False
            case _:
                m.log(4, f"NAS: {name}: restic backup failed (rc={rc})")
                return False
        m.write(done, f"{entry} {self.repo_id}\n")
        return True

    # ---- forget, prune, check ------------------------------------------------------------------
    def maintain(self) -> None:
        """forget every run, prune at most every NAS_PRUNE_EVERY_DAYS, check a random
        NAS_CHECK_SUBSET of the data at most every NAS_CHECK_EVERY_DAYS."""
        m, s = self.m, self.m.settings
        args = ["forget", "--host", s["NAS_RESTIC_HOST"], "--tag", "btrfs-setup", "--keep-daily", s["NAS_KEEP_DAILY"]]
        args += ["--keep-weekly", s["NAS_KEEP_WEEKLY"], "--keep-monthly", s["NAS_KEEP_MONTHLY"]]
        left = self.time_left()
        if left <= MIN_LEFT_S:
            m.log(5, "NAS: no time left for forget/prune/check; next run")
            return
        prune = self.due(f"{m.state}/nas-last-prune", "NAS_PRUNE_EVERY_DAYS")
        if prune:
            args.append("--prune")
        rc = self.restic(left, args)
        if rc == 0:
            if prune:
                m.write(f"{m.state}/nas-last-prune", f"{m.now()}\n")
        else:
            m.log(4, f"NAS: restic forget{' --prune' if prune else ''} failed (rc={rc}); retried next run")
        if not self.due(f"{m.state}/nas-last-check", "NAS_CHECK_EVERY_DAYS"):
            return
        left = self.time_left()
        if left <= MIN_LEFT_S:
            return
        made = m.run(["mktemp"], output="stdout-show-stderr")
        out = made.stdout.rstrip("\n")
        if made.fault is not None or made.returncode != 0:
            return
        try:
            # RESTIC_CAPTURE: bash's prefix assignment to restic_run reached restic's environment too.
            rc = self.restic(left, ["check", f"--read-data-subset={s['NAS_CHECK_SUBSET']}"], capture=out)
            self.check_result(rc, out)
        finally:
            m.rm_f(out)

    def check_result(self, rc: int, out: str) -> None:
        m = self.m
        match rc:
            case ResticExit.OK:
                m.write(f"{m.state}/nas-last-check", f"{m.now()}\n")
                m.rm_f(f"{m.state}/nas-check-failures")
                clear_alert(m, AlertKind.NAS_CHECK)
            case ResticExit.LOCKED | ResticExit.TIMED_OUT | ResticExit.INTERRUPTED | ResticExit.KILLED:
                m.log(4, f"NAS: restic check did not complete (rc={rc}); retried next run")
                self.check_stale()
            case _:
                # restic exits 1 for damaged data and for a NAS that went away mid-check: only
                # messages naming damaged data, with the NAS still reachable, mean damage.
                damaged = any(RESTIC_DAMAGE_RE.search(ln) for ln in m.read_raw(out).split("\n"))
                if damaged and self.reachable():
                    send_alert(
                        m,
                        AlertKind.NAS_CHECK,
                        Urgency.CRITICAL,
                        "NAS backup: repository damaged",
                        f"restic check found damaged data (rc={rc}); see journalctl -u btrfs-snapshot-nas and "
                        "restic's troubleshooting guide (restic repair).",
                    )
                elif not self.reachable():
                    m.log(4, f"NAS: restic check stopped, the NAS went away (rc={rc}); retried next run")
                    self.check_stale()
                else:
                    self.check_failed(rc)

    def check_failed(self, rc: int) -> None:
        m = self.m
        path = f"{m.state}/nas-check-failures"
        before = m.read(path)
        n = guard_number(before) if before is not None else 0
        if n is GuardError.NOT_A_NUMBER:
            # bash stopped here too ("abc: unbound variable"): say why, as the settings do.
            print(f"{m.front}: {path} holds {before!r}: not a number", file=sys.stderr, flush=True)
            raise RunStopped
        fails = (0 if n is GuardError.BAD_NUMBER else n) + 1
        m.write(path, f"{fails}\n")
        m.log(4, f"NAS: restic check failed (rc={rc}) without naming damaged data (connection lost?); retried next run")
        if fails >= CHECK_FAILURES_ALERTED:
            send_alert(
                m,
                AlertKind.NAS_CHECK,
                Urgency.NORMAL,
                "NAS backup: repository check keeps failing",
                f"restic check failed {fails} runs in a row (rc={rc}) with the NAS reachable, without naming "
                "damaged data; see journalctl -u btrfs-snapshot-nas.",
            )
        else:
            self.check_stale()

    def check_stale(self) -> None:
        """A check that keeps not completing is alerted only once it is overdue by another interval."""
        m = self.m
        last_text = m.read(f"{m.state}/nas-last-check")
        if last_text is None:
            made = m.mtime(f"{m.state}/nas-repo-id")
            last_text = str(made) if made is not None else str(m.now())
        last = int(last_text) if last_text.isascii() and last_text.isdigit() else m.now()
        age_d = self.days_since(last)
        every = self.number("NAS_CHECK_EVERY_DAYS")
        if every is not None and age_d >= 2 * every:
            send_alert(
                m,
                AlertKind.NAS_CHECK,
                Urgency.NORMAL,
                f"NAS backup: repository not checked for {age_d} days",
                "restic check did not complete at the last attempts; see journalctl -u btrfs-snapshot-nas.",
            )

    def summary(self) -> None:
        """One line per source about its latest restic snapshot, for snapshots status (read here,
        while connected; status stays offline)."""
        m = self.m
        argv = ["timeout", str(RESTIC_SHORT_S), "restic", "snapshots", "--json", "--latest", "1"]
        res = m.run([*argv, "--host", m.settings["NAS_RESTIC_HOST"], "--tag", "btrfs-setup"], output="stdout")
        if res.fault is not None or res.returncode != 0:
            return
        try:
            text = summary_text(res.stdout)
        except ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError:
            return  # not restic's JSON: the last summary stays
        with contextlib.suppress(OSError):
            write_atomic(f"{m.state}/nas-summary", text, mode="umask", durable=False, relabel=False)

    # ---- the run -------------------------------------------------------------------------------
    def run(self) -> int:
        m, s = self.m, self.m.settings
        if s["NAS_ENABLED"] != "1":
            m.log(6, "NAS backup not enabled")
            return 0
        if shutil.which("restic") is None:
            m.log(5, "NAS: restic is not installed yet (rpm-ostree: active after the next reboot); skipped")
            return 0
        if not s["NAS_PATH"] or not m.non_empty(m.restic_password):
            m.log(
                4,
                f"NAS: not configured completely (NAS_PATH, {m.restic_password}); re-run 'cst btrfs snapshots nas setup'",
            )
            self.stale_alert("The NAS backup is not configured completely.")
            return 0
        if m.lock(m.nas_lock, wait=False) is None:
            m.log(5, "another NAS run is active; skipped")
            return 0
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, m.trap_stop)
        # Always unmount and unpin at the end, also when the run is stopped (bash's EXIT trap),
        # unless leftovers of a killed run could not be unmounted: then nothing of this run is
        # mounted, and no new run starts until they are gone.
        leftovers = False
        try:
            if not self.unmount():
                leftovers = True
                self.stale_alert(f"A snapshot of an earlier run is still mounted under {m.nas_mnt}.")
                return 0
            self.backups()
        finally:
            if not leftovers:
                self.unmount()
        return 0

    def backups(self) -> None:
        m, s = self.m, self.m.settings
        budget = duration_s(s["NAS_TIMEOUT"])
        if budget is None:
            print(f"{m.front}: NAS_TIMEOUT={s['NAS_TIMEOUT']!r}: not a number", file=sys.stderr, flush=True)
            raise RunStopped
        self.deadline = m.now() + budget
        os.environ["RESTIC_REPOSITORY"] = self.repository
        os.environ["RESTIC_PASSWORD_FILE"] = m.restic_password
        os.environ["RESTIC_CACHE_DIR"] = m.restic_cache
        if not self.reachable():
            m.log(
                5,
                f"NAS {s['NAS_USER']}@{s['NAS_HOST']}:{s['NAS_PORT']} unreachable (ssh {NAS_SSH_ALIAS}); skipping this run",
            )
            self.stale_alert("The NAS was unreachable at the last attempts.")
            return
        if not self.repo_ready():
            return
        # Only locks whose process is gone (a run killed at the time limit).
        self.restic(RESTIC_SHORT_S, ["unlock"])
        if not self.pin():
            self.stale_alert("There was no local snapshot to back up.")
            return
        # device-id-for-hardlinks is an alpha feature flag in restic 0.19, and restic refuses unknown
        # flags: set only while this restic lists it as alpha or beta.
        if features_type(m.capture(["restic", "features"])) in ("alpha", "beta"):
            self.features = ["RESTIC_FEATURES=device-id-for-hardlinks"]
        ok = failed = 0
        for name, snap in zip(self.names, self.snaps, strict=True):
            if self.backup(name, snap):
                ok += 1
            else:
                failed += 1
        self.unmount()  # forget/prune/check don't need them: let them expire again
        if failed:
            m.log(4, f"NAS backup failed for {failed} of {ok + failed} source(s); will retry next run")
            self.stale_alert("Last attempt failed; see journalctl -u btrfs-snapshot-nas.")
            return
        m.write(f"{m.state}/nas-last-ok", f"{m.now()}\n")
        clear_alert(m, AlertKind.NAS)
        clear_alert(m, AlertKind.NAS_PASSWORD)
        self.maintain()
        self.summary()
        m.log(5, f"NAS backup ok: {' '.join(p.rsplit('/', 1)[-1] for p in self.snaps)}")


def nas(m: SnapshotRun, args: Sequence[str]) -> int:
    """The daily NAS backup (btrfs-snapshot-nas.service)."""
    del args
    try:
        return Nas(m).run()
    except Stopped:
        return EXIT_STOPPED
    except RunStopped:
        return 1
