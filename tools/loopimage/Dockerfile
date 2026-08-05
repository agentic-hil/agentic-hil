# The image the containerised review loop runs in: both agent CLIs plus enough
# of a toolchain to install this project and run its suite.
#
# Two bases rather than one, because the loop needs both runtimes at a version
# each ecosystem actually ships. Debian's own nodejs is far behind what the
# agent CLIs require, and installing Node from a piped shell script would put an
# unpinned download in the build. Both images are bookworm, so the glibc the
# copied binary was linked against is the one it lands next to.
FROM node:22-bookworm-slim@sha256:f32b81066cde10a75dbac96646099533316d94bac4150c55da1636e1f0ffdc46 AS node

# Not a slim variant: "pip install -e" needs the metadata build and git is
# required to clone, commit and diff. Diagnosing either from inside a container
# is a waste of an afternoon -- the same reason tools/ci_linux.py uses the full
# image.
FROM python:3.12-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version

# The agent CLI versions live in package.json and package-lock.json rather than
# in build arguments, so "npm ci" can refuse any tarball whose integrity hash
# does not match the lock. These are the versions evals/install/container pins,
# so the repository has one place to look up which CLI builds it has exercised.
COPY package.json package-lock.json /opt/agent-clis/

RUN cd /opt/agent-clis \
    && npm ci \
    && npm cache clean --force

RUN useradd --create-home --user-group --shell /bin/bash loop

COPY entrypoint.py agent_shim.py loop_options.py /opt/loop/

# The shim stands in front of both CLIs under their own names, first on PATH, so
# the loop's own `shutil.which` finds it without being told: it builds the
# environment of the commit that agent is working on and then execs the real
# CLI. One file under two names, which is how it knows which agent it is.
RUN chmod 0755 /opt/loop/agent_shim.py \
    && mkdir -p /opt/loop/bin \
    && ln -s ../agent_shim.py /opt/loop/bin/claude \
    && ln -s ../agent_shim.py /opt/loop/bin/codex

ENV HOME=/home/loop \
    USERPROFILE=/home/loop \
    XDG_CONFIG_HOME=/home/loop/.config \
    XDG_STATE_HOME=/home/loop/.local/state \
    XDG_DATA_HOME=/home/loop/.local/share \
    XDG_CACHE_HOME=/home/loop/.cache \
    PIP_CACHE_DIR=/home/loop/.cache/pip \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    PATH=/opt/loop/bin:/home/loop/.local/bin:/opt/agent-clis/node_modules/.bin:/usr/local/bin:/usr/bin:/bin

USER loop
WORKDIR /repo
ENTRYPOINT ["/usr/local/bin/python", "/opt/loop/entrypoint.py"]
