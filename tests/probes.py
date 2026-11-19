# SPDX-License-Identifier: MPL-2.0
"""Test probes that several modules' tests share.

- recorded_fsyncs and assert_replaced_durably: which paths a write fsyncs,
  and whether a file was replaced through cst_core's write_atomic with its
  file and its folder fsynced (durable=True);
- python_program: a program made of given Python lines, run by this
  interpreter (a Python program, not sh: a test that hands on its stdin
  would run bash with a socket there, and bash then reads ~/.bashrc);
- bytes_program: a program that prints given bytes, which need not be UTF-8.
"""

import os
import sys
import unittest
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from unittest import mock


@contextmanager
def recorded_fsyncs() -> Generator[list[str]]:
    """os.fsync still syncs, and also notes the path of each fd it syncs, in order."""
    synced: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    with mock.patch.object(os, "fsync", fsync):
        yield synced


def assert_replaced_durably(test: unittest.TestCase, path: str, synced: Sequence[str]) -> None:
    """PATH's temporary file (write_atomic's ".NAME.*") was fsynced once, then its folder after it."""
    folder = os.path.realpath(os.path.dirname(path))
    prefix = os.path.join(folder, "." + os.path.basename(path) + ".")
    temps = [i for i, p in enumerate(synced) if p.startswith(prefix)]
    test.assertEqual(len(temps), 1, synced)  # the new bytes, before the rename
    test.assertIn(folder, synced[temps[0] + 1 :], synced)  # the rename's folder entry


def python_program(folder: str, name: str, body: str) -> str:
    """An executable FOLDER/NAME that runs BODY (Python, after "import sys"); its path."""
    path = os.path.join(folder, name)
    with open(path, "w") as f:
        f.write(f"#!{sys.executable} -I\nimport sys\n{body}")
    os.chmod(path, 0o755)
    return path


def bytes_program(folder: str, name: str, out: bytes) -> str:
    """An executable FOLDER/NAME that writes OUT to its stdout as it is; its path."""
    return python_program(folder, name, f"sys.stdout.buffer.write({out!r})\n")
