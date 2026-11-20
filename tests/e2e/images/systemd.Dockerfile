# SPDX-License-Identifier: MPL-2.0
FROM registry.fedoraproject.org/fedora:45

ENV container=docker

# One step: the packages; the systemd units an unprivileged PID 1 cannot run, masked (a mask
# that fails is ignored, and only it); a non-root workstation user (uid 1000, passwordless
# sudo for the E2E harness); lingering, so systemd --user starts at boot without a login.
RUN dnf -y install \
    systemd \
    systemd-udev \
    dbus-broker \
    util-linux \
    sudo \
    python3 \
    git \
    cargo \
    uv \
    bluez \
    wireplumber \
    wireplumber-libs \
    pipewire-utils \
    pulseaudio-utils \
    restic \
    openssh-clients \
    acl \
    findutils \
    procps-ng \
    iproute \
    shadow-utils \
    which \
    && dnf clean all \
    && { systemctl mask \
        dev-hugepages.mount \
        sys-fs-fuse-connections.mount \
        systemd-remount-fs.service \
        systemd-udev-trigger.service \
        systemd-udevd.service \
        systemd-machine-id-commit.service \
        systemd-resolved.service \
        rtkit-daemon.service \
        getty@tty1.service \
        || true; } \
    && useradd -m -u 1000 -G wheel tester \
    && printf 'tester ALL=(ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/99-e2e-tester \
    && chmod 0440 /etc/sudoers.d/99-e2e-tester \
    && mkdir -p /var/lib/systemd/linger \
    && touch /var/lib/systemd/linger/tester

COPY tests/e2e/scripts/entrypoint-host-sim.sh /usr/local/sbin/e2e-entrypoint.sh
RUN chmod 0755 /usr/local/sbin/e2e-entrypoint.sh

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/sbin/e2e-entrypoint.sh"]
CMD ["/sbin/init"]
