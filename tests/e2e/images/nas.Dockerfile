FROM registry.fedoraproject.org/fedora:45

RUN dnf -y install \
    openssh-server \
    restic \
    && dnf clean all

RUN ssh-keygen -A \
    && mkdir -p /var/run/sshd /srv/backup /etc/ssh/sshd_config.d \
    && chmod 0755 /srv/backup \
    && sed -i 's/^root:[^:]*:/root:*:/' /etc/shadow \
    && printf 'PermitRootLogin prohibit-password\nPubkeyAuthentication yes\nPasswordAuthentication no\nUsePAM no\nSubsystem sftp /usr/libexec/openssh/sftp-server\n' \
        >/etc/ssh/sshd_config.d/00-e2e.conf \
    && chmod 0600 /etc/ssh/sshd_config.d/00-e2e.conf

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
