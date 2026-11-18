# SPDX-License-Identifier: MPL-2.0
"""The install record (cst_core/domain/record.py): its format, how InstallService
keeps it, the JSON adapter (modes, lock, a newer or broken file) and the
installer commands that read or change it (install, text --component, remove,
remove-text, mark).

Nothing here touches the machine: the service runs on an in-memory target and
record, the adapter and the installer under temporary folders.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest tests.test_install_record -v
"""

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from cst_core.adapters.install import JsonInstallRecord, SystemdUnitVerifier
from cst_core.adapters.runtime import checkout_commit
from cst_core.application.install import InstallRefused, InstallRequest, InstallService, Placement, record_file
from cst_core.domain.install import INSTALL_MANIFEST, ArtifactKind, GeneratedKind, Owner
from cst_core.domain.record import (
    EMPTY_RECORD,
    ComponentState,
    InstallRecord,
    RecordedFile,
    RecordFormatError,
    parse_record,
    render_record,
    with_files,
    with_state,
    without_files,
)
from cst_core.ports import CommandResult, UnitVerdict, WriteFailed
from tests.test_install import INSTALLER, SYSTEM_PYTHON, MemoryTarget, entry


def _f(sha: str = "aa", component: str = "m:t", commit: str = "c1") -> RecordedFile:
    return RecordedFile("m/a", component, "data", sha, 0o644, Owner.ROOT, None, commit, "t")


class _MemoryRecord:
    """InstallRecordPort in memory: {path: text}; writes counts the files written."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}
        self.writes = 0

    def read(self, path: str) -> InstallRecord | None:
        return parse_record(self.texts[path]) if path in self.texts else None

    def update(self, path: str, change: Callable[[InstallRecord], InstallRecord]) -> bool:
        old = self.read(path)
        new = render_record(change(old or EMPTY_RECORD))
        if old is not None and new == self.texts[path]:
            return False
        self.texts[path] = new
        self.writes += 1
        return True

    def sha256(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def doc(self, path: str = "/var/lib/cst/installed.json") -> InstallRecord:
        return parse_record(self.texts[path])


class RecordDomainTest(unittest.TestCase):
    def test_render_and_parse_give_back_the_same_record(self) -> None:
        rec = InstallRecord({"/etc/a": _f(), "/etc/t": _f("bb")}, {"m:t": ComponentState.DONE})
        self.assertEqual(parse_record(render_record(rec)), rec)
        doc = json.loads(render_record(rec))
        self.assertEqual(doc["format"], 1)
        self.assertEqual(doc["files"]["/etc/a"]["mode"], "0644")
        self.assertEqual(doc["files"]["/etc/a"]["cst"], {"commit": "c1", "tree": "t"})

    def test_values_round_trip(self) -> None:
        f = RecordedFile("m/t.in", "m:t", "data", "cc", 0o644, Owner.ROOT, {"UUID": "u-1"}, "c", "t")
        self.assertEqual(
            parse_record(render_record(InstallRecord({"/etc/t": f}, {}))).files["/etc/t"].values, {"UUID": "u-1"}
        )

    def test_a_newer_format_is_refused_and_says_so(self) -> None:
        with self.assertRaises(RecordFormatError) as cm:
            parse_record('{"format": 2, "files": {}}')
        self.assertTrue(cm.exception.newer)
        self.assertIn("newer CST", str(cm.exception))

    def test_text_that_is_no_record_is_an_error_but_not_a_newer_one(self) -> None:
        for text in (
            "",
            "[]",
            "{}",
            '{"format": "1"}',
            '{"format": 1, "files": {"/x": {"sha256": 1}}}',
            '{"format": 1, "components": {"m:t": "maybe"}}',
        ):
            with self.subTest(text=text), self.assertRaises(RecordFormatError) as cm:
                parse_record(text)
            self.assertFalse(cm.exception.newer)

    def test_a_write_or_a_new_file_makes_the_component_pending_a_file_found_as_recorded_does_not(self) -> None:
        rec = InstallRecord({"/etc/a": _f()}, {"m:t": ComponentState.DONE})
        # Found as recorded, even from another commit: the state stays.
        self.assertEqual(with_files(rec, {"/etc/a": (_f(commit="c2"), False)}).components, {"m:t": "done"})
        self.assertEqual(with_files(rec, {"/etc/a": (_f(commit="c2"), False)}).files["/etc/a"].commit, "c2")
        # Written, or changed bytes, or new to the record (adopted): pending.
        self.assertEqual(with_files(rec, {"/etc/a": (_f(), True)}).components, {"m:t": "pending"})
        self.assertEqual(with_files(rec, {"/etc/a": (_f("bb"), False)}).components, {"m:t": "pending"})
        self.assertEqual(with_files(EMPTY_RECORD, {"/etc/a": (_f(), False)}).components, {"m:t": "pending"})

    def test_marked_and_without_files(self) -> None:
        rec = with_files(EMPTY_RECORD, {"/etc/a": (_f(), True), "/etc/b": (_f(component="m:u"), True)})
        self.assertEqual(with_state(rec, "m:t", ComponentState.DONE).components, {"m:t": "done", "m:u": "pending"})
        self.assertEqual(with_state(rec, "m:none", ComponentState.DONE), rec, "a component with no file is not added")
        left = without_files(rec, ["/etc/a"])
        self.assertEqual((sorted(left.files), left.components), (["/etc/b"], {"m:u": "pending"}))


class InstallServiceRecordTest(unittest.TestCase):
    ROOT = "/var/lib/cst/installed.json"

    def setUp(self) -> None:
        self.payloads = {"m/a": b"A\n", "m/t.in": b"uuid=@UUID@\n", "m/u": b"U\n", "m/s.service": b"[Unit]\n"}
        self.a = entry("m/a", "/usr/local/sbin/a", mode=0o755, kind=ArtifactKind.SHELL)
        self.t = entry("m/t.in", "/etc/t")
        self.s = entry("m/s.service", "/etc/systemd/system/s.service", kind=ArtifactKind.UNIT)
        self.u = entry("m/u", "~/.config/u", owner=Owner.USER)
        self.target = MemoryTarget()
        self.record = _MemoryRecord()
        self.svc = InstallService(self.target, lambda e: self.payloads[e.src], record=self.record, commit="c0ffee")

    def test_install_records_every_file_and_a_second_run_changes_nothing(self) -> None:
        req = InstallRequest(values={"UUID": "u-1"})
        list(self.svc.install([self.a, self.t], req))
        rec = self.record.doc()
        self.assertEqual(sorted(rec.files), ["/etc/t", "/usr/local/sbin/a"])
        a = rec.files["/usr/local/sbin/a"]
        self.assertEqual(
            (a.src, a.kind, a.mode, a.owner, a.sha256, a.values, a.commit),
            ("m/a", "shell", 0o755, "root", hashlib.sha256(b"A\n").hexdigest(), None, "c0ffee"),
        )
        # A template: the rendered bytes and the values that rendered them.
        self.assertEqual(rec.files["/etc/t"].sha256, hashlib.sha256(b"uuid=u-1\n").hexdigest())
        self.assertEqual(rec.files["/etc/t"].values, {"UUID": "u-1"})
        self.assertEqual(rec.components, {"m:t": "pending"})
        self.svc.mark(self.ROOT, "m:t")
        self.assertEqual(self.record.doc().components, {"m:t": "done"})
        writes = self.record.writes
        self.assertEqual(
            list(self.svc.install([self.a, self.t], req)), ["unchanged: /usr/local/sbin/a", "unchanged: /etc/t"]
        )
        self.svc.mark(self.ROOT, "m:t")
        self.assertEqual(self.record.writes, writes, "a run that changes nothing rewrote the record")

    def test_a_recorded_file_edited_on_disk_is_rewritten_and_recorded_again(self) -> None:
        # The bytes on disk decide, never the record: an edited installed file is written back.
        list(self.svc.install([self.a], InstallRequest()))
        self.svc.mark(self.ROOT, "m:t")
        self.target.files["/usr/local/sbin/a"] = (b"A\n# edited\n", 0o755)
        self.assertEqual(list(self.svc.install([self.a], InstallRequest())), ["written:   /usr/local/sbin/a"])
        self.assertEqual(self.target.files["/usr/local/sbin/a"], (b"A\n", 0o755))
        rec = self.record.doc()
        self.assertEqual(rec.files["/usr/local/sbin/a"].sha256, hashlib.sha256(b"A\n").hexdigest())
        self.assertEqual(rec.components, {"m:t": "pending"})

    def test_files_from_before_the_record_are_adopted(self) -> None:
        self.target.files["/usr/local/sbin/a"] = (b"A\n", 0o755)
        self.assertEqual(list(self.svc.install([self.a], InstallRequest())), ["unchanged: /usr/local/sbin/a"])
        self.assertIn("/usr/local/sbin/a", self.record.doc().files)
        self.assertEqual(self.record.doc().components, {"m:t": "pending"})

    def test_plan_and_check_record_nothing(self) -> None:
        self.svc.plan([self.a], InstallRequest())
        self.assertEqual(self.record.texts, {})

    def test_user_files_go_to_the_record_in_the_home_and_staging_moves_both(self) -> None:
        list(self.svc.install([self.u], InstallRequest(home="/home/tester", root="/stage")))
        path = "/stage/home/tester/.local/state/cst/installed.json"
        self.assertEqual(list(self.record.texts), [path])
        # The record lists the destination as installed, not where the stage put it.
        self.assertEqual(list(self.record.doc(path).files), ["/home/tester/.config/u"])
        self.assertEqual(record_file(Owner.ROOT, None, "/stage"), "/stage/var/lib/cst/installed.json")

    def test_programs_are_written_before_units(self) -> None:
        lines = list(self.svc.install([self.s, self.a], InstallRequest()))
        self.assertEqual(lines, ["written:   /usr/local/sbin/a", "written:   /etc/systemd/system/s.service"])

    def test_a_failed_write_records_the_files_before_it(self) -> None:
        self.target.fail_on = "/etc/t"
        it = self.svc.install([self.a, self.t], InstallRequest(values={"UUID": "u"}))
        with self.assertRaises(WriteFailed):
            list(it)
        self.assertEqual(list(self.record.doc().files), ["/usr/local/sbin/a"])

    def test_a_record_of_a_newer_cst_stops_the_install_before_any_write(self) -> None:
        self.record.texts[self.ROOT] = '{"format": 9, "files": {}}'
        with self.assertRaisesRegex(InstallRefused, "newer CST"):
            self.svc.install([self.a], InstallRequest())
        self.assertEqual(self.target.writes, [])
        self.assertEqual(self.record.texts[self.ROOT], '{"format": 9, "files": {}}')

    def test_remove_deletes_the_files_and_their_lines_and_works_without_a_record(self) -> None:
        req = InstallRequest(values={"UUID": "u"})
        list(self.svc.install([self.a, self.t], req))
        lines = list(self.svc.remove([self.svc.placement(e, req) for e in (self.a, self.t)]))
        self.assertEqual(lines, ["removed:   /usr/local/sbin/a", "removed:   /etc/t"])
        self.assertEqual(self.target.files, {})
        self.assertEqual(self.record.doc(), EMPTY_RECORD)
        # No record at all (an install from before it): the manifest's files go all the same.
        self.target.files["/usr/local/sbin/a"] = (b"old\n", 0o755)
        self.record.texts.clear()
        self.assertEqual(list(self.svc.remove([self.svc.placement(self.a, req)])), ["removed:   /usr/local/sbin/a"])
        # A second removal finds nothing and says nothing.
        self.assertEqual(list(self.svc.remove([self.svc.placement(self.a, req)])), [])

    def test_put_and_forget_for_a_module_that_checks_its_files_itself(self) -> None:
        place = Placement(
            "/t/u.service",
            "/etc/systemd/system/u.service",
            self.ROOT,
            "m:t",
            None,
            GeneratedKind.TEXT,
            Owner.ROOT,
            0o644,
        )
        self.assertTrue(self.svc.put(place, b"[Unit]\n", as_root=False))
        self.assertFalse(self.svc.put(place, b"[Unit]\n", as_root=False))
        f = self.record.doc().files["/etc/systemd/system/u.service"]
        self.assertEqual((f.src, f.kind), (None, "text"))
        self.svc.forget(self.ROOT, ["/etc/systemd/system/u.service"])
        self.assertEqual(self.record.doc().files, {})

    def test_put_with_a_record_of_a_newer_cst_names_the_file_it_did_not_write(self) -> None:
        # put writes one file of a module that may have changed other things before it (swap
        # apply edits crypttab first): "nothing was changed" would be wrong there.
        place = Placement(
            "/t/u.service",
            "/etc/systemd/system/u.service",
            self.ROOT,
            "m:t",
            None,
            GeneratedKind.TEXT,
            Owner.ROOT,
            0o644,
        )
        self.record.texts[self.ROOT] = '{"format": 9, "files": {}}'
        with self.assertRaises(InstallRefused) as cm:
            self.svc.put(place, b"[Unit]\n", as_root=False)
        self.assertEqual(
            str(cm.exception),
            f"{self.ROOT}: format 9, written by a newer CST (this one reads format 1): /t/u.service was not written",
        )
        self.assertEqual(self.target.writes, [])
        self.assertEqual(self.record.texts[self.ROOT], '{"format": 9, "files": {}}')

    def test_no_record_port_records_nothing(self) -> None:
        svc = InstallService(self.target, lambda e: self.payloads[e.src])
        self.assertEqual(list(svc.install([self.a], InstallRequest())), ["written:   /usr/local/sbin/a"])
        svc.mark(self.ROOT, "m:t")
        self.assertEqual(self.record.texts, {})


class _Verifier:
    """UnitVerifierPort that records what it was asked and answers as told."""

    def __init__(self, verdict: UnitVerdict) -> None:
        self.verdict = verdict
        self.asked: list[list[str]] = []

    def verify(self, units: Mapping[str, bytes]) -> UnitVerdict:
        self.asked.append(sorted(units))
        return self.verdict


class VerifyBeforeReplaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payloads = {
            "m/p": b"P\n",
            "m/s.service": b"[Service]\nExecStart=/usr/local/sbin/p\n",
            "m/s.timer": b"[Timer]\n",
            "m/d.conf": b"[Service]\n",
            "m/t@.service": b"[Service]\n",
        }
        self.p = entry("m/p", "/usr/local/sbin/p", mode=0o755, kind=ArtifactKind.SHELL)
        self.s = entry("m/s.service", "/etc/systemd/system/s.service", kind=ArtifactKind.UNIT)
        self.t = entry("m/s.timer", "/etc/systemd/system/s.timer", kind=ArtifactKind.UNIT)
        self.d = entry("m/d.conf", "/etc/systemd/system/x.service.d/d.conf", kind=ArtifactKind.UNIT)
        self.tpl = entry("m/t@.service", "/etc/systemd/system/t@.service", kind=ArtifactKind.UNIT)
        self.target = MemoryTarget()

    def svc(self, verdict: UnitVerdict) -> tuple[InstallService, _Verifier]:
        v = _Verifier(verdict)
        return InstallService(self.target, lambda e: self.payloads[e.src], verifier=v), v

    def test_the_changed_units_are_verified_together_after_the_programs_drop_ins_and_templates_aside(self) -> None:
        svc, v = self.svc(UnitVerdict(ok=True))
        self.target.files["/etc/systemd/system/s.timer"] = (b"[Timer]\n", 0o644)  # already current
        lines = list(svc.install([self.s, self.t, self.d, self.tpl, self.p], InstallRequest(as_root=True)))
        self.assertEqual(v.asked, [["s.service"]])
        self.assertEqual(lines[0], "written:   /usr/local/sbin/p")

    def test_failing_units_are_not_written_and_say_why(self) -> None:
        svc, _ = self.svc(UnitVerdict(ok=False, lines=("s.service:2: Unknown key 'X' in section [Service]",)))
        it = svc.install([self.s, self.t, self.p], InstallRequest(as_root=True))
        self.assertEqual(next(it), "written:   /usr/local/sbin/p")
        with self.assertRaises(InstallRefused) as cm:
            list(it)
        self.assertIn("none of these units written: s.service, s.timer", str(cm.exception))
        self.assertIn("Unknown key 'X'", str(cm.exception))
        self.assertNotIn("/etc/systemd/system/s.service", self.target.files)
        self.assertNotIn("/etc/systemd/system/s.timer", self.target.files)

    def test_a_note_when_they_cannot_be_verified(self) -> None:
        svc, _ = self.svc(
            UnitVerdict(ok=True, note="systemd-analyze not found: the units are written without verification")
        )
        lines = list(svc.install([self.s], InstallRequest(as_root=True)))
        self.assertEqual(lines[0], "note:      systemd-analyze not found: the units are written without verification")
        self.assertEqual(lines[1], "written:   /etc/systemd/system/s.service")

    def test_not_verified_as_a_user_or_under_a_staging_root(self) -> None:
        for req in (InstallRequest(), InstallRequest(as_root=True, root="/stage")):
            with self.subTest(req=req):
                svc, v = self.svc(UnitVerdict(ok=False))
                list(svc.install([self.s], req))
                self.assertEqual(v.asked, [])


class _Runner:
    """CommandRunnerPort that answers one CommandResult and keeps the argv."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.argv: list[str] = []

    def run(self, argv: Sequence[str], **_options: object) -> CommandResult:
        self.argv = list(argv)
        return self.result


class SystemdUnitVerifierTest(unittest.TestCase):
    def test_the_strict_call_on_copies_and_only_the_lines_about_these_units(self) -> None:
        out = "x.service:6: Unknown key 'BogusKey' in section [Service], ignoring.\nother.service: something else\n"
        runner = _Runner(CommandResult(1, out, ""))
        v = SystemdUnitVerifier(runner, which=lambda _n: "/usr/bin/systemd-analyze", booted=lambda: True)
        verdict = v.verify({"x.service": b"[Service]\nBogusKey=1\n"})
        self.assertEqual(runner.argv[:4], ["systemd-analyze", "verify", "--man=no", "--recursive-errors=no"])
        self.assertTrue(runner.argv[4].endswith("/x.service"))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.lines, ("x.service:6: Unknown key 'BogusKey' in section [Service], ignoring.",))

    def test_ok_not_booted_and_no_systemd_analyze(self) -> None:
        ok = SystemdUnitVerifier(_Runner(CommandResult(0, "", "")), which=lambda _n: "/x", booted=lambda: True)
        self.assertEqual(ok.verify({"x.service": b""}), UnitVerdict(ok=True))
        never = _Runner(CommandResult(1, "", ""))
        self.assertTrue(
            SystemdUnitVerifier(never, which=lambda _n: "/x", booted=lambda: False).verify({"x.service": b""}).ok
        )
        self.assertEqual(never.argv, [])
        missing = SystemdUnitVerifier(never, which=lambda _n: None, booted=lambda: True).verify({"x.service": b""})
        self.assertEqual(
            (missing.ok, missing.note), (True, "systemd-analyze not found: the units are written without verification")
        )


class JsonRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-record-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "var/lib/cst/installed.json")
        self.port = JsonInstallRecord()

    def add(self, dest: str) -> Callable[[InstallRecord], InstallRecord]:
        return lambda rec: with_files(rec, {dest: (_f(), True)})

    def test_written_0644_in_folders_0755_whatever_the_umask(self) -> None:
        old = os.umask(0o077)
        try:
            self.assertTrue(self.port.update(self.path, self.add("/etc/a")))
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        for d in ("var", "var/lib", "var/lib/cst"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, d)).st_mode), 0o755, d)
        self.assertEqual(list(parse_record(Path(self.path).read_text()).files), ["/etc/a"])
        # The same change again: the file is not written.
        before = os.stat(self.path).st_mtime_ns
        self.assertFalse(self.port.update(self.path, self.add("/etc/a")))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_a_broken_record_is_started_again_a_newer_one_is_left_alone(self) -> None:
        os.makedirs(os.path.dirname(self.path))
        Path(self.path).write_text('{"format": 1, "files": {')
        self.assertTrue(self.port.update(self.path, self.add("/etc/a")))
        self.assertEqual(list(parse_record(Path(self.path).read_text()).files), ["/etc/a"])
        Path(self.path).write_text('{"format": 2}')
        with self.assertRaises(RecordFormatError):
            self.port.update(self.path, self.add("/etc/b"))
        self.assertEqual(Path(self.path).read_text(), '{"format": 2}')
        self.assertIsNone(self.port.read(os.path.join(self.tmp, "none.json")))

    def test_a_mark_with_nothing_listed_writes_no_record(self) -> None:
        # An empty record would hide the installs from before it from cst doctor.
        self.assertFalse(self.port.update(self.path, lambda rec: with_state(rec, "m:t", ComponentState.DONE)))
        self.assertFalse(os.path.exists(self.path))

    def test_two_installs_at_once_lose_nothing(self) -> None:
        # Two processes add 40 files each, one update per file: the lock keeps every line.
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[3])\n"
            "from cst_core.adapters.install import JsonInstallRecord\n"
            "from cst_core.domain.record import RecordedFile, with_files\n"
            "p, tag = sys.argv[1], sys.argv[2]\n"
            "for i in range(40):\n"
            "    f = RecordedFile('m/a', 'm:' + tag, 'data', 'aa', 0o644, 'root')\n"
            "    JsonInstallRecord().update(p, lambda r, d=f'/etc/{tag}{i}', f=f: with_files(r, {d: (f, True)}))\n"
        )
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        procs = [
            subprocess.Popen(
                [SYSTEM_PYTHON, "-I", "-B", "-c", script, self.path, tag, repo],
                cwd="/",
                env={"PATH": "/usr/bin:/bin"},
                stdin=subprocess.DEVNULL,
            )
            for tag in ("x", "y")
        ]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        self.assertEqual(len(parse_record(Path(self.path).read_text()).files), 80)


class CheckoutCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-commit-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def git(self, *parts: str, text: str) -> None:
        p = Path(self.tmp, *parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_branch_packed_detached_worktree_and_none(self) -> None:
        sha = "a" * 40
        self.git(".git/HEAD", text="ref: refs/heads/main\n")
        self.git(".git/refs/heads/main", text=sha + "\n")
        self.assertEqual(checkout_commit(self.tmp), sha)
        os.unlink(os.path.join(self.tmp, ".git/refs/heads/main"))
        self.git(".git/packed-refs", text=f"# pack-refs\n{'b' * 40} refs/heads/main\n")
        self.assertEqual(checkout_commit(self.tmp), "b" * 40)
        self.git(".git/HEAD", text="c" * 40 + "\n")
        self.assertEqual(checkout_commit(self.tmp), "c" * 40)
        # A worktree: .git is a file naming its git folder, the branches are in the common one.
        wt = os.path.join(self.tmp, "wt")
        self.git("wt/.git", text="gitdir: ../.git/worktrees/wt\n")
        self.git(".git/worktrees/wt/HEAD", text="ref: refs/heads/feature\n")
        self.git(".git/worktrees/wt/commondir", text="../..\n")
        self.git(".git/refs/heads/feature", text="d" * 40 + "\n")
        self.assertEqual(checkout_commit(wt), "d" * 40)
        self.assertEqual(checkout_commit(os.path.join(self.tmp, "nothing")), "unknown")
        self.assertEqual(checkout_commit(None), "unknown")

    def test_a_crafted_head_cannot_point_outside_the_git_folder(self) -> None:
        # A file outside .git that holds a commit-shaped text: HEAD must not reach it.
        Path(self.tmp, "outside").write_text("e" * 40 + "\n")
        for ref in (
            "../outside",
            "refs/../../outside",
            "/etc/hostname",
            "refs/heads/../../../outside",
            "refs/heads/a b",
        ):
            with self.subTest(ref=ref):
                self.git(".git/HEAD", text=f"ref: {ref}\n")
                self.assertEqual(checkout_commit(self.tmp), "unknown")
        # A link at HEAD (or at the ref) is never followed, even to a commit-shaped text.
        os.unlink(os.path.join(self.tmp, ".git/HEAD"))
        os.symlink(os.path.join(self.tmp, "outside"), os.path.join(self.tmp, ".git/HEAD"))
        self.assertEqual(checkout_commit(self.tmp), "unknown")
        os.unlink(os.path.join(self.tmp, ".git/HEAD"))
        self.git(".git/HEAD", text="ref: refs/heads/main\n")
        os.makedirs(os.path.join(self.tmp, ".git/refs/heads"), exist_ok=True)
        os.symlink(os.path.join(self.tmp, "outside"), os.path.join(self.tmp, ".git/refs/heads/main"))
        self.assertEqual(checkout_commit(self.tmp), "unknown")
        # Files owned by someone else than the checkout's owner are not read (root reading a
        # gitdir or commondir that points into another account's folder).
        with mock.patch(
            "cst_core.adapters.runtime.os.lstat",
            return_value=os.stat_result((0o40755, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0)),
        ):
            self.git(".git/HEAD", text="e" * 40 + "\n")
            self.assertEqual(checkout_commit(self.tmp), "unknown")
        self.git(".git/HEAD", text="ref: refs/heads/fix/a-b_c.1\n")
        self.git(".git/refs/heads/fix/a-b_c.1", text="f" * 40 + "\n")
        self.assertEqual(checkout_commit(self.tmp), "f" * 40)


class InstallerRecordCliTest(unittest.TestCase):
    """installer.py as the setup scripts run it, staged under --root (the record goes there too)."""

    def setUp(self) -> None:
        self.stage = tempfile.mkdtemp(prefix="cst-record-cli-")
        self.addCleanup(shutil.rmtree, self.stage)
        self.record = os.path.join(self.stage, "var/lib/cst/installed.json")

    def cli(self, *args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [SYSTEM_PYTHON, "-I", "-B", INSTALLER, *args, "--root", self.stage],
            input=stdin,
            capture_output=True,
            check=False,
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
        )

    def doc(self) -> InstallRecord:
        return parse_record(Path(self.record).read_text())

    def test_install_mark_remove_cycle_of_a_component(self) -> None:
        want = sorted(e.dest for e in INSTALL_MANIFEST if e.component == "btrfs:docker-gc")
        self.assertEqual(self.cli("install", "--component", "btrfs:docker-gc").returncode, 0)
        doc = self.doc()
        self.assertEqual(sorted(doc.files), want)
        for dest, f in doc.files.items():
            self.assertEqual(f.sha256, hashlib.sha256(Path(self.stage + dest).read_bytes()).hexdigest(), dest)
        self.assertEqual(doc.components, {"btrfs:docker-gc": "pending"})
        self.assertEqual(self.cli("mark", "--component", "btrfs:docker-gc", "done").returncode, 0)
        self.assertEqual(self.doc().components, {"btrfs:docker-gc": "done"})
        r = self.cli("remove", "--component", "btrfs:docker-gc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode().count("removed:   "), len(want))
        self.assertEqual(self.doc(), EMPTY_RECORD)
        self.assertFalse(any(os.path.lexists(self.stage + d) for d in want))

    def test_generated_text_with_a_component_is_recorded_and_remove_text_drops_it(self) -> None:
        r = self.cli("text", "/etc/systemd/system/x.service", "0644", "--component", "btrfs:core", stdin=b"[Unit]\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        # Run as a user here: the user's record, in this account's home under the stage.
        home = pwd.getpwuid(os.getuid()).pw_dir
        user_record = self.stage + home + "/.local/state/cst/installed.json"
        f = parse_record(Path(user_record).read_text()).files["/etc/systemd/system/x.service"]
        self.assertEqual((f.src, f.kind, f.component), (None, "text", "btrfs:core"))
        # Without --component nothing is recorded (settings files a user may edit).
        os.unlink(user_record)
        self.assertEqual(self.cli("text", "/etc/y.conf", "0644", stdin=b"y\n").returncode, 0)
        self.assertFalse(os.path.exists(user_record))
        self.assertEqual(self.cli("remove-text", "/etc/y.conf").returncode, 0)
        self.assertFalse(os.path.exists(self.stage + "/etc/y.conf"))

    def test_a_newer_record_refuses_the_install_and_nothing_is_written(self) -> None:
        os.makedirs(os.path.dirname(self.record))
        Path(self.record).write_text('{"format": 7}')
        r = self.cli("install", "--component", "btrfs:docker-gc")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"newer CST", r.stderr)
        self.assertFalse(os.path.exists(self.stage + "/usr/local/sbin/docker-gc"))

    def test_mark_needs_a_known_component_and_the_word_done(self) -> None:
        for args in (("mark", "--component", "btrfs:nope", "done"), ("mark", "--component", "btrfs:core")):
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
