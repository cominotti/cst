"""Concrete host adapters for the `btrfs` bounded context."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import importlib.machinery
import inspect
import os
import subprocess
import sys
import types
from typing import Protocol, cast

from btrfs.domain import KNOWN_HELPERS


class _HelperEntry(Protocol):
    main: Callable[..., int | None]


class SubprocessBtrfsCommandAdapter:
    """`BtrfsCommandPort` adapter backed by `subprocess.run`."""

    def run(self, argv: Sequence[str], *, timeout: int = 60) -> tuple[int, str, str]:
        """Run `argv` with `stdin=DEVNULL` and return `(returncode, stdout, stderr)`."""
        args = list(argv)
        if not args:
            return 2, "", "empty command"
        try:
            proc = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return 127, "", f"{args[0]}: command not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        return proc.returncode, proc.stdout, proc.stderr


class BtrfsHelperRuntimeAdapter:
    """`BtrfsHelperRuntimePort` adapter loading standalone helpers in `btrfs/`."""

    def __init__(self, btrfs_dir: str | None = None) -> None:
        self._dir = os.path.dirname(os.path.abspath(__file__)) if btrfs_dir is None else btrfs_dir
        self._modules: dict[str, types.ModuleType] = {}

    def _load(self, helper_name: str) -> types.ModuleType:
        if helper_name not in KNOWN_HELPERS:
            raise ValueError(f"unknown btrfs helper: {helper_name}")
        mod = self._modules.get(helper_name)
        if mod is None:
            path = os.path.join(self._dir, helper_name)
            mod_name = f"btrfs_runtime_{helper_name.replace('-', '_')}"
            loader = importlib.machinery.SourceFileLoader(mod_name, path)
            mod = types.ModuleType(mod_name)
            mod.__file__ = path
            loader.exec_module(mod)
            self._modules[helper_name] = mod
        return mod

    def run_helper(self, helper_name: str, argv: Sequence[str]) -> int:
        """Load `helper_name` and execute its `main` entrypoint with `argv`."""
        mod = self._load(helper_name)
        entry = cast("_HelperEntry", cast("object", mod)).main
        sig = inspect.signature(entry)
        args_list = list(argv)
        full_argv = [os.path.join(self._dir, helper_name), *args_list]
        old_argv = sys.argv
        try:
            sys.argv = full_argv
            if sig.parameters:
                passed = list(full_argv) if helper_name in {"chrome-cache", "btrfs-bulk"} else args_list
                result = entry(passed)
            else:
                result = entry()
        except SystemExit as exc:
            if isinstance(exc.code, int):
                return exc.code
            return 0 if exc.code is None else 1
        finally:
            sys.argv = old_argv
        return 0 if result is None else int(result)
