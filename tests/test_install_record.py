# SPDX-License-Identifier: MPL-2.0
"""The install record (cst_core/domain/record.py): its format, how InstallService
keeps it, the JSON adapter (modes, lock, a newer or broken file) and the
installer commands that read or change it (install, text --component, remove,
remove-text, forget, forget-text, mark).

Nothing here touches the machine: the service runs on an in-memory target and
record, the adapter and the installer under temporary folders.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest tests.test_install_record -v
"""

from collections.abc import Callable, Mapping, Sequence
import contextlib
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile
from typing import override
import unittest
from unittest import mock

from cst_core import installer
from cst_core.adapters.install import JsonInstallRecord, SystemdUnitVerifier
from cst_core.adapters.runtime import checkout_commit
from cst_core.application.install import InstallRefused, InstallRequest, InstallService, Placement, record_file
from cst_core.domain.install import (
    COMPONENTS,
    GENERATED_COMPONENTS,
    INSTALL_MANIFEST,
    ArtifactKind,
    GeneratedKind,
    Owner,
)
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


# What main's installer (4edbe5a, run from `git archive main`, so its commit is "unknown") wrote with --root STAGE for
# `install --component btrfs:docker-gc`, `mark --component btrfs:docker-gc done` and `forget btrfs/units/docker-gc.timer`
# (test_upgrade_a_record_written_by_main_without_a_files_line).
MAIN_RECORD = """\
{
 "components": {
  "btrfs:docker-gc": "done"
 },
 "files": {
  "/etc/systemd/system/docker-gc-recorder.service": {
   "component": "btrfs:docker-gc",
   "cst": {
    "commit": "unknown",
    "tree": "51c5080b7683ba507a92d98be5457e8b10dfaefcf265fdd21ac5c6d8a31e1045"
   },
   "kind": "unit",
   "mode": "0644",
   "owner": "root",
   "sha256": "e4e0a7889f29fde99f905a0f39d711aacb201b414f2e8c8cf371ef22e1b98dd2",
   "src": "btrfs/units/docker-gc-recorder.service",
   "values": null
  },
  "/etc/systemd/system/docker-gc.service": {
   "component": "btrfs:docker-gc",
   "cst": {
    "commit": "unknown",
    "tree": "51c5080b7683ba507a92d98be5457e8b10dfaefcf265fdd21ac5c6d8a31e1045"
   },
   "kind": "unit",
   "mode": "0644",
   "owner": "root",
   "sha256": "f9b258639a025f990bbe3161074fb6407bd4f66323bc2e994c7d46d56b8a6c7e",
   "src": "btrfs/units/docker-gc.service",
   "values": null
  },
  "/usr/local/sbin/docker-gc": {
   "component": "btrfs:docker-gc",
   "cst": {
    "commit": "unknown",
    "tree": "51c5080b7683ba507a92d98be5457e8b10dfaefcf265fdd21ac5c6d8a31e1045"
   },
   "kind": "python-entry",
   "mode": "0755",
   "owner": "root",
   "sha256": "1622a2fbe62d8c7d45c07a85bbfaebc27e255971a3bfec7c4856afc1f5dbae47",
   "src": "btrfs/docker-gc",
   "values": null
  }
 },
 "format": 1
}
"""


class MemoryRecord:
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

    def test_a_component_whose_last_file_is_recorded_under_another_loses_its_state(self) -> None:
        # A destination recorded under m:t, done, recorded again under m:u (a manifest move, or the same
        # generated text written with another --component): m:t has no file left, so no state either.
        rec = InstallRecord({"/etc/a": _f()}, {"m:t": ComponentState.DONE})
        moved = with_files(rec, {"/etc/a": (_f(component="m:u"), False)})
        self.assertEqual(moved.components, {"m:u": "pending"})
        # With another file of m:t still recorded, m:t keeps its state.
        rec = InstallRecord({"/etc/a": _f(), "/etc/b": _f("bb")}, {"m:t": ComponentState.DONE})
        moved = with_files(rec, {"/etc/a": (_f(component="m:u"), False)})
        self.assertEqual(moved.components, {"m:t": "done", "m:u": "pending"})

    def test_a_state_an_older_cst_left_without_a_file_goes_at_the_next_write(self) -> None:
        rec = InstallRecord({"/etc/a": _f()}, {"m:t": ComponentState.DONE, "m:gone": ComponentState.PENDING})
        self.assertEqual(with_files(rec, {"/etc/a": (_f(), False)}).components, {"m:t": "done"})

    def test_a_kept_component_keeps_the_state_the_record_has_for_it(self) -> None:
        keep = frozenset({"m:t"})
        for state in ComponentState:
            rec = InstallRecord({"/etc/a": _f()}, {"m:t": state})
            with self.subTest(state=state):
                # Written, changed bytes, a new file of it: the state stays as the record has it.
                self.assertEqual(with_files(rec, {"/etc/a": (_f("bb"), True)}, keep=keep).components, {"m:t": state})
                self.assertEqual(with_files(rec, {"/etc/b": (_f(), False)}, keep=keep).components, {"m:t": state})
                # Only the components in keep: another one written becomes pending.
                other = with_files(rec, {"/etc/b": (_f(component="m:u"), True)}, keep=keep)
                self.assertEqual(other.components, {"m:t": state, "m:u": "pending"})
        # No state yet: pending, as without keep.
        self.assertEqual(with_files(EMPTY_RECORD, {"/etc/a": (_f(), True)}, keep=keep).components, {"m:t": "pending"})
        # A destination moved to the kept component still drops the state of the one it left.
        rec = InstallRecord(
            {"/etc/a": _f(component="m:u"), "/etc/b": _f()}, {"m:u": ComponentState.DONE, "m:t": ComponentState.DONE}
        )
        self.assertEqual(with_files(rec, {"/etc/a": (_f(), True)}, keep=keep).components, {"m:t": "done"})

    def test_a_mark_drops_a_state_an_older_cst_left_without_a_file(self) -> None:
        rec = InstallRecord({"/etc/a": _f()}, {"m:t": ComponentState.PENDING, "m:gone": ComponentState.PENDING})
        self.assertEqual(with_state(rec, "m:t", ComponentState.DONE).components, {"m:t": "done"})
        # A mark of a component with no file changes nothing (the installer then writes no record).
        self.assertEqual(with_state(rec, "m:none", ComponentState.DONE), rec)

    def test_marked_and_without_files(self) -> None:
        rec = with_files(EMPTY_RECORD, {"/etc/a": (_f(), True), "/etc/b": (_f(component="m:u"), True)})
        self.assertEqual(with_state(rec, "m:t", ComponentState.DONE).components, {"m:t": "done", "m:u": "pending"})
        self.assertEqual(with_state(rec, "m:none", ComponentState.DONE), rec, "a component with no file is not added")
        left = without_files(rec, ["/etc/a"])
        self.assertEqual((sorted(left.files), left.components), (["/etc/b"], {"m:u": "pending"}))


class InstallServiceRecordTest(unittest.TestCase):
    ROOT = "/var/lib/cst/installed.json"

    @override
    def setUp(self) -> None:
        self.payloads = {"m/a": b"A\n", "m/t.in": b"uuid=@UUID@\n", "m/u": b"U\n", "m/s.service": b"[Unit]\n"}
        self.a = entry("m/a", "/usr/local/sbin/a", mode=0o755, kind=ArtifactKind.SHELL)
        self.t = entry("m/t.in", "/etc/t")
        self.s = entry("m/s.service", "/etc/systemd/system/s.service", kind=ArtifactKind.UNIT)
        self.u = entry("m/u", "~/.config/u", owner=Owner.USER)
        self.target = MemoryTarget()
        self.record = MemoryRecord()
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

    def test_plan_names_a_current_file_the_record_does_not_list_and_counts_no_write_for_it(self) -> None:
        # An install from before the record: the file is right, no line lists it. plan says install would
        # record it (it adopts it); it is no file to write, so check stays 0. plan writes no record.
        req = InstallRequest(values={"UUID": "u-1"})
        self.target.files["/usr/local/sbin/a"] = (b"A\n", 0o755)
        self.assertEqual(
            self.svc.plan([self.a, self.t], req), (["would record: /usr/local/sbin/a", "would write: /etc/t"], 1)
        )
        self.assertEqual(self.record.texts, {})
        self.assertEqual(list(self.svc.install([self.a], InstallRequest())), ["unchanged: /usr/local/sbin/a"])
        self.assertEqual(self.target.writes, [])
        self.assertIn("/usr/local/sbin/a", self.record.doc().files)
        self.assertEqual(self.svc.plan([self.a], InstallRequest()), (["unchanged: /usr/local/sbin/a"], 0))

    def test_plan_reads_which_files_the_record_lists(self) -> None:
        req = InstallRequest(values={"UUID": "u-1"})
        list(self.svc.install([self.t], req))
        self.target.files["/usr/local/sbin/a"] = (b"A\n", 0o755)
        self.assertEqual(
            self.svc.plan([self.a, self.t], req), (["would record: /usr/local/sbin/a", "unchanged: /etc/t"], 0)
        )
        # A file to write is "would write:" alone, listed or not.
        self.target.files["/usr/local/sbin/a"] = (b"old\n", 0o755)
        self.target.files["/etc/t"] = (b"edited\n", 0o644)
        self.assertEqual(
            self.svc.plan([self.a, self.t], req), (["would write: /usr/local/sbin/a", "would write: /etc/t"], 2)
        )

    def test_plan_names_a_current_file_whose_record_line_install_would_change(self) -> None:
        # The file is right, but its line in the record is not what install writes (a write whose record
        # could not be saved): plan says install would record it again, and counts no write for it.
        req = InstallRequest(values={"UUID": "u-1"})
        list(self.svc.install([self.a, self.t], req))
        self.svc.mark(self.ROOT, "m:t")
        good = self.record.texts[self.ROOT]
        stale: dict[str, Callable[[RecordedFile], RecordedFile]] = {
            "sha256": lambda f: dataclasses.replace(f, sha256="0" * 64),
            "mode": lambda f: dataclasses.replace(f, mode=0o600),
            "component": lambda f: dataclasses.replace(f, component="m:other"),
            "src": lambda f: dataclasses.replace(f, src="m/old"),
            "values": lambda f: dataclasses.replace(f, values={"UUID": "u-0"}),
        }
        for what, change in stale.items():
            with self.subTest(stale=what):
                rec = parse_record(good)
                files = {**rec.files, "/etc/t": change(rec.files["/etc/t"])}
                self.record.texts[self.ROOT] = text = render_record(InstallRecord(files, rec.components))
                self.assertEqual(
                    self.svc.plan([self.a, self.t], req), (["unchanged: /usr/local/sbin/a", "would record: /etc/t"], 0)
                )
                self.assertEqual(self.record.texts[self.ROOT], text, "plan changed the record")
                # install does rewrite that line (its component pending again); plan then says unchanged.
                self.assertEqual(
                    list(self.svc.install([self.a, self.t], req)), ["unchanged: /usr/local/sbin/a", "unchanged: /etc/t"]
                )
                self.assertEqual(self.record.doc().files["/etc/t"], parse_record(good).files["/etc/t"])
                self.assertEqual(self.record.doc().components["m:t"], "pending")
                self.assertEqual(
                    self.svc.plan([self.a, self.t], req), (["unchanged: /usr/local/sbin/a", "unchanged: /etc/t"], 0)
                )
                self.svc.mark(self.ROOT, "m:t")
        # Only the checkout's stamp differs (another commit installed it): install keeps the state, plan
        # says unchanged.
        rec = parse_record(good)
        files = {d: dataclasses.replace(f, commit="older", tree="t0") for d, f in rec.files.items()}
        self.record.texts[self.ROOT] = render_record(InstallRecord(files, rec.components))
        self.assertEqual(
            self.svc.plan([self.a, self.t], req), (["unchanged: /usr/local/sbin/a", "unchanged: /etc/t"], 0)
        )

    def test_plan_takes_a_record_that_is_no_record_as_none_and_says_unchanged_when_it_cannot_read_it(self) -> None:
        # Not JSON, or JSON of the wrong shape: install starts such a record again, so the file would be recorded.
        self.target.files["/usr/local/sbin/a"] = (b"A\n", 0o755)
        for text in ("not json\n", '{"format": 1, "files": []}\n'):
            with self.subTest(text=text):
                self.record.texts[self.ROOT] = text
                self.assertEqual(self.svc.plan([self.a], InstallRequest()), (["would record: /usr/local/sbin/a"], 0))
                self.assertEqual(self.record.texts[self.ROOT], text)

        class _Unreadable(MemoryRecord):
            @override
            def read(self, path: str) -> InstallRecord | None:
                raise PermissionError(13, "Permission denied", path)

        svc = InstallService(self.target, lambda e: self.payloads[e.src], record=_Unreadable())
        self.assertEqual(svc.plan([self.a], InstallRequest()), (["unchanged: /usr/local/sbin/a"], 0))

    def test_plan_of_the_users_files_reads_the_users_record(self) -> None:
        req = InstallRequest(home="/home/tester")
        self.target.files["/home/tester/.config/u"] = (b"U\n", 0o644)
        self.assertEqual(self.svc.plan([self.u], req), (["would record: /home/tester/.config/u"], 0))
        list(self.svc.install([self.u], req))
        self.assertEqual(list(self.record.texts), ["/home/tester/.local/state/cst/installed.json"])
        self.assertEqual(self.svc.plan([self.u], req), (["unchanged: /home/tester/.config/u"], 0))
        # The same file listed in root's record only: the user's record decides.
        self.record.texts = {self.ROOT: self.record.texts.pop("/home/tester/.local/state/cst/installed.json")}
        self.assertEqual(self.svc.plan([self.u], req), (["would record: /home/tester/.config/u"], 0))

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
        # In the reverse of the order given (the manifest's, which install writes in): what is written
        # later may use what came before (a front that runs a program written before it, a unit that
        # runs a program), so it goes first.
        self.assertEqual(lines, ["removed:   /etc/t", "removed:   /usr/local/sbin/a"])
        self.assertEqual(self.target.files, {})
        self.assertEqual(self.record.doc(), EMPTY_RECORD)
        # No record at all (an install from before it): the manifest's files go all the same.
        self.target.files["/usr/local/sbin/a"] = (b"old\n", 0o755)
        self.record.texts.clear()
        self.assertEqual(list(self.svc.remove([self.svc.placement(self.a, req)])), ["removed:   /usr/local/sbin/a"])
        # A second removal finds nothing and says nothing.
        self.assertEqual(list(self.svc.remove([self.svc.placement(self.a, req)])), [])

    def test_a_record_of_a_newer_cst_stops_the_remove_before_anything_changes(self) -> None:
        req = InstallRequest(home="/home/u")
        list(self.svc.install([self.a, self.u], req))
        files = dict(self.target.files)
        user_record = "/home/u/.local/state/cst/installed.json"
        user_text = self.record.texts[user_record]
        self.record.texts[self.ROOT] = '{"format": 9, "files": {}}'
        with self.assertRaises(InstallRefused) as cm:
            list(self.svc.remove([self.svc.placement(e, req) for e in (self.a, self.u)]))
        self.assertEqual(self.target.files, files)
        self.assertEqual(self.record.texts[self.ROOT], '{"format": 9, "files": {}}')
        self.assertEqual(self.record.texts[user_record], user_text)
        self.assertEqual(
            str(cm.exception),
            f"{self.ROOT}: format 9, written by a newer CST (this one reads format 1): nothing was changed",
        )

    def test_a_newer_root_record_does_not_stop_a_remove_of_the_users_files_only(self) -> None:
        req = InstallRequest(home="/home/u")
        list(self.svc.install([self.a, self.u], req))
        self.record.texts[self.ROOT] = '{"format": 9, "files": {}}'
        self.assertEqual(list(self.svc.remove([self.svc.placement(self.u, req)])), ["removed:   /home/u/.config/u"])
        self.assertEqual(list(self.target.files), ["/usr/local/sbin/a"])
        self.assertEqual(self.record.texts[self.ROOT], '{"format": 9, "files": {}}')

    def test_a_record_of_a_newer_cst_stops_forget_places_before_any_record_changes(self) -> None:
        # The user's record sorts first (/home before /var): it is not rewritten before the root one refuses.
        req = InstallRequest(home="/home/u")
        list(self.svc.install([self.a, self.u], req))
        user_record = "/home/u/.local/state/cst/installed.json"
        user_text = self.record.texts[user_record]
        self.record.texts[self.ROOT] = '{"format": 9, "files": {}}'
        with self.assertRaises(InstallRefused) as cm:
            self.svc.forget_places([self.svc.placement(e, req) for e in (self.a, self.u)])
        self.assertEqual(self.record.texts[user_record], user_text)
        self.assertEqual(self.record.texts[self.ROOT], '{"format": 9, "files": {}}')
        self.assertEqual(
            str(cm.exception),
            f"{self.ROOT}: format 9, written by a newer CST (this one reads format 1): nothing was changed",
        )

    def test_forget_places_drops_the_lines_of_each_record_and_leaves_the_files(self) -> None:
        req = InstallRequest(home="/home/u")
        list(self.svc.install([self.a, self.t, self.u], InstallRequest(home="/home/u", values={"UUID": "u-1"})))
        files = dict(self.target.files)
        self.svc.forget_places([self.svc.placement(e, req) for e in (self.a, self.u)])
        self.assertEqual(self.target.files, files)
        self.assertEqual(sorted(self.record.doc().files), ["/etc/t"])
        self.assertEqual(self.record.doc("/home/u/.local/state/cst/installed.json").files, {})

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

    def test_check_record_refuses_a_record_of_a_newer_cst_and_changes_nothing(self) -> None:
        self.record.texts[self.ROOT] = '{"format": 9, "files": {}}'
        with self.assertRaises(InstallRefused) as cm:
            self.svc.check_record(self.ROOT)
        self.assertEqual(
            str(cm.exception),
            f"{self.ROOT}: format 9, written by a newer CST (this one reads format 1): nothing was changed",
        )
        self.assertEqual(self.record.texts, {self.ROOT: '{"format": 9, "files": {}}'})
        self.assertEqual((self.record.writes, self.target.writes), (0, []))

    def test_check_record_passes_no_record_a_current_one_a_broken_one_and_none(self) -> None:
        self.svc.check_record(self.ROOT)  # no record yet
        self.svc.check_record("")  # no record named
        self.assertEqual(self.record.texts, {})
        list(self.svc.install([self.a], InstallRequest()))
        writes = self.record.writes
        self.svc.check_record(self.ROOT)  # this CST's record
        self.record.texts[self.ROOT] = "not a record"
        self.svc.check_record(self.ROOT)  # install starts it again; check_record leaves it
        self.assertEqual(self.record.texts[self.ROOT], "not a record")
        self.assertEqual(self.record.writes, writes)

    def test_no_record_port_records_nothing(self) -> None:
        svc = InstallService(self.target, lambda e: self.payloads[e.src])
        self.assertEqual(list(svc.install([self.a], InstallRequest())), ["written:   /usr/local/sbin/a"])
        self.assertIsNone(svc.mark(self.ROOT, "m:t"))
        self.assertEqual(self.record.texts, {})

    def test_mark_returns_the_state_the_record_had_before(self) -> None:
        # A caller that marks a component pending for a while (iocost's revert) puts the old state back.
        list(self.svc.install([self.a], InstallRequest()))
        self.assertEqual(self.svc.mark(self.ROOT, "m:t"), "pending")
        self.assertEqual(self.svc.mark(self.ROOT, "m:t", ComponentState.PENDING), "done")
        self.assertEqual(self.record.doc().components, {"m:t": "pending"})
        self.assertEqual(self.svc.mark(self.ROOT, "m:t", ComponentState.PENDING), "pending")
        self.assertEqual(self.svc.mark(self.ROOT, "m:t"), "pending")
        self.assertEqual(self.record.doc().components, {"m:t": "done"})

    def test_mark_returns_none_when_the_record_has_no_state_for_the_component(self) -> None:
        # No record file: None (JsonRecordTest: and no record file is made).
        self.assertIsNone(self.svc.mark(self.ROOT, "m:t", ComponentState.PENDING))
        # A record that lists none of its files (another component's): None, the record unchanged.
        list(self.svc.install([self.a], InstallRequest()))
        text = self.record.texts[self.ROOT]
        self.assertIsNone(self.svc.mark(self.ROOT, "m:other", ComponentState.PENDING))
        self.assertEqual(self.record.texts[self.ROOT], text)
        # Its files listed but no state yet (a record an older CST left without one): None, then marked.
        rec = parse_record(text)
        self.record.texts[self.ROOT] = render_record(InstallRecord(rec.files, {}))
        self.assertIsNone(self.svc.mark(self.ROOT, "m:t", ComponentState.PENDING))
        self.assertEqual(self.record.doc().components, {"m:t": "pending"})
        # A state the record keeps for a component none of whose files it lists (an older CST's, a hand
        # edit): None, not that state, which has nothing behind it.
        self.record.texts[self.ROOT] = render_record(InstallRecord({}, {"m:t": ComponentState.DONE}))
        self.assertEqual(parse_record(self.record.texts[self.ROOT]).components, {"m:t": "done"})
        self.assertIsNone(self.svc.mark(self.ROOT, "m:t", ComponentState.PENDING))
        # No record named (""): None, nothing read or written.
        writes = self.record.writes
        self.assertIsNone(self.svc.mark("", "m:t"))
        self.assertEqual(self.record.writes, writes)

    def test_mark_refuses_a_record_of_a_newer_cst_and_changes_nothing(self) -> None:
        self.record.texts[self.ROOT] = '{"format": 9, "files": {}}'
        with self.assertRaisesRegex(InstallRefused, "newer CST"):
            self.svc.mark(self.ROOT, "m:t", ComponentState.PENDING)
        self.assertEqual(self.record.texts, {self.ROOT: '{"format": 9, "files": {}}'})
        self.assertEqual(self.record.writes, 0)


class _Verifier:
    """UnitVerifierPort that records what it was asked and answers as told."""

    def __init__(self, verdict: UnitVerdict) -> None:
        self.verdict = verdict
        self.asked: list[list[str]] = []

    def verify(self, units: Mapping[str, bytes]) -> UnitVerdict:
        self.asked.append(sorted(units))
        return self.verdict


class VerifyBeforeReplaceTest(unittest.TestCase):
    @override
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
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-record-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "var/lib/cst/installed.json")
        self.port = JsonInstallRecord()

    def add(self, dest: str) -> Callable[[InstallRecord], InstallRecord]:
        return lambda rec: with_files(rec, {dest: (_f(), True)})

    def test_a_mark_without_a_record_file_returns_none_and_makes_no_record(self) -> None:
        svc = InstallService(MemoryTarget(), lambda e: b"", record=self.port)
        self.assertIsNone(svc.mark(self.path, "m:t", ComponentState.PENDING))
        self.assertFalse(os.path.exists(self.path))

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
    @override
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
        # A .git file that names no git folder.
        self.git("wt/.git", text="not a gitdir line\n")
        self.assertEqual(checkout_commit(wt), "unknown")
        self.git("wt/.git", text="")
        self.assertEqual(checkout_commit(wt), "unknown")

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

    @override
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

    def test_a_newer_record_refuses_the_remove_and_nothing_is_changed(self) -> None:
        self.assertEqual(self.cli("install", "--component", "btrfs:docker-gc").returncode, 0)
        Path(self.record).write_text('{"format": 7}')
        r = self.cli("remove", "--component", "btrfs:docker-gc")
        for e in INSTALL_MANIFEST:
            if e.component == "btrfs:docker-gc":
                self.assertTrue(os.path.lexists(self.stage + e.dest), e.dest)
        self.assertEqual(Path(self.record).read_text(), '{"format": 7}')
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"newer CST", r.stderr)
        self.assertIn(b"nothing was changed", r.stderr)
        self.assertEqual(r.stdout, b"")

    def test_a_newer_root_record_refuses_forget_before_the_users_record_changes(self) -> None:
        home = pwd.getpwuid(os.getuid()).pw_dir
        sel = ("btrfs/docker-gc", "cpu/gamemode.ini", "--home", home)
        self.assertEqual(self.cli("install", *sel).returncode, 0)
        user_record = Path(self.stage + home + "/.local/state/cst/installed.json")
        before = user_record.read_bytes()
        Path(self.record).write_text('{"format": 7}')
        r = self.cli("forget", *sel)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(b"newer CST", r.stderr)
        self.assertEqual(user_record.read_bytes(), before)
        self.assertEqual(Path(self.record).read_text(), '{"format": 7}')

    def test_forget_drops_the_record_line_and_leaves_the_file(self) -> None:
        self.assertEqual(self.cli("install", "--component", "btrfs:docker-gc").returncode, 0)
        prog = self.stage + "/usr/local/sbin/docker-gc"
        before = (Path(prog).read_bytes(), os.stat(prog).st_mode)
        r = self.cli("forget", "btrfs/docker-gc")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        self.assertNotIn("/usr/local/sbin/docker-gc", self.doc().files)
        self.assertEqual((Path(prog).read_bytes(), os.stat(prog).st_mode), before)
        # The other files of the component stay listed, and the component pending.
        self.assertEqual(self.doc().components, {"btrfs:docker-gc": "pending"})
        # A second forget changes nothing, not even the record's bytes.
        record = Path(self.record).read_bytes()
        self.assertEqual(self.cli("forget", "btrfs/docker-gc").returncode, 0)
        self.assertEqual(Path(self.record).read_bytes(), record)

    def test_forget_of_a_component_drops_its_lines_and_its_state(self) -> None:
        self.assertEqual(self.cli("install", "--component", "btrfs:docker-gc").returncode, 0)
        self.assertEqual(self.cli("mark", "--component", "btrfs:docker-gc", "done").returncode, 0)
        self.assertEqual(self.cli("forget", "--component", "btrfs:docker-gc").returncode, 0)
        self.assertEqual(self.doc(), EMPTY_RECORD)
        want = [e.dest for e in INSTALL_MANIFEST if e.component == "btrfs:docker-gc"]
        self.assertTrue(all(os.path.isfile(self.stage + d) for d in want))

    def test_plan_names_the_files_an_older_install_left_unrecorded_and_check_says_not_current(self) -> None:
        # An install from before the record (its lines taken out): plan says "would record:" for each file
        # and changes nothing; check exits 1 (current means current and recorded); install writes nothing
        # and lists them; plan then says only "unchanged:", and check exits 0.
        sel = ("--component", "btrfs:docker-gc")
        want = sorted(e.dest for e in INSTALL_MANIFEST if e.component == "btrfs:docker-gc")
        self.assertEqual(self.cli("install", *sel).returncode, 0)
        self.assertEqual(self.cli("forget", *sel).returncode, 0)
        before = Path(self.record).read_bytes()
        plan = self.cli("plan", *sel)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(sorted(plan.stdout.decode().splitlines()), [f"would record: {self.stage}{d}" for d in want])
        self.assertEqual(Path(self.record).read_bytes(), before)
        check = self.cli("check", *sel)
        self.assertEqual((check.returncode, check.stdout), (1, b""), check.stderr)
        self.assertEqual(Path(self.record).read_bytes(), before)
        r = self.cli("install", *sel)
        self.assertEqual(sorted(r.stdout.decode().splitlines()), [f"unchanged: {self.stage}{d}" for d in want])
        self.assertEqual(sorted(self.doc().files), want)
        again = self.cli("plan", *sel)
        self.assertEqual(sorted(again.stdout.decode().splitlines()), [f"unchanged: {self.stage}{d}" for d in want])
        self.assertEqual(self.cli("check", *sel).returncode, 0)

    def test_plan_names_a_file_whose_record_line_is_stale_and_check_says_not_current(self) -> None:
        # The file was written, the record kept an older line (its sha256): plan says "would record:" for
        # that file alone and changes nothing; check exits 1; install puts the line right; plan is then
        # "unchanged:" for every file, and check exits 0.
        sel = ("--component", "btrfs:docker-gc")
        want = sorted(e.dest for e in INSTALL_MANIFEST if e.component == "btrfs:docker-gc")
        self.assertEqual(self.cli("install", *sel).returncode, 0)
        rec = self.doc()
        stale = "/usr/local/sbin/docker-gc"
        files = {**rec.files, stale: dataclasses.replace(rec.files[stale], sha256="0" * 64)}
        Path(self.record).write_text(render_record(InstallRecord(files, rec.components)))
        before = Path(self.record).read_bytes()
        plan = self.cli("plan", *sel)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(
            sorted(plan.stdout.decode().splitlines()),
            sorted(f"{'would record' if d == stale else 'unchanged'}: {self.stage}{d}" for d in want),
        )
        self.assertEqual(Path(self.record).read_bytes(), before)
        check = self.cli("check", *sel)
        self.assertEqual((check.returncode, check.stdout), (1, b""), check.stderr)
        self.assertEqual(self.cli("install", *sel).returncode, 0)
        self.assertEqual(self.doc().files[stale].sha256, rec.files[stale].sha256)
        again = self.cli("plan", *sel)
        self.assertEqual(sorted(again.stdout.decode().splitlines()), [f"unchanged: {self.stage}{d}" for d in want])
        self.assertEqual(self.cli("check", *sel).returncode, 0)

    def test_check_judges_the_files_alone_when_the_record_cannot_be_read(self) -> None:
        # A record that cannot be read (here a folder in its place): plan cannot tell what it lists, so
        # check exits 0 for current files and 1 for a file that is not; nothing is written.
        sel = ("--component", "btrfs:docker-gc")
        self.assertEqual(self.cli("install", *sel).returncode, 0)
        os.remove(self.record)
        os.mkdir(self.record)
        self.assertEqual(self.cli("check", *sel).returncode, 0)
        with open(self.stage + "/usr/local/sbin/docker-gc", "ab") as f:
            f.write(b"# edited\n")
        self.assertEqual(self.cli("check", *sel).returncode, 1)
        self.assertTrue(os.path.isdir(self.record))

    def test_upgrade_a_record_written_by_main_without_a_files_line(self) -> None:
        # MAIN_RECORD: what main's installer (4edbe5a) wrote for `install --component btrfs:docker-gc`,
        # `mark ... done`, then `forget btrfs/units/docker-gc.timer`: the timer is current on disk but has
        # no line. check exits 1, install adopts it (no write) and keeps main's other lines (the component
        # is pending again, as after any install, until its setup command marks it), and check then exits 0.
        timer = "btrfs/units/docker-gc.timer"
        dest = "/etc/systemd/system/docker-gc.timer"
        self.assertEqual(self.cli("install", timer).returncode, 0)
        Path(self.record).write_text(MAIN_RECORD)
        self.assertEqual(self.cli("check", timer).returncode, 1)
        r = self.cli("install", timer)
        self.assertEqual((r.returncode, r.stdout), (0, f"unchanged: {self.stage}{dest}\n".encode()), r.stderr)
        doc = self.doc()
        self.assertEqual(doc.files[dest].src, timer)
        self.assertEqual(doc.components, {"btrfs:docker-gc": "pending"})
        main_lines = json.loads(MAIN_RECORD)["files"]
        self.assertEqual(sorted(doc.files), sorted([*main_lines, dest]))
        self.assertEqual({d: doc.files[d].sha256 for d in main_lines}, {d: f["sha256"] for d, f in main_lines.items()})
        self.assertEqual(self.cli("check", timer).returncode, 0)

    def test_upgrade_a_record_an_older_cst_left_with_a_state_no_file_names(self) -> None:
        # An older CST's with_files() kept the state of a component whose last file moved to another
        # component. Such a record still reads as it is; the next text --component writes it without the
        # orphan state, and a second identical call changes no byte of it.
        dest, data = "/etc/x.conf", b"x\n"
        user_record = Path(self.stage + pwd.getpwuid(os.getuid()).pw_dir + "/.local/state/cst/installed.json")
        f = RecordedFile(None, "btrfs:core", GeneratedKind.TEXT, hashlib.sha256(data).hexdigest(), 0o644, Owner.USER)
        old = InstallRecord({dest: f}, {"btrfs:core": ComponentState.DONE, "m:gone": ComponentState.PENDING})
        user_record.parent.mkdir(parents=True)
        user_record.write_text(render_record(old))
        Path(self.stage + "/etc").mkdir()
        Path(self.stage + dest).write_bytes(data)
        os.chmod(self.stage + dest, 0o644)
        self.assertEqual(parse_record(user_record.read_text()), old)
        # The file is current and recorded so: btrfs:core keeps done, and only the orphan goes.
        r = self.cli("text", dest, "0644", "--component", "btrfs:core", stdin=data)
        self.assertEqual((r.returncode, r.stdout), (0, f"unchanged: {self.stage}{dest}\n".encode()), r.stderr)
        self.assertEqual(parse_record(user_record.read_text()).components, {"btrfs:core": "done"})
        before = user_record.read_bytes()
        r = self.cli("text", dest, "0644", "--component", "btrfs:core", stdin=data)
        self.assertEqual((r.returncode, r.stdout), (0, f"unchanged: {self.stage}{dest}\n".encode()), r.stderr)
        self.assertEqual(user_record.read_bytes(), before)

    def _seed_user_record(self, dest: str, data: bytes, state: ComponentState | None) -> Path:
        """The user's record under the stage (text writes it, run as a user here), with btrfs:core in state
        (None: no file of btrfs:core recorded) and dest recorded with the sha256 of data."""
        user_record = Path(self.stage + pwd.getpwuid(os.getuid()).pw_dir + "/.local/state/cst/installed.json")
        user_record.parent.mkdir(parents=True, exist_ok=True)
        files: dict[str, RecordedFile] = {}
        states: dict[str, ComponentState] = {}
        if state is not None:
            sha = hashlib.sha256(data).hexdigest()
            files[dest] = RecordedFile(None, "btrfs:core", GeneratedKind.TEXT, sha, 0o644, Owner.USER)
            states["btrfs:core"] = state
        user_record.write_text(render_record(InstallRecord(files, states)))
        return user_record

    def test_text_keep_state_writes_the_file_and_its_line_and_keeps_the_component_s_state(self) -> None:
        dest, old, new = "/etc/x.conf", b"old\n", b"new\n"
        keep = (dest, "0644", "--component", "btrfs:core", "--keep-state")
        for state in ComponentState:
            with self.subTest(state=state):
                user_record = self._seed_user_record(dest, old, state)
                Path(self.stage + "/etc").mkdir(exist_ok=True)
                Path(self.stage + dest).write_bytes(old)
                os.chmod(self.stage + dest, 0o644)
                r = self.cli("text", *keep, stdin=new)
                self.assertEqual((r.returncode, r.stdout), (0, f"written:   {self.stage}{dest}\n".encode()), r.stderr)
                self.assertEqual(Path(self.stage + dest).read_bytes(), new)
                rec = parse_record(user_record.read_text())
                self.assertEqual(rec.files[dest].sha256, hashlib.sha256(new).hexdigest())
                self.assertEqual(rec.components, {"btrfs:core": state})
                # The same again: unchanged, and no byte of the record changes.
                before = user_record.read_bytes()
                r = self.cli("text", *keep, stdin=new)
                self.assertEqual((r.returncode, r.stdout), (0, f"unchanged: {self.stage}{dest}\n".encode()), r.stderr)
                self.assertEqual(user_record.read_bytes(), before)

    def test_text_keep_state_of_a_component_the_record_has_no_state_for_makes_it_pending(self) -> None:
        dest = "/etc/x.conf"
        user_record = self._seed_user_record(dest, b"", None)
        r = self.cli("text", dest, "0644", "--component", "btrfs:core", "--keep-state", stdin=b"x\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(parse_record(user_record.read_text()).components, {"btrfs:core": "pending"})

    def test_text_keep_state_records_again_a_current_file_whose_line_was_lost_and_keeps_done(self) -> None:
        # An earlier text wrote the file and died before its record update: the line has the old sha256.
        dest, old, new = "/etc/x.conf", b"old\n", b"new\n"
        user_record = self._seed_user_record(dest, old, ComponentState.DONE)
        Path(self.stage + "/etc").mkdir()
        Path(self.stage + dest).write_bytes(new)
        os.chmod(self.stage + dest, 0o644)
        r = self.cli("text", dest, "0644", "--component", "btrfs:core", "--keep-state", stdin=new)
        self.assertEqual((r.returncode, r.stdout), (0, f"unchanged: {self.stage}{dest}\n".encode()), r.stderr)
        rec = parse_record(user_record.read_text())
        self.assertEqual(rec.files[dest].sha256, hashlib.sha256(new).hexdigest())
        self.assertEqual(rec.components, {"btrfs:core": "done"})
        # Without the option, the same lost line makes btrfs:core pending (a setup command's write).
        self._seed_user_record(dest, old, ComponentState.DONE)
        r = self.cli("text", dest, "0644", "--component", "btrfs:core", stdin=new)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(parse_record(user_record.read_text()).components, {"btrfs:core": "pending"})

    def test_keep_state_is_a_usage_error_outside_text_with_a_component(self) -> None:
        dest = "/etc/x.conf"
        user_record = self._seed_user_record(dest, b"x\n", ComponentState.DONE)
        before = user_record.read_bytes()
        for args in (
            ("text", dest, "0644", "--keep-state"),
            ("text-plan", dest, "0644", "--component", "btrfs:core", "--keep-state"),
            ("install", "--component", "btrfs:docker-gc", "--keep-state"),
            ("remove-text", dest, "--keep-state"),
            ("mark", "--component", "btrfs:core", "done", "--keep-state"),
        ):
            with self.subTest(args=args):
                r = self.cli(*args, stdin=b"y\n")
                self.assertEqual((r.returncode, r.stdout), (2, b""), r.stderr)
                self.assertIn(b"--keep-state: only with text DEST MODE --component C", r.stderr)
                self.assertFalse(os.path.lexists(self.stage + dest), "a usage error wrote the file")
                self.assertEqual(user_record.read_bytes(), before)

    def test_text_plan_with_a_component_names_a_current_file_its_record_does_not_list_so(self) -> None:
        # text-plan --component says what text --component would do to the record too: "would record:" for
        # a current file with no line or a stale one, "unchanged:" once text recorded it; without
        # --component it reads no record. A record of a newer CST refuses, as text does.
        dest, data = "/etc/x.conf", b"x\n"
        user_record = Path(self.stage + pwd.getpwuid(os.getuid()).pw_dir + "/.local/state/cst/installed.json")
        sel = (dest, "0644", "--component", "btrfs:degraded-boot")
        self.assertEqual(self.cli("text-plan", *sel, stdin=data).stdout, f"would write: {self.stage}{dest}\n".encode())
        Path(self.stage + "/etc").mkdir()
        Path(self.stage + dest).write_bytes(data)
        os.chmod(self.stage + dest, 0o644)
        r = self.cli("text-plan", *sel, stdin=data)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, f"would record: {self.stage}{dest}\n".encode(), b""))
        self.assertFalse(user_record.exists(), "text-plan wrote a record")
        plain = self.cli("text-plan", dest, "0644", stdin=data)
        self.assertEqual(plain.stdout, f"unchanged: {self.stage}{dest}\n".encode())
        self.assertEqual(self.cli("text", *sel, stdin=data).stdout, f"unchanged: {self.stage}{dest}\n".encode())
        self.assertEqual(self.cli("text-plan", *sel, stdin=data).stdout, f"unchanged: {self.stage}{dest}\n".encode())
        rec = parse_record(user_record.read_text())
        files = {dest: dataclasses.replace(rec.files[dest], sha256="0" * 64)}
        user_record.write_text(render_record(InstallRecord(files, rec.components)))
        before = user_record.read_bytes()
        r = self.cli("text-plan", *sel, stdin=data)
        self.assertEqual(r.stdout, f"would record: {self.stage}{dest}\n".encode())
        self.assertEqual(user_record.read_bytes(), before)
        user_record.write_text('{"format": 7}')
        r = self.cli("text-plan", *sel, stdin=data)
        self.assertEqual((r.returncode, r.stdout), (1, b""))
        self.assertIn(b"newer CST", r.stderr)
        self.assertEqual(self.cli("text-plan", dest, "0644", stdin=data).returncode, 0)

    def test_forget_without_a_record_writes_none(self) -> None:
        r = self.cli("forget", "btrfs/docker-gc")
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertFalse(os.path.exists(self.record))

    def test_forget_text_drops_a_generated_line_and_leaves_the_file(self) -> None:
        dest = "/etc/systemd/system/x.service"
        self.assertEqual(self.cli("text", dest, "0644", "--component", "btrfs:core", stdin=b"[Unit]\n").returncode, 0)
        user_record = Path(self.stage + pwd.getpwuid(os.getuid()).pw_dir + "/.local/state/cst/installed.json")
        self.assertIn(dest, parse_record(user_record.read_text()).files)
        r = self.cli("forget-text", dest)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))
        self.assertEqual(parse_record(user_record.read_text()), EMPTY_RECORD)
        self.assertEqual(Path(self.stage + dest).read_bytes(), b"[Unit]\n")

    def test_forget_usage_errors(self) -> None:
        for args in (
            ("forget", "btrfs/docker-gc", "--component", "btrfs:docker-gc"),
            ("forget", "--component", "btrfs:nope"),
            ("forget",),
            ("forget-text",),
        ):
            with self.subTest(args=args):
                r = self.cli(*args)
                self.assertEqual(r.returncode, 2, r.stderr)
        self.assertFalse(os.path.exists(self.record))

    def test_remove_and_forget_refuse_a_component_of_generated_files_only(self) -> None:
        # No manifest entry names its files (generated text): --component would select nothing and say
        # nothing. Refused (exit 2), naming the DEST form; nothing is changed: a file the record lists
        # under it and a file with no record line stay, and so do the record's bytes.
        listed, loose = "/etc/x-listed.conf", Path(self.stage + "/etc/x-loose.conf")
        user_record = Path(self.stage + pwd.getpwuid(os.getuid()).pw_dir + "/.local/state/cst/installed.json")
        for component, owner in GENERATED_COMPONENTS.items():
            r = self.cli("text", listed, "0644", "--component", component, stdin=b"x\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            loose.write_bytes(b"y\n")
            before = user_record.read_bytes()
            self.assertIn(listed, parse_record(before.decode()).files)
            for command in ("remove", "forget"):
                with self.subTest(component=component, command=command):
                    r = self.cli(command, "--component", component)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertEqual(r.stdout, b"")
                    as_user = " --home DIR, as the user" if owner == "user" else ""
                    self.assertEqual(
                        r.stderr.decode(),
                        f"cst installer: {command} --component {component}: its files are generated text, not "
                        f"manifest files; name them: {command}-text DEST...{as_user}\n",
                    )
                    self.assertEqual(user_record.read_bytes(), before)
                    self.assertEqual(Path(self.stage + listed).read_bytes(), b"x\n")
                    self.assertEqual(loose.read_bytes(), b"y\n")
            # install, plan and check select nothing either (check would say "current"): refused too,
            # naming the setup command that writes them.
            for command in ("install", "plan", "check"):
                with self.subTest(component=component, command=command):
                    r = self.cli(command, "--component", component)
                    self.assertEqual((r.returncode, r.stdout), (2, b""), r.stderr)
                    self.assertEqual(
                        r.stderr.decode(),
                        f"cst installer: {command} --component {component}: its files are generated text, not "
                        f"manifest files; {COMPONENTS[component]} writes them (text or text-plan DEST MODE "
                        f"--component {component})\n",
                    )
                    self.assertEqual(user_record.read_bytes(), before)
                    self.assertEqual(Path(self.stage + listed).read_bytes(), b"x\n")
            # dests and srcs still answer: such a component has no manifest file.
            for command in ("dests", "srcs"):
                r = self.cli(command, "--component", component)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""), command)
        self.assertFalse(os.path.exists(self.record), "root's record was written")

    def test_another_usage_error_wins_over_the_generated_files_refusal(self) -> None:
        # Files named and a component selected: that usage error is the one said, not the refusal.
        for command in ("remove", "forget", "install", "check"):
            with self.subTest(command=command):
                r = self.cli(command, "--component", "btrfs:degraded-boot", "btrfs/docker-gc")
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(
                    r.stderr.decode(),
                    "cst installer: name the files (SRC...) or select them (--module/--kind/--owner/--component), "
                    "not both\n",
                )
        self.assertFalse(os.path.exists(self.record), "root's record was written")

    def test_a_new_component_of_generated_files_only_is_refused_too(self) -> None:
        # The refusal follows GENERATED_COMPONENTS: a component added to it is refused without a change here.
        service = mock.Mock(spec=InstallService)
        with (
            mock.patch.dict("cst_core.domain.install.COMPONENTS", {"m:gen": "cst m install"}),
            mock.patch.dict("cst_core.domain.install.GENERATED_COMPONENTS", {"m:gen": Owner.ROOT}),
            mock.patch("cst_core.wiring.start_installer"),
            mock.patch("cst_core.wiring.installer_identity", return_value=(False, "/home/u")),
            mock.patch("cst_core.wiring.install_service", return_value=service),
        ):
            for command in ("remove", "forget", "install", "plan", "check"):
                err = io.StringIO()
                with self.subTest(command=command), contextlib.redirect_stderr(err):
                    self.assertEqual(installer.main([command, "--component", "m:gen", "--root", self.stage]), 2)
                    how = (
                        f"name them: {command}-text DEST..."
                        if command in ("remove", "forget")
                        else "cst m install writes them (text or text-plan DEST MODE --component m:gen)"
                    )
                    self.assertEqual(
                        err.getvalue(),
                        f"cst installer: {command} --component m:gen: its files are generated text, not manifest "
                        f"files; {how}\n",
                    )
        self.assertEqual(service.method_calls, [])

    def test_files_of_the_user_are_not_forgotten_as_root(self) -> None:
        # As root on the real root (no --root), a user's file is forgotten as that user, not here.
        err = io.StringIO()
        with (
            mock.patch("cst_core.wiring.start_installer"),
            mock.patch("cst_core.wiring.installer_identity", return_value=(True, "/root")),
            mock.patch("cst_core.wiring.install_service", return_value=mock.Mock(spec=InstallService)) as service,
            contextlib.redirect_stderr(err),
        ):
            rc = installer.main(["forget", "--home", "/home/u", "--component", "cpu:gamemode"])
        self.assertEqual(rc, 1)
        self.assertIn("files of the user are forgotten as the user, not as root", err.getvalue())
        service.return_value.forget_places.assert_not_called()

    def test_generated_text_commands_as_root_refuse_another_accounts_home(self) -> None:
        # As root on the real root, text commands keep root's record whatever --home says: a --home of
        # another account is refused (exit 1), not ignored; root's own home and a staged call go on.
        service = mock.Mock(spec=InstallService)
        service.plan_put.return_value = "unchanged: /etc/x"
        service.put.return_value = False
        dest = "/home/u/.local/bin/btrfs-stall-report"
        hint = {
            "text": "cst_install_text DEST MODE USER",
            "text-plan": "cst_install_text_plan DEST MODE USER",
            "remove-text": "cst_remove_text_as USER",
            "forget-text": "cst_forget_text_as USER",
        }
        args = {"text": [dest, "0755"], "text-plan": [dest, "0755"], "remove-text": [dest], "forget-text": [dest]}
        with (
            mock.patch("cst_core.wiring.start_installer"),
            mock.patch("cst_core.wiring.installer_identity", return_value=(True, "/root")),
            mock.patch("cst_core.wiring.install_service", return_value=service),
        ):
            for command, how in hint.items():
                err = io.StringIO()
                # text reads stdin after the place: a closed-off stdin keeps a regression from waiting on ours.
                stdin = io.TextIOWrapper(io.BytesIO(b"x\n"))
                self.addCleanup(stdin.close)
                with (
                    self.subTest(command=command),
                    mock.patch("sys.stdin", stdin),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(err),
                ):
                    rc = installer.main([command, "--home", "/home/u", *args[command]])
                    self.assertEqual(rc, 1)
                    self.assertEqual(
                        err.getvalue(),
                        f"cst installer: --home /home/u: a user's generated files are handled as that user ({how}), "
                        f"not as root: as root {command} keeps root's record\n",
                    )
            self.assertEqual(service.method_calls, [])
            for extra in (["--home", "/root/"], ["--home", "/home/u", "--root", self.stage]):
                stdin = io.TextIOWrapper(io.BytesIO(b"x\n"))
                self.addCleanup(stdin.close)
                with (
                    self.subTest(extra=extra),
                    mock.patch("sys.stdin", stdin),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(installer.main(["forget-text", dest, *extra]), 0)
                    self.assertEqual(installer.main(["text-plan", "/etc/x", "0644", *extra]), 0)
        self.assertEqual(
            [p.record for p in service.forget_places.call_args_list[0].args[0]], ["/var/lib/cst/installed.json"]
        )
        self.assertEqual(len(service.plan_put.call_args_list), 2)

    def test_mark_of_a_component_of_generated_files_only(self) -> None:
        # btrfs:degraded-boot has no manifest file: its owner (root) comes from GENERATED_COMPONENTS.
        dest = "/etc/systemd/system/btrfs-degraded-boot-record.service"
        f = RecordedFile(None, "btrfs:degraded-boot", GeneratedKind.TEXT, "aa", 0o644, Owner.ROOT, None, "c1", "aa")
        os.makedirs(os.path.dirname(self.record))
        Path(self.record).write_text(render_record(with_files(EMPTY_RECORD, {dest: (f, True)})))
        self.assertEqual(self.doc().components, {"btrfs:degraded-boot": "pending"})
        r = self.cli("mark", "--component", "btrfs:degraded-boot", "done")
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertEqual(self.doc().components, {"btrfs:degraded-boot": "done"})

    def test_mark_of_a_record_an_older_cst_left_with_a_state_no_file_names(self) -> None:
        # An older CST's with_files() kept the state of a component whose last file moved to another
        # component; the next mark writes the record without it.
        dest = "/etc/systemd/system/btrfs-degraded-boot-record.service"
        f = RecordedFile(None, "btrfs:degraded-boot", GeneratedKind.TEXT, "aa", 0o644, Owner.ROOT, None, "c1", "aa")
        old = InstallRecord(
            {dest: f}, {"btrfs:degraded-boot": ComponentState.PENDING, "m:gone": ComponentState.PENDING}
        )
        os.makedirs(os.path.dirname(self.record))
        Path(self.record).write_text(render_record(old))
        self.assertEqual(self.doc(), old)
        r = self.cli("mark", "--component", "btrfs:degraded-boot", "done")
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        self.assertEqual(self.doc().components, {"btrfs:degraded-boot": "done"})

    def test_mark_of_a_user_component_of_generated_files_only(self) -> None:
        # A user's: refused as root on the real root; as the user, the user's record says done.
        home = os.path.join(self.stage, "home", "u")
        user_record = Path(self.stage + home + "/.local/state/cst/installed.json")
        with (
            mock.patch.dict("cst_core.domain.install.COMPONENTS", {"m:gen": "cst m install"}),
            mock.patch.dict("cst_core.domain.install.GENERATED_COMPONENTS", {"m:gen": Owner.USER}),
            mock.patch("cst_core.wiring.start_installer"),
        ):
            err = io.StringIO()
            with (
                mock.patch("cst_core.wiring.installer_identity", return_value=(True, "/root")),
                contextlib.redirect_stderr(err),
            ):
                rc = installer.main(["mark", "--home", home, "--component", "m:gen", "done"])
            self.assertEqual(rc, 1)
            self.assertIn("m:gen: belongs to the user; mark it as the user, not as root", err.getvalue())
            stdin = io.TextIOWrapper(io.BytesIO(b"x\n"))
            self.addCleanup(stdin.close)
            with (
                mock.patch("cst_core.wiring.installer_identity", return_value=(False, home)),
                mock.patch("sys.stdin", stdin),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    installer.main(["text", "/etc/x.conf", "0644", "--component", "m:gen", "--root", self.stage]), 0
                )
                self.assertEqual(parse_record(user_record.read_text()).components, {"m:gen": "pending"})
                rc = installer.main(["mark", "--home", home, "--component", "m:gen", "done", "--root", self.stage])
            self.assertEqual(rc, 0)
            self.assertEqual(parse_record(user_record.read_text()).components, {"m:gen": "done"})

    def test_mark_needs_a_known_component_and_the_word_done(self) -> None:
        for args in (("mark", "--component", "btrfs:nope", "done"), ("mark", "--component", "btrfs:core")):
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
