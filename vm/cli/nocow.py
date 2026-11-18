# SPDX-License-Identifier: MPL-2.0
"""`cst vm nocow plan [--root]` and `cst vm nocow apply`: the NOCOW mark of the VM image folders."""

import os

from cst_core.adapters.terminal import confirm
from cst_core.domain.errors import Refused
from vm.adapters import knobs
from vm.adapters.images import image_dirs, mark_dir, needs_mark, probe_dir
from vm.domain import VmSettings, nocow_lines, todo_note


def cmd_nocow_plan(cfg: VmSettings, as_root: bool = False) -> int:
    if as_root and os.geteuid() != 0 and not cfg.nonroot_ok:
        raise Refused(
            f"--root reads the system folder as root: run it as `{cfg.prog} nocow plan --root` (it calls sudo by itself)"
        )
    if not knobs(cfg.paths)["VM_NOCOW"]:
        print(f"VM_NOCOW=no in {cfg.paths.vm_conf}: the VM image folders are not managed.")
        return 0
    dirs = [probe_dir(d) for d in image_dirs(cfg)]
    for d in dirs:
        for ln in nocow_lines(d, cfg.prog):
            print(ln)
    todo = [d for d in dirs if needs_mark(d)]
    print()
    if todo:
        print(
            f"Set the mark with: {cfg.prog} nocow apply   (folders: "
            + ", ".join(d.path + todo_note(d) for d in todo)
            + ")"
        )
    else:
        print("No folder to mark.")
    print("Existing images are never converted by CST: the commands above are for you to run.")
    return 0


def cmd_nocow_apply(cfg: VmSettings) -> int:
    if not knobs(cfg.paths)["VM_NOCOW"]:
        print(f"VM_NOCOW=no in {cfg.paths.vm_conf}: nothing to do.")
        return 0
    dirs = [probe_dir(d) for d in image_dirs(cfg)]
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
        f"({cfg.prog} nocow plan shows how to convert them)."
    )
    if not confirm("Set the NOCOW mark on these folders?"):
        print("Nothing changed.")
        return 0
    errors: list[str] = []
    for d in todo:
        if err := mark_dir(d, lambda line: print(line, flush=True)):
            errors.append(err)
        else:
            print(f"  done: {d.path}")
    if errors:
        raise Refused("not done:\n  " + "\n  ".join(errors))
    return 0
