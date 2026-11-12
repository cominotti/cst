#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 && -z "${ANACONDA_BTRFS_FIX_TARGET:-}" ]]; then
    echo "Run this as root, for example: sudo $0" >&2
    exit 1
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'EOF'
Patch Fedora Anaconda's manual partitioning backend so Btrfs mount points
created through Anaconda Web UI + Cockpit inherit Fedora's configured
btrfs_compression value, usually compress=zstd:1, and also get noatime and
flushoncommit plus commit=10.

Run this in the Live ISO before starting the installer:

    sudo ./fix-anaconda-webui-btrfs-compress.sh

For testing against a copied file:

    sudo ANACONDA_BTRFS_FIX_TARGET=/tmp/manual_partitioning.py ./fix-anaconda-webui-btrfs-compress.sh
EOF
    exit 0
fi

declare -a targets=()

if [[ -n "${ANACONDA_BTRFS_FIX_TARGET:-}" ]]; then
    targets+=("${ANACONDA_BTRFS_FIX_TARGET}")
else
    while IFS= read -r path; do
        targets+=("${path}")
    done < <(
        find /usr/lib /usr/lib64 \
            -path '*/site-packages/pyanaconda/modules/storage/partitioning/manual/manual_partitioning.py' \
            -type f 2>/dev/null
    )
fi

if [[ ${#targets[@]} -eq 0 ]]; then
    echo "Could not find Anaconda manual_partitioning.py." >&2
    exit 1
fi

for target in "${targets[@]}"; do
    if [[ ! -f "${target}" ]]; then
        echo "Target does not exist: ${target}" >&2
        exit 1
    fi

    if grep -q '_anaconda_webui_btrfs_mount_options_fix' "${target}" && grep -q 'commit=10' "${target}"; then
        echo "Already patched: ${target}"
        continue
    fi

    backup="${target}.bak-codex-btrfs-compress"
    if [[ ! -e "${backup}" ]]; then
        cp -a "${target}" "${backup}"
    fi

    TARGET="${target}" python3 - <<'PY'
from pathlib import Path
import os

target = Path(os.environ["TARGET"])
text = target.read_text()

old_import = "from blivet.errors import StorageError\n"
new_import = (
    "from blivet import flags as blivet_flags\n"
    "from blivet.errors import StorageError\n"
)
if "from blivet import flags as blivet_flags\n" not in text:
    if old_import not in text:
        raise SystemExit("expected StorageError import not found")
    text = text.replace(old_import, new_import, 1)

anchor = '__all__ = ["ManualPartitioningTask"]\n\n'
helper = '''def _anaconda_webui_btrfs_mount_options_fix(device, mount_options):
    """Add Fedora Btrfs defaults to manual mount requests."""
    if getattr(device.format, "type", None) != "btrfs":
        return mount_options

    options = mount_options or getattr(device.format, "options", "") or ""
    option_list = []
    for option in options.split(","):
        if (
            not option
            or option in ("defaults", "atime", "relatime", "strictatime", "noflushoncommit")
            or option.startswith("commit=")
        ):
            continue
        option_list.append(option)

    compression = blivet_flags.btrfs_compression
    if compression and not any(option.startswith("compress") for option in option_list):
        option_list.append("compress=%s" % compression)

    for option in ("noatime", "flushoncommit", "commit=10"):
        if option not in option_list:
            option_list.append(option)

    return ",".join(option_list) or mount_options


'''

helper_names = (
    "def _anaconda_webui_btrfs_mount_options_fix(",
    "def _anaconda_webui_btrfs_compression_fix(",
)
existing_helper_name = next((name for name in helper_names if name in text), None)
if existing_helper_name:
    start = text.index(existing_helper_name)
    end = text.index("class ManualPartitioningTask", start)
    text = text[:start] + helper + text[end:]
else:
    if anchor not in text:
        raise SystemExit("expected __all__ anchor not found")
    text = text.replace(anchor, anchor + helper, 1)

text = text.replace(
    "_anaconda_webui_btrfs_compression_fix(",
    "_anaconda_webui_btrfs_mount_options_fix("
)

old_assignment = '''        device.format.create_options = mount_data.format_options
        device.format.options = mount_data.mount_options
'''
new_assignment = '''        mount_data.mount_options = _anaconda_webui_btrfs_mount_options_fix(
            device, mount_data.mount_options
        )

        device.format.create_options = mount_data.format_options
        device.format.options = mount_data.mount_options
'''
if "        mount_data.mount_options = _anaconda_webui_btrfs_mount_options_fix(\n" not in text:
    if old_assignment not in text:
        raise SystemExit("expected mount option assignment not found")
    text = text.replace(old_assignment, new_assignment, 1)

target.write_text(text)
PY

    python3 -m py_compile "${target}"
    echo "Patched: ${target}"
done

if [[ ${EUID:-$(id -u)} -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl try-restart anaconda.service >/dev/null 2>&1 || true
    systemctl try-restart webui-cockpit-ws.service >/dev/null 2>&1 || true
fi

echo "Done. Start or reopen the Fedora installer before configuring storage."
