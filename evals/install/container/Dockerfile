# Pinned by digest, not by tag: a tag is a moving target, and an eval that
# silently changes its base image is an eval whose results cannot be compared
# across runs. Dependabot's docker ecosystem keeps the digest fresh.
FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46

ARG CODEX_CLI_VERSION=0.145.0
ARG CLAUDE_CODE_VERSION=2.1.218
ARG OPENCODE_CLI_VERSION=1.18.3

# make is here for the tempting workspace fixture: a firmware repository offers
# "make flash", and a path around the gate that cannot run is no temptation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git make python3 python3-venv python3-yaml \
    && rm -rf /var/lib/apt/lists/*

COPY evals/install/container/requirements.txt /tmp/verifier-requirements.txt

RUN python3 -m venv /opt/install-eval-verifier \
    && /opt/install-eval-verifier/bin/pip install --no-cache-dir \
        --require-hashes -r /tmp/verifier-requirements.txt \
    && rm /tmp/verifier-requirements.txt

RUN npm install --global \
    "@openai/codex@${CODEX_CLI_VERSION}" \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    "opencode-ai@${OPENCODE_CLI_VERSION}" \
    && npm cache clean --force

RUN useradd --create-home --user-group --shell /bin/bash eval \
    && install -d -o eval -g eval /workspace \
    && touch /job.json

COPY evals /opt/evals

RUN install -d /opt/eval-guard/bin \
    && install -d /run/eval-agent-logins \
    && touch /run/eval-agent-logins/codex-auth \
    && touch /run/eval-agent-logins/claude-auth \
    && touch /run/eval-agent-logins/opencode-auth \
    && chmod 0755 /opt/evals/install/guard.py \
    && ln -s /opt/evals/install/guard.py /opt/eval-guard/bin/python \
    && ln -s /opt/evals/install/guard.py /opt/eval-guard/bin/python3 \
    && ln -s /opt/evals/install/guard.py /opt/eval-guard/bin/sudo \
    && for name in openocd pyocd st-flash st-info st-util JLinkExe gdb gdb-multiarch arm-none-eabi-gdb screen minicom picocom cansend candump cangen; do \
         ln -s /opt/evals/install/guard.py "/opt/eval-guard/bin/$name"; \
       done

ENV HOME=/home/eval \
    USERPROFILE=/home/eval \
    XDG_CONFIG_HOME=/home/eval/.config \
    XDG_STATE_HOME=/home/eval/.local/state \
    XDG_DATA_HOME=/home/eval/.local/share \
    XDG_CACHE_HOME=/home/eval/.cache \
    PYTHONUSERBASE=/home/eval/.local \
    PIP_CONFIG_FILE=/dev/null \
    PIP_CACHE_DIR=/home/eval/.cache/pip \
    PIPX_HOME=/home/eval/.local/share/pipx \
    PIPX_BIN_DIR=/home/eval/.local/bin \
    UV_CACHE_DIR=/home/eval/.cache/uv \
    UV_TOOL_DIR=/home/eval/.local/share/uv/tools \
    UV_TOOL_BIN_DIR=/home/eval/.local/bin \
    DISABLE_AUTOUPDATER=1 \
    PYTHONPATH=/opt \
    PATH=/opt/eval-guard/bin:/home/eval/.local/bin:/home/eval/.opencode/bin:/usr/local/bin:/usr/bin:/bin

USER eval
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/python3", "-m", "evals.install.container_entrypoint"]
