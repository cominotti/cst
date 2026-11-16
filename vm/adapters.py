"""Concrete host adapters for the `vm` bounded context."""

from __future__ import annotations

from collections.abc import Sequence
import importlib.machinery
import os
import subprocess
import types
from typing import Protocol, cast

from vm.domain import URI


class SubprocessVirshAdapter:
    """`VirshPort` adapter backed by `virsh` subprocesses."""

    def __init__(self, uri: str = URI) -> None:
        self._uri = uri

    def run(self, *args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
        """Execute `virsh -c <uri>` with `args`, returning `(returncode, stdout, stderr)`."""
        try:
            r = subprocess.run(
                ["virsh", "-c", self._uri, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "LC_ALL": "C"},
                start_new_session=bool(detach),
            )
        except FileNotFoundError:
            return 127, "", "virsh: command not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        return r.returncode, r.stdout, r.stderr


class _VmModule(Protocol):
    def main(self, argv: Sequence[str]) -> int: ...


class CstVmRuntimeAdapter:
    """`VmRuntimePort` adapter delegating execution to `vm/cst-vm`."""

    def __init__(self, tool_path: str | None = None) -> None:
        self._tool_path = (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cst-vm") if tool_path is None else tool_path
        )
        self._mod: _VmModule | None = None

    def _module(self) -> _VmModule:
        if self._mod is None:
            loader = importlib.machinery.SourceFileLoader("cst_vm_runtime", self._tool_path)
            mod = types.ModuleType("cst_vm_runtime")
            mod.__file__ = self._tool_path
            loader.exec_module(mod)
            self._mod = cast("_VmModule", cast("object", mod))
        return self._mod

    def main(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments to `vm/cst-vm`."""
        return self._module().main(list(argv))
