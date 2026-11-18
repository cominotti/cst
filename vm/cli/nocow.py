# SPDX-License-Identifier: MPL-2.0
"""`cst vm nocow plan [--root]` and `cst vm nocow apply`: the NOCOW mark of the VM image folders."""

import os
import shlex

from cst_core.adapters.terminal import confirm
from cst_core.domain.errors import Refused
from cst_core.domain.values import format_bytes_gib_mib
from vm.adapters import PROG, VM_CONF, knobs, run, run_on_terminal
from vm.adapters.images import flag_state, image_dirs, readable_inode_flags, needs_mark, probe_dir, mark_folder_nocow
from vm.domain import ImageProbe, convert_lines


def nocow_lines(d: ImageProbe, verbose: bool = True) -> list[str]:
    """Lines describing one probed folder (plan and status)."""
    head = f"{d.path}  [{d.what}]"
    if d.link is not None:
        return [
            f"{head}: a symbolic link to {d.link}: not managed by CST (set the mark on the real "
            f"folder by hand if it is on btrfs: chattr +C <folder>)"
        ]
    if not d.present:
        return [f"{head}: not present"]
    if d.btrfs is False:
        return [f"{head}: not on btrfs (NOCOW does not apply)"]
    out = [head]
    root_hint = f"{PROG} nocow plan --root"
    if d.nocow is None:
        out.append(f"  NOCOW mark (+C): not readable as your user (root-only folder): {root_hint}")
    else:
        out.append(
            "  NOCOW mark (+C): "
            + ("yes" if d.nocow else f"no: {PROG} nocow apply sets it (only files created later get it)")
        )
    if not d.listed:
        if d.nocow is not None or d.scope != "system":
            out.append("  images: folder not listable as your user")
        return out
    cow = [i for i in d.images if i.nocow is False]
    unknown = [i for i in d.images if i.nocow is None]
    ok = [i for i in d.images if i.nocow]
    if not d.images:
        out.append("  images: none")
    if ok:
        out.append(f"  NOCOW images: {', '.join(i.name for i in ok)}")
    if unknown:
        out.append(
            f"  images whose mark is not readable as your user: {', '.join(i.name for i in unknown)} ({root_hint})"
        )
    for i in cow:
        out.append(
            f"  still copy-on-write: {i.name}  {format_bytes_gib_mib(i.alloc, mib_decimals=1)} allocated, {format_bytes_gib_mib(i.size, mib_decimals=1)} apparent"
        )
        if verbose:
            out.append(
                f"    to convert (every VM using it shut off; needs about {format_bytes_gib_mib(i.alloc, mib_decimals=1)} free):"
            )
            out += [f"      {ln}" for ln in convert_lines(i, d.scope == "system")]
    for n in d.leftovers:
        out.append(f"  leftover of a conversion: {n} (remove it once the VM booted well from the new file)")
    return out


def cmd_nocow_plan(as_root: bool = False) -> int:
    if as_root and os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused(
            f"--root reads the system folder as root: run it as `{PROG} nocow plan --root` (it calls sudo by itself)"
        )
    if not knobs()["VM_NOCOW"]:
        print(f"VM_NOCOW=no in {VM_CONF}: the VM image folders are not managed.")
        return 0
    dirs = [probe_dir(d) for d in image_dirs()]
    for d in dirs:
        for ln in nocow_lines(d):
            print(ln)
    todo = [d for d in dirs if needs_mark(d)]
    print()
    if todo:
        print(
            f"Set the mark with: {PROG} nocow apply   (folders: "
            + ", ".join(
                d.path
                + (
                    " (not created yet)"
                    if not d.present
                    else " (mark not readable here: set again, harmless)"
                    if d.nocow is None
                    else ""
                )
                for d in todo
            )
            + ")"
        )
    else:
        print("No folder to mark.")
    print("Existing images are never converted by CST: the commands above are for you to run.")
    return 0


def mark_system_dir(d: ImageProbe) -> str:
    """Create (root:root 0711, as libvirt does) and mark the system folder:
    directly as root, else with sudo. Returns an error text or ""."""
    path, q = d.path, shlex.quote(d.path)
    if os.path.islink(path) or not d.present and os.path.lexists(path):
        return f"{path}: appeared or is a symbolic link: not touched"
    if os.geteuid() == 0:
        try:
            if not d.present:
                os.mkdir(path, 0o711)
                os.chmod(path, 0o711)
                run(["restorecon", path])
            mark_folder_nocow(path)
        except OSError as e:
            return f"{path}: {e.strerror}"
        return "" if flag_state(readable_inode_flags(path)) else f"{path}: the mark did not stick"
    steps: list[list[str]] = []
    if not d.present:
        steps += [["install", "-d", "-o", "root", "-g", "root", "-m", "0711", path], ["restorecon", path]]
    steps.append(["chattr", "+C", path])
    for cmd in steps:
        print(f"  sudo {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
        rc = run_on_terminal(["sudo", "--", *cmd])
        if rc != 0:
            return f"sudo {cmd[0]} {q}: exit {rc}"
    rc, so, _ = run(["sudo", "--", "lsattr", "-d", path])
    attrs = so.split()[0] if rc == 0 and so.split() else ""
    return "" if "C" in attrs else f"{path}: the mark could not be confirmed (sudo lsattr -d {q})"


def cmd_nocow_apply() -> int:
    if not knobs()["VM_NOCOW"]:
        print(f"VM_NOCOW=no in {VM_CONF}: nothing to do.")
        return 0
    dirs = [probe_dir(d) for d in image_dirs()]
    todo = [d for d in dirs if needs_mark(d)]
    if not todo:
        print(
            "Every VM image folder present on btrfs already has the NOCOW mark (+C)"
            + (" (symbolic links are not managed)." if any(d.link for d in dirs) else ".")
        )
        return 0
    for d in todo:
        what = "mark" if (seen := d.present) else "create (root:root 0711) and mark"
        unknown = " (its mark is not readable as your user: set again, harmless)" if seen and d.nocow is None else ""
        print(f"  {what} +C: {d.path}{' (sudo)' if d.scope == 'system' and os.geteuid() else ''}{unknown}")
    print(
        "Only files created there later get the mark; existing images stay as they are "
        f"({PROG} nocow plan shows how to convert them)."
    )
    if not confirm("Set the NOCOW mark on these folders?"):
        print("Nothing changed.")
        return 0
    errors: list[str] = []
    for d in todo:
        if d.scope == "system":
            err = mark_system_dir(d)
        else:
            try:
                mark_folder_nocow(d.path)
                err = "" if flag_state(readable_inode_flags(d.path)) else f"{d.path}: the mark did not stick"
            except OSError as e:
                err = f"{d.path}: {e.strerror}"
        if err:
            errors.append(err)
        else:
            print(f"  done: {d.path}")
    if errors:
        raise Refused("not done:\n  " + "\n  ".join(errors))
    return 0
