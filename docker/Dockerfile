# syntax=docker/dockerfile:1
# Ratchet on top of an official Hermes Agent release image.
# Build from the repository root with docker/build.sh, which picks the Hermes release.

ARG HERMES_IMAGE=nousresearch/hermes-agent
ARG HERMES_VERSION=latest
FROM ${HERMES_IMAGE}:${HERMES_VERSION}

ARG HERMES_VERSION
ARG RATCHET_REVISION=unknown
ARG SOURCE_URL=
# Claude Code runs the Anthropic reviewer of the default review panel. Empty skips it.
ARG CLAUDE_CODE_VERSION=latest

USER root
SHELL ["/bin/sh", "-ec"]

# GitHub CLI from its own apt repository. The Hermes pull request acceptance calls
# `gh api --slurp`, which needs 2.48 or newer; a distribution package can be older.
RUN apt-get -o Acquire::Retries=3 update; \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends jq; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL --retry 3 -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
        https://cli.github.com/packages/githubcli-archive-keyring.gpg; \
    chmod 0644 /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get -o Acquire::Retries=3 update; \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*

RUN if [ -n "${CLAUDE_CODE_VERSION}" ]; then \
        npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
        # npm in this image skips install scripts, and the package fetches its binary in one.
        node "$(npm root -g)/@anthropic-ai/claude-code/install.cjs"; \
        npm cache clean --force; \
        # The image's npm prefix is outside PATH.
        ln -sf "$(npm config get prefix)/bin/claude" /usr/local/bin/claude; \
        claude --version; \
    fi

# The Ratchet source stays root-owned and read-only. The boot hook installs it into the
# data volume on every start, so the image decides which Ratchet version runs.
COPY ai_dev/ /opt/ratchet/ai_dev/
COPY bin/ /opt/ratchet/bin/
COPY stands/ /opt/ratchet/stands/
COPY hermes/ /opt/ratchet/hermes/
COPY docs/ /opt/ratchet/docs/
COPY examples/ /opt/ratchet/examples/
COPY INSTALL.md README.md /opt/ratchet/
COPY docker/rootfs/ /

RUN chmod 0755 /etc/cont-init.d/03-ratchet /etc/s6-overlay/s6-rc.d/ratchet-*/run; \
    chmod -R go-w /opt/ratchet; \
    printf '%s\n' "${RATCHET_REVISION}" > /opt/ratchet/REVISION

# A failed boot hook stops the container: without the gate plugin no worker may run.
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2

LABEL org.opencontainers.image.title="ratchet-bot" \
      org.opencontainers.image.description="Hermes Agent with the Ratchet layer for gated coding cards" \
      org.opencontainers.image.revision="${RATCHET_REVISION}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      io.ratchet.hermes.version="${HERMES_VERSION}"

CMD ["gateway", "run"]
