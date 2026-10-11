"""Builds the hq178 validation images (reference/hq178/README.md says what they check)."""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

JOB = "hq178"
CRATE = toolchain.REPO_ROOT / "firmware" / JOB / "embassy"
REVISIONS = {"base": "embassy-7224/base", "head": "embassy-7224/head"}


def build_images() -> dict[str, Path]:
    """Every hq178 image, by name; each ELF lies below HILVAL_STATE_DIR."""
    stm32_data = toolchain.source_cache("stm32-data-generated")
    return {
        name: toolchain.cargo_image(
            JOB,
            name,
            CRATE,
            binary="hqimage",
            paths={"EMBASSY": toolchain.source_cache(cache), "STM32_DATA": stm32_data},
        )
        for name, cache in REVISIONS.items()
    }
