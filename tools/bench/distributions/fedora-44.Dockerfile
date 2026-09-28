# The part of the bench tier's image that is Fedora 44's: its base image and the
# packages the tier builds firmware with and drives the board through.
#
# tools/bench_in_container.py --distribution fedora-44 builds this file with
# everything from tools/bench/Dockerfile's first WORKDIR on after it, so the
# checkout, its locked dependencies, the marker and the entry point are the
# default image's own and only what Fedora 44 packages differs: its OpenOCD,
# its cross compiler and C library, its GDB, its CMake and its Python. See
# tools/bench/README.md.
#
# Pinned by digest, with the tag it was resolved from beside it, and named
# with its registry, for the reasons tools/bench/Dockerfile gives for its own.
# The stage is bench-tier, the name that file's later stages build on.
FROM docker.io/library/fedora@sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80 AS bench-tier
# ^ fedora:44

# The default image's packages under Fedora's names. Fedora packages the C++
# compiler apart from the C compiler, and the demo's CMake project enables
# C++, so both are named. It packages no GDB for ARM of its own and no
# gdb-multiarch: its `gdb` is the one the product finds on PATH. python3 and
# python-unversioned-command, for the `python` the shared part creates its
# virtual environment with. Weak dependencies are left out, as recommended
# packages are in the default image.
RUN dnf install --assumeyes --setopt=install_weak_deps=False openocd arm-none-eabi-gcc-cs arm-none-eabi-gcc-cs-c++ arm-none-eabi-newlib gdb cmake ninja-build libusb1 tini python3 python-unversioned-command \
    && dnf clean all

# The distribution's name, where the bench tier reads which image it runs in.
# A stage that a clean account here cannot run is left out under this name.
# The default image names no distribution, and nothing is left out there for
# what the image lacks.
RUN mkdir -p /etc/agentic-hil && printf '%s\n' fedora-44 > /etc/agentic-hil/bench-distribution
