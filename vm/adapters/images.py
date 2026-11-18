# SPDX-License-Identifier: MPL-2.0
"""The VM image folders: where they are, their NOCOW mark (+C), btrfs or not, and the images in them."""

from collections.abc import Callable
import ctypes
from dataclasses import replace
import os
import pwd
import shlex
import struct

from cst_core.adapters import nocow as inode_attrs
from vm.adapters import run, run_on_terminal
from vm.domain import BTRFS_MAGIC, USER_IMAGES, ImageDir, ImageInfo, ImageProbe, VmSettings


def flag_state(flags: int | None) -> bool | None:
    return None if flags is None else bool(flags & inode_attrs.FS_NOCOW_FL)


def nocow(path: str) -> bool:
    return flag_state(readable_inode_flags(path)) is True


def user_home(sudo_user: str | None) -> str:
    """The home of the user who runs CST: under sudo, SUDO_USER's (sudo may
    set HOME to /root), else HOME."""
    if os.geteuid() == 0 and sudo_user:
        try:
            return pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def is_btrfs(path: str) -> bool | None:
    """True/False, or None when statfs fails (statfs needs only search
    permission, so a root folder of mode 0711 still answers)."""
    buf = ctypes.create_string_buffer(256)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.statfs(os.fsencode(path), buf) != 0:
            return None
    except OSError, AttributeError:
        return None
    return struct.unpack_from("l", buf.raw, 0)[0] == BTRFS_MAGIC


def readable_inode_flags(path: str) -> int | None:
    """The inode flags (FS_IOC_GETFLAGS), or None when this user may not read
    them: the system folder is root's (0711) and the images are qemu's (0600).
    Unreadable is not "copy-on-write": callers say "not readable"."""
    try:
        return inode_attrs.inode_flags(path)
    except OSError:
        return None


def mark_folder_nocow(path: str) -> None:
    """Set +C on a folder this user owns (files created in it later inherit
    it). Raises OSError."""
    inode_attrs.set_nocow(path, directory=True)


def image_dirs(s: VmSettings) -> list[ImageDir]:
    """Every known VM image folder."""
    home = user_home(s.sudo_user)
    out = [ImageDir("system", s.paths.system_images, "libvirt qemu:///system: virt-manager, virsh")]
    out += [ImageDir("user", os.path.join(home, rel), what) for rel, what in USER_IMAGES]
    return out


def probe_dir(d: ImageDir, readable: Callable[[str], str] = lambda p: p) -> ImageProbe:
    """What a VM image folder looks like (ImageProbe). readable maps a path to where this
    process reads it (the doctor in a toolbox: under /run/host)."""
    rp = readable(d.path)
    link = os.readlink(rp) if os.path.islink(rp) else None
    here = ImageProbe(d.scope, d.path, d.what, present=link is None and os.path.isdir(rp), link=link)
    if not here.present:
        return here
    btrfs = is_btrfs(rp)
    if btrfs is False:
        return replace(here, btrfs=False)
    nocow = flag_state(readable_inode_flags(rp))
    try:
        entries = sorted(os.scandir(rp), key=lambda e: e.name)
    except OSError:
        return replace(here, btrfs=btrfs, nocow=nocow)
    images: list[ImageInfo] = []
    leftovers: list[str] = []
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if e.name.endswith(".cow-old"):
            leftovers.append(e.name)
            continue
        if e.name.lower().endswith(".iso"):
            continue  # read-only installers: nothing to gain
        images.append(
            ImageInfo(
                name=e.name,
                path=os.path.join(d.path, e.name),
                alloc=st.st_blocks * 512,
                size=st.st_size,
                nocow=flag_state(readable_inode_flags(os.path.join(rp, e.name))),
            )
        )
    return replace(here, btrfs=btrfs, nocow=nocow, listed=True, images=images, leftovers=leftovers)


def needs_mark(d: ImageProbe) -> bool:
    """True when nocow apply would act on the folder (also: unknown state of
    the system folder, or the system folder missing while libvirt is there).
    A symbolic link is never acted on (install -d and chattr would follow it)."""
    if d.link is not None or os.path.lexists(d.path) and not d.present:
        return False
    if d.scope == "system" and not d.present:
        return os.path.isdir(os.path.dirname(d.path))
    return d.present and d.btrfs is not False and d.nocow is not True


def mark_system_dir(d: ImageProbe, say: Callable[[str], None]) -> str:
    """Create (root:root 0711, as libvirt does) and mark the system folder:
    directly as root, else with sudo (say shows each sudo command before it runs).
    Returns an error text or ""."""
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
        say(f"  sudo {' '.join(shlex.quote(c) for c in cmd)}")
        rc = run_on_terminal(["sudo", "--", *cmd])
        if rc != 0:
            return f"sudo {cmd[0]} {q}: exit {rc}"
    rc, so, _ = run(["sudo", "--", "lsattr", "-d", path])
    attrs = so.split()[0] if rc == 0 and so.split() else ""
    return "" if "C" in attrs else f"{path}: the mark could not be confirmed (sudo lsattr -d {q})"


def mark_dir(d: ImageProbe, say: Callable[[str], None]) -> str:
    """Set the NOCOW mark on a folder nocow apply acts on (needs_mark): the system one through
    mark_system_dir, a user's directly. Returns an error text or ""."""
    if d.scope == "system":
        return mark_system_dir(d, say)
    try:
        mark_folder_nocow(d.path)
    except OSError as e:
        return f"{d.path}: {e.strerror}"
    return "" if flag_state(readable_inode_flags(d.path)) else f"{d.path}: the mark did not stick"


class ImageFolders:
    """ImagesPort on this machine: image_dirs of cfg, probe_dir, needs_mark and mark_dir."""

    def __init__(self, cfg: VmSettings) -> None:
        self.cfg = cfg

    def folders(self) -> list[ImageProbe]:
        return [probe_dir(d) for d in image_dirs(self.cfg)]

    def needs_mark(self, folder: ImageProbe) -> bool:
        return needs_mark(folder)

    def mark(self, folder: ImageProbe, say: Callable[[str], None]) -> str:
        return mark_dir(folder, say)
