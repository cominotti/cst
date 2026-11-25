# SPDX-License-Identifier: MPL-2.0
"""What rpm-ostree's answers to a kernel-arguments change mean, for compress-force and commit-interval."""

from __future__ import annotations

from typing import Final

# The start of what rpm-ostreed answers a change while it runs another transaction (an automatic update, a
# user's upgrade), followed by that transaction's title: rpmostreed_sysroot_prep_for_txn in
# src/daemon/rpmostreed-sysroot.cxx, which the KernelArgs method calls first. It refuses the call before it
# starts a transaction of its own, so the change staged nothing.
KARGS_REFUSED_BUSY: Final[str] = "Transaction in progress:"


def kargs_refused(err: str) -> bool:
    """Whether a failed `rpm-ostree kargs` change's error (update_kargs's) is rpm-ostreed's refusal while it
    runs another transaction (KARGS_REFUSED_BUSY): the change started no transaction and staged nothing."""
    return KARGS_REFUSED_BUSY in err
