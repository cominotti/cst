# SPDX-License-Identifier: MPL-2.0
"""The contract of the game profile's adapters (cpu/game/adapters) and of the Faulty* ones the tests inject
(cpu/tests/fakes.py): each failure a Faulty adapter stages has the shape the real one gives (run on the real
adapter in a temporary folder, a full disk staged with RLIMIT_FSIZE 0), and the pins of what the real adapters
pass on: systemctl's argv and options, the files' encoding and modes, the run lock, the order of the boxes and
the output's lines."""

from collections.abc import Callable, Sequence
import contextlib
import fcntl
import io
import os
import shutil
import tempfile
import threading
import unittest
from typing import override

from cpu.game.adapters.files import GameLeftoversFile, GameRunLock, GameStateFiles, GameUnitFiles
from cpu.game.adapters.kernel import GameKernelFiles
from cpu.game.adapters.output import GameStreamOutput
from cpu.game.adapters.power import GameSysfsFiles
from cpu.game.adapters.profile import machine_ports
from cpu.game.adapters.systemd import GameSystemctl
from cpu.game.domain import GamePaths
from cpu.tests.fakes import (
    FaultyKernelFiles,
    FaultyLeftoversFile,
    FaultyStateFiles,
    FaultySysfsFiles,
    FaultyUnitFiles,
)
from cst_core.ports import CommandResult, OutputMode
from tests.fault_injection import FaultMode, FaultPlan, Killed, no_file_may_grow


class RecordingRunner:
    """A command runner that answers exit 0 and keeps each call with its options."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        self.calls.append((list(argv), kw))
        return CommandResult(0, "out\n", "err\n")


class Case(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-game-ports-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def put(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def got(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    def failing(self, mode: FaultMode = FaultMode.FAIL) -> FaultPlan:
        """A plan whose first point fails (mode)."""
        return FaultPlan().arm((mode, 1))


class FailureShapeTest(Case):
    """Each failure a Faulty adapter stages, beside the real adapter's on a full disk (or a missing file): the
    same exception (or answer), and the file as it was."""

    def full_write(self, real: Callable[[], object], faulty: Callable[[], object], path: str) -> None:
        self.put(path, b"before\n")
        with no_file_may_grow(), self.assertRaises(OSError):
            real()
        self.assertEqual(self.got(path), b"before\n")
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), [os.path.basename(path)])  # no temporary file
        with self.assertRaises(OSError):
            faulty()
        self.assertEqual(self.got(path), b"before\n")

    def test_an_append_that_fails_leaves_the_file(self) -> None:
        f = self.path("run", "game.state")
        self.full_write(
            lambda: GameStateFiles().append(f, "prop a\n"), lambda: FaultyStateFiles(self.failing()).append(f, "x"), f
        )

    def test_a_replace_that_fails_leaves_the_file(self) -> None:
        f = self.path("run", "drift-warned")
        self.full_write(
            lambda: GameStateFiles().replace(f, "said\n"), lambda: FaultyStateFiles(self.failing()).replace(f, "x"), f
        )

    def test_a_dropin_that_cannot_be_written_leaves_the_old_one(self) -> None:
        f = self.path("ur", "user.slice.d", "zz-cst-game.conf")
        self.full_write(
            lambda: GameUnitFiles().write_dropin(f, "[Slice]\nMemoryLow=16G\n"),
            lambda: FaultyUnitFiles(self.failing()).write_dropin(f, "x"),
            f,
        )

    def test_a_leftovers_file_that_cannot_be_written_stays(self) -> None:
        f = self.path("run", "game.leftovers")
        self.full_write(
            lambda: GameLeftoversFile(f).write("cst-game-leftovers 2\n"),
            lambda: FaultyLeftoversFile(self.failing(), f).write("x"),
            f,
        )

    def test_a_create_that_fails_raises_and_leaves_an_empty_file_on_the_real_side(self) -> None:
        # The known gap (out-of-scope note of R36): the real open "w" truncates before the write fails; the
        # machine's FAIL keeps main's "no effect" model, for parity.
        f = self.path("run", "game.intended")
        self.put(f, b"before\n")
        with no_file_may_grow(), self.assertRaises(OSError):
            GameStateFiles().create(f, "cst-game-intended 1\n")
        self.assertEqual(self.got(f), b"")
        self.put(f, b"before\n")
        with self.assertRaises(OSError):
            FaultyStateFiles(self.failing()).create(f, "x")
        self.assertEqual(self.got(f), b"before\n")

    def test_a_cgroup_or_sysfs_write_that_fails_answers_false(self) -> None:
        f = self.path("cg", "cpuset.cpus")
        for real, faulty in ((GameKernelFiles(), FaultyKernelFiles), (GameSysfsFiles(), FaultySysfsFiles)):
            self.put(f, b"0-23\n")
            with no_file_may_grow():
                self.assertFalse(real.write(f, "12-23"))
            self.assertFalse(faulty(self.failing()).write(f, "12-23"))

    def test_a_write_whose_folder_is_gone_answers_false_and_its_folder_is_gone(self) -> None:
        f = self.path("gone", "cpuset.cpus")
        k = GameKernelFiles()
        self.assertFalse(k.write(f, "1"))
        self.assertFalse(k.lexists(os.path.dirname(f)))
        self.assertFalse(GameSysfsFiles().write(f, "1"))
        self.assertFalse(FaultyKernelFiles(FaultPlan().arm()).write(f, "1"))

    def test_removals_raise_as_the_real_calls_do_also_with_a_failure_armed(self) -> None:
        missing, folder = self.path("run", "nothing"), self.path("run", "a.d")
        os.makedirs(folder)
        state: list[Callable[[str], None]] = [GameStateFiles().remove, FaultyStateFiles(self.failing()).remove]
        units: list[Callable[[str], None]] = [GameUnitFiles().remove, FaultyUnitFiles(self.failing()).remove]
        for remove in state + units:
            with self.assertRaises(FileNotFoundError):
                remove(missing)
            with self.assertRaises(OSError):
                remove(folder)
        for remove_folder in (GameUnitFiles().remove_folder, FaultyUnitFiles(self.failing()).remove_folder):
            with self.assertRaises(OSError):
                remove_folder(missing)
            self.put(os.path.join(folder, "zz-cst-game.conf"), b"x")
            with self.assertRaises(OSError):
                remove_folder(folder)
        for left in (GameLeftoversFile(missing), FaultyLeftoversFile(self.failing(), missing)):
            with self.assertRaises(FileNotFoundError):
                left.remove()
        for truncate in (GameStateFiles().truncate, FaultyStateFiles(self.failing()).truncate):
            with self.assertRaises(OSError):
                truncate(folder, 0)

    def test_a_folder_made_where_a_file_is_raises(self) -> None:
        f = self.path("plain")
        self.put(f, b"x")
        for make in (GameUnitFiles().make_folder, FaultyUnitFiles(self.failing()).make_folder):
            with self.assertRaises(OSError):
                make(os.path.join(f, "user.slice.d"))
            make(self.path("ur", "user.slice.d"))  # one there already is fine
            self.assertTrue(os.path.isdir(self.path("ur", "user.slice.d")))

    def test_a_kill_takes_effect_then_dies(self) -> None:
        f = self.path("run", "game.state")
        self.put(f, b"")
        with self.assertRaises(Killed):
            FaultyStateFiles(self.failing(FaultMode.KILL)).append(f, "prop a\n")
        self.assertEqual(self.got(f), b"prop a\n")
        with self.assertRaises(Killed):
            FaultyStateFiles(self.failing(FaultMode.KILL)).remove(f)
        self.assertFalse(os.path.exists(f))
        g = self.path("cg", "cpu.weight")
        self.put(g, b"100\n")
        with self.assertRaises(Killed):
            FaultyKernelFiles(self.failing(FaultMode.KILL)).write(g, "1")
        self.assertEqual(self.got(g), b"1\n")

    def test_the_points_are_named_as_the_machine_names_them(self) -> None:
        plan = FaultPlan().arm()
        f, d = self.path("run", "game.state"), self.path("ur", "user.slice.d")
        state, units = FaultyStateFiles(plan), FaultyUnitFiles(plan)
        os.makedirs(os.path.dirname(f))
        state.create(f, "")
        state.append(f, "prop a b\n")
        state.truncate(f, 0)
        state.replace(f, "x")
        state.remove(f)
        units.make_folder(d)
        units.write_dropin(os.path.join(d, "zz-cst-game.conf"), "x")
        units.remove(os.path.join(d, "zz-cst-game.conf"))
        units.remove_folder(d)
        FaultyLeftoversFile(plan, f).write("x")
        FaultyLeftoversFile(plan, f).remove()
        FaultyKernelFiles(plan).write(f, "1")
        FaultySysfsFiles(plan).write(f, "1")
        dropin = os.path.join(d, "zz-cst-game.conf")
        self.assertEqual(
            plan.calls,
            [
                *(f"write {f}", f"write {f}", "os.truncate", f"write {f}", "os.unlink"),
                *("os.makedirs", f"write {dropin}", "os.unlink", "os.rmdir"),
                *(f"write {f}", "os.unlink", f"write {f}", f"write {f}"),
            ],
        )


class ReadTest(Case):
    def test_what_a_file_there_that_cannot_be_read_reads_as(self) -> None:
        folder = self.path("run", "game.leftovers")
        os.makedirs(folder)
        with self.assertRaises(OSError):
            GameLeftoversFile(folder).read()
        self.assertIsNone(GameLeftoversFile(self.path("run", "none")).read())
        s = GameStateFiles()
        self.assertEqual(s.snapshot(folder), "")
        self.assertIsNone(s.snapshot(self.path("run", "none")))
        self.assertIsNone(s.read_text(folder))
        self.assertIsNone(s.read_bytes(folder))

    def test_two_reads_give_two_objects(self) -> None:
        f = self.path("run", "game.state")
        self.put(f, b"prop sys machine.slice CPUWeight new -\n")
        s = GameStateFiles()
        a, b = s.read_text(f), s.read_text(f)
        self.assertEqual(a, b)
        self.assertIsNot(a, b)
        self.assertIsNot(s.read_bytes(f), s.read_bytes(f))
        left = GameLeftoversFile(f)
        self.assertIsNot(left.read(), left.read())

    def test_a_leftovers_file_is_read_byte_for_byte(self) -> None:
        f = self.path("run", "game.leftovers")
        self.put(f, b"cst-game-leftovers 2\r\nuser -\nsysfs /x\xff OLD\n")
        self.assertEqual(GameLeftoversFile(f).read(), "cst-game-leftovers 2\r\nuser -\nsysfs /x\udcff OLD\n")


class PinTest(Case):
    def test_systemctl_argv_and_options(self) -> None:
        runner = RecordingRunner()
        ctl = GameSystemctl(runner)
        ctl.run("sys", "bob", ["show", "-P", "CPUWeight", "machine.slice"], output=OutputMode.STDOUT)
        ctl.run("usr", "bob", ["set-property", "--runtime", "user.slice", "CPUWeight=idle"], output=OutputMode.INHERIT)
        ctl.run("usr", "", ["-q", "is-active", "x"], output=OutputMode.CAPTURE)
        self.assertEqual(
            runner.calls,
            [
                (["systemctl", "show", "-P", "CPUWeight", "machine.slice"], {"output": OutputMode.STDOUT}),
                (
                    ["systemctl", "--user", "-M", "bob@", "set-property", "--runtime", "user.slice", "CPUWeight=idle"],
                    {"output": OutputMode.INHERIT},
                ),
                (["systemctl", "--user", "-M", "@", "-q", "is-active", "x"], {"output": OutputMode.CAPTURE}),
            ],
        )

    def test_the_state_files_bytes_and_modes(self) -> None:
        f = self.path("run", "game.state")
        os.makedirs(os.path.dirname(f))
        s = GameStateFiles()
        s.create(f, "")
        self.assertEqual(self.got(f), b"")
        s.append(f, "prop usr lush\\x2dbatch.slice MemoryLow new -\n")
        s.append(f, "sysfs /sys/x\udcff OLD\n")  # a field that is no UTF-8 is written back as its byte
        self.assertEqual(self.got(f), b"prop usr lush\\x2dbatch.slice MemoryLow new -\nsysfs /sys/x\xff OLD\n")
        s.truncate(f, 4)
        self.assertEqual(self.got(f), b"prop")
        old = os.umask(0o077)
        try:
            mark = self.path("run", "settings-warned")
            s.replace(mark, "é said")
            self.assertEqual((os.stat(mark).st_mode & 0o777, self.got(mark)), (0o644, "é said".encode()))
            s.create(f, "cst-game-intended 1\n")
            self.assertEqual(self.got(f), b"cst-game-intended 1\n")
            d = self.path("ur", "user.slice.d")
            u = GameUnitFiles()
            u.make_folder(d)
            u.write_dropin(os.path.join(d, "zz-cst-game.conf"), "[Slice]\n")
            self.assertEqual(os.stat(os.path.join(d, "zz-cst-game.conf")).st_mode & 0o777, 0o600)  # as the umask gives
            GameLeftoversFile(self.path("run", "game.leftovers")).write("cst-game-leftovers 2\n")
            self.assertEqual(os.stat(self.path("run", "game.leftovers")).st_mode & 0o777, 0o644)
        finally:
            os.umask(old)
        self.assertEqual(sorted(os.listdir(self.path("run"))), ["game.leftovers", "game.state", "settings-warned"])

    def test_the_dropins_and_the_processes_maps_are_sorted(self) -> None:
        for unit in ("user.slice", "app.slice", "user@1.service"):
            self.put(self.path("ur", f"{unit}.d", "zz-cst-game.conf"), b"x")
        os.makedirs(self.path("ur", "empty.slice.d"))
        self.assertEqual(
            GameUnitFiles().dropins(self.path("ur"), "zz-cst-game.conf"),
            [self.path("ur", f"{u}.d", "zz-cst-game.conf") for u in ("app.slice", "user.slice", "user@1.service")],
        )
        for pid in ("12", "3", "self"):
            self.put(self.path("proc", pid, "maps"), b"libgamemode\n")
        maps = GameKernelFiles().process_maps(self.path("proc"))
        self.assertEqual(maps, [self.path("proc", p, "maps") for p in ("12", "3")])

    def test_the_boxes_folders_only_odd_names_as_they_are(self) -> None:
        cg = self.path("cg[1]")  # a glob character in the folder above is no pattern
        odd = os.fsdecode(b"x\xff.slice")
        for name in ("b.slice", "a.slice", odd, "a b.slice"):
            os.makedirs(os.path.join(cg, name))
        self.put(os.path.join(cg, "file.slice"), b"")
        got = GameKernelFiles().boxes(cg, "*.slice")
        self.assertEqual(sorted(got), sorted(os.path.join(cg, n) for n in ("b.slice", "a.slice", odd, "a b.slice")))

    def test_procs_texts_stop_at_the_depth_and_read_lazily(self) -> None:
        d = self.path("box")
        for depth in range(6):
            self.put(os.path.join(d, *["sub"] * depth, "cgroup.procs"), f"{depth}\n".encode())
        texts = GameKernelFiles().procs_texts(d, 3)
        self.assertEqual(next(texts), "0\n")
        self.assertEqual(list(texts), ["1\n", "2\n", "3\n"])
        lazy = GameKernelFiles().procs_texts(d, 3)
        self.assertEqual(next(lazy), "0\n")
        shutil.rmtree(os.path.join(d, "sub"))  # read after the first: nothing more is there
        self.assertEqual(list(lazy), [])

    def test_the_run_lock(self) -> None:
        run = self.path("run")
        os.makedirs(run, mode=0o700)
        os.chmod(run, 0o700)
        lock = GameRunLock(run, os.path.join(run, "lock"), timeout=0.1)
        self.assertTrue(lock.take())
        self.assertEqual(os.stat(run).st_mode & 0o777, 0o755)  # the mode of a folder already there too
        self.assertEqual(os.stat(os.path.join(run, "lock")).st_mode & 0o777, 0o644)
        other = GameRunLock(run, os.path.join(run, "lock"), timeout=0.1)
        fd = os.open(os.path.join(run, "lock"), os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        self.assertFalse(other.take())  # busy past its wait
        lock.release()
        self.assertTrue(other.take())
        other.release()
        self.put(self.path("plain"), b"")
        self.assertFalse(GameRunLock(self.path("plain", "run"), self.path("plain", "run", "lock"), timeout=0.1).take())

    def test_the_run_lock_waits_for_a_lock_given_back_meanwhile(self) -> None:
        run = self.path("run")
        holder = GameRunLock(run, os.path.join(run, "lock"), timeout=0.1)
        self.assertTrue(holder.take())
        timer = threading.Timer(0.2, holder.release)
        timer.start()
        self.addCleanup(timer.join)
        waiter = GameRunLock(run, os.path.join(run, "lock"), timeout=10)
        self.assertTrue(waiter.take())
        waiter.release()

    def test_the_front_waits_60_seconds_for_the_lock(self) -> None:
        lock = machine_ports(GamePaths(), RecordingRunner()).lock
        assert isinstance(lock, GameRunLock)
        self.assertEqual(lock.timeout, 60)

    def test_the_output_lines(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        o = GameStreamOutput(out)
        with contextlib.redirect_stderr(err):
            o.log(4, "a: b")
            o.say("  line")
            o.relay(CommandResult(0, "its out\n", "its err\n"))
            o.error("x: No space left on device")
            o.stderr_text("raw")
            o.flush()
        self.assertEqual(out.getvalue(), "<4>game-profile: a: b\n  line\nits out\n")
        self.assertEqual(err.getvalue(), "its err\nx: No space left on device\nraw")
        sys_out = io.StringIO()
        with contextlib.redirect_stdout(sys_out):
            GameStreamOutput().say("to stdout")  # no stream given: stdout as it is when it says
        self.assertEqual(sys_out.getvalue(), "to stdout\n")


class DrmDiscoveryPinTest(Case):
    """The DRM discovery (GameSysfsFiles): which cards have a display, which a process renders on. A connector
    listed among other entries (in the middle, so neither creation order nor its reverse lists it first)."""

    def among(self, folder: str, make: Callable[[str], None], name: str) -> None:
        for i in range(10):
            self.put(os.path.join(folder, f"a{i}"), b"")
        make(os.path.join(folder, name))
        for i in range(10):
            self.put(os.path.join(folder, f"z{i}"), b"")

    def test_a_card_with_a_connected_connector_counts_once(self) -> None:
        drm = self.path("drm")
        for card in ("card0", "card1", "card2"):
            os.makedirs(os.path.join(drm, card))

        def connector(status: bytes | None) -> Callable[[str], None]:
            def make(path: str) -> None:
                os.makedirs(path)
                if status is not None:
                    self.put(os.path.join(path, "status"), status)

            return make

        def two_connected(path: str) -> None:
            connector(b"connected\n")(path)
            connector(b"connected\n")(path.replace("DP-1", "DP-2"))

        self.among(os.path.join(drm, "card0"), two_connected, "card0-DP-1")
        self.among(os.path.join(drm, "card1"), connector(None), "card1-HDMI-A-1")  # no status: not connected
        self.among(os.path.join(drm, "card2"), connector(b"disconnected\n"), "card2-DP-1")
        self.assertEqual(GameSysfsFiles().connected_cards(drm, ["card0", "card1", "card2"]), ["card0"])

    def test_the_cards_a_process_holds_open(self) -> None:
        proc, drm = self.path("proc"), self.path("drm")
        fd = os.path.join(proc, "42", "fd")
        os.makedirs(fd)
        dev_to_card = {os.path.realpath(os.path.join(drm, "renderD128", "device")): "card1"}
        self.among(fd, lambda p: os.symlink("/dev/dri/renderD128", p), "7")
        self.put(os.path.join(fd, "8"), b"")  # no link: its readlink fails
        os.symlink("/dev/null", os.path.join(fd, "9"))
        files = GameSysfsFiles()
        self.assertEqual(files.pid_cards(proc, drm, "42", dev_to_card), {"card1"})
        os.unlink(os.path.join(fd, "7"))
        self.among(os.path.join(proc, "43", "fd"), lambda p: self.put(p, b""), "1")  # readlink fails mid-list
        os.symlink("/dev/dri/renderD128", os.path.join(proc, "43", "fd", "zz"))
        self.assertEqual(files.pid_cards(proc, drm, "43", dev_to_card), {"card1"})
        self.among(os.path.join(proc, "44", "fd"), lambda p: os.symlink("/dev/null", p), "1")  # no DRM node mid-list
        os.symlink("/dev/dri/renderD128", os.path.join(proc, "44", "fd", "zz"))
        self.assertEqual(files.pid_cards(proc, drm, "44", dev_to_card), {"card1"})
        self.assertEqual(files.pid_cards(proc, drm, "45", dev_to_card), set())  # gone: its fds cannot be listed
