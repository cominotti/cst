# SPDX-License-Identifier: MPL-2.0
"""Replays the traces of the Quint spec of vm's NAS backup against the real code.

vm/tests/quint/nas.qnt models backup_nas and nas_run (vm/application/nas.py), one action per port
call that changes the machine, the repository or the records, or whose answer the code branches on,
with a stop, a kill, the guest and libvirt acting between any two steps. `invowk cmd test quint --traces`
writes runs of it as ITF traces (JSON) into vm/tests/quint/traces. This test replays each trace on
backup_nas over in-process fakes of its ports (built on test_vm_nas.py's FakeNas* fakes), run by run,
driven by a World: the VM's state, each disk's source in the live and the persistent definition, the
overlay files, the repository's snapshots, the torn record and the index.

- every port call the spec labels is recorded with the spec's label and gets the trace's outcome at
  its position (virsh's exit status, restic's answer, a record that fails); what libvirt and restic do
  on their own (which disks a snapshot switched, a pivot that leaves the definition on the overlay,
  the retention's choice) is the spec's state after that step;
- a stop between two steps is raised by the next port call (the code's next step); one in a held
  block (the snapshot call, the torn record) at the block's end; a kill makes that call and every
  later one raise tests' Crash, so the World stays as the kill left it; the guest and a late switch of
  libvirt change the World there;
- after each run, the calls must be the spec's, in its order; the run's end (done, Refused, Stop,
  killed) the spec's; and the World the spec's state, with the overlays each alert named (and their
  recovery kind) and the torn snapshots a message gave to forget by hand.

A trace that does not match fails with the first difference: the spec and the code disagree. What
the replay does not compare is in docs/dev-tools.md, "Model checking with Quint (optional)".
CST_NAS_TRACES names a folder of more traces to replay (written by quint by hand).
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
import contextlib
from dataclasses import dataclass, field
import json
import os
import re
import signal
from typing import Final, override
import unittest

from btrfs.tests.fakes import Crash
from cst_core.domain.errors import ExitStatus, Refused
from cst_core.domain.values import WEEK
from tests.quint.itf import Value, items, number, record, text, trace_states, traces_in, truth
from vm import domain as vm_domain, wiring as vm_wiring
from vm.application import nas as vm_app_nas
from vm.domain import backup as vm_domain_backup
from vm.tests.test_vm_nas import (
    NOW,
    fake_blklist,
    fake_nas_xml,
    FakeNasCommands,
    FakeNasMovingClock,
    FakeNasOut,
    FakeNasRestic,
    FakeNasSettings,
    FakeNasSignals,
    FakeNasStore,
)

HERE = os.path.dirname(os.path.abspath(__file__))
TRACES: Final = os.path.join(HERE, "quint", "traces")
MORE_TRACES: Final = "CST_NAS_TRACES"
VM: Final = "win"
KEEP_NAME: Final = "pinned"
IMAGES: Final = {"vda": "/img/vda.qcow2", "vdb": "/img/vdb.qcow2"}
NAS: Final[vm_domain.NasRepo] = {"host": "sophia", "repo": "/repo", "env": {}}
SILENT: Final = "~"
FAIL: Final = "fail"
OK: Final = "ok"
AGENT: Final = "<channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0' state='connected'/></channel>"
# The spec's VM states as virsh domstate says them.
DOMSTATE: Final = {"running": "running", "paused": "paused", "shutoff": vm_domain.SHUT_OFF}
# The recovery kind of each line the refusal gives an overlay (vm.domain.overlay_recovery).
KINDS: Final = (
    ("live", re.compile(r"^  # (\w+): the VM still runs on ", re.MULTILINE)),
    ("definition", re.compile(r"^  # (\w+): the VM is back on ", re.MULTILINE)),
    ("offline", re.compile(r"^  # (\w+): \S+ is shut off on ", re.MULTILINE)),
    ("unknown", re.compile(r"^  # (\w+): the VM's state or the files could not be read", re.MULTILINE)),
)
# The outcome of the merge back's check: (the live listing fails, the --inactive one fails).
UNREADABLE: Final = {"unreadable": (True, True), "live_unreadable": (True, False), "inactive_unreadable": (False, True)}
# restic's exit status of each outcome of restic_backup that a stop ended once restic had written its
# snapshot: terminated by the stop, or exited 0 just before it.
STOPPED_AFTER_SNAPSHOT: Final = {"stopped_after_commit": -signal.SIGTERM, "stopped_answered": 0}
# Every (label, outcome) of a recorded call in the spec's checked instances: the checked-in traces
# replay each at least once.
SPEC_CALLS: Final = frozenset(
    {
        ("preflight", OK),
        ("preflight", "refused"),
        ("restic_ready", OK),
        ("restic_ready", FAIL),
        *(("snapshot", o) for o in ("ok", "fail_none", "fail_in_place", "timeout_all", "timeout_some")),
        *(("snapshot", o) for o in ("ok_some", "fail_some")),  # nas_partial's (without --atomic): refused
        ("drop_overlay", ""),
        *(("restic_backup", o) for o in ("ok", "incomplete", FAIL, "stopped_after_commit", "stopped_answered")),
        ("check_after", "same"),
        ("check_after", "changed"),
        *(
            (name, o)
            for name in ("record_torn", "forget", "drop_torn", "blockcommit", "remove_overlay", "retention")
            for o in (OK, FAIL)
        ),
        ("find_overlays", ""),
        *(("domstate", o) for o in ("live", "stopped", "unreadable")),
        *(("domblklist", o) for o in (OK, "inactive_unreadable", "live_unreadable", "unreadable")),
        ("alert", ""),
        ("restic_snapshots", OK),
        ("restic_snapshots", FAIL),
        ("write_index", OK),
    }
)


def snap_id(i: int) -> str:
    """restic's id of the spec's snapshot i (the run that made it)."""
    return f"{i:08x}" + "c" * 56


def snap_number(sid: str) -> int:
    return int(sid[:8], 16)


@dataclass(frozen=True, slots=True)
class Machine:
    """What outlives a run, as the spec sees it (the overlay files as (target, run))."""

    vm: str
    live: Mapping[str, int]
    pers: Mapping[str, int]
    files: frozenset[tuple[str, int]]
    repo: frozenset[tuple[int, bool]]
    record: frozenset[int]
    index: frozenset[int]


@dataclass(frozen=True, slots=True)
class Step:
    """One state of a trace: the label of the step that led to it and the spec's state after it."""

    name: str
    target: str
    ident: int
    outcome: str
    stop: bool
    pc: str
    r: int
    end: str
    keep: bool
    quiesce: bool
    machine: Machine
    alerted: frozenset[tuple[str, int, str]]
    told: frozenset[int]


def pairs(value: Value) -> Iterator[list[Value]]:
    for v in items(value):
        yield items(v)


def machine_of(s: Mapping[str, Value]) -> Machine:
    return Machine(
        vm=text(s["vm"]),
        live={t: number(v) for t, v in record(s["live"]).items()},
        pers={t: number(v) for t, v in record(s["pers"]).items()},
        files=frozenset((text(p[0]), number(p[1])) for p in pairs(s["files"])),
        repo=frozenset((number(record(x)["id"]), truth(record(x)["keep"])) for x in items(s["repo"])),
        record=frozenset(number(i) for i in items(s["record"])),
        index=frozenset(number(i) for i in items(s["index"])),
    )


def load_trace(path: str) -> list[Step]:
    return [
        Step(
            name=text(label["name"]),
            target=text(label["target"]),
            ident=number(label["id"]),
            outcome=text(label["outcome"]),
            stop=truth(label["stop"]),
            pc=text(s["pc"]),
            r=number(s["r"]),
            end=text(s["end"]),
            keep=truth(s["keep"]),
            quiesce=truth(s["quiesce"]),
            machine=machine_of(s),
            alerted=frozenset((text(a[0]), number(a[1]), text(a[2])) for a in pairs(s["alerted"])),
            told=frozenset(number(i) for i in items(s["told"])),
        )
        for s, label in trace_states(path)
    ]


def runs(trace: list[Step]) -> Iterator[list[Step]]:
    """The trace after its start, cut into runs: the steps from leaving "idle" back to it, or one idle-to-idle step."""
    run: list[Step] = []
    for step in trace[1:]:
        run.append(step)
        if step.pc == "idle":
            yield run
            run = []
    if run:
        yield run  # the trace ends inside a run


@dataclass
class World:
    """The machine, the repository and the records the fakes serve, in the spec's terms."""

    targets: tuple[str, ...]
    vm: str = "running"
    live: dict[str, int] = field(default_factory=dict)
    pers: dict[str, int] = field(default_factory=dict)
    files: set[tuple[str, int]] = field(default_factory=set)
    repo: dict[int, bool] = field(default_factory=dict)  # snapshot -> made with --keep-tag
    record: list[vm_domain_backup.TornEntry] = field(default_factory=list)
    index: list[vm_domain_backup.IndexEntry] = field(default_factory=list)
    writes: int = 0  # the guest's writes to the disks of a shut-off VM it was started on (fingerprints)

    def take(self, m: Machine) -> None:
        """What libvirt, the guest or the user did, as the spec's state after it has it."""
        self.vm, self.live, self.pers, self.files = m.vm, dict(m.live), dict(m.pers), set(m.files)

    def machine(self) -> Machine:
        return Machine(
            vm=self.vm,
            live=dict(self.live),
            pers=dict(self.pers),
            files=frozenset(self.files),
            repo=frozenset(self.repo.items()),
            record=frozenset(snap_number(e["snapshot"]) for e in self.record),
            index=frozenset(snap_number(e["snapshot"]) for e in self.index if e.get("mode") == "nas"),
        )

    @staticmethod
    def stamp(r: int) -> str:
        return vm_domain_backup.utc_stamp(run_time(r))

    def path(self, t: str, r: int) -> str:
        return IMAGES[t] if r == 0 else f"{IMAGES[t]}{vm_domain.OVERLAY_MARK}{self.stamp(r)}"

    def overlay_of(self, path: str) -> tuple[str, int] | None:
        for t, r in self.files | {(t, r) for t in self.targets for r in (*self.live.values(), *self.pers.values())}:
            if r and self.path(t, r) == path:
                return t, r
        return None

    def sources(self, definition: Mapping[str, int]) -> list[tuple[str, str]]:
        return [(t, self.path(t, definition[t])) for t in self.targets]

    def listed(self) -> str:
        """restic snapshots --json of the VM's tags."""
        tags = [vm_domain.TAG, f"vm:{VM}"]
        return json.dumps(
            [
                {"id": snap_id(i), "tags": tags + ([vm_domain.KEEP_TAG, KEEP_NAME] if keep else [])}
                for i, keep in sorted(self.repo.items())
            ]
        )


def run_time(r: int) -> float:
    return float(NOW + r * WEEK)


class TraceEnd(BaseException):
    """The trace ends inside a run: the code is stopped at its next recorded call."""


class Recorder:
    """The recorded calls of a run with the spec's labels, and the environment's steps between them.

    An environment step applies at the next port call after the recorded step before it, but not
    before the run has read the VM's state (catch_stops: the spec's start), not inside a held block
    (the spec's snapshot and record steps hold their poll and their wait), and not at the second
    listing of a step that reads two (find_overlays, the check after the commits)."""

    def __init__(self, world: World, plan: Sequence[Step], *, truncated: bool) -> None:
        self.world = world
        self.plan = list(plan)
        self.truncated = truncated
        self.pos = 0
        self.calls: list[tuple[str, str, int, str]] = []
        self.started = False
        self.holding = False  # in a held block that freezes the environment (the snapshot's, the record's)
        self.depth = 0  # held blocks entered: a stop that came in them is raised at the outermost one's end
        self.answering = False  # restic's answered (the hold from restic's answer on) is being called
        self.pending_stop = False
        self.held_stop = False
        self.killed = False
        self.ignored = False
        self.merged = False  # the merge back is over: forget and drop_torn are retry_torn's
        self.index_phase = False
        self.after_backup = False
        self.committed = False  # the merge back's domstate said the VM is alive
        self.listing = ""  # the outcome of the recorded listing of a pair

    def environment(self) -> None:
        """The environment's steps planned before the next recorded one."""
        while self.pos < len(self.plan) and self.plan[self.pos].name.startswith(SILENT):
            st = self.plan[self.pos]
            self.pos += 1
            match st.name:
                case "~stop":
                    self.pending_stop = True
                case "~kill":
                    self.killed = True
                case "~guest" | "~late_switch":
                    if st.outcome == "start":
                        self.world.writes += 1
                    self.world.take(st.machine)
                case _:
                    pass  # a decision of the code without a call (~no_torn, ~no_drop)

    def port(self) -> None:
        """Any port call: the environment acts first; a kill or a stop that came is raised here."""
        if not self.started or self.holding:
            return
        self.environment()
        if self.killed:
            raise Crash("killed")
        if self.pending_stop:
            self.pending_stop = False
            if self.depth:  # the hold from restic's answer on: the stop waits for its end
                self.held_stop = True
            elif not self.ignored:  # during the merge back a stop is dropped (signals_ignored)
                raise vm_domain_backup.Stop("stopped by signal SIGTERM")

    def call(self, name: str, target: str = "", ident: int = 0) -> Step:
        """A recorded call: the spec's next step must be it; its outcome is the answer."""
        if name == "restic_ready":
            self.preflight(OK)
        self.port()
        if self.pos >= len(self.plan) and self.truncated:
            raise TraceEnd
        want = self.plan[self.pos] if self.pos < len(self.plan) else None
        got = (name, target, ident)
        if want is None or (want.name, want.target, want.ident) != got:
            raise AssertionError(
                f"call {len(self.calls)}: the code made {got}, the spec {want and (want.name, want.target, want.ident)}"
            )
        self.pos += 1
        self.calls.append((name, target, ident, want.outcome))
        if want.stop:
            self.held_stop = True
        return want

    def preflight(self, outcome: str) -> None:
        """The preflight has no call of its own: it passed when restic is asked, else it refused."""
        if self.started and not self.holding:
            self.environment()
        want = self.plan[self.pos] if self.pos < len(self.plan) else None
        if want is not None and want.name == "preflight":
            if want.outcome != outcome:
                raise AssertionError(f"preflight: the code's {outcome}, the spec's {want.outcome}")
            self.pos += 1
            self.calls.append(("preflight", "", 0, outcome))


class Signals(FakeNasSignals):
    """The stop signals: a stop that came in a held block is raised at the end of the outermost one
    (signals_held). The hold from restic's answer on (entered while restic's answered is called) lets the
    environment act at each port call, as it holds only the stops; the snapshot's and the record's freeze it."""

    def __init__(self, rec: Recorder) -> None:
        super().__init__()
        self.rec = rec

    @override
    def catch_stops(self) -> None:
        self.rec.started = True
        self.rec.port()

    @override
    def held(self) -> contextlib.AbstractContextManager[object]:
        self.rec.port()
        return _Held(self.rec, freezes=not self.rec.answering)

    @override
    @contextlib.contextmanager
    def ignored(self) -> Generator[None]:
        self.rec.ignored = True
        try:
            with super().ignored():
                yield
        finally:
            self.rec.ignored = False
            self.rec.merged = True


class _Held(contextlib.AbstractContextManager[object]):
    def __init__(self, rec: Recorder, *, freezes: bool) -> None:
        self.rec = rec
        self.freezes = freezes
        self.was = False

    @override
    def __enter__(self) -> object:
        self.rec.depth += 1
        if self.freezes:
            self.was, self.rec.holding = self.rec.holding, True
        return self

    @override
    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        self.rec.depth -= 1
        if self.freezes:
            self.rec.holding = self.was
        if self.rec.depth == 0 and self.rec.held_stop and exc_type is None:
            self.rec.held_stop = False
            raise vm_domain_backup.Stop("stopped by signal SIGTERM")


class Store(FakeNasStore):
    """The files: the images, the overlays of the World, the torn record and the index."""

    def __init__(self, rec: Recorder) -> None:
        super().__init__(list(IMAGES.values()))
        self.rec = rec
        self.world = rec.world

    @override
    def is_file(self, path: str) -> bool:
        return self.exists(path)

    @override
    def exists(self, path: str) -> bool:
        self.rec.port()
        o = self.world.overlay_of(path)
        return path in IMAGES.values() if o is None else o in self.world.files

    @override
    def lexists(self, path: str) -> bool:
        return self.exists(path)

    @override
    def fingerprint(self, path: str) -> vm_domain_backup.Fingerprint:
        self.rec.port()
        return (path, self.world.writes)

    @override
    def stage(self, vm: str, xml_text: str, manifest: str) -> str:
        self.rec.port()
        return super().stage(vm, xml_text, manifest)

    @override
    def unstage(self, folder: str) -> None:
        self.rec.port()

    @override
    def remove_overlay(self, path: str) -> str | None:
        o = self.world.overlay_of(path)
        assert o is not None, path
        if self.rec.call("remove_overlay", o[0]).outcome == FAIL:
            return "Device or resource busy"
        self.world.files.discard(o)
        return None

    @override
    def drop(self, path: str) -> None:
        t = next(t for t in self.world.targets if path.startswith(IMAGES[t] + vm_domain.OVERLAY_MARK))
        self.rec.call("drop_overlay", t)
        self.world.files = {o for o in self.world.files if self.world.path(*o) != path}

    @override
    def read_index(self) -> list[vm_domain_backup.IndexEntry]:
        self.rec.port()
        if self.rec.merged:
            self.rec.index_phase = True
        return list(self.world.index)

    @override
    def write_index(self, entries: Sequence[vm_domain_backup.IndexEntry]) -> None:
        mine = [e for e in entries if e.get("snapshot") not in {x["snapshot"] for x in self.world.index}]
        new = mine[0]["snapshot"] if mine else entries[-1]["snapshot"]
        self.rec.call("write_index", "", snap_number(new))
        self.world.index = list(entries)

    @override
    def read_torn(self) -> list[vm_domain_backup.TornEntry]:
        self.rec.port()
        return list(self.world.record)

    @override
    def record_torn(self, entry: vm_domain_backup.TornEntry) -> None:
        if self.rec.call("record_torn", "", snap_number(entry["snapshot"])).outcome == FAIL:
            raise OSError(28, "No space left on device")
        self.world.record.append(entry)

    @override
    def drop_torn(self, entries: Sequence[vm_domain_backup.TornEntry]) -> None:
        ident = snap_number(entries[0]["snapshot"]) if len(entries) == 1 and not self.rec.merged else 0
        if self.rec.call("drop_torn", "retry" if self.rec.merged else "", ident).outcome == FAIL:
            raise OSError(28, "No space left on device")
        gone = {e["snapshot"] for e in entries}
        self.world.record = [e for e in self.world.record if e["snapshot"] not in gone]


class Restic(FakeNasRestic):
    """The repository: its snapshots in the World."""

    def __init__(self, rec: Recorder, run: int) -> None:
        super().__init__()
        self.rec = rec
        self.world = rec.world
        self.run_number = run

    @override
    def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        del env, timeout
        match args[0]:
            case "cat":
                ok = self.rec.call("restic_ready").outcome == OK
                return (0, "{}", "") if ok else (1, "", "Fatal: unable to open the repository")
            case "snapshots":
                target = "index" if self.rec.index_phase else "torn"
                if self.rec.call("restic_snapshots", target).outcome == FAIL:
                    return 1, "", "Fatal: unable to open the repository"
                return 0, self.world.listed(), ""
            case "forget":
                return self.forget(args)
            case _:
                raise AssertionError(f"restic {args}: not a call the spec has")

    def forget(self, args: Sequence[str]) -> tuple[int, str, str]:
        """The retention (--keep-weekly: the spec's choice of what goes), or one snapshot forgotten."""
        if "--keep-weekly" in args:
            st = self.rec.call("retention")
            repo = dict(st.machine.repo)
        else:
            i = snap_number(args[-1])
            st = self.rec.call("forget", "retry" if self.rec.merged else "", i)
            repo = {k: v for k, v in self.world.repo.items() if k != i}
        if st.outcome == FAIL:
            return 1, "", "Fatal: repository is already locked"
        self.world.repo = repo
        return 0, "", ""

    @override
    def backup(
        self,
        env: Mapping[str, str],
        args: Sequence[str],
        timeout: int,
        out: object,
        *,
        answered: Callable[[], None] | None = None,
    ) -> tuple[int, vm_domain_backup.ResticSummary]:
        del env, timeout, out
        outcome = self.rec.call("restic_backup").outcome
        self.rec.after_backup = True
        tags = args[args.index("--tag") + 1].split(",")
        if outcome == FAIL:
            return 1, {}
        self.world.repo[self.run_number] = vm_domain.KEEP_TAG in tags
        summary: vm_domain_backup.ResticSummary = {
            "snapshot_id": snap_id(self.run_number),
            "total_bytes_processed": 1024,
            "data_added": 512,
        }
        if outcome in STOPPED_AFTER_SNAPSHOT:
            # The stop ended restic once it had written its snapshot: Restic.backup carries the id.
            raise vm_domain_backup.StopAfterSnapshot(
                "stopped by signal SIGTERM", STOPPED_AFTER_SNAPSHOT[outcome], summary
            )
        if answered is not None:
            self.rec.answering = True
            try:
                answered()
            finally:
                self.rec.answering = False
        return (3 if outcome == "incomplete" else 0), summary


class Settings(FakeNasSettings):
    @override
    def number(self, key: str) -> int:
        return {"VM_NAS_KEEP_WEEKLY": 2}.get(key, 0)


class Clock(FakeNasMovingClock):
    """now: the run's week; monotonic moves on with sleep (a poll until a deadline ends)."""

    def __init__(self, r: int) -> None:
        super().__init__()
        self.r = r

    @override
    def now(self) -> float:
        return run_time(self.r)


class Virsh:
    """libvirt over the World: the definitions, the listings, the snapshot and the commit with the spec's outcomes."""

    def __init__(self, rec: Recorder, *, quiesce: bool) -> None:
        self.rec = rec
        self.world = rec.world
        self.quiesce = quiesce

    def xml(self, definition: Mapping[str, int], *, agent: bool) -> str:
        text_ = fake_nas_xml(self.world.sources(definition))
        return text_.replace("</devices>", AGENT + "</devices>") if agent else text_

    def listing(self, *, inactive: bool) -> str:
        w = self.world
        return fake_blklist(w.sources(w.pers if inactive or w.vm == "shutoff" else w.live))

    def __call__(self, args: Sequence[str]) -> tuple[int, str, str]:
        handlers: Mapping[str, Callable[[Sequence[str]], tuple[int, str, str]]] = {
            "dumpxml": self.dumpxml,
            "domstate": self.domstate,
            "domblklist": self.domblklist,
            "snapshot-create-as": self.snapshot,
            "blockcommit": self.blockcommit,
            "list": self.quiet,
            "domfsthaw": self.quiet,
        }
        if args[0] not in handlers:
            raise AssertionError(f"virsh {args}: not a call the spec has")
        return handlers[args[0]](args)

    def quiet(self, args: Sequence[str]) -> tuple[int, str, str]:
        """virsh list --with-managed-save (no managed save) and domfsthaw: no recorded call."""
        del args
        self.rec.port()
        return 0, "", ""

    def dumpxml(self, args: Sequence[str]) -> tuple[int, str, str]:
        self.rec.port()
        w = self.world
        return 0, self.xml(w.pers, agent=False) if "--inactive" in args else self.xml(w.live, agent=self.quiesce), ""

    def domstate(self, args: Sequence[str]) -> tuple[int, str, str]:
        """The merge back's (recorded with its outcome), the shut-off VM's check after the read, or one not recorded."""
        del args
        rec = self.rec
        if rec.ignored:
            outcome = rec.call("domstate").outcome
            rec.committed = outcome == "live"
            if outcome == "unreadable":
                return 1, "", "error: failed to get domain state"
        elif rec.after_backup:
            rec.call("check_after")
        else:
            rec.port()
        return 0, DOMSTATE[self.world.vm] + "\n", ""

    def domblklist(self, args: Sequence[str]) -> tuple[int, str, str]:
        """In the merge back, the pairs of find_overlays and of the check after the commits: the live listing is
        the recorded call, the --inactive one gets its outcome; else the snapshot's poll (not recorded)."""
        rec = self.rec
        inactive = "--inactive" in args
        if not rec.ignored:
            rec.port()
        elif inactive:  # the second listing of the pair: no environment step before it
            if UNREADABLE.get(rec.listing, (False, False))[1]:
                return 1, "", "error: failed to get domain"
        else:
            rec.listing = rec.call("domblklist" if rec.committed else "find_overlays").outcome
            if UNREADABLE.get(rec.listing, (False, False))[0]:
                return 1, "", "error: failed to get domain"
        return 0, self.listing(inactive=inactive), ""

    def snapshot(self, args: Sequence[str]) -> tuple[int, str, str]:
        st = self.rec.call("snapshot", "quiesced" if "--quiesce" in args else "crash")
        self.world.take(st.machine)
        if st.outcome.startswith("timeout"):
            return int(ExitStatus.TIMED_OUT), "", ""
        return (0, "", "") if st.outcome in ("ok", "ok_some") else (1, "", "error: snapshot failed")

    def blockcommit(self, args: Sequence[str]) -> tuple[int, str, str]:
        st = self.rec.call("blockcommit", args[2])
        if st.outcome == FAIL:
            return 1, "", "error: block commit timed out"
        self.world.take(st.machine)
        return 0, "", ""


def cfg() -> vm_domain.VmSettings:
    return vm_wiring.vm_settings({"CST_PROG": "cst vm", "CST_VM_NONROOT_OK": "1"})


@dataclass
class Ran:
    end: str
    message: str
    alerts: list[tuple[str, str]]
    calls: list[tuple[str, str, int, str]]


def replay_run(world: World, run: Sequence[Step]) -> Ran:
    """backup_nas once, as the spec's run (its first step is the start) has it."""
    start = run[0]
    rec = Recorder(world, run[1:], truncated=run[-1].pc != "idle")
    virsh = Virsh(rec, quiesce=start.quiesce)
    signals = Signals(rec)
    alerts: list[tuple[str, str]] = []

    def alert(title: str, body: str) -> None:
        rec.call("alert")
        alerts.append((title, body))

    port = vm_app_nas.NasBackup(
        commands=FakeNasCommands(virsh, signals),
        store=Store(rec),
        settings=Settings(),
        restic=Restic(rec, start.r),
        signals=signals,
        lock=lambda _vm: contextlib.nullcontext(),
        alert=alert,
        clock=Clock(start.r),
        out=FakeNasOut(),
        root=True,
    )
    end, message = "ok", ""
    try:
        vm_app_nas.backup_nas(cfg(), VM, KEEP_NAME if start.keep else None, port, quiesce=start.quiesce)
    except Refused as e:
        end, message = "refused", str(e)
        rec.preflight("refused")
    except vm_domain_backup.Stop as e:
        end, message = "stopped", str(e)
    except Crash:
        end = "killed"
    except TraceEnd:
        end = ""
    rec.environment()  # the spec's steps after the code's last call (none but "~" ones)
    left = rec.plan[rec.pos :]
    if left:
        raise AssertionError(
            f"the code ended ({end}: {message[:300]}) where the spec goes on: {[(s.name, s.target, s.outcome) for s in left]}"
        )
    return Ran(end, message, alerts, rec.calls)


def named_overlays(message: str) -> set[tuple[str, str]]:
    """(target, kind) of each overlay the refusal gives the commands to put back."""
    return {(t, kind) for kind, pattern in KINDS for t in pattern.findall(message)}


def replay(case: unittest.TestCase, path: str) -> set[tuple[str, str]]:
    """Replay the trace at path on the code; the (label, outcome) of every recorded call."""
    trace = load_trace(path)
    first = trace[0]
    world = World(tuple(first.machine.live))
    world.take(first.machine)
    seen: set[tuple[str, str]] = set()
    for k, run in enumerate(runs(trace)):
        where = f"{os.path.basename(path)}, run {k + 1} (trace state {trace.index(run[-1])})"
        last = run[-1]
        if run[0].name != "~start":
            for st in run:  # the user or libvirt between runs
                world.take(st.machine)
            continue
        ran = replay_run(world, run)
        seen |= {(c[0], c[3]) for c in ran.calls}
        want_calls = [(s.name, s.target, s.ident, s.outcome) for s in run[1:] if not s.name.startswith(SILENT)]
        case.assertEqual(ran.calls, want_calls, where)
        if last.pc == "idle":
            case.assertEqual(ran.end, last.end, f"{where}: {ran.message[:500]}")
        case.assertEqual(world.machine(), last.machine, where)
        # The alert is a recorded call; the refusal after it names the overlays the spec's alert named.
        if ran.alerts:
            alerted = {(t, kind) for t, rr, kind in last.alerted if rr == run[0].r}
            case.assertEqual(named_overlays(ran.message), alerted, f"{where}: {ran.message}")
        for i in last.told - run[0].told:
            case.assertIn(f"restic forget {snap_id(i)}", ran.message, where)
    return seen


def trace_files() -> list[str]:
    paths = sorted(traces_in(TRACES))
    more = os.environ.get(MORE_TRACES, "")
    if more:
        paths += sorted(traces_in(more))
    return paths


class NasReplayTest(unittest.TestCase):
    def test_every_trace_replays_on_the_code(self) -> None:
        paths = trace_files()
        self.assertTrue(paths, f"no trace in {TRACES}")
        for path in paths:
            with self.subTest(trace=os.path.basename(path)):
                replay(self, path)

    def test_the_traces_replay_every_recorded_call_of_the_spec(self) -> None:
        seen: set[tuple[str, str]] = set()
        for path in sorted(traces_in(TRACES)):
            seen |= replay(self, path)
        self.assertEqual(sorted(SPEC_CALLS - seen), [], "no checked-in trace replays these")
        self.assertEqual(sorted(seen - SPEC_CALLS), [], "SPEC_CALLS lacks these")


if __name__ == "__main__":
    unittest.main()
