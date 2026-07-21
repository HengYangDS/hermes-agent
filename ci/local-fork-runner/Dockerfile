# Isolated, repository-scoped GitHub Actions Runner for HengYangDS/hermes-agent.
# It deliberately has no host worktree, Docker socket, SSH material, or Git
# configuration mount. Registration and job execution occur in separate
# containers; only the former reads the short-lived registration token.
FROM node:22-bookworm

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG RUNNER_VERSION=2.335.1
ARG RUNNER_ARCHIVE_SHA256=6d1e85bfd1a506a8b17c1f1b9b57dba458ffed90898799aaa9f599520b0d9207
ARG RUNNER_UID=10001
ARG RUNNER_GID=10001

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl git jq libicu72 libkrb5-3 liblttng-ust1 libssl3 python3 zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$RUNNER_GID" hermes-runner \
    && useradd --create-home --uid "$RUNNER_UID" --gid hermes-runner --shell /bin/bash hermes-runner

WORKDIR /opt/actions-runner-dist
RUN curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
      --output /tmp/actions-runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-arm64-${RUNNER_VERSION}.tar.gz" \
    && printf '%s  %s\n' "$RUNNER_ARCHIVE_SHA256" /tmp/actions-runner.tar.gz | sha256sum --check --status \
    && tar -xzf /tmp/actions-runner.tar.gz \
    && rm /tmp/actions-runner.tar.gz \
    && chown -R "$RUNNER_UID:$RUNNER_GID" /opt/actions-runner-dist

COPY register-entrypoint.sh /usr/local/bin/hermes-fork-runner-register
COPY run-entrypoint.sh /usr/local/bin/hermes-fork-runner-run
RUN chmod 0755 /usr/local/bin/hermes-fork-runner-register /usr/local/bin/hermes-fork-runner-run

USER ${RUNNER_UID}:${RUNNER_GID}
ENV HOME=/tmp/home \
    RUNNER_ALLOW_RUNASROOT=0
ENTRYPOINT ["/usr/local/bin/hermes-fork-runner-run"]
