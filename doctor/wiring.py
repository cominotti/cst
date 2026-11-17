# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst doctor`: the environment, the signals and the adapters.

doctor/cli.py parses argv, takes the Host and the VM tools from here, runs
the checks and renders the report.
"""

from collections.abc import Mapping
import os

from cst_core.adapters.runtime import checkout_root, restore_default_signals
from doctor.adapters import Host, VmTools
from doctor.ports import DoctorHostPort, VmToolsPort


def doctor_env() -> Mapping[str, str]:
    """This process's environment, read when asked (tests patch it)."""
    return os.environ


def start_doctor() -> None:
    """SIGPIPE at its default: `cst doctor | head` ends quietly (the doctor only reads)."""
    restore_default_signals()


def host(env: Mapping[str, str], entry_file: str) -> DoctorHostPort:
    """The machine (or the host seen from a toolbox), with the checkout entry_file runs from."""
    return Host(env, repo=checkout_root(entry_file) or "")


def vm_tools() -> VmToolsPort:
    """The vm package's checks of the VM hardware and image folders."""
    return VmTools()
