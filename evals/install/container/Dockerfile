FROM node:22-bookworm-slim

ARG CODEX_CLI_VERSION=0.145.0
ARG CLAUDE_CODE_VERSION=2.1.218
ARG OPENCODE_CLI_VERSION=1.18.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv python3-yaml \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/install-eval-verifier \
    && /opt/install-eval-verifier/bin/pip install --no-cache-dir \
        "jsonschema==4.25.1" \
        "pyserial==3.5" \
        "PyYAML==6.0.2"

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
    && install -d /run/agentic-hil-secrets \
    && touch /run/agentic-hil-secrets/codex-auth \
    && touch /run/agentic-hil-secrets/opencode-auth \
    && chmod 0755 /opt/evals/install/guard.py \
    && ln -s /opt/evals/install/guard.py /opt/eval-guard/bin/python \
    && ln -s /opt/evals/install/guard.py /opt/eval-guard/bin/python3 \
    && ln -s /opt/evals/install/guard.py /opt/eval-guard/bin/sudo

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
