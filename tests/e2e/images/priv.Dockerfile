FROM cst-e2e-systemd:local

RUN dnf -y install \
    btrfs-progs \
    e2fsprogs \
    attr \
    fio \
    moby-engine \
    containerd \
    polkit \
    gamemode \
    libvirt-client \
    libvirt-daemon-driver-qemu \
    && dnf clean all

COPY tests/e2e/scripts/setup-loop-btrfs.sh /usr/local/sbin/e2e-setup-loop-btrfs.sh
COPY tests/e2e/scripts/setup-dind.sh /usr/local/sbin/e2e-setup-dind.sh
RUN chmod 0755 /usr/local/sbin/e2e-setup-loop-btrfs.sh /usr/local/sbin/e2e-setup-dind.sh

# Systemd service inside privileged E2E container to start isolated inner dockerd on loop btrfs
RUN cat >/etc/systemd/system/e2e-dind.service <<'EOF'
[Unit]
Description=E2E Inner Docker Daemon (DinD)
After=network.target

[Service]
Type=notify
ExecStart=/usr/local/sbin/e2e-setup-dind.sh
NotifyAccess=main

[Install]
WantedBy=multi-user.target
EOF
