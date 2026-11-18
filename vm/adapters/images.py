# SPDX-License-Identifier: MPL-2.0
"""The VM image folders: where they are, their NOCOW mark (+C), btrfs or not, and the images in them."""

from collections.abc import Callable
import ctypes
from dataclasses import replace
import os
import pwd
import struct

from cst_core.adapters import nocow as inode_attrs
from vm.adapters import SYSTEM_IMAGES
from vm.domain import BTRFS_MAGIC, USER_IMAGES, ImageDir, ImageInfo, ImageProbe


def flag_state(flags: int | None) -> bool | None:
    return None if flags is None else bool(flags & inode_attrs.FS_NOCOW_FL)


def nocow(path: str) -> bool:
    return flag_state(readable_inode_flags(path)) is True


def user_home() -> str:
    """The home of the user who runs CST: under sudo, SUDO_USER's (sudo may
    set HOME to /root), else HOME."""
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        try:
            return pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir
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


def image_dirs(home: str | None = None) -> list[ImageDir]:
    """Every known VM image folder."""
    home = home or user_home()
    out = [ImageDir("system", SYSTEM_IMAGES, "libvirt qemu:///system: virt-manager, virsh")]
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
