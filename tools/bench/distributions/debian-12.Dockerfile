# The part of the bench tier's image that is Debian 12's: its base image and the
# packages the tier builds firmware with and drives the board through.
#
# tools/bench_in_container.py --distribution debian-12 builds this file with
# everything from tools/bench/Dockerfile's first WORKDIR on after it, so the
# checkout, its locked dependencies, the marker and the entry point are the
# default image's own and only what Debian 12 packages differs: its OpenOCD,
# its cross compiler and C library, its GDB, its CMake and its Python. See
# tools/bench/README.md.
#
# Pinned by digest, with the tag it was resolved from beside it, and named
# with its registry, for the reasons tools/bench/Dockerfile gives for its own.
# The stage is bench-tier, the name that file's later stages build on.
FROM docker.io/library/debian@sha256:f37a335e82bca302e955fa39f9dfe28f1be618f016f8a2b56318e5a5111afc26 AS bench-tier
# ^ debian:12

# The default image's packages under the same names, for the reasons
# tools/bench/Dockerfile gives, and the three its base image carries
# already: python3, python3-venv for the virtual environment the shared
# part creates, and python-is-python3 for the `python` it is created with.
# DEBIAN_FRONTEND on the one command, so no package stops the build with a
# question and the image does not keep the setting.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends --yes openocd gcc-arm-none-eabi libnewlib-arm-none-eabi gdb-multiarch cmake ninja-build libusb-1.0-0 tini python3 python3-venv python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

# The distribution's name, where the bench tier reads which image it runs in.
# A stage that a clean account here cannot run is left out under this name.
# The default image names no distribution, and nothing is left out there for
# what the image lacks.
RUN mkdir -p /etc/agentic-hil && printf '%s\n' debian-12 > /etc/agentic-hil/bench-distribution
