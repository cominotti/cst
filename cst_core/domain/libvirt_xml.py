"""Rules for rewriting a libvirt domain definition with ElementTree (cpu vm-pin, cst-vm tune)."""

import io
import re
import xml.etree.ElementTree as ET


def keep_prefixes(xml_text: str) -> None:
    """Register the document's own namespace prefixes (libosinfo metadata etc.),
    so serializing does not rename them to ns0. A default namespace
    (xmlns='...' on a <metadata> child) is not registered: as "" ElementTree
    would write it on <domain>, moving every libvirt element into it."""
    for _, (prefix, uri) in ET.iterparse(io.StringIO(xml_text), events=("start-ns",)):
        if prefix and not re.fullmatch(r"ns\d+", prefix):  # ns0...: ElementTree's own, it refuses them
            ET.register_namespace(prefix, uri)
