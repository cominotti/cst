# SPDX-License-Identifier: MPL-2.0
"""The libvirt adapter (VirshPort): virsh in the C locale through a CommandRunnerPort."""

from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.ports import CommandResult, CommandRunnerPort, ContainerProbePort, VmName


class SubprocessVirshAdapter:
    """VirshPort adapter: `virsh [-r] -c URI ARGS` in the C locale, through a CommandRunnerPort.

    on_host: inside a container, virsh runs on the host (flatpak-spawn --host);
    read_only: -r, for the queries libvirt answers on a read-only connection.
    Output that is not UTF-8 is decoded as the runner does by default (U+FFFD).
    ctx only builds the default runner (the runner decides on_host): a ctx
    next to a runner is refused, since it would be ignored.
    """

    def __init__(
        self,
        uri: str = "qemu:///system",
        runner: CommandRunnerPort | None = None,
        ctx: ContainerProbePort | None = None,
        *,
        on_host: bool = True,
        read_only: bool = False,
    ) -> None:
        if runner is not None and ctx is not None:
            raise ValueError("SubprocessVirshAdapter: ctx only builds the default runner; give the runner its ctx")
        self._uri = uri
        self._runner = SubprocessCommandRunner(ctx) if runner is None else runner
        self._on_host = on_host
        self._read_only = read_only

    def call(
        self,
        *args: str,
        check: bool = False,
        timeout: float | None = 60,
        start_new_session: bool = False,
    ) -> CommandResult:
        """Run `virsh -c <uri> *args`; start_new_session: in a session of its own."""
        cmd = ["virsh", *(["-r"] if self._read_only else []), "-c", self._uri, *args]
        return self._runner.run(
            cmd,
            check=check,
            timeout=timeout,
            locale_c=True,
            new_session=start_new_session,
            on_host=self._on_host,
        )

    def domains(self, *, all_domains: bool = True) -> list[VmName]:
        """Return configured domain names, or empty list on failure."""
        args = ["list", "--all", "--name"] if all_domains else ["list", "--name"]
        res = self.call(*args, check=False)
        if res.returncode != 0:
            return []
        return [VmName(line.strip()) for line in res.stdout.splitlines() if line.strip()]

    def dumpxml(self, domain: str, *, inactive: bool = False) -> str | None:
        """Return XML definition of `domain`, or None on failure."""
        args = ["dumpxml", "--inactive", domain] if inactive else ["dumpxml", domain]
        res = self.call(*args, check=False)
        return res.stdout if res.returncode == 0 else None
