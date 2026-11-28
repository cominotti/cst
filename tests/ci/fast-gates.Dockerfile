# SPDX-License-Identifier: MPL-2.0
FROM registry.fedoraproject.org/fedora:45

ARG UV_VERSION=0.12.0
ARG UV_SHA256=eaf842262aa1c418d8ecc5605f02ee1ebfd369124fa48548e85f9481a47831a9

RUN dnf -y install --setopt=install_weak_deps=False \
        git-core \
        python3 \
        ShellCheck \
        curl \
        tar \
        gzip \
        findutils \
        util-linux \
        diffutils \
    && dnf clean all \
    && id -u ci >/dev/null 2>&1 || useradd -m -s /bin/bash ci \
    && git config --system --add safe.directory /repo

RUN set -euo pipefail; \
    arch="$(uname -m)"; \
    if [[ "${arch}" != "x86_64" ]]; then \
        echo "fast-gates.Dockerfile: uv checksum is pinned for x86_64 only (got ${arch})" >&2; \
        exit 1; \
    fi; \
    https_only="=https"; \
    curl -fsSL --proto "${https_only}" --proto-redir "${https_only}" \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
        -o /tmp/uv.tar.gz; \
    printf '%s  /tmp/uv.tar.gz\n' "${UV_SHA256}" | sha256sum -c -; \
    mkdir -p /tmp/uv-extract; \
    tar -xzf /tmp/uv.tar.gz -C /tmp/uv-extract --strip-components=1; \
    install -m 0755 /tmp/uv-extract/uv /tmp/uv-extract/uvx /usr/local/bin/; \
    rm -rf /tmp/uv.tar.gz /tmp/uv-extract
