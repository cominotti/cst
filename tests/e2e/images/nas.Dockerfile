# SPDX-License-Identifier: MPL-2.0
FROM registry.fedoraproject.org/fedora:45

RUN dnf -y install \
    openssh-server \
    restic \
    && dnf clean all \
    && ssh-keygen -A \
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
CMD ["/usr/sbin/sshd", "-D", "-e"]
