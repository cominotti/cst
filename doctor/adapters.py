"""Adapters implementing the `doctor` ports against Linux/toolbox hosts."""

from collections.abc import Mapping
import importlib.machinery
import os
import subprocess
import sys
import time
from types import ModuleType

from doctor.domain import Deployment, OstreeAnswer


class Host:
    """How to reach the host: directly, or from a toolbox (flatpak-spawn
    --host for commands, /run/host for files). Tests set CST_DOCTOR_CONTAINER
    (1 or 0), CST_DOCTOR_ROOT (the host's / for files) and CST_DOCTOR_NOW."""

    _ostree: OstreeAnswer
    _ostree_list: list[Deployment]
    _domains: tuple[list[str] | None, str]
    _dumpxml: dict[str, tuple[int, str, str]]

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        forced = env.get("CST_DOCTOR_CONTAINER")
        if forced in ("0", "1"):
            self.container = forced == "1"
        else:
            self.container = os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")
        self.root = env.get("CST_DOCTOR_ROOT") or ("/run/host" if self.container else "/")
        self.now = float(env.get("CST_DOCTOR_NOW") or time.time())
        self.uid = os.getuid()
        self.state_home = env.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")

    def path(self, p: str) -> str:
        """A host path, as this process can read it."""
        return os.path.join(self.root, p.lstrip("/"))

    def run(self, cmd: list[str], timeout: float = 60) -> tuple[int, str, str]:
        """(rc, stdout, stderr) of a read-only query command on the host;
        rc 127 when it cannot be started, 124 on a timeout."""
        full = (["flatpak-spawn", "--host"] + cmd) if self.container else cmd
        try:
            p = subprocess.run(
                full, check=False, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
            )
        except FileNotFoundError as e:
            return 127, "", f"{full[0]}: not found ({e.strerror})"
        except subprocess.TimeoutExpired:
            return 124, "", f"{' '.join(cmd)}: no answer within {timeout} s"
        except OSError as e:
            return 126, "", f"{full[0]}: {e.strerror}"
        return p.returncode, p.stdout, p.stderr


def load_vm_module(vm_tool_path: str) -> ModuleType | None:
    """Load `vm/cst-vm` as a module so doctor reuses its tune and NOCOW logic, or None."""
    try:
        loader = importlib.machinery.SourceFileLoader("cst_vm", vm_tool_path)
        mod = ModuleType("cst_vm")
        mod.__file__ = vm_tool_path
        sys.modules["cst_vm"] = mod  # registered first: @dataclass looks the module up
        loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 - a missing or broken copy is an info line, not an ERROR
        return None
