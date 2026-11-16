"""Application use cases for the `cpu` bounded context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import difflib
import xml.etree.ElementTree as ET

from cpu.domain import (
    CpuSetupConfig,
    FrameStats,
    QuietQuery,
    cpu_line,
    features_note,
    frame_stats,
    keep_prefixes,
    parse_quiet_args,
    pinned,
    unsupported_vcpu_elements,
)
from cpu.ports import CpuWatchRuntimePort, VmPinRuntimePort


class CpuService:
    """Coordinates VM CPU pinning, frame-pacing telemetry, and benchmark quietness checks."""

    def __init__(self, watch_runtime: CpuWatchRuntimePort, vm_pin_runtime: VmPinRuntimePort) -> None:
        self._watch_runtime = watch_runtime
        self._vm_pin_runtime = vm_pin_runtime

    def plan_vm_pin(self, xml_text: str, config: CpuSetupConfig) -> tuple[str, list[str], str]:
        """Compute updated domain XML, unified diff, and CPU feature note for `config`."""
        keep_prefixes(xml_text)
        croot = ET.fromstring(xml_text)
        unsupported = unsupported_vcpu_elements(croot)
        if unsupported:
            raise ValueError(
                f"{config.vm_name} has <{unsupported[0]}>: adjust it by hand (virsh edit {config.vm_name}); "
                "nothing changed"
            )
        cur_root = ET.fromstring(xml_text)
        ET.indent(cur_root, space="  ")
        before = ET.tostring(cur_root, encoding="unicode") + "\n"
        after = pinned(xml_text, config.vm_vcpus, config.build_cpus, config.vm_cpu_disable)
        diff = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "current", "pinned"))
        note = features_note(croot, config.vm_cpu_disable, planned=True)
        return after, diff, note

    def vm_cpu_status_line(self, xml_text: str, config: CpuSetupConfig) -> str:
        """Render the one-line CPU mode and feature status string for `xml_text`."""
        return cpu_line(xml_text, config.vm_cpu_disable)

    def evaluate_frame_stats(self, frametimes_ms: Sequence[float]) -> FrameStats | None:
        """Compute `FrameStats` summary from a sequence of frame times in milliseconds."""
        return frame_stats(frametimes_ms)

    def validate_quiet_args(self, argv: Sequence[str], environ: Mapping[str, str]) -> QuietQuery:
        """Parse and validate `cpu-watch quiet` flags and environment overrides."""
        return parse_quiet_args(argv, environ)

    def watch(self, argv: Sequence[str]) -> int:
        """Run `cpu-watch watch` telemetry capture."""
        return self._watch_runtime.watch(argv)

    def report(self, argv: Sequence[str]) -> int:
        """Run `cpu-watch report` across recorded telemetry runs."""
        return self._watch_runtime.report(argv)

    def quiet(self, argv: Sequence[str], *, environ: Mapping[str, str] | None = None) -> int:
        """Run `cpu-watch quiet` benchmark readiness check."""
        return self._watch_runtime.quiet(argv, environ=environ)

    def vm_pin(self, argv: Sequence[str]) -> int:
        """Run `cpu-vm-pin` command with `argv`."""
        return self._vm_pin_runtime.main(argv)
