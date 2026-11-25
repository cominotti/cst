# SPDX-License-Identifier: MPL-2.0
"""Where swap reads and writes: the paths and identity of one run (SwapEnvironment), and the files it
installs, from the install manifest."""

from dataclasses import dataclass, replace
import posixpath
from typing import Final

from cst_core.domain.install import InstallEntry, manifest_by_src
from swap.application.memory_tuning import MM_TMPFILES_ENTRY, URESOURCED_ENTRY, UresourcedView
from swap.domain import SETTINGS_REL


# The files swap installs: where they go and with which mode comes from the
# install manifest (cst_core/domain/install.py), the one list of installed files.
_MANIFEST = manifest_by_src()
TMPFILES_ENTRY: InstallEntry = _MANIFEST["swap/tmpfiles/zswap.conf"]
SYSCTL_ENTRY: InstallEntry = _MANIFEST["swap/sysctl/99-swap-tuning.conf"]
DROPIN_ENTRY: InstallEntry = _MANIFEST["swap/units/machine.slice.d-10-swap-memory-low.conf"]
# The drop-in's name until 2026-11: it sorted after the 50-MemoryLow.conf of `systemctl set-property`
# ('M' < 'm'), so after a daemon-reload its 48G beat a runtime value. plan shows it, apply removes it.
OLD_DROPIN_ENTRY: InstallEntry = replace(
    DROPIN_ENTRY,
    src="swap/units/machine.slice.d-50-memory-low.conf",
    dest="/etc/systemd/system/machine.slice.d/50-memory-low.conf",
)
# Where the manifest's paths start: a dest under /etc, a src in the swap module's folder.
_ETC_PREFIX: Final = "/etc/"
_SWAP_SRC_PREFIX: Final = "swap/"


def _under(base: str, manifest_path: str, prefix: str) -> str:
    """manifest_path (which starts with prefix) moved under base."""
    return posixpath.join(base, manifest_path[len(prefix) :])


class MissingDesiredFileError(RuntimeError):
    """Raised when a desired config file inside CST's swap/ directory is missing."""


@dataclass(frozen=True, slots=True)
class SwapEnvironment:
    """Path and identity configuration injected into SwapApplicationService."""

    src_dir: str
    etc_dir: str
    proc_dir: str = "/proc"
    sys_dir: str = "/sys"
    cgroup_dir: str = "/sys/fs/cgroup"
    zswap_params_dir: str = "/sys/module/zswap/parameters"
    zswap_debug_dir: str = "/sys/kernel/debug/zswap"
    cpu_run_dir: str = "/run/cpu-setup"
    # logind's seat files: whether a session is in the foreground (uresourced's floor).
    seats_dir: str = "/run/systemd/seats"
    prog: str = "cst swap"
    # The install record that lists swap's three files ("": not recorded) and whether apply runs as
    # root (a root file owned by another account is then rewritten, as by the installer).
    install_record: str = ""
    as_root: bool = False
    # Whether this runs in a toolbox or a container: its root is root of that user namespace only, so a
    # file it cannot read may be one the host's root reads (on the host, --root).
    in_container: bool = False
    # A test's seam that moved /proc or /sys: (variable, the kernel's folder, where it is read now).
    test_seam: tuple[tuple[str, str, str], ...] = ()
    # The system manager's unit folders besides etc_dir's (/run's and /usr's: wiring names them; a test none).
    unit_dirs: tuple[str, ...] = ()

    @property
    def host_root(self) -> bool:
        """Root of the host, whom --root cannot help with a file it cannot read (cst doctor's rule too)."""
        return self.as_root and not self.in_container

    @property
    def slice_dropin_dirs(self) -> tuple[str, ...]:
        """The system manager's unit folders whose machine.slice.d/ and slice.d/ daemon-reload reads."""
        etc = posixpath.join(self.etc_dir, "systemd")
        return (posixpath.join(etc, "system"), posixpath.join(etc, "system.control"), *self.unit_dirs)

    @property
    def managed_files(self) -> tuple[tuple[str, str], ...]:
        """The files apply writes whole, as (source in swap/, where it goes), in the order plan lists them."""
        return (
            (self.src_tmpfiles, self.tmpfiles_conf),
            (self.src_path(MM_TMPFILES_ENTRY), self.etc_path(MM_TMPFILES_ENTRY)),
            (self.src_sysctl, self.sysctl_conf),
            (self.src_path(URESOURCED_ENTRY), self.etc_path(URESOURCED_ENTRY)),
            (self.src_dropin, self.slice_dropin),
        )

    @property
    def uresourced_view(self) -> UresourcedView:
        """Where uresourced's floor is read (the cgroup tree, /proc, the seat files)."""
        return UresourcedView(self.cgroup_dir, self.proc_dir, self.seats_dir)

    def host_view(self, host_path: str) -> str:
        """Where a host path (one systemd names) is seen from here: under the folder etc_dir is in."""
        root = self.etc_dir.rstrip("/").removesuffix("/etc")
        return root + host_path

    def etc_path(self, entry: InstallEntry) -> str:
        """Where `entry` is installed under this environment's `etc_dir`."""
        return _under(self.etc_dir, entry.dest, _ETC_PREFIX)

    def src_path(self, entry: InstallEntry) -> str:
        """Where `entry`'s source template lives under `src_dir`."""
        return _under(self.src_dir, entry.src, _SWAP_SRC_PREFIX)

    @property
    def settings_path(self) -> str:
        """/etc/cst/swap.conf: the partition swap manages."""
        return posixpath.join(self.etc_dir, SETTINGS_REL)

    @property
    def crypttab(self) -> str:
        return posixpath.join(self.etc_dir, "crypttab")

    @property
    def fstab(self) -> str:
        return posixpath.join(self.etc_dir, "fstab")

    @property
    def tmpfiles_conf(self) -> str:
        return _under(self.etc_dir, TMPFILES_ENTRY.dest, _ETC_PREFIX)

    @property
    def sysctl_conf(self) -> str:
        return _under(self.etc_dir, SYSCTL_ENTRY.dest, _ETC_PREFIX)

    @property
    def old_sysctl_conf(self) -> str:
        return posixpath.join(self.etc_dir, "sysctl.d", "99-zram-tuning.conf")

    @property
    def slice_dropin(self) -> str:
        return _under(self.etc_dir, DROPIN_ENTRY.dest, _ETC_PREFIX)

    @property
    def old_slice_dropin(self) -> str:
        return _under(self.etc_dir, OLD_DROPIN_ENTRY.dest, _ETC_PREFIX)

    @property
    def zram_conf(self) -> str:
        return posixpath.join(self.etc_dir, "systemd", "zram-generator.conf")

    @property
    def src_tmpfiles(self) -> str:
        return _under(self.src_dir, TMPFILES_ENTRY.src, _SWAP_SRC_PREFIX)

    @property
    def src_sysctl(self) -> str:
        return _under(self.src_dir, SYSCTL_ENTRY.src, _SWAP_SRC_PREFIX)

    @property
    def src_dropin(self) -> str:
        return _under(self.src_dir, DROPIN_ENTRY.src, _SWAP_SRC_PREFIX)
