# Pinned by digest, not by tag: a tag is a moving target, and an eval that
# silently changes its base image is an eval whose results cannot be compared
# across runs. Dependabot's docker ecosystem keeps the digest fresh.
FROM node:25-bookworm-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370

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

# The agent CLI versions live in package.json and package-lock.json rather than
# in build arguments, so "npm ci" can refuse any tarball whose integrity hash
# does not match the lock. "python -m evals.install build --codex-version ..."
# still works: it rewrites the lock first, which also makes the version a
# committed fact instead of a build flag nobody can reconstruct afterwards.
COPY evals/install/container/package.json evals/install/container/package-lock.json /opt/agent-clis/

RUN cd /opt/agent-clis \
    && npm ci \
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
    PATH=/opt/eval-guard/bin:/home/eval/.local/bin:/home/eval/.opencode/bin:/opt/agent-clis/node_modules/.bin:/usr/local/bin:/usr/bin:/bin

USER eval
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/python3", "-m", "evals.install.container_entrypoint"]
