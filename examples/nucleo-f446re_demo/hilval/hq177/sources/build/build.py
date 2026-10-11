"""Builds the hq177 validation images (reference/hq177/README.md says what they check).

One image, `dual-dac`: the dual-channel DAC of the F446 driven by DMA on one
timer trigger and read back by ADC1, for libopencm3 issue #528. It links
against the unmodified libopencm3 source cache, because the example has to
work with the library as it is; the documentation patch beside the firmware is
applied to a copy of that cache and syntax-checked here, so a patch that no
longer applies, or one that closes the Doxygen comment it lives in, fails
offline instead of after a bench run.

The SHA-256 of every compiled source is passed in as a string literal and
printed by the image, so the evidence of a run names the sources it was built
from and the example's digest is in the binary itself.
"""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

FIRMWARE = toolchain.REPO_ROOT / "firmware" / "hq177"
IMAGE = "dual-dac"

# The sources the image is compiled from, in the order the compiler sees them.
# The build hashes exactly this list and the image prints one `src` record per
# entry, so nothing it was built from is left unhashed.
SOURCES = ("main.c", "dual_dac_dma.c", "console.c", "faults.c")
# One define per source, in the same order. `dual_dac_dma.c` holds the
# documented example, so its digest is the one named after the source itself.
DIGEST_DEFINES = (
    "HQ177_SHA_MAIN_C",
    "HQ177_SOURCE_SHA",
    "HQ177_SHA_CONSOLE_C",
    "HQ177_SHA_FAULTS_C",
)

PATCH = FIRMWARE / "0001-dac-doc-split-dual-dma-example-by-dma-family.patch"
DAC_DOC = "lib/stm32/common/dac_common_all.c"


def _check_doc_patch() -> None:
    """Apply the documentation patch to a copy of the cache and compile the result.

    `patched_copy` fails if the patch does not apply. The compile is what makes
    the check worth running: the whole text the patch changes sits inside one
    `/** ... */` Doxygen comment, so a stray comment delimiter anywhere in the
    insertion would end that comment early and leave the file uncompilable.
    Only the copy is read and written; the source cache never is.
    """
    cache = toolchain.source_cache("libopencm3")
    copy = toolchain.patched_copy("hq177", cache, [PATCH], name="libopencm3-doc", include=[DAC_DOC])
    toolchain.run(
        [
            toolchain.arm_tool("gcc"),
            *toolchain.CORTEX_M4_FLAGS,
            "-std=c11",
            "-fsyntax-only",
            "-DSTM32F4",
            "-I",
            (cache / "include").as_posix(),
            (copy / DAC_DOC).as_posix(),
        ],
        cwd=copy,
    )


def build_images() -> dict[str, Path]:
    """Every hq177 image, by name; each ELF lies below HILVAL_STATE_DIR."""
    _check_doc_patch()
    # Built after the patch check and from the unmodified cache: criterion 1 is
    # that the example works against the library as it is published.
    root = toolchain.libopencm3("hq177")
    digests = [
        f'{define}="{toolchain.sha256(FIRMWARE / source)}"'
        for define, source in zip(DIGEST_DEFINES, SOURCES)
    ]
    elf = toolchain.gcc_image(
        "hq177",
        IMAGE,
        [FIRMWARE / source for source in SOURCES],
        linker_script=FIRMWARE / "link.ld",
        defines=["STM32F4", *digests],
        include_dirs=[FIRMWARE],
        system_include_dirs=[root / "include"],
        library_dirs=[root / "lib"],
        libraries=["opencm3_stm32f4"],
        # Keeps GCC from turning the copy loops of memcpy and memset into calls
        # to themselves; the image links with -nostdlib against libgcc alone.
        cflags=["-fno-tree-loop-distribute-patterns"],
    )
    return {IMAGE: elf}
