# SPDX-License-Identifier: MPL-2.0
"""Replays the traces of the Quint spec of docker-volumes-nocow against the real code.

btrfs/tests/quint/docker_volumes_nocow.qnt models install's mark (with btrfs-setup.sh's touch of
the flag after it) and each boot's migrate, one action per call that changes the disk or the
helper's files, with a crash after any step. `invowk cmd test quint --traces` writes runs of it as
ITF traces (JSON) into btrfs/tests/quint/traces. This test replays each trace on the use case over
its real adapters (the fakes of test_docker_volumes_nocow: NOCOW ioctls, commands, os.sync), run
by run:

- the trace's start becomes the volumes on disk, the folder's +C and the flag;
- a run (a boot, an install, Docker recreating a volume) gets the faults the trace picked, each at
  the position of its call in the run (Docker running, no room, chcon, cp failing, killed or
  copying wrong; a rename, the flag, the counter or its reset failing) and dies where the trace
  crashes; a power loss also spoils the copies the spec says it loses;
- every call the code makes that changes the volumes or the helper's files is recorded with the
  spec's label: the calls of a run must be the spec's, in the spec's order;
- after each run, what is on disk (each volume's _data, twin and old copies; +C on the folder;
  the flag; the counter) must be the spec's state.

A trace that does not match fails with the first difference: the spec and the code disagree. What
the replay does not compare is in docs/dev-tools.md, "Model checking with Quint (optional)".
CST_NOCOW_TRACES names a folder of more traces to replay (written by quint by hand).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
import contextlib
from dataclasses import dataclass, field
import errno
import os
import shutil
from types import SimpleNamespace
from typing import Final, override
from unittest import mock

from btrfs.docker_volumes_nocow.adapters import DockerDaemons, LocalVolumeTree, NocowStateFiles
from btrfs.docker_volumes_nocow.application import DockerVolumesNocow
from btrfs.docker_volumes_nocow.domain import DATA_DIR, NEW_SUFFIX, OLD_MARK, NocowCommand, NocowPaths
from btrfs.tests.fakes import Crash, FakeSystem, entries, tree
from btrfs.tests.test_docker_volumes_nocow import Base, fresh
from cst_core.adapters import SystemClockAdapter
from cst_core.adapters import nocow as core_nocow
from cst_core.domain.shapes import json_object, parse_json
from cst_core.ports import CommandResult, CommandRunnerPort
from tests.fault_injection import stub_fsync

HERE = os.path.dirname(os.path.abspath(__file__))
TRACES: Final = os.path.join(HERE, "quint", "traces")
MORE_TRACES: Final = "CST_NOCOW_TRACES"
# Small volumes: two files with data (a partial copy lacks one) and an empty one.
VOLUME_FILES: Final = {"db/1": b"\x01" * 4096, "db/empty": b"", "README": b"hello volume\n"}
SPOILED: Final = "README"  # the file a lost copy lacks
LABEL: Final = b"system_u:object_r:container_file_t:s0"
SILENT: Final = "~"
FAIL: Final = "fail"
# The recorded calls whose "fail" outcome this test injects (an OSError, at that call's position).
INJECTED: Final = frozenset({"request_migration", "save_attempts", "remove_flag", "rename_new_data", "rename_old_data"})
# Every (label, outcome) of a recorded call in the spec: the checked-in traces replay each at least once.
SPEC_CALLS: Final = frozenset(
    {
        ("rename_old_data", "ok"),
        ("rename_old_data", FAIL),
        ("remove_old", ""),
        ("remove_new", ""),
        ("save_attempts", "ok"),
        ("save_attempts", FAIL),
        ("request_migration", "ok"),
        ("request_migration", FAIL),
        ("forget_attempts", ""),
        ("set_nocow_vols", ""),
        ("touch_flag", ""),
        ("remove_attempts", ""),
        ("remove_flag", "ok"),
        ("remove_flag", FAIL),
        ("make_twin", ""),
        ("reference_label", "ok"),
        ("reference_label", FAIL),
        ("set_nocow_new", ""),
        ("copy", "ok"),
        ("copy", FAIL),
        ("copy", "mismatch"),
        ("copy", "killed"),
        ("sync", ""),
        ("rename_data_old", ""),
        ("rename_new_data", "ok"),
        ("rename_new_data", FAIL),
    }
)

type Value = bool | int | str | list[Value] | dict[str, Value]
type VolumeState = tuple[str, str, int]  # _data's content, the twin's, the number of old copies


def itf(value: object) -> Value:
    """An ITF value as Python: {"#bigint": "N"} an int, {"#map": [[k, v], ...]} a dict, records and lists as they are."""
    if isinstance(value, bool | str):
        return value
    if isinstance(value, list):
        return [itf(v) for v in value]
    if not json_object(value):
        raise AssertionError(f"not an ITF value: {value!r}")
    if "#bigint" in value:
        return int(str(value["#bigint"]))
    if "#map" in value:
        pairs = value["#map"]
        if not isinstance(pairs, list):
            raise AssertionError(f"not an ITF map: {value!r}")
        out: dict[str, Value] = {}
        for pair in pairs:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise AssertionError(f"not an ITF map entry: {pair!r}")
            out[str(itf(pair[0]))] = itf(pair[1])
        return out
    return {k: itf(v) for k, v in value.items() if not k.startswith("#")}


def record(value: Value) -> dict[str, Value]:
    if not isinstance(value, dict):
        raise AssertionError(f"not a record: {value!r}")
    return value


def text(value: Value) -> str:
    if not isinstance(value, str):
        raise AssertionError(f"not a string: {value!r}")
    return value


def number(value: Value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionError(f"not an int: {value!r}")
    return value


def truth(value: Value) -> bool:
    if not isinstance(value, bool):
        raise AssertionError(f"not a bool: {value!r}")
    return value


def items(value: Value) -> list[Value]:
    if not isinstance(value, list):
        raise AssertionError(f"not a list: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class Disk:
    """What is on disk, as the spec sees it."""

    vols: Mapping[str, VolumeState]
    vols_nocow: bool
    flag: bool
    attempts: int


@dataclass(frozen=True, slots=True)
class Step:
    """One state of a trace: the label of the step that led to it and what the spec says is on disk."""

    name: str
    vol: str
    outcome: str
    pc: str
    disk: Disk
    todo: tuple[str, ...]


def disk_of(s: dict[str, Value]) -> Disk:
    """The part of the spec's state that is on disk."""
    vols = {
        v: (text(record(x)["data"]), text(record(x)["twin"]), number(record(x)["olds"]))
        for v, x in record(s["vols"]).items()
    }
    return Disk(vols, truth(s["volsNocow"]), truth(s["flag"]), number(s["attempts"]))


def load_trace(path: str) -> list[Step]:
    with open(path, encoding="utf-8") as f:
        doc = parse_json(f.read())
    if not json_object(doc) or not isinstance(doc.get("states"), list):
        raise AssertionError(f"{path}: not an ITF trace")
    out = []
    for raw in items(itf(doc["states"])):
        state = record(raw)
        s = next(record(v) for k, v in state.items() if k.endswith("::s"))
        label = next(record(v) for k, v in state.items() if k.endswith("::step"))
        out.append(
            Step(
                text(label["name"]),
                text(label["vol"]),
                text(label["outcome"]),
                text(s["pc"]),
                disk_of(s),
                tuple(text(t) for t in items(s["todo"])),
            )
        )
    return out


def runs(trace: list[Step]) -> Iterator[list[Step]]:
    """The trace after its start, cut into runs: the steps from leaving "idle" back to it, or one idle-to-idle step."""
    run: list[Step] = []
    for step in trace[1:]:
        run.append(step)
        if step.pc == "idle":
            yield run
            run = []
    if run:
        yield run  # the trace ends inside a run: replayed as a crash at its end


def observable(run: list[Step]) -> list[Step]:
    """The steps of a run that the code makes as a recorded call."""
    return [st for st in run if not st.name.startswith(SILENT)]


class Recorder:
    """Every mutating call of a run, with the spec's label; the crash and the injected faults of the trace."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.plan: list[Step] = []  # the run's recorded steps, as the spec has them
        self.crash_after: int | None = None

    def step[T](self, name: str, vol: str, fn: Callable[[], T]) -> T:
        k = len(self.calls)
        if self.crash_after is not None and k >= self.crash_after:
            raise Crash(f"killed before {name} {vol}")
        self.calls.append((name, vol))
        want = self.plan[k] if k < len(self.plan) else None
        if want is not None and (want.name, want.vol, want.outcome) == (name, vol, FAIL) and name in INJECTED:
            raise OSError(errno.EIO, "Input/output error (injected)")
        return fn()


class Where:
    """A path below the volumes folder, as the spec names it: (volume, "data" | "twin" | "old" | "folder" | "")."""

    def __init__(self, volumes: str) -> None:
        self.volumes = volumes

    def __call__(self, path: str) -> tuple[str, str]:
        rel = os.path.relpath(path, self.volumes)
        if rel == ".":
            return "", "folder"
        vol, _, leaf = rel.partition(os.sep)
        if leaf == DATA_DIR:
            return vol, "data"
        if leaf == DATA_DIR + NEW_SUFFIX:
            return vol, "twin"
        if leaf.startswith(DATA_DIR + OLD_MARK):
            return vol, "old"
        return vol, ""


RENAMES: Final = {
    ("data", "old"): "rename_data_old",
    ("twin", "data"): "rename_new_data",
    ("old", "data"): "rename_old_data",
}


class RecordedTree(LocalVolumeTree):
    """LocalVolumeTree whose mutating calls go through the Recorder (scan's fix is not recorded: the spec has
    no files), and whose volume folders come in the spec's order (sorted names)."""

    def __init__(self, paths: NocowPaths, runner: CommandRunnerPort, rec: Recorder) -> None:
        super().__init__(paths, runner)
        self.rec, self.where = rec, Where(paths.volumes)

    @override
    def volume_folders(self) -> list[tuple[str, str]]:
        return sorted(super().volume_folders())

    @override
    def set_nocow(self, path: str, *, on: bool) -> None:
        vol, kind = self.where(path)
        name = {"folder": "set_nocow_vols", "twin": "set_nocow_new"}.get(kind, f"set_nocow_{kind}")
        self.rec.step(name, vol, lambda: super(RecordedTree, self).set_nocow(path, on=on))

    @override
    def make_twin(self, data: str, new: str) -> None:
        self.rec.step("make_twin", self.where(new)[0], lambda: super(RecordedTree, self).make_twin(data, new))

    @override
    def reference_label(self, data: str, new: str) -> None:
        self.rec.step(
            "reference_label", self.where(new)[0], lambda: super(RecordedTree, self).reference_label(data, new)
        )

    @override
    def copy(self, data: str, new: str) -> CommandResult:
        return self.rec.step("copy", self.where(new)[0], lambda: super(RecordedTree, self).copy(data, new))

    @override
    def remove_tree(self, path: str, *, ignore_errors: bool = False) -> None:
        vol, kind = self.where(path)
        name = {"twin": "remove_new", "old": "remove_old"}.get(kind, f"remove_{kind}")
        self.rec.step(name, vol, lambda: super(RecordedTree, self).remove_tree(path, ignore_errors=ignore_errors))

    @override
    def rename(self, src: str, dst: str) -> None:
        vol, a = self.where(src)
        b = self.where(dst)[1]
        name = RENAMES.get((a, b), f"rename_{a}_{b}")
        self.rec.step(name, vol, lambda: super(RecordedTree, self).rename(src, dst))

    @override
    def sync(self) -> None:
        self.rec.step("sync", "", super().sync)


class RecordedState(NocowStateFiles):
    """NocowStateFiles whose writes of the flag and the counter go through the Recorder."""

    def __init__(self, paths: NocowPaths, rec: Recorder) -> None:
        super().__init__(paths)
        self.rec = rec

    @override
    def request_migration(self) -> None:
        self.rec.step("request_migration", "", super().request_migration)

    @override
    def remove_flag(self) -> None:
        self.rec.step("remove_flag", "", super().remove_flag)

    @override
    def save_attempts(self, count: int) -> None:
        self.rec.step("save_attempts", "", lambda: super(RecordedState, self).save_attempts(count))

    @override
    def remove_attempts(self) -> None:
        self.rec.step("remove_attempts", "", super().remove_attempts)

    @override
    def forget_attempts(self) -> None:
        self.rec.step("forget_attempts", "", super().forget_attempts)


@dataclass
class Faults:
    """What one run's fakes do, from the labels the trace picked."""

    busy: bool = False
    no_room: list[bool] = field(default_factory=list[bool])  # free_bytes' answers, in the order of todo
    chcon_fails: set[str] = field(default_factory=set[str])
    copies: dict[str, str] = field(default_factory=dict[str, str])  # volume -> "fail" | "mismatch" | "killed"


class TraceSystem(FakeSystem):
    """FakeSystem with the faults of the run being replayed."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.faults = Faults()
        self.cp = self.faulty_copy

    @override
    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        args = list(argv)
        if args[0] == "systemctl":
            self.docker_state = "active" if self.faults.busy else "inactive"
        if args[0] == "btrfs":
            self.free = 0 if (self.faults.no_room and self.faults.no_room.pop(0)) else 1 << 50
        if (
            args[0] == "chcon"
            and len(args) == 4
            and os.path.basename(os.path.dirname(args[3])) in self.faults.chcon_fails
        ):
            self.calls.append(args)
            return CommandResult(1, "", "chcon: Operation not permitted\n", argv=tuple(args))
        return super().run(argv, **kw)

    def faulty_copy(self, src: str, dst: str) -> tuple[int, str]:
        match self.faults.copies.get(os.path.basename(os.path.dirname(src))):
            case "fail":
                with contextlib.suppress(Crash):
                    self.copy_into(src, dst, stop_after=1)
                return 1, "cp: No space left on device"
            case "mismatch":
                self.copy(src, dst)
                os.remove(os.path.join(dst, SPOILED))
                return 0, ""
            case "killed":
                self.copy_into(src, dst, stop_after=1)  # raises Crash
                return 0, ""
            case _:
                return self.copy(src, dst)


class TraceOutput:
    """NocowOutputPort that keeps what the helper says."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def say(self, line: str) -> None:
        self.lines.append(line)

    def log(self, prio: int, msg: str) -> None:
        self.lines.append(f"<{prio}>{msg}")

    def notify(self, title: str, body: str) -> None:
        self.lines.append(f"notify: {title}: {body}")


class ReplayBase(Base):
    @override
    def setUp(self) -> None:
        super().setUp()
        stub_fsync(self)
        self.trace_fs = TraceSystem(self.tmp)
        self.trace_fs.notify = self.notify
        self.fs = self.trace_fs  # Base's helpers (fill) use this one too
        self.rec = Recorder()
        self.service = DockerVolumesNocow(
            paths=self.paths,
            tree=RecordedTree(self.paths, self.fs, self.rec),
            state=RecordedState(self.paths, self.rec),
            daemon=DockerDaemons(self.paths, self.fs),
            out=TraceOutput(),
            clock=SystemClockAdapter(),
        )
        # Base's patches point at its own FakeSystem: point them at this one.
        self.enterContext(mock_ioctls(self.fs))
        self.names: list[str] = []
        self.original: dict[str, tuple[object, ...]] = {}

    # ---- the disk as the spec sees it --------------------------------------------------------------
    def content(self, top: str) -> str:
        """What a folder holds: "absent", "empty", "cow" (the original), "copy" (the original, NOCOW) or "partial"."""
        if not os.path.lexists(top):
            return "absent"
        if not os.listdir(top):
            return "empty"
        if tree(top) != self.original:
            return "partial"
        nocow = [
            self.fs.nocow(p) for p, st in entries(top) if os.path.isfile(p) and not os.path.islink(p) and st.st_size
        ]
        if all(nocow):
            return "copy"
        return "cow" if not any(nocow) else "partial"

    def disk(self) -> Disk:
        vols: dict[str, VolumeState] = {}
        for name in self.names:
            d = os.path.join(self.vols, name)
            olds = [x for x in os.listdir(d) if x.startswith(DATA_DIR + OLD_MARK)]
            for x in olds:
                self.assertEqual(self.content(os.path.join(d, x)), "cow", f"{name}/{x} is not the original")
            vols[name] = (
                self.content(os.path.join(d, DATA_DIR)),
                self.content(os.path.join(d, DATA_DIR + NEW_SUFFIX)),
                len(olds),
            )
        attempts = 0
        if os.path.exists(self.attempts):
            with open(self.attempts, encoding="utf-8") as f:
                attempts = int(f.read().strip() or 0)
        return Disk(vols, self.fs.nocow(self.vols), os.path.exists(self.flag), attempts)

    # ---- set up and replay -------------------------------------------------------------------------
    def make_volume(self, name: str, *, nocow: bool) -> None:
        """name's _data, the original (the test's own set-up: not recorded)."""
        d = os.path.join(self.vols, name, DATA_DIR)
        if os.path.lexists(d):
            shutil.rmtree(d)
        else:
            os.mkdir(os.path.join(self.vols, name))
        self.fill(d, nocow=nocow, data=VOLUME_FILES)
        self.fs.label(d, LABEL)
        for p, _ in entries(d):
            self.fs.label(p, LABEL)

    def start(self, first: Step) -> None:
        self.names = sorted(first.disk.vols)
        probe = os.path.join(self.tmp, "original")
        self.fill(probe, nocow=False, data=VOLUME_FILES)
        self.original = tree(probe)
        for name, (data, _, _) in first.disk.vols.items():
            self.make_volume(name, nocow=data == "copy")
        self.fs.set(self.vols, nocow=first.disk.vols_nocow)
        if first.disk.flag:
            open(self.flag, "a").close()
        self.assertEqual(self.disk(), first.disk, "the start")

    def faults_of(self, run: list[Step]) -> Faults:
        f = Faults(busy=run[0].name == "~postpone")
        todo = next((st.todo for st in run if st.name == "set_nocow_vols" and st.pc == "vol_start"), ())
        no_room = {st.vol for st in run if st.name == "~no_room"}
        f.no_room = [v in no_room for v in todo]
        f.chcon_fails = {st.vol for st in run if st.name == "reference_label" and st.outcome == FAIL}
        f.copies = {st.vol: st.outcome for st in run if st.name == "copy" and st.outcome != "ok"}
        return f

    def lose_unsynced(self, before: Disk, after: Disk) -> None:
        """A power loss: each copy the spec says it lost (a copy before, partial after) lacks a file."""
        for name, (data, twin, _) in after.vols.items():
            was_data, was_twin, _ = before.vols[name]
            for leaf, was, now in ((DATA_DIR, was_data, data), (DATA_DIR + NEW_SUFFIX, was_twin, twin)):
                if (was, now) == ("copy", "partial"):
                    os.remove(os.path.join(self.vols, name, leaf, SPOILED))

    def replay_run(self, before: Step, run: list[Step], where: str) -> None:
        first, last = run[0], run[-1]
        expected = observable(run)
        self.rec.calls, self.rec.plan = [], expected
        crashed = any(st.name in ("~crash", "~power_loss") for st in run) or last.pc != "idle"
        killed = any(st.name == "copy" and st.outcome == "killed" for st in run)
        self.rec.crash_after = len(expected) if crashed and not killed else None
        self.trace_fs.faults = self.faults_of(run)
        if first.name == "~recreate":  # Docker made again: the folder without +C, a volume created meanwhile
            self.fs.set(self.vols, nocow=False)
            if last.disk.vols[first.vol] != self.disk().vols[first.vol]:
                self.make_volume(first.vol, nocow=False)
        elif first.name == "~install":  # cst btrfs install: mark, then btrfs-setup.sh's touch on a non-zero exit
            with contextlib.suppress(Crash):
                if self.service.execute(NocowCommand.MARK) != 0:
                    self.rec.step("touch_flag", "", lambda: open(self.flag, "a").close())
        elif first.name in ("~boot", "~postpone"):
            with contextlib.suppress(Crash):
                self.service.execute(NocowCommand.MIGRATE)
        else:
            self.fail(f"{where}: a run cannot start with {first.name}")
        for i, st in enumerate(run):
            if st.name == "~power_loss":
                self.lose_unsynced((run[i - 1] if i else before).disk, st.disk)
        self.assertEqual(self.rec.calls, [(st.name, st.vol) for st in expected], f"{where}: the calls of the run")
        self.assertEqual(self.disk(), last.disk, f"{where}: the disk after the run")

    def replay(self, path: str) -> None:
        trace = load_trace(path)
        self.start(trace[0])
        at, before = 1, trace[0]
        for run in runs(trace):
            self.replay_run(before, run, f"{os.path.basename(path)} states {at}..{at + len(run) - 1} ({run[0].name})")
            at, before = at + len(run), run[-1]


def mock_ioctls(fs: FakeSystem) -> contextlib.ExitStack:
    """The NOCOW ioctls, getxattr and os.sync of the code under test, on fs."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(core_nocow, "fcntl", SimpleNamespace(ioctl=fs.ioctl)))
    stack.enter_context(mock.patch.object(os, "getxattr", fs.getxattr))
    stack.enter_context(mock.patch.object(os, "sync", fs.sync))
    return stack


def traces_in(folder: str) -> list[str]:
    return [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".itf.json")]


def trace_files() -> list[str]:
    more = os.environ.get(MORE_TRACES)
    return sorted([*traces_in(TRACES), *(traces_in(more) if more is not None and more != "" else [])])


class ReplayTest(ReplayBase):
    def test_the_checked_in_traces_cover_every_call_of_the_spec(self) -> None:
        seen = {(st.name, st.outcome) for path in traces_in(TRACES) for st in load_trace(path)}
        self.assertEqual(SPEC_CALLS - seen, set())

    def test_every_trace_matches_the_code(self) -> None:
        files = trace_files()
        self.assertTrue(files, TRACES)
        for path in files:
            with self.subTest(trace=os.path.basename(path)):
                fresh(self)
                self.replay(path)
