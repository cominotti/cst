#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/lib/docker /etc/docker

# Ensure containerd socket is running for dockerd inside DinD
if ! pgrep -x containerd >/dev/null 2>&1; then
    containerd >/var/log/containerd-e2e.log 2>&1 &
    for _ in $(seq 1 30); do
        [[ -S /run/containerd/containerd.sock ]] && break
        sleep 0.2
    done
fi

exec dockerd \
    --data-root=/var/lib/docker \
    --containerd=/run/containerd/containerd.sock \
    --exec-opt=native.cgroupdriver=systemd \
    --storage-driver=btrfs \
    --iptables=false \
    --ip-masq=false \
    --bridge=none
