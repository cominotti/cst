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
        'HostKey /etc/ssh/ssh_host_ed25519_key' \
        'Subsystem sftp /usr/libexec/openssh/sftp-server' \
        >/etc/ssh/sshd_config.d/00-e2e.conf \
    && chmod 0600 /etc/ssh/sshd_config.d/00-e2e.conf

EXPOSE 22
# The SSH host key is made when the container starts, not baked into the image: each
# container has a key of its own, and the published image holds no private key. Only the
# ed25519 key, the one sshd loads (HostKey above): ssh-keygen -A would also make RSA,
# ECDSA and ML-DSA keys nobody uses, about half a second (most of it the RSA key) before
# sshd listens. A restarted container keeps its key. exec keeps sshd as PID 1. No client of
# the tests checks the host key.
CMD ["/bin/sh", "-c", "{ [ -e /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key; } && exec /usr/sbin/sshd -D -e"]
