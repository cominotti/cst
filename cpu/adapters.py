"""Concrete host adapters for the `cpu` bounded context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import importlib.machinery
import os
import subprocess
import sys
import types
from typing import Protocol, cast

from cpu.domain import URI


class SubprocessVirshAdapter:
    """`VirshPort` adapter backed by `virsh` subprocesses."""

    def __init__(self, uri: str = URI) -> None:
        self._uri = uri

    def run(self, *args: str, check: bool = True) -> str:
        """Run `virsh -c <uri>` with `args`, returning stdout."""
        r = subprocess.run(["virsh", "-c", self._uri, *args], check=False, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"virsh {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout


class _CpuWatchModule(Protocol):
    def main(self) -> int: ...

    def watch(self, argv: list[str]) -> int: ...

    def report(self, argv: list[str]) -> int: ...

    def quiet(self, argv: list[str], *, environ: Mapping[str, str] | None = None) -> int: ...


class CstCpuWatchRuntimeAdapter:
    """`CpuWatchRuntimePort` adapter delegating to `cpu/cpu-watch`."""

    def __init__(self, tool_path: str | None = None) -> None:
        self._tool_path = (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpu-watch") if tool_path is None else tool_path
        )
        self._mod: _CpuWatchModule | None = None

    def _module(self) -> _CpuWatchModule:
        if self._mod is None:
            loader = importlib.machinery.SourceFileLoader("cst_cpu_watch_runtime", self._tool_path)
            mod = types.ModuleType("cst_cpu_watch_runtime")
            mod.__file__ = self._tool_path
            sys.modules["cst_cpu_watch_runtime"] = mod  # registered first: @dataclass looks the module up
            loader.exec_module(mod)
            self._mod = cast("_CpuWatchModule", cast("object", mod))
        return self._mod

    def main(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments to `cpu-watch`."""
        saved = sys.argv
        sys.argv = [self._tool_path, *argv]
        try:
            return self._module().main()
        finally:
            sys.argv = saved

    def watch(self, argv: Sequence[str]) -> int:
        """Delegate `watch` execution."""
        return self._module().watch(list(argv))

    def report(self, argv: Sequence[str]) -> int:
        """Delegate `report` execution."""
        return self._module().report(list(argv))

    def quiet(self, argv: Sequence[str], *, environ: Mapping[str, str] | None = None) -> int:
        """Delegate `quiet` execution."""
        return self._module().quiet(list(argv), environ=environ)


class _VmPinModule(Protocol):
    def main(self) -> int: ...


class CstVmPinRuntimeAdapter:
    """`VmPinRuntimePort` adapter delegating to `cpu/vm-pin`."""

    def __init__(self, tool_path: str | None = None) -> None:
        self._tool_path = (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "vm-pin") if tool_path is None else tool_path
        )
        self._mod: _VmPinModule | None = None

    def _module(self) -> _VmPinModule:
        if self._mod is None:
            loader = importlib.machinery.SourceFileLoader("cst_cpu_vm_pin_runtime", self._tool_path)
            mod = types.ModuleType("cst_cpu_vm_pin_runtime")
            mod.__file__ = self._tool_path
            sys.modules["cst_cpu_vm_pin_runtime"] = mod  # registered first: @dataclass looks the module up
            loader.exec_module(mod)
            self._mod = cast("_VmPinModule", cast("object", mod))
        return self._mod

    def main(self, argv: Sequence[str]) -> int:
        """Dispatch CLI arguments to `cpu-vm-pin`."""
        saved = sys.argv
        sys.argv = [self._tool_path, *argv]
        try:
            return self._module().main()
        finally:
            sys.argv = saved
