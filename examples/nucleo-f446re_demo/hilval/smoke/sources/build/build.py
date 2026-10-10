"""Builds the smoke images: bare-metal C, no vendor code, for the bench gate's own check.

The images in firmware/smoke/ test the gate and the board rather than any
upstream project: that the bench path carries every byte both ways, and which
measurements an image can make on the board by itself, without a jumper. The
job gates build on what they find.
"""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

FIRMWARE = toolchain.REPO_ROOT / "firmware" / "smoke"
IMAGES = ("basic", "echo", "afin", "analog")


def build_images() -> dict[str, Path]:
    """Every smoke image, by name; each ELF lies below HILVAL_STATE_DIR."""
    common = [FIRMWARE / "startup.c", FIRMWARE / "board.c"]
    return {
        name: toolchain.gcc_image(
            "smoke",
            name,
            [*common, FIRMWARE / f"{name}.c"],
            linker_script=FIRMWARE / "link.ld",
            include_dirs=[FIRMWARE],
            # Keeps GCC from turning the copy loops of memcpy and memset into calls to themselves.
            cflags=["-fno-tree-loop-distribute-patterns"],
        )
        for name in IMAGES
    }
