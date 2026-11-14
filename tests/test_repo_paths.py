"""Static gate: no file in the repository names the folder it is cloned in.

CST works from wherever it is cloned: `cst` and the module scripts find
their own folder at run time (readlink -f), and installed programs are
copies that do not depend on the checkout. Text that names a fixed location
for the checkout (a home-anchored path such as ~/Workspace/.../cst/cst or
/home/USER/src/cst/AGENTS.md) goes stale when the folder moves, and it ties
the repository to one machine.

Rule: name repository files relative to the CST folder (`./cst`, "at the
top of the CST folder", `"$PWD/cst"`), or through the `cst` command, which
the installs link into ~/.local/bin. See AGENTS.md, "No fixed checkout path".

What counts: a path anchored at a home folder (~, $HOME, ${HOME},
/home/USER, /var/home/USER) whose first component is not a dot folder
(~/.local/bin/cst and ~/.config/cst are install targets, not the checkout),
with a component that names the checkout folder (the folder this copy is
cloned in, or a name the repository had: cst, f44-tools) and is either the
last component or followed by a top-level entry of the repository
(~/src/cst/cst, ~/Workspace/cst/AGENTS.md, ~/Workspace/github/x/cst). Other
home paths, such as ~/Workspace (a snapshotted subvolume) or
~/Bulk/toolchains, are fine.
"""

import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME_PATH = re.compile(r"(?:~|\$\{?HOME\}?|/var/home/[\w.-]+|/home/[\w.-]+)/(?!\.)[^\s`'\"()\[\]<>{},;|]+")


def tracked_files():
    out = subprocess.run(["git", "-C", REPO, "ls-files", "-co", "--exclude-standard"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise unittest.SkipTest("not a git checkout: " + out.stderr.strip())
    return [rel for rel in out.stdout.splitlines() if os.path.isfile(os.path.join(REPO, rel))]


FORMER_NAMES = {"cst", "f44-tools"}


def top_level_entries(files):
    return {rel.split("/")[0] for rel in files}


def offending_paths(text, folders, entries):
    found = []
    for match in HOME_PATH.finditer(text):
        parts = match.group(0).rstrip(".:").split("/")[1:]
        for i, part in enumerate(parts):
            if part in folders and (i == len(parts) - 1 or parts[i + 1] in entries):
                found.append(match.group(0))
                break
    return found


class RepoPathTest(unittest.TestCase):
    def test_detector(self):
        folders, entries = {"cst", "f44-tools", "mytools"}, {"cst", "AGENTS.md", "btrfs", "toolchains"}
        for bad in ("~/Workspace/github/cominotti/cst/cst btrfs install",
                    "ln -s $HOME/src/cst/cst x", "/var/home/u/Workspace/mytools/btrfs/README.md",
                    "cd ${HOME}/code/cst", "lived in ~/Workspace/f44-tools;"):
            self.assertTrue(offending_paths(bad, folders, entries), bad)
        for good in ("~/.local/bin/cst", "~/.config/cst/toolchains.conf", "~/Workspace 48h 7d",
                     "-v ~/Workspace/x:/src", "/var/home/u/.var/app/Steam", "./cst btrfs install",
                     "~/Bulk/toolchains/jvm", "~/Workspace/lush/.github/workflows/ci.yml"):
            self.assertFalse(offending_paths(good, folders, entries), good)

    def test_no_fixed_checkout_path(self):
        files = tracked_files()
        folders = FORMER_NAMES | {os.path.basename(REPO)}
        entries = top_level_entries(files)
        problems = []
        for rel in files:
            if rel == "tests/test_repo_paths.py":    # its own examples of bad paths
                continue
            try:
                with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                    lines = fh.readlines()
            except (UnicodeDecodeError, OSError):
                continue
            for no, line in enumerate(lines, 1):
                for path in offending_paths(line, folders, entries):
                    problems.append(f"{rel}:{no}: {path}")
        self.assertEqual(problems, [], "fixed path to the checkout (see AGENTS.md, "
                         "'No fixed checkout path'):\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
