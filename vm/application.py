"""Application use cases for the `vm` bounded context."""

from __future__ import annotations

from collections.abc import Sequence
import difflib
import xml.etree.ElementTree as ET

from vm.domain import (
    DomainParts,
    TuneResult,
    VmCliCommand,
    VmKnobs,
    extract_domain_parts,
    guest_kind,
    normalized_xml,
    parse_vm_args,
    tune_domain_xml,
)
from vm.ports import VmRuntimePort


class VmService:
    """Coordinates domain XML inspection, virtio tuning plans, and `cst-vm` execution."""

    def __init__(self, runtime: VmRuntimePort) -> None:
        self._runtime = runtime

    def parse_args(self, argv: Sequence[str]) -> VmCliCommand:
        """Parse and validate `cst vm` command-line arguments."""
        return parse_vm_args(argv)

    def inspect_domain(self, xml_text: str) -> DomainParts:
        """Extract storage, firmware, and validation items from inactive domain XML."""
        return extract_domain_parts(xml_text)

    def plan_tune(
        self,
        xml_text: str,
        knobs: VmKnobs | None = None,
        *,
        guest_has_virtio: bool = False,
    ) -> tuple[TuneResult, list[str], str, str]:
        """Compute `TuneResult`, unified diff, and guest OS classification for `xml_text`."""
        resolved = VmKnobs() if knobs is None else knobs
        kind, why = guest_kind(ET.fromstring(xml_text))
        allow_bus = kind != "windows" or guest_has_virtio
        result = tune_domain_xml(xml_text, resolved.as_dict(), allow_bus=allow_bus)
        diff: list[str] = []
        if result.after_xml is not None:
            before = normalized_xml(xml_text)
            diff = list(
                difflib.unified_diff(before.splitlines(True), result.after_xml.splitlines(True), "current", "tuned")
            )
        return result, diff, kind, why

    def run(self, argv: Sequence[str]) -> int:
        """Delegate execution of a `cst vm` CLI invocation to the configured runtime."""
        return self._runtime.main(argv)
