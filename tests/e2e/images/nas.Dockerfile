# SPDX-License-Identifier: MPL-2.0
FROM registry.fedoraproject.org/fedora:45

RUN dnf -y install \
    openssh-server \
    restic \
    && dnf clean all \
    && mkdir -p /var/run/sshd /srv/backup /etc/ssh/sshd_config.d \
    && chmod 0755 /srv/backup \
    && sed -i 's/^root:[^:]*:/root:*:/' /etc/shadow \
    && printf '%s\n' \
        'PermitRootLogin prohibit-password' \
        'PubkeyAuthentication yes' \
        'PasswordAuthentication no' \
        'UsePAM no' \
        'Subsystem sftp /usr/libexec/openssh/sftp-server' \
        >/etc/ssh/sshd_config.d/00-e2e.conf \
    && chmod 0600 /etc/ssh/sshd_config.d/00-e2e.conf

EXPOSE 22
# The SSH host keys are made when the container starts, not baked into the image: each
# container has keys of its own, and the published image holds no private key. exec keeps
# sshd as PID 1. No client of the tests checks the host key.
CMD ["/bin/sh", "-c", "ssh-keygen -A && exec /usr/sbin/sshd -D -e"]
