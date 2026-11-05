# foreman/executor-base: the sandbox every executor step runs in.
#
# Minimal Ubuntu Server userland plus git and the executor CLIs. Projects that
# need language toolchains add them in their own `.foreman/Dockerfile`
# (FROM foreman/executor-base) - not here. pi needs Node.js; it gets a private copy
# under /opt/pi that is not on PATH, so `node` is still the project's own choice.
#
# socat runs the gateway relay for the isolated executor network (foreman.sandbox).
#
#   docker build -t foreman/executor-base:latest docker/executor-base
#   docker build --build-arg PI_VERSION=1.4.0 -t foreman/executor-base:latest docker/executor-base
#   docker build --build-arg WITH_AIDER=1 -t foreman/executor-base:latest docker/executor-base

FROM ubuntu:24.04

ARG OPENCODE_VERSION=1.19.3
ARG PI_VERSION=1.4.0
ARG PI_NODE_VERSION=22.23.4
# Optional extra executor: Aider (adds Python and ~300 MB).
ARG WITH_AIDER=0
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git bash tar gzip socat \
 && rm -rf /var/lib/apt/lists/*

# OpenCode: single static binary from the GitHub release.
RUN set -eux; \
    arch="$(case "${TARGETARCH:-amd64}" in amd64) echo x64 ;; arm64) echo arm64 ;; *) echo "${TARGETARCH}" ;; esac)"; \
    if [ "$OPENCODE_VERSION" = "latest" ]; then \
      url="https://github.com/sst/opencode/releases/latest/download/opencode-linux-${arch}.tar.gz"; \
    else \
      url="https://github.com/sst/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${arch}.tar.gz"; \
    fi; \
    curl -fsSL "$url" | tar -xz -C /usr/local/bin opencode; \
    chmod 0755 /usr/local/bin/opencode; \
    opencode --version

# pi (https://pi.dev): its own Node runtime under /opt/pi/node, invoked explicitly by
# the wrapper so neither node nor npm lands on PATH for the agent's shell commands.
RUN set -eux; \
    arch="$(case "${TARGETARCH:-amd64}" in amd64) echo x64 ;; arm64) echo arm64 ;; *) echo "${TARGETARCH}" ;; esac)"; \
    mkdir -p /opt/pi/node; \
    curl -fsSL "https://nodejs.org/dist/v${PI_NODE_VERSION}/node-v${PI_NODE_VERSION}-linux-${arch}.tar.gz" \
      | tar -xz -C /opt/pi/node --strip-components=1; \
    PATH="/opt/pi/node/bin:$PATH" npm install -g --prefix /opt/pi --ignore-scripts --no-fund --no-audit \
      "@earendil-works/pi-coding-agent@${PI_VERSION}"; \
    rm -rf /root/.npm; \
    printf '#!/bin/sh\nexec /opt/pi/node/bin/node /opt/pi/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js "$@"\n' \
      > /usr/local/bin/pi; \
    chmod 0755 /usr/local/bin/pi; \
    pi --version; \
    ! command -v node

RUN set -eux; \
    if [ "$WITH_AIDER" = "1" ]; then \
      apt-get update; \
      apt-get install -y --no-install-recommends python3 pipx; \
      PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install aider-chat; \
      aider --version; \
      rm -rf /var/lib/apt/lists/* /root/.cache; \
    fi

# Non-root user the orchestrator runs containers as (uid/gid 1000).
# ubuntu:24.04 ships an "ubuntu" user with uid 1000; replace it with "agent".
RUN userdel -r ubuntu 2>/dev/null || true \
 && groupadd -g 1000 agent \
 && useradd -m -u 1000 -g 1000 -s /bin/bash agent \
 # Task clones are mounted from the host and may be owned by another uid.
 && git config --system safe.directory '*' \
 && git config --system user.name foreman-agent \
 && git config --system user.email agent@foreman.local

USER agent
WORKDIR /workspace
