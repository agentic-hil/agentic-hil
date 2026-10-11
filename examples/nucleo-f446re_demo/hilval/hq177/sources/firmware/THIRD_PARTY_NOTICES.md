# Third-party components in the hq177 images

The image `dual-dac` links one third-party component.

## libopencm3

* Upstream: <https://github.com/libopencm3/libopencm3>
* Revision used: `ddcbb2889fa018626c604e27ea50e66db5755b78`
* License: LGPL-3.0-or-later (`COPYING.LGPL3`, which builds on `COPYING.GPL3`)

The image links `libopencm3_stm32f4.a` statically, built from that revision
for the target `stm32/f4` with `FP_FLAGS=-mfloat-abi=soft`, and uses its
linker script `cortex-m-generic.ld`. The complete source of the library is
that revision of the repository above; nothing in it was modified for the
image. `reference/hq177/libopencm3/` in this repository holds read-only copies
of the files the check is about, each with its own header.

`0001-dac-doc-split-dual-dma-example-by-dma-family.patch` in this directory is
a change to one LGPL-3.0-or-later file of that revision
(`lib/stm32/common/dac_common_all.c`) and is offered under the same terms. It
is not applied to the library the image links against: the image is built
against the unmodified revision, because the point of the check is that the
example works with libopencm3 as it is published.
