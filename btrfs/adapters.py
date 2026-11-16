"""Concrete host adapters for the `btrfs` bounded context."""

from __future__ import annotations

from collections.abc import Sequence
import subprocess


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
