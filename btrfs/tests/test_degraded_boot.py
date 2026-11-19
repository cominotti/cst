# SPDX-License-Identifier: MPL-2.0
"""Unit tests for btrfs/btrfs-degraded-boot-setup.sh (no root, no real udev or rpm-ostree).

The script has no "sourced, so stop here" guard and runs from top to
bottom, so the tests run it in parts. The part before its argument dispatch
(the settings and the functions) is sourced as a library; the parts of the
install path between its section comments (files, self-test, initramfs
overlay) are then run on their own. Every command that would reach the
machine is a fake in a temporary folder: rpm-ostree, findmnt, udevadm,
systemctl, systemd-analyze, journalctl, lsblk and cpio. The paths the
script writes are pointed into the temporary folder, and the re-exec
through sudo is never taken. Nothing reads the real disks or writes outside
the temporary folder.

The host paths the guards and next_overlay read (/run/ostree-booted,
/run/ostree/staged-deployment, /boot) are path settings of the prelude
too, so the tests point them into the fake root like the rest; the argument
dispatch is cut out and run on its own. The tier 1 E2E test
test_13_degraded_boot_install_cycle in tests/e2e/suites/suite_tier1_systemd.py
runs the whole script, through `cst btrfs degraded-boot`.
These tests know the script's function names and section comments: they go
when the script becomes Python, and the E2E test stays as the safety net.

Run all unit tests with python3 -I -B tests/run_unit.py; this module alone:
    python3 -B -m unittest btrfs.tests.test_degraded_boot -v
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Final, override

from cst_core.domain.record import USER_RECORD_REL
from cst_core.domain.shapes import items_of, object_of, parse_json
from cst_core.domain.text import group
import unittest

sys.dont_write_bytecode = True

HERE: Final = os.path.dirname(os.path.abspath(__file__))
BTRFS: Final = os.path.dirname(HERE)
REPO: Final = os.path.dirname(BTRFS)
SCRIPT: Final = os.path.join(BTRFS, "btrfs-degraded-boot-setup.sh")
DOCTOR_FIXTURES: Final = os.path.join(REPO, "doctor", "tests", "fixtures")

UUID: Final = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
DISPATCH: Final = 'case "${1:-}" in'
# The section comments of the install path; the argument dispatch is the
# part between the prelude and "guards".
SECTIONS: Final = ("guards", "files", "self-test", "initramfs overlay")
SECTION: Final = re.compile(r"^# -+ (" + "|".join(map(re.escape, SECTIONS)) + r") -+$", re.MULTILINE)

RULE: Final = "/etc/udev/rules.d/65-btrfs-last-resort.rules"
SERVICE: Final = "/etc/systemd/system/btrfs-last-resort@.service"
TIMER: Final = "/etc/systemd/system/btrfs-last-resort@.timer"
HELPER: Final = "/etc/btrfs-degraded-boot/btrfs-last-resort"
RECORD: Final = "/etc/systemd/system/btrfs-degraded-boot-record.service"
SYNCED: Final = "/etc/btrfs-degraded-boot/initramfs-synced.sha256"
OLD_RULE: Final = "/etc/udev/rules.d/64-btrfs.rules"
OLD_SUM: Final = "/etc/btrfs-degraded-boot/upstream-64-btrfs.rules.sha256"
TRACKED: Final = (RULE, SERVICE, TIMER, HELPER)

# The paths the script reads and writes, pointed under $R (the fake root).
# test_every_path_setting_is_pointed_into_the_fake_root keeps this complete.
REPOINT: Final = """
UPSTREAM_RULE="${R}/usr/lib/udev/rules.d/64-btrfs.rules"
CONF_DIR="${R}/etc/btrfs-degraded-boot"
HELPER="${CONF_DIR}/btrfs-last-resort"
SYNCED_SUM_FILE="${CONF_DIR}/initramfs-synced.sha256"
RULE="${R}/etc/udev/rules.d/65-btrfs-last-resort.rules"
UNIT_DIR="${R}/etc/systemd/system"
SERVICE="${UNIT_DIR}/btrfs-last-resort@.service"
TIMER="${UNIT_DIR}/btrfs-last-resort@.timer"
FLAG_DIR="${R}/run/btrfs-allow-degraded"
STATE_DIR="${R}/var/lib/btrfs-degraded-boot"
OSTREE_BOOTED="${R}/run/ostree-booted"
STAGED_DEPLOYMENT="${R}/run/ostree/staged-deployment"
STAGED_INITRDS="${R}/run/ostree/staged-initrds"
BOOT_DIR="${R}/boot"
TRACKED=("${RULE}" "${SERVICE}" "${TIMER}" "${HELPER}")
OLD_RULE="${R}/etc/udev/rules.d/64-btrfs.rules"
OLD_FILES=("${CONF_DIR}/upstream-64-btrfs.rules.sha256")
"""
# Before the files section: the generated text keeps the real paths (the
# prelude's settings are not repointed), and each file is written under $R.
GENERATE: Final = """RULE="${R}${RULE}"
eval "real_$(declare -f write_if_changed)"
write_if_changed() {
    local dest="$1"
    [[ "${dest}" == "${R}"/* ]] || dest="${R}${dest}"
    real_write_if_changed "${dest}" "$2"
}"""
# The installer as the developer (never root), its record in $FAKE/home: every call is logged to
# $FAKE/installer-calls; text, text-plan and remove-text run the real installer with --home there (a
# run as a user records under that home); mark only logs (it would go to root's record).
INSTALLER_REDIRECT: Final = """_cst_installer_as() {
    shift
    echo "installer $*" >>"${FAKE}/installer-calls"
    [[ "$1" != mark ]] || return 0
    local command="$1"
    shift
    /usr/bin/python3 -I -B "${CST_INSTALLER}" "${command}" --home "${FAKE}/home" "$@"
}"""
REPOINTED: Final = frozenset(re.findall(r"^([A-Z_]+)=", REPOINT, re.MULTILINE))
# sha256 of the generated files as the previous version of the script
# wrote them (main at a2cbb82, the version installed today): an install of
# it must stay current.
PREVIOUS_SHA256: Final = {
    RULE: "c8ab30283943b551730c0db39ae57db398e158b22a52f1a846bb0fcc1470b3b1",
    SERVICE: "1dd1f77006551c98cda6826ca1d25f5ca6c60e3cad9b5bb0f0446f7a60fb8458",
    TIMER: "75e29b95cd064f890be79c86cfabef7430b3ffd92ccc9c2d56c91b254917eda3",
    HELPER: "6e8ccfcad2636e1ddff768cce0427b058bfa7e1bea6b1a1aae1a15ec2c70f891",
    RECORD: "ed372e03b49cf9e731b29edb8f3fde89ee74868f43923cf8cd095585748142b5",
}
# Settings of the prelude that name no file.
NOT_PATHS: Final = frozenset({"RECORD_UNIT", "OURS_MARK"})

# Fedora's /usr/lib/udev/rules.d/64-btrfs.rules, whose semantics the rule
# builds on (the script refuses to install when they change).
UPSTREAM_64: Final = """# do not edit this file, it will be overwritten on update

SUBSYSTEM!="block", GOTO="btrfs_end"
ACTION=="remove", GOTO="btrfs_end"
ENV{ID_FS_TYPE}!="btrfs", GOTO="btrfs_end"
ENV{SYSTEMD_READY}=="0", GOTO="btrfs_end"

# let the kernel know about this btrfs filesystem, and check if it is complete
IMPORT{builtin}="btrfs ready $devnode"

# mark the device as not ready to be used by the system
ENV{ID_BTRFS_READY}=="0", ENV{SYSTEMD_READY}="0"

# reconsider pending devices in case when multidevice volume awaits
ENV{ID_BTRFS_READY}=="1", RUN+="/usr/bin/udevadm trigger -s block -p ID_BTRFS_READY=0"

LABEL="btrfs_end"
"""

EXPECTED_RULE: Final = """# Generated by btrfs-degraded-boot-setup.sh (CST). Do not edit.
#
# btrfs last resort for the root filesystem @UUID@. Runs after the upstream
# /usr/lib/udev/rules.d/64-btrfs.rules, which imports ID_BTRFS_READY and sets
# SYSTEMD_READY=0 while the fs is incomplete:
#   complete   -> stop btrfs-last-resort@<uuid>.timer
#   incomplete -> start that timer (45 s after the first member appeared the
#                 service re-checks and may create the flag)
#   incomplete + flag /run/btrfs-allow-degraded/<uuid> -> SYSTEMD_READY=1, so
#                 the fs can be mounted with -o degraded

SUBSYSTEM!="block", GOTO="btrfs_last_resort_end"
ACTION=="remove", GOTO="btrfs_last_resort_end"
ENV{ID_FS_TYPE}!="btrfs", GOTO="btrfs_last_resort_end"
ENV{ID_FS_UUID}!="@UUID@", GOTO="btrfs_last_resort_end"
# Never override device-mapper's own "not ready" (suspended / rules disabled).
ENV{DM_UDEV_DISABLE_OTHER_RULES_FLAG}=="1", GOTO="btrfs_last_resort_end"
ENV{DM_SUSPENDED}=="1", GOTO="btrfs_last_resort_end"
ENV{.DM_SUSPENDED}=="1", GOTO="btrfs_last_resort_end"

ENV{ID_BTRFS_READY}=="1", RUN+="/usr/bin/systemctl --no-block stop btrfs-last-resort@$env{ID_FS_UUID}.timer", GOTO="btrfs_last_resort_end"
# Only act on this event's result from 64-btrfs.rules.
ENV{ID_BTRFS_READY}!="0", GOTO="btrfs_last_resort_end"
TEST=="/run/btrfs-allow-degraded/$env{ID_FS_UUID}", ENV{SYSTEMD_READY}="1", GOTO="btrfs_last_resort_end"
RUN+="/usr/bin/systemctl --no-block start btrfs-last-resort@$env{ID_FS_UUID}.timer"

LABEL="btrfs_last_resort_end"
""".replace("@UUID@", UUID)

EXPECTED_TIMER: Final = """# Generated by btrfs-degraded-boot-setup.sh (CST).
# Started by /etc/udev/rules.d/65-btrfs-last-resort.rules when a member of
# btrfs fs %i appears but the fs is incomplete. Starting it again while it
# runs is a no-op, so the delay counts from the first member, not from boot.
[Unit]
Description=Wait for missing members of btrfs filesystem %i before allowing it degraded
Documentation=file:/etc/btrfs-degraded-boot/btrfs-last-resort
DefaultDependencies=no
Conflicts=shutdown.target
Before=shutdown.target

[Timer]
OnActiveSec=45s
AccuracySec=1s
RemainAfterElapse=no
"""

EXPECTED_SERVICE: Final = """# Generated by btrfs-degraded-boot-setup.sh (CST).
[Unit]
Description=Allow btrfs filesystem %i to mount degraded (member device missing)
Documentation=file:/etc/btrfs-degraded-boot/btrfs-last-resort
DefaultDependencies=no
Conflicts=shutdown.target
Before=shutdown.target

[Service]
Type=oneshot
ExecStart=/usr/bin/bash /etc/btrfs-degraded-boot/btrfs-last-resort %i
SyslogIdentifier=btrfs-last-resort
# Waits for pending passphrase prompts / locked disks, however long they take.
TimeoutStartSec=infinity
"""

EXPECTED_RECORD: Final = """# Generated by btrfs-degraded-boot-setup.sh (CST). Real root only.
[Unit]
Description=Record a degraded btrfs boot persistently (btrfs-last-resort)
Documentation=file:/etc/btrfs-degraded-boot/btrfs-last-resort
ConditionPathExists=!/etc/initrd-release
ConditionDirectoryNotEmpty=/run/btrfs-allow-degraded
RequiresMountsFor=/var/lib/btrfs-degraded-boot

[Service]
Type=oneshot
ExecStart=/usr/bin/bash /etc/btrfs-degraded-boot/btrfs-last-resort --record

[Install]
WantedBy=multi-user.target
"""

# The fakes. Each one first logs its argv to $FAKE/calls, one line per call.
FAKES: Final[dict[str, str]] = {
    "rpm-ostree": """case "$1" in
    status) cat "${FAKE}/status.json" ;;
    kargs) cat "${FAKE}/kargs" ;;
    initramfs-etc) exit 0 ;;
    *) exit 64 ;;
esac""",
    "findmnt": """case "$*" in
    *FSTYPE*) echo "${FAKE_FSTYPE:-btrfs}" ;;
    *) echo "${FAKE_UUID}" ;;
esac""",
    "btrfs": """[[ "$1 $2" == "filesystem show" ]] || exit 64
printf 'Label: none  uuid: %s\n\tTotal devices %s FS bytes used 1.00GiB\n' "$3" "${FAKE_NDEV:-2}"
""",
    "systemctl": """case "$1" in
    is-enabled) if [[ -e "${FAKE}/enabled" ]]; then [[ "$2" == -q ]] || echo enabled; else [[ "$2" == -q ]] || echo disabled; exit 1; fi ;;
esac""",
    "journalctl": """case "$*" in
    *"^Started"*) cat "${FAKE}/journal-started" 2>/dev/null ;;
    *"^Stopped"*) cat "${FAKE}/journal-stopped" 2>/dev/null ;;
esac""",
    "udevadm": """case "$1" in
    verify) cp "${@: -1}" "${FAKE}/verified.rules"; exit "${FAKE_VERIFY_RC:-0}" ;;
    test) cat "${FAKE}/udev-test-$(basename "${@: -1}")" ;;
    info) cat "${FAKE}/udev-info-$(basename "${@: -1}")" ;;
esac""",
    # It keeps each unit file it is handed under the name it verifies it as
    # (FILE:NAME, or the file's own name) in $FAKE/verified-units.
    # FAKE_UNIT_TYPO: a unit with an unknown key, as the real verify sees it:
    # a warning, and a failure only with --recursive-errors=no.
    "systemd-analyze": """mkdir -p "${FAKE}/verified-units"
for a in "${@:2}"; do
    [[ "$a" == -* ]] && continue
    f="${a%%:*}"; n="${f##*/}"
    [[ "$a" == *:* ]] && n="${a#*:}"
    cp "$f" "${FAKE}/verified-units/$n" || exit 65
done
if [[ -n "${FAKE_UNIT_TYPO:-}" ]]; then
    echo "btrfs-last-resort@.service:12: Unknown key 'TimeoutStratSec' in section [Service], ignoring." >&2
    [[ " $* " == *" --recursive-errors=no "* ]] && exit 1
fi
exit "${FAKE_ANALYZE_RC:-0}"
""",
    "lsblk": 'cat "${FAKE}/lsblk"',
    # The overlay in these tests is a tar archive: what the script checks is
    # which files come out and their bytes, not the archive format.
    "cpio": "exec tar -xf -",
}
# The calls that change the machine; everything else only reads.
MUTATING: Final = re.compile(
    r"^(rpm-ostree initramfs-etc|systemctl (enable|disable|stop|start|daemon-reload|reset-failed)|udevadm (control|trigger))"
)


def script_parts() -> dict[str, str]:
    """The script cut at its argument dispatch and at its section comments."""
    with open(SCRIPT, encoding="utf-8") as f:
        text = f.read()
    if text.count(DISPATCH) != 1:
        raise AssertionError(f"expected one {DISPATCH!r} in {SCRIPT}")
    marks = list(SECTION.finditer(text))
    names = [m.group(1) for m in marks]
    if tuple(names) != SECTIONS:
        raise AssertionError(f"unexpected section comments in {SCRIPT}: {names}")
    parts = {"prelude": text[: text.index(DISPATCH)], "dispatch": text[text.index(DISPATCH) : marks[0].start()]}
    ends = [m.start() for m in marks[1:]] + [len(text)]
    for m, end in zip(marks, ends, strict=True):
        parts[group(m, 1).replace(" ", "-")] = text[m.start() : end]
    return parts


def fixture(name: str, *changes: Mapping[str, object]) -> dict[str, object]:
    """A doctor fixture of `rpm-ostree status --json`; changes[i] updates deployment i."""
    with open(os.path.join(DOCTOR_FIXTURES, name), encoding="utf-8") as f:
        data = object_of(parse_json(f.read()))
    for deployment, keys in zip(items_of(data["deployments"]), changes, strict=False):
        object_of(deployment).update(keys)
    return data


class DegradedBootTest(unittest.TestCase):
    tmp: str
    root: str

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="degraded-boot-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "root")
        # The prelude sources ../cst-install.bash next to its own folder.
        os.makedirs(os.path.join(self.tmp, "lib", "btrfs"))
        os.symlink(os.path.join(REPO, "cst-install.bash"), os.path.join(self.tmp, "lib", "cst-install.bash"))
        for name, text in script_parts().items():
            self.put(f"lib/btrfs/{name}.sh", text)
        for name, body in FAKES.items():
            os.chmod(self.put(f"bin/{name}", f'#!/bin/bash\necho "{name} $*" >>"${{FAKE}}/calls"\n{body}\n'), 0o755)
        self.put("calls", "")
        self.put("root/usr/lib/udev/rules.d/64-btrfs.rules", UPSTREAM_64)
        self.status_json(fixture("unpinned.json"))

    # ------------------------------------------------------------ helpers --
    def put(self, rel: str, text: str) -> str:
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def read(self, rel: str) -> str:
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as f:
            return f.read()

    def at(self, dest: str) -> str:
        """The fake root's copy of an absolute path."""
        return self.root + dest

    def status_json(self, data: dict[str, object]) -> None:
        self.put("status.json", json.dumps(data))

    def run_bash(
        self,
        code: str,
        *,
        repoint: bool = True,
        env: dict[str, str] | None = None,
        stub_overlay: bool = True,
        argv0: str = "degraded-boot-test",
    ) -> subprocess.CompletedProcess[str]:
        """Source the prelude, then run code with the fakes first on PATH.

        stub_overlay=False keeps the real next_overlay: only with repoint,
        so that it reads the fake root's /boot and /run/ostree.
        """
        if not (repoint or stub_overlay):
            raise AssertionError("the real next_overlay needs the repointed paths")
        stub = 'next_overlay() { echo "next_overlay is not stubbed" >&2; return 97; }' if stub_overlay else ""
        if not repoint:  # BOOT_DIR is the host's /boot
            stub += '\nnext_boot_entry() { echo "next_boot_entry is not stubbed" >&2; return 97; }'
        lib = os.path.join(self.tmp, "lib", "btrfs")
        script = f"""
# Never re-exec through sudo: the prelude does that when not root.
exec() {{ [[ "$1" == sudo ]] || {{ echo "unexpected exec $*" >&2; return 97; }}; }}
. "{lib}/prelude.sh"
unset -f exec
{INSTALLER_REDIRECT}
PATH="{self.tmp}/bin:/usr/bin:/bin"
R="{self.root}"
{REPOINT if repoint else ""}
# They read /boot and /run/ostree: a test that reaches them stubs them, or repoints them.
{stub}
{code}
"""
        return subprocess.run(
            ["bash", "-c", script, argv0],
            check=False,
            env={"PATH": "/usr/bin:/bin", "FAKE": self.tmp, "FAKE_UUID": UUID, "LC_ALL": "C", **(env or {})},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def ok(
        self, code: str, *, repoint: bool = True, env: dict[str, str] | None = None, stub_overlay: bool = True
    ) -> str:
        r = self.run_bash(code, repoint=repoint, env=env, stub_overlay=stub_overlay)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def section(self, name: str, *args: str) -> str:
        """Code that runs one section of the install path with these arguments."""
        return f'set -- {" ".join(args)}\n. "{self.tmp}/lib/btrfs/{name}.sh"'

    def calls(self) -> list[str]:
        return self.read("calls").splitlines()

    def mutating_calls(self) -> list[str]:
        return [c for c in self.calls() if MUTATING.match(c)]

    def write_files(self, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """The files section with the real paths in the generated text, written under the fake root."""
        return self.run_bash(f"UUID={UUID}\n{GENERATE}\n{self.section('files')}", repoint=False, env=env)

    def install(self) -> None:
        """The four tracked files and the record unit, as an install leaves them."""
        r = self.write_files()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def overlay(self, files: dict[str, bytes]) -> str:
        """A next-boot overlay holding these files (fake-root paths); its path."""
        path = os.path.join(self.tmp, "overlay.img")
        with tarfile.open(path, "w:gz") as tar:
            for dest, data in files.items():
                info = tarfile.TarInfo(self.at(dest).lstrip("/"))
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def tracked_bytes(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for dest in TRACKED:
            with open(self.at(dest), "rb") as f:
                out[dest] = f.read()
        return out

    def tree(self) -> dict[str, str]:
        """Every file under the fake root and its sha256 (to prove nothing changed)."""
        out: dict[str, str] = {}
        for top, _dirs, names in os.walk(self.root):
            for name in names:
                path = os.path.join(top, name)
                with open(path, "rb") as f:
                    out[path] = hashlib.sha256(f.read()).hexdigest()
        return out

    def tracked_json(self, booted: list[str], nxt: list[str] | None = None) -> None:
        """A staged deployment (index 0, next boot) and the booted one, with their initramfs-etc lists."""
        staged = {"serial": 1, "initramfs-etc": [self.at(p) for p in (booted if nxt is None else nxt)]}
        self.status_json(fixture("staged-clean.json", staged, {"initramfs-etc": [self.at(p) for p in booted]}))

    # ---------------------------------------------------- harness checks --
    def test_every_path_setting_is_pointed_into_the_fake_root(self) -> None:
        prelude = script_parts()["prelude"]
        settings = set(re.findall(r"^([A-Z_]+)=", prelude, re.MULTILINE))
        self.assertEqual(settings - NOT_PATHS, REPOINTED)
        out = self.ok(
            "for v in " + " ".join(sorted(REPOINTED)) + ' ; do declare -n ref="$v"; printf "%s\\n" "${ref[@]}"; done'
        )
        for line in out.splitlines():
            self.assertTrue(line.startswith(self.root + "/"), line)

    def test_next_overlay_must_be_stubbed(self) -> None:
        """next_overlay reads the host's /boot and /run/ostree: unstubbed, it stops the test."""
        r = self.run_bash("next_overlay")
        self.assertEqual(r.returncode, 97, r.stdout + r.stderr)
        self.assertIn("next_overlay is not stubbed", r.stderr)

    def test_next_boot_entry_must_be_stubbed_without_the_repointed_paths(self) -> None:
        """next_boot_entry reads the host's /boot: without the repointed paths, unstubbed, it stops the test."""
        r = self.run_bash("next_boot_entry", repoint=False)
        self.assertEqual(r.returncode, 97, r.stdout + r.stderr)
        self.assertIn("next_boot_entry is not stubbed", r.stderr)

    def test_the_fakes_are_the_commands_the_functions_find(self) -> None:
        out = self.ok("for c in " + " ".join(FAKES) + ' ; do type -P "$c"; done')
        self.assertEqual(out.splitlines(), [os.path.join(self.tmp, "bin", c) for c in FAKES])

    # ------------------------------------------------------- ostree_query --
    def test_ostree_query_on_doctor_fixtures(self) -> None:
        cases = {
            "unpinned.json": ("no", "  44.20261028.0  booted        bbbbbbbbbbbb initramfs-etc=[]"),
            "staged-clean.json": (
                "yes",
                (
                    "  44.20261104.0  staged        555555555555 initramfs-etc=[]\n"
                    "  44.20261028.0  booted        bbbbbbbbbbbb initramfs-etc=[]"
                ),
            ),
            "pinned.json": (
                "no",
                (
                    "  44.20261028.0  booted        bbbbbbbbbbbb initramfs-etc=[]\n"
                    "  44.20261009.0  -             000000000000 initramfs-etc=[]"
                ),
            ),
        }
        for name, (staged, summary) in cases.items():
            with self.subTest(name):
                self.status_json(fixture(name))
                out = self.ok('ostree_query next-staged; ostree_query summary; echo "[$(ostree_query tracked)]"')
                self.assertEqual(out, f"{staged}\n{summary}\n[]\n")
                # The fixtures as rpm-ostree writes them: with each deployment's serial.
                first = "5" if staged == "yes" else "b"
                root = self.ok("ostree_query next-root")
                self.assertEqual(root, f"/ostree/deploy/fedora/deploy/{first * 64}.0\n")

    def test_ostree_query_tracked_lists_are_per_deployment(self) -> None:
        self.status_json(
            fixture(
                "staged-clean.json",
                {"serial": 2, "initramfs-etc": ["/etc/a", "/etc/b"]},
                {"serial": 0, "initramfs-etc": ["/etc/a"]},
            )
        )
        out = self.ok("ostree_query tracked; echo --; ostree_query booted-tracked; echo --; ostree_query next-root")
        self.assertEqual(out, "/etc/a\n/etc/b\n--\n/etc/a\n--\n/ostree/deploy/fedora/deploy/" + "5" * 64 + ".2\n")

    # -------------------------------------------------------- upstream_ok --
    def test_upstream_ok_needs_both_lines_of_64_btrfs(self) -> None:
        rule = "${R}/usr/lib/udev/rules.d/64-btrfs.rules"
        self.assertEqual(self.ok(f'upstream_ok "{rule}" && echo ok'), "ok\n")
        changed = UPSTREAM_64.replace('ENV{ID_BTRFS_READY}=="0", ENV{SYSTEMD_READY}="0"\n', "")
        self.put("root/usr/lib/udev/rules.d/64-btrfs.rules", changed)
        self.assertEqual(self.ok(f'upstream_ok "{rule}" || echo changed'), "changed\n")
        self.assertEqual(self.ok('upstream_ok "${R}/missing" || echo changed'), "changed\n")

    # ------------------------------------------------------------ dispatch --
    def dispatch(self, *args: str) -> subprocess.CompletedProcess[str]:
        """The argument dispatch, with status and uninstall stubbed; "install path" when it falls through."""
        code = (
            "status() { echo STATUS; }\nuninstall() { echo UNINSTALL; }\n"
            f'set -- {" ".join(args)}\n. "{self.tmp}/lib/btrfs/dispatch.sh"\necho "install path plan=$plan"'
        )
        return self.run_bash(code, argv0=SCRIPT)

    def test_dispatch_runs_status_and_uninstall_and_stops(self) -> None:
        for arg, word in (("--status", "STATUS"), ("--uninstall", "UNINSTALL")):
            with self.subTest(arg):
                r = self.dispatch(arg)
                self.assertEqual((r.returncode, r.stdout), (0, f"{word}\n"), r.stderr)

    def test_dispatch_lets_install_files_only_and_plan_through(self) -> None:
        for args, plan in (((), 0), (("--files-only",), 0), (("--plan",), 1)):
            with self.subTest(args):
                r = self.dispatch(*args)
                self.assertEqual((r.returncode, r.stdout), (0, f"install path plan={plan}\n"), r.stderr)

    def test_dispatch_prints_the_help_on_an_unknown_argument(self) -> None:
        for arg in ("--bogus", "status", "plan", "--status=x"):
            with self.subTest(arg):
                r = self.dispatch(arg)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertTrue(r.stdout.startswith("\nbtrfs-degraded-boot-setup.sh\n"), r.stdout)
                self.assertIn("cst btrfs degraded-boot --status", r.stdout)
                self.assertNotIn("install path", r.stdout)
                self.assertNotIn("set -Eeuo pipefail", r.stdout)

    # -------------------------------------------------------------- guards --
    GOOD_KARGS: Final = f"root=UUID={UUID} rootflags=subvol=root,degraded rd.luks.uuid=luks-1111 quiet\n"

    def guards(
        self, *args: str, kargs: str = GOOD_KARGS, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """The guards section with these arguments; the next deployment's root is $FAKE/next. Prints UUID, ndev and NEXT_ROOT."""
        self.put("kargs", kargs)
        self.put("calls", "")
        code = f"""
eval "real_$(declare -f ostree_query)"
ostree_query() {{ if [[ "$1" == next-root ]]; then echo "{self.tmp}/next"; else real_ostree_query "$1"; fi; }}
{self.section("guards", *args)}
echo "UUID=$UUID ndev=$ndev NEXT_ROOT=$NEXT_ROOT"
"""
        return self.run_bash(code, env=env)

    def guard_root(self) -> None:
        """A fake root and next deployment (with its boot entry) the guards accept."""
        self.put("root/run/ostree-booted", "")
        self.put("next/usr/.keep", "")
        self.put("next" + self.at("/usr/lib/udev/rules.d/64-btrfs.rules"), UPSTREAM_64)
        self.boot_entry("ostree-1.conf", f"{self.tmp}/next")

    def boot_entry(self, name: str, deployment: str) -> None:
        """A boot entry in the fake root's /boot whose ostree= is this deployment."""
        self.put(
            f"root/boot/loader/entries/{name}",
            f"title e\noptions root=UUID={UUID} ostree={deployment} rw\ninitrd /ostree/e/initramfs.img\n",
        )

    def test_guards_accept_a_multi_device_btrfs_root_and_change_nothing(self) -> None:
        self.guard_root()
        before = self.tree()
        r = self.guards()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"UUID={UUID} ndev=2 NEXT_ROOT={self.tmp}/next\n")
        self.assertEqual(r.stderr, "")
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertIn(f"btrfs filesystem show {UUID}", self.calls())

    def test_guards_refuse(self) -> None:
        upstream = "/usr/lib/udev/rules.d/64-btrfs.rules"
        changed = UPSTREAM_64.replace('ENV{ID_BTRFS_READY}=="0", ENV{SYSTEMD_READY}="0"\n', "")
        cases: dict[str, tuple[dict[str, str], str, str]] = {
            "not ostree": ({"rm": "root/run/ostree-booted"}, "", "not an ostree-booted system"),
            "not btrfs": ({}, "FAKE_FSTYPE=ext4", "/sysroot is not btrfs"),
            "bad uuid": ({}, "FAKE_UUID=not-a-uuid", "unexpected root fs UUID 'not-a-uuid'"),
            "one device": ({}, "FAKE_NDEV=1", f"root btrfs {UUID} is not multi-device (devices: 1)"),
            "other root": (
                {"kargs": "root=UUID=11111111-2222-3333-4444-555555555555 rootflags=degraded\n"},
                "",
                f"next-boot root= is not UUID={UUID}",
            ),
            "no degraded": (
                {"kargs": f"root=UUID={UUID} rootflags=subvol=root,noatime\n"},
                "",
                "'degraded' missing from next-boot rootflags",
            ),
            "no next deployment": ({"rm": "next/usr/.keep"}, "", "cannot resolve next-boot deployment"),
            "booted 64-btrfs changed": (
                {"put": "root" + upstream},
                "",
                f"UPSTREAM CHANGED: {self.at(upstream)} no longer sets SYSTEMD_READY=0",
            ),
            "next-boot 64-btrfs changed": (
                {"put": "next" + self.at(upstream)},
                "",
                f"UPSTREAM CHANGED: {self.tmp}/next{self.at(upstream)} no longer sets SYSTEMD_READY=0",
            ),
        }
        for label, (change, env, error) in cases.items():
            with self.subTest(label):
                self.guard_root()
                self.put("root" + upstream, UPSTREAM_64)
                if "rm" in change:
                    os.remove(os.path.join(self.tmp, change["rm"]))
                    if change["rm"].endswith(".keep"):
                        os.rmdir(os.path.join(self.tmp, "next", "usr"))
                if "put" in change:
                    self.put(change["put"], changed)
                before = self.tree()
                key, _, value = env.partition("=")
                r = self.guards(kargs=change.get("kargs", self.GOOD_KARGS), env={key: value} if key else None)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f"ERROR: {error}", r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.mutating_calls(), [])

    def test_guards_refuse_without_a_boot_entry_for_the_next_deployment(self) -> None:
        """Every file tracked, install could not find the next boot's overlay: plan and install refuse before anything is written."""
        entries = self.at("/boot/loader/entries")
        cases = ("no entries folder", "an empty entries folder", "an entry for another deployment only")
        for case in cases:
            with self.subTest(case):
                self.guard_root()
                shutil.rmtree(self.at("/boot"))
                if case != "no entries folder":
                    os.makedirs(entries)
                if case == "an entry for another deployment only":
                    os.makedirs(os.path.join(self.tmp, "other"), exist_ok=True)
                    self.boot_entry("ostree-2.conf", f"{self.tmp}/other")
                self.status_json(fixture("unpinned.json", {"initramfs-etc": [self.at(p) for p in TRACKED]}))
                before = self.tree()
                r = self.guards()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(
                    r.stderr,
                    f"ERROR: no boot entry for the next deployment {self.tmp}/next in {entries} "
                    f"(is {self.at('/boot')} mounted?). Nothing was changed.\n",
                )
                self.assertEqual(r.stdout, "")
                self.assertEqual(self.tree(), before)
                self.assertEqual(self.mutating_calls(), [])

    def test_guards_need_no_boot_entry_when_install_compares_no_overlay(self) -> None:
        """A first install only tracks the files, and files-only leaves the initramfs alone: no entry needed."""
        some = [self.at(p) for p in TRACKED[:2]]
        every = [self.at(p) for p in TRACKED]
        cases = {"first install": ([], ()), "some files tracked": (some, ()), "files-only": (every, ("--files-only",))}
        for case, (tracked, args) in cases.items():
            with self.subTest(case):
                self.guard_root()
                shutil.rmtree(self.at("/boot"))
                self.status_json(fixture("unpinned.json", {"initramfs-etc": tracked}))
                r = self.guards(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
                self.assertEqual(r.stdout, f"UUID={UUID} ndev=2 NEXT_ROOT={self.tmp}/next\n")

    def test_guards_need_no_boot_entry_for_a_staged_deployment(self) -> None:
        """ostree writes a staged deployment's boot entry only at shutdown."""
        self.guard_root()
        shutil.rmtree(self.at("/boot"))
        self.status_json(fixture("staged-clean.json"))
        r = self.guards()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")

    def test_guards_only_warn_without_rd_luks_uuid(self) -> None:
        self.guard_root()
        r = self.guards(kargs=f"root=UUID={UUID} rootflags=degraded\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: no rd.luks.uuid= in next-boot kargs", r.stderr)

    # -------------------------------------------------------- next_overlay --
    def test_next_overlay_of_a_staged_deployment(self) -> None:
        """A staged deployment: the overlay named after "overlay-initrds" in /run/ostree/staged-deployment."""
        self.status_json(fixture("staged-clean.json"))
        before, after = "a" * 64, "c" * 64
        staged = self.at("/run/ostree/staged-deployment")
        os.makedirs(os.path.dirname(staged))
        with open(staged, "wb") as f:
            f.write(b"\x00" + before.encode() + b"\x00overlay-initrds\x00\x01" + after.encode() + b"\x00")
        out = self.ok("next_overlay", stub_overlay=False)
        self.assertEqual(out, self.at(f"/run/ostree/staged-initrds/{after}") + "\n")
        with open(staged, "wb") as f:
            f.write(b"\x00" + before.encode() + b"\x00no overlay here\x00")
        self.assertEqual(self.ok("next_overlay", stub_overlay=False), "")

    def test_next_overlay_of_the_booted_deployment_reads_its_boot_entry(self) -> None:
        """No staged deployment: the overlay initrd of the boot entry whose ostree= is the next root."""
        self.status_json(fixture("unpinned.json"))
        os.makedirs(os.path.join(self.tmp, "deploy", "next"))
        os.makedirs(os.path.join(self.tmp, "deploy", "other"))
        os.makedirs(os.path.join(self.tmp, "boot.1"))
        os.symlink("../deploy/next", os.path.join(self.tmp, "boot.1", "0"))
        entries = "root/boot/loader/entries"
        self.put(
            f"{entries}/ostree-1.conf",
            f"title old\noptions root=UUID={UUID} ostree={self.tmp}/deploy/other\n"
            "initrd /ostree/old/initramfs.img /ostree/initramfs-overlays/old.img\n",
        )
        self.put(
            f"{entries}/ostree-2.conf",
            f"title next\noptions root=UUID={UUID} ostree={self.tmp}/boot.1/0 rw\n"
            "initrd /ostree/next/initramfs.img /ostree/initramfs-overlays/next.img\n",
        )
        code = f"""
eval "real_$(declare -f ostree_query)"
ostree_query() {{ if [[ "$1" == next-root ]]; then echo "{self.tmp}/deploy/next"; else real_ostree_query "$1"; fi; }}
# Its callers read its output only (an entry with no overlay ends in a failed grep).
next_overlay || :
"""
        self.assertEqual(self.ok(code, stub_overlay=False), self.at("/boot/ostree/initramfs-overlays/next.img") + "\n")
        self.put(f"{entries}/ostree-2.conf", f"options ostree={self.tmp}/boot.1/0\ninitrd /ostree/next/initramfs.img\n")
        self.assertEqual(self.ok(code, stub_overlay=False), "")

    def test_next_overlay_without_a_boot_entry_says_so(self) -> None:
        """No boot entry for the next deployment: no overlay, exit status 3
        (the callers word the message) and nothing on stderr. An unmatched
        pattern never reaches awk; an entry awk really cannot read still shows."""
        self.status_json(fixture("unpinned.json"))
        os.makedirs(os.path.join(self.tmp, "deploy", "next"))
        os.makedirs(os.path.join(self.tmp, "deploy", "other"))
        code = f"""
eval "real_$(declare -f ostree_query)"
ostree_query() {{ if [[ "$1" == next-root ]]; then echo "{self.tmp}/deploy/next"; else real_ostree_query "$1"; fi; }}
rc=0; ov="$(next_overlay)" || rc=$?
echo "[$ov] $rc"
"""
        entries = self.at("/boot/loader/entries")
        for case in ("no entries folder", "an empty entries folder", "an entry for another deployment only"):
            with self.subTest(case):
                if case == "an empty entries folder":
                    os.makedirs(entries)
                if case == "an entry for another deployment only":
                    self.boot_entry("ostree-1.conf", f"{self.tmp}/deploy/other")
                r = self.run_bash(code, stub_overlay=False)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "[] 3\n", ""))
        os.makedirs(os.path.join(entries, "odd.conf"))  # not a file: awk says so
        r = self.run_bash(code, stub_overlay=False)
        self.assertEqual((r.returncode, r.stdout), (0, "[] 3\n"))
        self.assertIn("odd.conf", r.stderr)

    # ---------------------------------------------------- overlay_in_sync --
    def overlay_rc(self, overlay: str, *, rc: int = 0) -> str:
        """overlay_in_sync's exit status, with next_overlay printing overlay and exiting with rc."""
        return self.ok(
            f'next_overlay() {{ echo "{overlay}"; return {rc}; }}; rc=0; overlay_in_sync || rc=$?; echo "$rc"'
        ).strip()

    def test_overlay_in_sync_states(self) -> None:
        self.install()
        same = self.tracked_bytes()
        cases: dict[str, tuple[dict[str, bytes], str]] = {
            "same files, same bytes": (same, "0"),
            "a file differs": ({**same, RULE: b"old rule\n"}, "1"),
            "a tracked file is missing": ({k: v for k, v in same.items() if k != HELPER}, "1"),
            "an extra file": ({**same, OLD_RULE: b"old\n"}, "1"),
        }
        for label, (files, rc) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.overlay_rc(self.overlay(files)), rc)
        with self.subTest("no overlay found"):
            self.assertEqual(self.overlay_rc(""), "2")
        with self.subTest("unreadable overlay"):
            self.assertEqual(self.overlay_rc(self.put("garbage.img", "not an archive\n")), "2")
        with self.subTest("no boot entry for the next deployment"):
            self.assertEqual(self.overlay_rc("", rc=3), "3")

    # ---------------------------------------------------- generated files --
    def test_generated_rule_and_units_for_the_root_uuid(self) -> None:
        self.install()
        self.assertEqual(self.read("verified.rules"), EXPECTED_RULE)  # what udevadm verify checked
        for dest, text, mode in (
            (RULE, EXPECTED_RULE, 0o644),
            (TIMER, EXPECTED_TIMER, 0o644),
            (SERVICE, EXPECTED_SERVICE, 0o644),
            (RECORD, EXPECTED_RECORD, 0o644),
        ):
            with self.subTest(dest):
                self.assertEqual(self.read("root" + dest), text)
                self.assertEqual(os.stat(self.at(dest)).st_mode & 0o7777, mode)
        self.assertIn("udevadm verify --resolve-names=never --no-style", "\n".join(self.calls()))

    def test_generated_helper_is_executable_bash(self) -> None:
        self.install()
        helper = self.at(HELPER)
        self.assertEqual(os.stat(helper).st_mode & 0o7777, 0o755)
        self.assertTrue(self.read("root" + HELPER).startswith("#!/usr/bin/bash\n"))
        r = subprocess.run(
            ["bash", "-n", helper], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_files_second_run_writes_nothing(self) -> None:
        self.install()
        r = self.write_files()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 5, r.stdout)
        self.assertTrue(all(line.startswith("unchanged: ") for line in lines), r.stdout)

    def test_units_are_verified_as_the_instances_the_rule_starts(self) -> None:
        """systemd-analyze verify gets the work folder's copies, named as the units the rule and the timer start."""
        self.install()
        analyze = [c for c in self.calls() if c.startswith("systemd-analyze ")]
        self.assertEqual(len(analyze), 1, analyze)
        self.assertRegex(
            analyze[0],
            rf"^systemd-analyze verify --recursive-errors=no \S+/units/btrfs-last-resort@\.timer:btrfs-last-resort@{UUID}\.timer "
            rf"\S+/units/btrfs-last-resort@\.service:btrfs-last-resort@{UUID}\.service "
            r"\S+/units/btrfs-degraded-boot-record\.service$",
        )
        self.assertNotIn("/etc/", analyze[0])
        verified = os.path.join(self.tmp, "verified-units")
        self.assertEqual(
            {name: self.read(f"verified-units/{name}") for name in os.listdir(verified)},
            {
                f"btrfs-last-resort@{UUID}.timer": EXPECTED_TIMER,
                f"btrfs-last-resort@{UUID}.service": EXPECTED_SERVICE,
                "btrfs-degraded-boot-record.service": EXPECTED_RECORD,
            },
        )

    def previous_install_with_local_edits(self) -> None:
        """An install whose units then changed by hand (an install would write them back)."""
        self.install()
        with open(self.at(TIMER), "a", encoding="utf-8") as f:
            f.write("# local edit\n")
        self.put("root" + SERVICE, "# an older service\n")

    def test_units_that_fail_systemd_analyze_verify_leave_nothing_written(self) -> None:
        """The units are verified before anything is written: a failed verify keeps the previous files as they are."""
        cases = {
            "verify fails": {"FAKE_ANALYZE_RC": "1"},
            "an unknown key (a failure with --recursive-errors=no)": {"FAKE_UNIT_TYPO": "1"},
        }
        for previous in (False, True):
            for label, env in cases.items():
                with self.subTest(label, previous_install=previous):
                    shutil.rmtree(self.root)
                    self.put("root/usr/lib/udev/rules.d/64-btrfs.rules", UPSTREAM_64)
                    if previous:
                        self.previous_install_with_local_edits()
                    self.put("calls", "")
                    before = self.tree()
                    r = self.write_files(env=env)
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn(
                        "ERROR: systemd-analyze verify failed for the generated units; nothing was written", r.stderr
                    )
                    if "FAKE_UNIT_TYPO" in env:
                        self.assertIn("Unknown key 'TimeoutStratSec'", r.stderr)
                    self.assertEqual(r.stdout, "")
                    self.assertEqual(self.tree(), before)
                    self.assertEqual(os.path.exists(self.at("/etc")), previous)
                    self.assertEqual(self.mutating_calls(), [])

    def test_a_helper_with_a_syntax_error_leaves_nothing_written(self) -> None:
        """bash -n runs on the work folder's copy of the helper, before anything is written."""
        self.previous_install_with_local_edits()
        files = self.read("lib/btrfs/files.sh")
        self.assertEqual(files.count("\nset -u\n"), 1)
        self.put("lib/btrfs/files.sh", files.replace("\nset -u\n", "\nset -u\nif then\n"))
        before = self.tree()
        r = self.write_files()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"ERROR: syntax error in the generated {HELPER}", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.tree(), before)

    def test_rule_that_fails_udevadm_verify_leaves_nothing_written(self) -> None:
        """The rule is verified before the helper and the units are written: a bad one writes nothing."""
        before = self.tree()
        r = self.write_files(env={"FAKE_VERIFY_RC": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("ERROR: udevadm verify failed for the generated rule", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.exists(self.at("/etc")))
        self.assertEqual(self.read("verified.rules"), EXPECTED_RULE)

    def test_tracked_sums_fails_on_a_file_it_cannot_read(self) -> None:
        """The recorded sums never hold an empty hash: a missing tracked file fails tracked_sums."""
        self.install()
        self.assertEqual(self.ok("tracked_sums"), self.ok('sha256sum "${TRACKED[@]}"'))
        os.remove(self.at(TIMER))
        r = self.run_bash("tracked_sums")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertNotIn(f"  {self.at(TIMER)}\n", r.stdout)

    # ---------------------------------------------------------- self-test --
    def selftest(
        self,
        *,
        ready: dict[str, bool],
        rule_was_present: int = 0,
        env: dict[str, str] | None = None,
        ndev: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """The self-test with these lsblk members; ndev (the guards' btrfs device count) defaults to their number."""
        self.install()
        lsblk = "".join(f"{name} btrfs {UUID}\n" for name in ready) + "nvme2n1p1 vfat 1234-ABCD\n"
        self.put("lsblk", lsblk)
        for name, is_ready in ready.items():
            self.put(f"udev-test-{name}", f"Reading rules file: {self.at(RULE)}\n")
            props = "ID_BTRFS_READY=1\n" if is_ready else "ID_BTRFS_READY=0\nSYSTEMD_READY=0\n"
            self.put(f"udev-info-{name}", props)
        self.put("calls", "")
        count = len(ready) if ndev is None else ndev
        code = f"UUID={UUID}\nndev={count}\nrule_was_present={rule_was_present}\n" + self.section("self-test")
        return self.run_bash(code, env=env)

    def test_selftest_passes_when_every_member_is_ready(self) -> None:
        r = self.selftest(ready={"dm-0": True, "dm-1": True})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("self-test: dm-0: rule loaded, ID_BTRFS_READY=1, SYSTEMD_READY not 0", r.stdout)
        self.assertIn("self-test: dm-1: rule loaded, ID_BTRFS_READY=1, SYSTEMD_READY not 0", r.stdout)
        self.assertEqual(
            self.mutating_calls(),
            [
                "systemctl daemon-reload",
                "udevadm control --reload",
                "systemctl enable btrfs-degraded-boot-record.service",
            ],
        )

    def test_selftest_failure_removes_a_new_rule(self) -> None:
        r = self.selftest(ready={"dm-0": True, "dm-1": False})
        self.assertEqual(r.returncode, 1)
        self.assertIn("self-test: dm-1: NOT ready: ID_BTRFS_READY=0 SYSTEMD_READY=0", r.stderr)
        self.assertIn("ERROR: udev self-test failed; no initramfs change made", r.stderr)
        self.assertFalse(os.path.exists(self.at(RULE)))
        self.assertEqual(self.mutating_calls()[-1], "udevadm control --reload")

    def test_selftest_failure_keeps_a_rule_that_was_there(self) -> None:
        r = self.selftest(ready={"dm-0": False, "dm-1": True}, rule_was_present=1)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(self.at(RULE)))

    def test_selftest_fails_when_lsblk_lists_another_member_count(self) -> None:
        """Every member btrfs counts must be checked: fewer (or more) in lsblk fails, all ready or not."""
        cases: dict[str, tuple[dict[str, bool], int, int]] = {
            "no member listed": ({}, 2, 0),
            "one of two": ({"dm-0": True}, 2, 1),
            "three of two": ({"dm-0": True, "dm-1": True, "dm-2": True}, 2, 3),
        }
        for label, (ready, ndev, listed) in cases.items():
            with self.subTest(label):
                r = self.selftest(ready=ready, ndev=ndev)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f"self-test: lsblk lists {listed} member(s) of {UUID}, btrfs counts {ndev}", r.stderr)
                self.assertIn("ERROR: udev self-test failed; no initramfs change made", r.stderr)
                self.assertFalse(os.path.exists(self.at(RULE)))

    # -------------------------------------------------- initramfs overlay --
    def overlay_step(self, *args: str, overlay: str = "", rc: int = 0) -> str:
        """The initramfs overlay section, with status (its last step) replaced by a marker;
        next_overlay prints overlay and exits with rc."""
        self.put("calls", "")
        code = f'next_overlay() {{ echo "{overlay}"; return {rc}; }}\nstatus() {{ echo STATUS; }}\n' + self.section(
            "initramfs-overlay", *args
        )
        return self.ok(code)

    def test_files_only_leaves_the_initramfs_alone(self) -> None:
        self.install()
        out = self.overlay_step("--files-only")
        self.assertEqual(out, "--files-only: initramfs overlay not updated\n")
        self.assertEqual(self.mutating_calls(), [])

    def test_first_install_tracks_every_file_and_records_the_sums(self) -> None:
        self.install()
        self.tracked_json([])
        out = self.overlay_step()
        track = " ".join(f"--track={self.at(p)}" for p in TRACKED)
        self.assertEqual(self.mutating_calls(), [f"rpm-ostree initramfs-etc {track}"])
        self.assertIn(f"written:   {self.at(SYNCED)}", out)
        self.assertTrue(out.endswith("STATUS\n"), out)
        sums = self.read("root" + SYNCED).splitlines()
        self.assertEqual([line.split()[1] for line in sums], [self.at(p) for p in TRACKED])

    def test_only_the_untracked_files_are_added(self) -> None:
        self.install()
        self.tracked_json([RULE, HELPER])
        self.overlay_step()
        self.assertEqual(
            self.mutating_calls(), [f"rpm-ostree initramfs-etc --track={self.at(SERVICE)} --track={self.at(TIMER)}"]
        )

    def test_second_install_with_the_overlay_in_sync_changes_nothing(self) -> None:
        self.install()
        self.tracked_json(list(TRACKED))
        overlay = self.overlay(self.tracked_bytes())
        self.overlay_step(overlay=overlay)
        out = self.overlay_step(overlay=overlay)
        self.assertIn("unchanged: initramfs-etc (tracked, next-boot overlay in sync)", out)
        self.assertIn(f"unchanged: {self.at(SYNCED)}", out)
        self.assertEqual(self.mutating_calls(), [])

    def test_an_out_of_sync_overlay_is_synced(self) -> None:
        self.install()
        self.tracked_json(list(TRACKED))
        out = self.overlay_step(overlay=self.overlay({**self.tracked_bytes(), TIMER: b"old timer\n"}))
        self.assertIn("next-boot overlay differs from /etc: rpm-ostree initramfs-etc --force-sync", out)
        self.assertEqual(self.mutating_calls(), ["rpm-ostree initramfs-etc --force-sync"])

    def test_without_an_overlay_the_recorded_sums_decide(self) -> None:
        """Overlay not found (2), or no boot entry (3: the guards refuse that first, unless the entry goes between
        them and this step): the sums recorded at the last sync decide."""
        for case, rc in (("overlay not found", 0), ("no boot entry", 3)):
            with self.subTest(case):
                shutil.rmtree(self.root)
                self.put("root/usr/lib/udev/rules.d/64-btrfs.rules", UPSTREAM_64)
                self.install()
                self.tracked_json(list(TRACKED))
                out = self.overlay_step(rc=rc)  # no sums yet: sync and record them
                self.assertIn("next-boot overlay differs from /etc: rpm-ostree initramfs-etc --force-sync", out)
                self.assertEqual(self.mutating_calls(), ["rpm-ostree initramfs-etc --force-sync"])
                out = self.overlay_step(rc=rc)  # same sums: nothing to do
                self.assertIn("unchanged: initramfs-etc (tracked, next-boot overlay in sync)", out)
                self.assertEqual(self.mutating_calls(), [])
                with open(self.at(HELPER), "a", encoding="utf-8") as f:
                    f.write("# changed\n")
                self.overlay_step(rc=rc)  # a tracked file changed since the last sync
                self.assertEqual(self.mutating_calls(), ["rpm-ostree initramfs-etc --force-sync"])

    def test_old_full_rule_of_the_first_version_is_migrated(self) -> None:
        self.install()
        self.put("root" + OLD_RULE, "# Generated by btrfs-degraded-boot-setup.sh (CST)\nold shadow rule\n")
        self.put("root" + OLD_SUM, "abc\n")
        self.tracked_json([*TRACKED, OLD_RULE])
        self.overlay_step()
        self.assertEqual(
            self.mutating_calls(),
            [f"rpm-ostree initramfs-etc --untrack={self.at(OLD_RULE)}", "udevadm control --reload"],
        )
        self.assertFalse(os.path.exists(self.at(OLD_RULE)))
        self.assertFalse(os.path.exists(self.at(OLD_SUM)))

    def test_a_foreign_64_btrfs_rule_is_left_alone(self) -> None:
        self.install()
        self.put("root" + OLD_RULE, "someone else's rule\n")
        self.tracked_json([*TRACKED, OLD_RULE])
        overlay = self.overlay({**self.tracked_bytes()})
        self.overlay_step(overlay=overlay)
        self.assertNotIn("--untrack", "\n".join(self.mutating_calls()))
        self.assertTrue(os.path.exists(self.at(OLD_RULE)))

    # ---------------------------------------------------------------- plan --
    def udev_ready(self) -> None:
        """lsblk lists two members of the root fs, udev loads the rule and sees both ready."""
        self.put("lsblk", f"dm-0 btrfs {UUID}\ndm-1 btrfs {UUID}\nnvme2n1p1 vfat 1234-ABCD\n")
        for name in ("dm-0", "dm-1"):
            self.put(f"udev-test-{name}", f"Reading rules file: {self.at(RULE)}\n")
            self.put(f"udev-info-{name}", "ID_BTRFS_READY=1\n")

    def install_path(self, *, plan: bool, overlay: str = "") -> subprocess.CompletedProcess[str]:
        """The files, self-test and initramfs overlay sections in one run, as install (or --plan) runs them; status stubbed."""
        tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(tmpdir, exist_ok=True)
        self.put("calls", "")
        code = f"""UUID={UUID}
ndev=2
plan={int(plan)}
{GENERATE}
set -- {"--plan" if plan else ""}
. "{self.tmp}/lib/btrfs/files.sh"
{REPOINT}
next_overlay() {{ echo "{overlay}"; }}
status() {{ echo STATUS; }}
. "{self.tmp}/lib/btrfs/self-test.sh"
. "{self.tmp}/lib/btrfs/initramfs-overlay.sh"
"""
        r = self.run_bash(code, repoint=False, env={"TMPDIR": tmpdir})
        self.assertEqual(os.listdir(tmpdir), [], "the work folder is removed")
        return r

    def plan(self, *, overlay: str = "") -> str:
        """--plan, which must change nothing: no file of the fake root, no call that changes the machine."""
        before = self.tree()
        r = self.install_path(plan=True, overlay=overlay)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.mutating_calls(), [])
        self.assertTrue(r.stdout.endswith("plan: nothing was changed\n"), r.stdout)
        self.assertNotIn("STATUS", r.stdout)
        self.assertNotIn("written:", r.stdout)
        return r.stdout

    def installed(self) -> None:
        """A first install (every file tracked from then on), the record unit enabled."""
        self.udev_ready()
        self.tracked_json([])
        r = self.install_path(plan=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.tracked_json(list(TRACKED))
        self.put("enabled", "")

    def test_plan_on_a_clean_root(self) -> None:
        self.udev_ready()
        self.tracked_json([])
        out = self.plan()
        for dest in (HELPER, TIMER, SERVICE, RECORD, RULE, SYNCED):
            self.assertIn(f"would write: {self.at(dest)}\n", out)
        self.assertIn(f"self-test: runs at install; lsblk lists 2 member(s) of {UUID}, btrfs counts 2\n", out)
        self.assertIn("would run: systemctl enable btrfs-degraded-boot-record.service\n", out)
        track = " ".join(f"--track={self.at(p)}" for p in TRACKED)
        self.assertIn(f"would run: rpm-ostree initramfs-etc {track}\n", out)
        self.assertIn(f"udevadm verify --resolve-names=never --no-style {self.tmp}/tmpdir/", "\n".join(self.calls()))
        self.assertEqual(self.read("verified.rules"), EXPECTED_RULE)

    def test_plan_on_an_installed_root_would_change_nothing(self) -> None:
        self.installed()
        overlay = self.overlay(self.tracked_bytes())
        out = self.plan(overlay=overlay)
        self.assertEqual([line for line in out.splitlines() if line.startswith("would")], [], out)
        for dest in (HELPER, TIMER, SERVICE, RECORD, RULE, SYNCED):
            self.assertIn(f"unchanged: {self.at(dest)}\n", out)
        self.assertIn("unchanged: btrfs-degraded-boot-record.service enabled\n", out)
        self.assertIn("unchanged: initramfs-etc (tracked, next-boot overlay in sync)\n", out)

    def test_plan_stops_where_install_stops(self) -> None:
        """A check plan shares with install stops it the same way: a rule that fails udevadm verify,
        units that fail systemd-analyze verify. (The self-test needs the files written: install runs it, plan does not.)"""
        self.udev_ready()
        self.tracked_json([])
        before = self.tree()
        cases = {
            "FAKE_VERIFY_RC": "ERROR: udevadm verify failed for the generated rule",
            "FAKE_ANALYZE_RC": "ERROR: systemd-analyze verify failed for the generated units; nothing was written",
        }
        for key, error in cases.items():
            with self.subTest(key):
                r = self.run_bash(
                    f"plan=1\nUUID={UUID}\n{GENERATE}\n{self.section('files', '--plan')}",
                    repoint=False,
                    env={key: "1"},
                )
                self.assertEqual(r.returncode, 1)
                self.assertIn(error, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertEqual(self.tree(), before)

    def test_plan_warns_when_the_self_test_would_fail(self) -> None:
        self.udev_ready()
        self.put("lsblk", f"dm-0 btrfs {UUID}\n")
        self.tracked_json([])
        r = self.install_path(plan=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(
            "WARNING: the self-test of an install would fail: lsblk lists 1 member(s), btrfs counts 2", r.stderr
        )

    def assert_plan_predicts_install(self, *, overlay: str = "", runs: list[str] | None = None) -> None:
        """What plan says it would write and run is what install then writes and runs."""
        out = self.plan(overlay=overlay)
        would_write = sorted(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("would write: "))
        would_run = [
            line.removeprefix("would run: ") for line in out.splitlines() if line.startswith("would run: rpm-ostree ")
        ]
        r = self.install_path(plan=False, overlay=overlay)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        written = sorted(line.split()[1] for line in r.stdout.splitlines() if line.startswith("written: "))
        self.assertEqual(would_write, written)
        self.assertEqual(would_run, [c for c in self.mutating_calls() if c.startswith("rpm-ostree ")])
        if runs is not None:
            self.assertEqual(would_run, runs)

    def test_plan_predicts_a_first_install(self) -> None:
        self.udev_ready()
        self.tracked_json([])
        self.assert_plan_predicts_install(
            runs=[" ".join(["rpm-ostree initramfs-etc", *(f"--track={self.at(p)}" for p in TRACKED)])]
        )

    def test_plan_predicts_a_second_install(self) -> None:
        self.installed()
        self.assert_plan_predicts_install(overlay=self.overlay(self.tracked_bytes()), runs=[])

    def test_plan_predicts_a_local_edit_put_back(self) -> None:
        """/etc edited by hand, the overlay already holds what install writes: one file back, no deployment."""
        self.installed()
        overlay = self.overlay(self.tracked_bytes())
        with open(self.at(TIMER), "a", encoding="utf-8") as f:
            f.write("# local edit\n")
        self.assertIn(f"would write: {self.at(TIMER)}\n", self.plan(overlay=overlay))
        self.assert_plan_predicts_install(overlay=overlay, runs=[])

    def test_plan_predicts_the_sync_after_an_older_version(self) -> None:
        """/etc and the overlay both hold an older timer: comparing them would say "in sync"; install syncs."""
        self.installed()
        with open(self.at(TIMER), "w", encoding="utf-8") as f:
            f.write("old timer\n")
        overlay = self.overlay(self.tracked_bytes())
        self.assert_plan_predicts_install(overlay=overlay, runs=["rpm-ostree initramfs-etc --force-sync"])

    def test_plan_predicts_the_sync_without_an_overlay_or_sums(self) -> None:
        self.installed()
        os.remove(self.at(SYNCED))
        self.assert_plan_predicts_install(runs=["rpm-ostree initramfs-etc --force-sync"])

    def test_plan_predicts_the_migration_of_the_first_version(self) -> None:
        self.installed()
        self.put("root" + OLD_RULE, "# Generated by btrfs-degraded-boot-setup.sh (CST)\nold shadow rule\n")
        self.put("root" + OLD_SUM, "abc\n")
        self.tracked_json([*TRACKED, OLD_RULE])
        overlay = self.overlay(self.tracked_bytes())
        out = self.plan(overlay=overlay)
        self.assertIn(f"would run: rm -fv {self.at(OLD_RULE)}\n", out)
        self.assertIn(f"would run: rm -fv {self.at(OLD_SUM)}\n", out)
        self.assert_plan_predicts_install(
            overlay=overlay, runs=[f"rpm-ostree initramfs-etc --untrack={self.at(OLD_RULE)}"]
        )
        self.assertFalse(os.path.exists(self.at(OLD_RULE)))

    # ------------------------------------------------------------- upgrade --
    def test_an_install_of_the_previous_version_stays_current(self) -> None:
        """The files and the sums file as the previous version wrote them: status, plan and a second install see them as current."""
        self.installed()
        for dest, digest in PREVIOUS_SHA256.items():
            with open(self.at(dest), "rb") as f:
                self.assertEqual(
                    hashlib.sha256(f.read()).hexdigest(), digest, f"{dest} is not what the previous version wrote"
                )
        # The previous version recorded the sums with one `sha256sum` call.
        self.ok('sha256sum "${TRACKED[@]}" >"${SYNCED_SUM_FILE}"')
        overlay = self.overlay(self.tracked_bytes())
        before = self.tree()
        summary = self.summary(self.status(overlay=overlay))
        self.assertIn("installed:        yes", summary)
        self.assertIn("next boot:        ready (overlay in sync with /etc)", summary)
        self.assertNotIn("would", self.plan(overlay=overlay))
        for found in (overlay, ""):  # without an overlay, the recorded sums decide
            with self.subTest(overlay=bool(found)):
                r = self.install_path(plan=False, overlay=found)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn("written:", r.stdout)
                self.assertIn("unchanged: initramfs-etc (tracked, next-boot overlay in sync)\n", r.stdout)
                self.assertEqual([c for c in self.mutating_calls() if c.startswith("rpm-ostree ")], [])
                self.assertEqual(self.tree(), before)

    # ------------------------------------------------------------- status --
    def status(self, *, overlay: str | None = "") -> str:
        """--status, with the next deployment's root under $FAKE/next; overlay=None keeps the real next_overlay."""
        self.put("next" + self.at("/usr/lib/udev/rules.d/64-btrfs.rules"), UPSTREAM_64)
        self.put("calls", "")
        stub = "" if overlay is None else f'next_overlay() {{ echo "{overlay}"; }}'
        code = f"""
eval "real_$(declare -f ostree_query)"
ostree_query() {{ if [[ "$1" == next-root ]]; then echo "{self.tmp}/next"; else real_ostree_query "$1"; fi; }}
{stub}
status
"""
        return self.ok(code, stub_overlay=overlay is not None)

    def summary(self, out: str) -> list[str]:
        return out.split("================ summary ================\n", 1)[1].splitlines()

    def test_status_of_a_complete_install_after_a_normal_boot(self) -> None:
        self.install()
        self.put("enabled", "")
        self.tracked_json(list(TRACKED))
        self.put("journal-started", "1731800000.250000 host systemd[1]: Started btrfs-last-resort@x.timer\n")
        self.put("journal-stopped", "1731800002.750000 host systemd[1]: Stopped btrfs-last-resort@x.timer\n")
        out = self.status(overlay=self.overlay(self.tracked_bytes()))
        self.assertIn(f"root fs uuid: {UUID}\n", out)
        self.assertIn("  btrfs-degraded-boot-record.service: enabled\n", out)
        self.assertIn("semantics: booted ok, next boot ok\n", out)
        self.assertIn(f"next-boot initramfs overlay {self.tmp}/overlay.img: in sync with /etc\n", out)
        self.assertIn(f"  missing                    {self.at(SYNCED)}\n", out)
        self.assertEqual(
            self.summary(out),
            [
                "installed:        yes",
                "booted initramfs: active (all 4 files in this boot's initramfs-etc overlay)",
                "this boot:        normal - rule ran in the initramfs: timer armed when the first member appeared,",
                "                  cancelled 2.5 s later when the last one arrived",
                "past degraded:    none recorded",
                "next boot:        ready (overlay in sync with /etc)",
                "verdict:          OK - an NVMe failure at boot would mount degraded after 45 s and alert",
            ],
        )

    def test_status_when_nothing_is_installed(self) -> None:
        out = self.status()
        self.assertIn(f"  missing                    {self.at(RULE)}\n", out)
        self.assertIn("next-boot initramfs overlay: not found\n", out)
        summary = self.summary(out)
        self.assertTrue(summary[0].startswith(f"installed:        NO - missing {self.at(RULE)} "), summary[0])
        self.assertTrue(
            summary[1].startswith("booted initramfs: NOT active - not in this boot's overlay: "), summary[1]
        )
        self.assertEqual(
            summary[2:],
            [
                "this boot:        no trace of the rule (both members appeared at once, or it did not run)",
                "past degraded:    none recorded",
                "next boot:        overlay not found - re-run this script before rebooting",
                "verdict:          ATTENTION - see the lines above",
            ],
        )

    def test_status_of_a_degraded_boot_and_a_stale_overlay(self) -> None:
        self.install()
        self.put("enabled", "")
        self.tracked_json(list(TRACKED))
        self.put(f"root/run/btrfs-allow-degraded/{UUID}", "")
        self.put(f"root/var/lib/btrfs-degraded-boot/degraded-{UUID}", "since=1\n")
        out = self.status(overlay=self.overlay({**self.tracked_bytes(), RULE: b"old\n"}))
        self.assertIn("OUT OF SYNC with /etc (re-run installer)", out)
        self.assertEqual(
            self.summary(out)[2:],
            [
                "this boot:        DEGRADED - a member was missing and the fs was mounted degraded. Restore RAID1!",
                "past degraded:    YES - marker(s) in "
                + self.at("/var/lib/btrfs-degraded-boot")
                + "; cleared after a clean scrub with RAID1 complete",
                "next boot:        OUT OF SYNC - re-run this script before rebooting",
                "verdict:          ATTENTION - see the lines above",
            ],
        )

    def test_status_without_a_boot_entry_for_the_next_deployment(self) -> None:
        """No boot entry (here: no /boot in the fake root at all): status says so, not "not found"."""
        self.install()
        self.put("enabled", "")
        self.status_json(fixture("unpinned.json", {"initramfs-etc": [self.at(p) for p in TRACKED]}))
        out = self.status(overlay=None)
        boot = self.at("/boot")
        self.assertIn(
            "next-boot initramfs overlay: unknown - no boot entry for the next deployment in "
            f"{boot}/loader/entries (is {boot} mounted?)\n",
            out,
        )
        self.assertEqual(
            self.summary(out)[-2:],
            [
                f"next boot:        NO BOOT ENTRY for the next deployment in {boot}/loader/entries (is {boot} mounted?)",
                "verdict:          ATTENTION - see the lines above",
            ],
        )

    def test_status_timer_that_never_stopped(self) -> None:
        self.install()
        self.put("enabled", "")
        self.tracked_json(list(TRACKED))
        self.put("journal-started", "1731800000.250000 host systemd[1]: Started btrfs-last-resort@x.timer\n")
        out = self.status(overlay=self.overlay(self.tracked_bytes()))
        self.assertIn(
            f"this boot:        timer started but never stopped and no degraded flag: check 'journalctl -b -u btrfs-last-resort@{UUID}.timer'",
            self.summary(out),
        )

    def test_status_changes_nothing(self) -> None:
        """--status is the read-only view: no file changes, no command that changes the machine."""
        self.install()
        self.put("enabled", "")
        self.tracked_json(list(TRACKED))
        self.put(f"root/run/btrfs-allow-degraded/{UUID}", "")
        before = self.tree()
        self.status(overlay=self.overlay({**self.tracked_bytes(), RULE: b"old\n"}))
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.mutating_calls(), [])

    def test_status_shows_a_leftover_old_override(self) -> None:
        self.put("root" + OLD_RULE, "# Generated by btrfs-degraded-boot-setup.sh (CST)\n")
        self.assertIn(f"  leftover old override: {self.at(OLD_RULE)} (re-run installer to migrate)\n", self.status())

    # ------------------------------------------------------ install record --
    def record(self) -> tuple[dict[str, object], dict[str, object]]:
        """The files and the component states of the install record the harness keeps in $FAKE/home."""
        path = os.path.join(self.tmp, "home", USER_RECORD_REL)
        if not os.path.exists(path):
            return {}, {}
        with open(path, encoding="utf-8") as f:
            doc = object_of(parse_json(f.read()))
        return object_of(doc["files"]), object_of(doc["components"])

    def installer_calls(self) -> list[str]:
        path = os.path.join(self.tmp, "installer-calls")
        return self.read("installer-calls").splitlines() if os.path.exists(path) else []

    def test_install_records_every_file_and_marks_the_component_after_the_overlay(self) -> None:
        self.installed()
        files, states = self.record()
        six = (HELPER, TIMER, SERVICE, RECORD, RULE, SYNCED)
        self.assertEqual(sorted(files), sorted(self.at(d) for d in six))
        for dest in six:
            f = object_of(files[self.at(dest)])
            with open(self.at(dest), "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(
                (f["component"], f["kind"], f["src"], f["sha256"]), ("btrfs:degraded-boot", "text", None, digest)
            )
        # The harness only logs mark (it would write root's record): the record says pending.
        self.assertEqual(states, {"btrfs:degraded-boot": "pending"})
        calls = self.installer_calls()
        mark = "installer mark --component btrfs:degraded-boot done"
        self.assertEqual(calls[-1], mark)
        self.assertEqual(calls.count(mark), 1)
        synced = next(i for i, c in enumerate(calls) if c.startswith(f"installer text {self.at(SYNCED)} "))
        self.assertLess(synced, calls.index(mark), "marked before the overlay step wrote its sums")

    def test_plan_and_files_only_mark_nothing(self) -> None:
        self.udev_ready()
        self.tracked_json([])
        self.plan()
        self.assertTrue(
            all(c.startswith("installer text-plan ") for c in self.installer_calls()), self.installer_calls()
        )
        self.assertEqual(self.record(), ({}, {}))
        self.install()
        self.overlay_step("--files-only")
        self.assertNotIn("mark", "\n".join(self.installer_calls()))
        self.assertEqual(self.record()[1], {"btrfs:degraded-boot": "pending"})

    def test_selftest_failure_drops_the_line_of_the_new_rule(self) -> None:
        r = self.selftest(ready={"dm-0": True, "dm-1": False})
        self.assertEqual(r.returncode, 1)
        files, _ = self.record()
        self.assertNotIn(self.at(RULE), files)
        self.assertEqual(sorted(files), sorted(self.at(d) for d in (HELPER, TIMER, SERVICE, RECORD)))

    def test_an_install_from_before_the_record_is_adopted(self) -> None:
        # What an older CST left: the same files, no line of them in the install record.
        self.installed()
        os.unlink(os.path.join(self.tmp, "home", USER_RECORD_REL))
        overlay = self.overlay(self.tracked_bytes())
        before = self.tree()
        r = self.install_path(plan=False, overlay=overlay)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("written:", r.stdout)
        self.assertEqual(self.tree(), before)
        files, _ = self.record()
        self.assertEqual(len(files), 6, sorted(files))

    # ---------------------------------------------------------- uninstall --
    def uninstall(self) -> subprocess.CompletedProcess[str]:
        self.put("calls", "")
        return self.run_bash("uninstall")

    def test_uninstall_refuses_on_a_degraded_boot(self) -> None:
        self.install()
        self.put(f"root/run/btrfs-allow-degraded/{UUID}", "")
        before = self.tree()
        r = self.uninstall()
        self.assertEqual(r.returncode, 1)
        self.assertIn("this boot is DEGRADED", r.stderr)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.mutating_calls(), [])

    def test_uninstall_removes_everything_and_untracks(self) -> None:
        self.install()
        self.put("root" + SYNCED, "sums\n")
        self.put("root" + OLD_RULE, "# Generated by btrfs-degraded-boot-setup.sh (CST)\n")
        self.tracked_json([], nxt=[*TRACKED, OLD_RULE])
        r = self.uninstall()
        self.assertEqual(r.returncode, 0, r.stderr)
        untrack = " ".join(f"--untrack={self.at(p)}" for p in (*TRACKED, OLD_RULE))
        self.assertEqual(
            self.mutating_calls(),
            [
                f"rpm-ostree initramfs-etc {untrack}",
                "systemctl disable btrfs-degraded-boot-record.service",
                "systemctl stop btrfs-last-resort@*.timer",
                "systemctl daemon-reload",
                "udevadm control --reload",
            ],
        )
        self.assertEqual(
            sorted(os.path.relpath(p, self.root) for p in self.tree()), ["usr/lib/udev/rules.d/64-btrfs.rules"]
        )
        self.assertFalse(os.path.exists(self.at("/etc/btrfs-degraded-boot")))
        self.assertTrue(r.stdout.endswith("Uninstalled (initramfs: from the next deployment on).\n"), r.stdout)
        # The installer removed the generated files, with their lines in the install record.
        for dest in (*TRACKED, RECORD, SYNCED):
            self.assertIn(f"removed:   {self.at(dest)}\n", r.stdout)
        self.assertEqual(self.record(), ({}, {}))

    def test_uninstall_with_a_record_it_cannot_change_still_removes_and_reloads(self) -> None:
        # The install record was written by a newer CST: the installer refuses to change it. The
        # initramfs is untracked first, so uninstall must not stop there: the files go without their
        # lines, and systemd and udev are reloaded.
        self.install()
        self.put("root" + SYNCED, "sums\n")
        self.tracked_json([], nxt=list(TRACKED))
        newer = '{"format": 999, "files": {}, "components": {}}\n'
        self.put(f"home/{USER_RECORD_REL}", newer)
        r = self.uninstall()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("written by a newer CST", r.stderr)
        self.assertIn(
            "WARNING: the install record was not changed (see above); removing the files without it\n", r.stderr
        )
        self.assertEqual(self.mutating_calls()[-2:], ["systemctl daemon-reload", "udevadm control --reload"])
        self.assertEqual(
            sorted(os.path.relpath(p, self.root) for p in self.tree()), ["usr/lib/udev/rules.d/64-btrfs.rules"]
        )
        self.assertTrue(r.stdout.endswith("Uninstalled (initramfs: from the next deployment on).\n"), r.stdout)
        self.assertEqual(self.read(f"home/{USER_RECORD_REL}"), newer)

    def test_uninstall_keeps_a_foreign_64_btrfs_rule_and_past_markers(self) -> None:
        self.install()
        self.put("root" + OLD_RULE, "someone else's rule\n")
        self.put(f"root/var/lib/btrfs-degraded-boot/degraded-{UUID}", "since=1\n")
        self.tracked_json([], nxt=[RULE, OLD_RULE])
        r = self.uninstall()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutating_calls()[0], f"rpm-ostree initramfs-etc --untrack={self.at(RULE)}")
        self.assertTrue(os.path.exists(self.at(OLD_RULE)))
        self.assertIn(f"kept {self.at('/var/lib/btrfs-degraded-boot')} (degraded-boot markers;", r.stdout)
        self.assertTrue(os.path.exists(self.at(f"/var/lib/btrfs-degraded-boot/degraded-{UUID}")))

    def test_uninstall_with_nothing_tracked_makes_no_deployment(self) -> None:
        self.install()
        r = self.uninstall()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not tracked in initramfs-etc: nothing to untrack\n", r.stdout)
        self.assertNotIn("rpm-ostree initramfs-etc", "\n".join(self.calls()))
        self.assertFalse(os.path.exists(self.at(RULE)))


if __name__ == "__main__":
    unittest.main()
