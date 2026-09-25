# The image the bench tier runs in: the tests under tests/bench, this checkout's
# product, and the tools the tier builds firmware with and drives the board
# through.
#
# The bench tier is the part of the suite that needs an in-circuit debugger or
# programmer and the board behind it, so neither the hosted matrix nor the
# container tier beside this directory can run it. Run directly on a bench, it
# measured whatever that machine happened to carry: its OpenOCD, its cross
# compiler, its GDB, its Python and whatever sat in its user site. This image
# takes all of that out of the machine and into one commit's build, the way
# tools/container/Dockerfile does for the tier that needs no hardware. What the
# bench still provides is the board, the probe, the device nodes they appear as
# and the right to open them, and none of that is written here.
#
# Built and run by tools/bench_in_container.py, which builds this file from the
# committed tree rather than the working tree, hands the probe's USB node and
# its serial port to the container, mounts the machine's device locks and reads
# pytest's summary line as the verdict. From a checkout on the machine the board
# is attached to:
#
#     python3 tools/bench_in_container.py
#
# The tier needs no network, and the runner starts it with none.
#
# Pinned by digest, with the tag it was resolved from beside it, for the reason
# tools/container/Dockerfile gives: two builds of one commit carry the same
# interpreter, the same distribution release and therefore the same debugger and
# the same compiler. The distribution's packages below stay unpinned for that
# file's reason too, and the digest is what fixes the versions a build gets.
#
# Named with its registry, because Podman resolves a short name only through an
# alias table the machine may not have and otherwise asks which registry to use,
# which a run with no terminal cannot answer. Docker reads the same reference as
# the short name it has always used.
FROM docker.io/library/python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
# ^ python:3.12-slim

# OpenOCD is the debugger backend the tier flashes, resets and debugs the board
# through. gcc-arm-none-eabi is the cross compiler the tier builds the demo
# firmware and its own images with, and libnewlib-arm-none-eabi is the C library
# they link against: the compiler only recommends it, so an install without
# recommends that did not name it would build nothing. gdb-multiarch is the GDB
# the debug sessions run, because the distribution packages no
# arm-none-eabi-gdb and the product finds gdb-multiarch on PATH by itself.
# cmake and ninja-build are what the demo's Debug preset builds with.
# libusb-1.0-0 is how OpenOCD and pyOCD reach the probe over USB; OpenOCD pulls
# it in anyway, and it is named because the tier depends on it directly. tini
# is process 1: it hands the stop signal on to pytest and the commands pytest
# is running, and it reaps what the tier's commands leave behind. A debugger
# server whose parent exited is reparented to process 1, and under a process 1
# that never waits it stays a zombie, which still answers a liveness check by
# pid.
#
# Nothing else. No udev, no editor, no compiler for the host: an image that grew
# a tool the tier does not use would be an image whose results depend on it.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes openocd gcc-arm-none-eabi libnewlib-arm-none-eabi gdb-multiarch cmake ninja-build libusb-1.0-0 tini \
    && rm -rf /var/lib/apt/lists/*

# The same two locked files the container tier installs, with their hashes and
# in the same order. `requirements/container.txt` carries pyOCD at the version
# the rest of the suite is pinned against, the libusb binding it reaches USB
# through, and setuptools and wheel, the build backend this project declares. It
# also carries uv, which nothing in this tier runs: the lock is installed whole,
# because a subset of a hashed lock is a second lock nobody regenerates.
COPY requirements/container.txt /tmp/container.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /tmp/container.txt

WORKDIR /work

# The dependency set from the file the hosted matrix installs, with its hashes,
# before the checkout is copied in, so that a change to a source file does not
# rebuild this layer.
COPY requirements/dev.txt /tmp/dev.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes -r /tmp/dev.txt

COPY . /work
# `--no-deps`, because the line above installed them from the locked file and a
# resolver run here would be free to move any of them. `--no-build-isolation`,
# because an isolated build fetches the build backend from the index without a
# hash, and the one installed from the lock above is the one this build uses.
RUN python -m pip install --no-cache-dir --disable-pip-version-check --no-deps --no-build-isolation -e .

# What says a run really is in this image, written by this build and by nothing
# else. The runner prints it before pytest starts and refuses a verdict from a
# run whose output does not carry it, so a summary line from some other image
# cannot pass for this tier's.
RUN mkdir -p /etc/agentic-hil \
    && printf 'The bench tier runs here. Written by tools/bench/Dockerfile.\n' > /etc/agentic-hil/bench-test-image

# A home that belongs to no account. The tier's commands keep HOME, because the
# machine-wide device locks are under it, so the runner mounts the machine's
# lock directory at .agentic-hil/device-locks in here; nothing else of the
# machine's home comes in. Writable by every user and sticky, because the image
# cannot know who it runs as: under rootless Podman it is the container's root,
# which is the invoking user, and under Docker it is the invoking user's uid.
RUN install -d -m 1777 /bench-home /bench-home/.agentic-hil
ENV HOME=/bench-home \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Deliberately not set here: AGENTIC_HIL_BENCH. It is the statement that a probe
# and a board are attached, and an image cannot know that; the runner sets it on
# the run that hands the devices in.
LABEL agentic-hil.image="bench-tier"

# SIGINT rather than SIGTERM, so a stopped run tears down the way an interrupted
# pytest does: its fixtures' finalizers run, and the one that puts the demo back
# on the board runs among them. SIGTERM would end the interpreter with the
# board still running whichever image the interrupted test had put on it.
#
# `-g` hands the signal to pytest's whole process group, as Ctrl-C in a
# terminal does, and not to pytest alone. The tier drives the board through the
# product's commands and servers, which it starts and waits on, and Python's
# subprocess.run gives a command it is waiting on a quarter of a second after an
# interrupt before it kills it. In the group, each of them hears the interrupt
# itself and ends the way the product ends an interrupted command.
STOPSIGNAL SIGINT
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["python", "-m", "pytest", "tests/bench", "-p", "no:cacheprovider"]
