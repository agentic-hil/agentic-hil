# The image the container tests run in, and the whole of what they are tested
# against.
#
# The suite under tests/ runs against fakes written beside the code, so it can
# only be as right as the person who wrote the fake was about the tool. Four
# defects reached a bench through exactly that gap: uv writes `python` at the
# `[tool]` level of its receipt and not under `[tool.options]`, a virtual
# environment's process resolves its image to the system interpreter so
# `/proc/<pid>/exe` never names the environment, a procfs directory's creation
# time is the time of the first lookup rather than the process's start, and
# OpenOCD prints a failure-worded line after a reset that succeeded. Every one
# of them is invisible to a fake and obvious to the real tool.
#
# So this image carries the real tools and nothing that needs hardware: uv at
# one pinned version, the distribution's OpenOCD, and this checkout installed
# with its dev and can extras. What still needs a probe or a board is the bench
# tier under tests/bench, which never runs here.
#
# Build and run it from the repository root:
#
#     DOCKER_BUILDKIT=1 docker build -f tools/container/Dockerfile -t agentic-hil-container-tests .
#     docker run --rm agentic-hil-container-tests
#
# The run needs the network. uv resolves this distribution's dependencies from
# the package index, and one test reads the release index over HTTPS to check
# that a currency claim is only made after the index has answered.
FROM python:3.12-slim

# OpenOCD is the debugger backend the no-probe tests drive, ca-certificates is
# what lets uv and the release-index read verify anything at all, and procps is
# `ps`: one test reads a process's start time out of procfs and checks it
# against what a program with no stake in the answer says. Nothing else: an
# image that grew a toolchain would be an image whose results depend on which
# toolchain.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes openocd ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# One version, named here, because the receipts these tests read are written by
# this program and a different one may write them differently: that is the whole
# reason the tests exist. Moving the pin is a deliberate change with a test run
# behind it, not something a rebuild does by itself.
ARG UV_VERSION=0.12.9
# setuptools and wheel are the build backend this project declares, and the
# tests build wheels of this checkout with `--no-isolation` so the build needs
# no network of its own.
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    "uv==${UV_VERSION}" "setuptools>=77" "wheel"

WORKDIR /work
COPY . /work
RUN python -m pip install --no-cache-dir --disable-pip-version-check -e ".[dev,can]"

# What tells the tier it is where it belongs. Without it every test under
# tests/container skips, which is what keeps a developer's `pytest` and the
# hosted matrix from trying to drive uv and OpenOCD.
ENV AGENTIC_HIL_CONTAINER_TESTS=1

CMD ["python", "-m", "pytest", "tests/container", "-m", "container"]
