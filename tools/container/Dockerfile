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
# So this image carries the real tools and nothing that needs hardware: uv and
# pyOCD at one pinned version each, the distribution's OpenOCD and curl, this
# project's dev and can dependency set installed from the locked file the hosted
# matrix uses, and this checkout on top of it. What still needs a probe or a board is the bench tier
# under tests/bench, which never runs here.
#
# Build and run it from the repository root:
#
#     DOCKER_BUILDKIT=1 docker build -f tools/container/Dockerfile -t agentic-hil-container-tests .
#     docker run --rm agentic-hil-container-tests
#
# The run needs the network. uv resolves this distribution's dependencies from
# the package index, and one test reads the release index over HTTPS to check
# that a currency claim is only made after the index has answered.
# Pinned by digest, with the tag it was resolved from beside it, the way this
# repository's workflows pin their actions. A moving tag means two builds of one
# commit can carry a different interpreter patch and a different debugger, which
# is a poor property for the required check whose whole claim is that it ran the
# real ones. The distribution's own packages below stay unpinned deliberately:
# the mirror drops superseded versions, so a version pin without a snapshot
# mirror turns a stable job red days later for a reason nothing in the change
# under it can explain.
FROM python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
# ^ python:3.12-slim

# OpenOCD is the debugger backend the no-probe tests drive, ca-certificates is
# what lets uv and the release-index read verify anything at all, procps is
# `ps`: one test reads a process's start time out of procfs and checks it
# against what a program with no stake in the answer says, and curl is what
# install.sh fetches Astral's pinned uv installer with on the route that has
# no uv and no Python, which one test runs for real to read the shell rc files
# back. Nothing else: an image that grew a toolchain would be an image whose
# results depend on which toolchain.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes openocd ca-certificates procps curl \
    && rm -rf /var/lib/apt/lists/*

# One version, named here, because the receipts these tests read are written by
# this program and a different one may write them differently: that is the whole
# reason the tests exist. Moving the pin is a deliberate change with a test run
# behind it, not something a rebuild does by itself.
ARG UV_VERSION=0.12.9
# pyOCD, pinned for the same reason. The suite's fixture reproduces what this
# version prints when nothing is on USB, `No connected debug probes` and the
# `--uid` form of it, and the classifier reads those words; a release that
# rewords them should fail the drift test in this tier on a deliberate bump,
# not on a rebuild. The mirror argument above does not reach it: the package
# index keeps every release.
ARG PYOCD_VERSION=0.45.1
# setuptools and wheel are the build backend this project declares, and the
# tests build wheels of this checkout with `--no-isolation` so the build needs
# no network of its own.
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    "uv==${UV_VERSION}" "pyocd==${PYOCD_VERSION}" "setuptools>=77" "wheel"

WORKDIR /work

# The dependency set from the file this repository already generates, with its
# hashes, before the checkout is copied in. The matrix job in the same workflow
# installs exactly this set with `--require-hashes` from the same file, and the
# required check resolving it live meant a release of any transitive dependency
# could turn it red on a pull request that changed nothing, with no lock to
# bisect against. It is also the one layer worth caching: it does not invalidate
# when a source file changes.
COPY requirements/dev.txt /tmp/dev.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /tmp/dev.txt

COPY . /work
# `--no-deps`, because the line above installed them from the locked file and a
# resolver run here would be free to move any of them.
RUN python -m pip install --no-cache-dir --disable-pip-version-check --no-deps -e .

# What tells the tier it is where it belongs, in two parts. The variable says a
# run means to be here and can be exported by anybody; this file says a run
# really is here and is written by this build alone. Without the variable every
# test under tests/container skips, which is what keeps a developer's `pytest`
# and the hosted matrix from trying to drive uv and OpenOCD; with the variable
# and without the file the tier stops the collection instead of running commands
# that read hardware on a machine that may have some.
RUN mkdir -p /etc/agentic-hil \
    && printf 'The container test tier runs here. Written by tools/container/Dockerfile.\n' > /etc/agentic-hil/container-test-image
ENV AGENTIC_HIL_CONTAINER_TESTS=1

CMD ["python", "-m", "pytest", "tests/container", "-m", "container"]
