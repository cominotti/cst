"""Concrete host adapters for the `toolchains` bounded context."""

from collections.abc import Sequence
import importlib.machinery
import os
import subprocess
import sys
import types
from typing import Protocol, cast

from toolchains.domain import Refused


def _in_container() -> bool:
    return os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")


def _systemctl_cmd(*args: str) -> list[str]:
    stub = os.environ.get("CST_TOOLCHAINS_SYSTEMCTL")
    if stub:
        return [stub, "--user", *args]
    cmd = ["systemctl", "--user", *args]
    return (["flatpak-spawn", "--host", *cmd]) if _in_container() else cmd


def _systemctl_env() -> dict[str, str]:
    env = dict(os.environ)
    rt = f"/run/user/{os.getuid()}"
    if not env.get("XDG_RUNTIME_DIR") and os.path.isdir(rt):
        env["XDG_RUNTIME_DIR"] = rt
    return env


class SubprocessSystemctlUserAdapter:
    """`SystemctlUserPort` adapter backed by `systemctl --user` subprocesses."""

    def run(self, *args: str, check: bool = False, ok: tuple[int, ...] = (0,)) -> str:
        """Run `systemctl --user` with `args`, returning stripped stdout."""
        try:
            r = subprocess.run(
                _systemctl_cmd(*args),
                check=False,
                capture_output=True,
                text=True,
                env=_systemctl_env(),
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            if check:
                raise Refused(f"systemctl --user {' '.join(args)}: {e}") from None
            return ""
        if check and r.returncode not in ok:
            raise Refused(f"systemctl --user {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout.strip()

    def manager_path(self) -> str | None:
        """Return `PATH` from the user systemd manager environment, or None."""
        for line in self.run("show-environment").splitlines():
            if line.startswith("PATH="):
                val = line[len("PATH=") :]
                return None if val[:1] in ("$", "'", '"') else val
        return None


class _RuntimeModule(Protocol):
    def main(self, argv: list[str]) -> int: ...

    def cmd_status(self, home: str, now: float, brief: bool) -> int: ...

    def cmd_prune(self, home: str, now: float, action: str, names: list[str]) -> int: ...

    def cmd_auto_on(self, home: str, now: float, unless_off: bool = False) -> int: ...

    def cmd_auto_off(self, home: str, now: float) -> int: ...

    def cmd_config(self, home: str, action: str, lean: bool | None = None, sccache: bool | None = None) -> int: ...


class CstToolchainsRuntimeAdapter:
    """`ToolchainsRuntimePort` adapter delegating to `toolchains/cst-toolchains`."""

    def __init__(self, tool_path: str | None = None) -> None:
        self._tool_path = (
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cst-toolchains")
            if tool_path is None
            else tool_path
        )
        self._mod: _RuntimeModule | None = None

    def _module(self) -> _RuntimeModule:
        if self._mod is None:
            loader = importlib.machinery.SourceFileLoader("cst_toolchains_runtime", self._tool_path)
            mod = types.ModuleType("cst_toolchains_runtime")
            mod.__file__ = self._tool_path
            sys.modules["cst_toolchains_runtime"] = mod  # registered first: @dataclass looks the module up
            loader.exec_module(mod)
            self._mod = cast(_RuntimeModule, cast(object, mod))
        return self._mod

    def main(self, argv: Sequence[str]) -> int:
        """Delegate full CLI entrypoint dispatch."""
        return self._module().main(list(argv))

    def status(self, home: str, now: float, brief: bool) -> int:
        """Delegate `status` execution."""
        return self._module().cmd_status(home, now, brief)

    def prune(self, home: str, now: float, action: str, names: Sequence[str]) -> int:
        """Delegate `prune plan|apply` execution."""
        return self._module().cmd_prune(home, now, action, list(names))

    def auto_on(self, home: str, now: float, unless_off: bool = False) -> int:
        """Delegate `auto on` execution."""
        return self._module().cmd_auto_on(home, now, unless_off)

    def auto_off(self, home: str, now: float) -> int:
        """Delegate `auto off` execution."""
        return self._module().cmd_auto_off(home, now)

    def config(self, home: str, action: str, lean: bool | None = None, sccache: bool | None = None) -> int:
        """Delegate `config plan|apply` execution."""
        return self._module().cmd_config(home, action, lean, sccache)
