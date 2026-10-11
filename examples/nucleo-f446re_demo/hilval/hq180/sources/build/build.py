"""Builds the hq180 validation images (reference/hq180/README.md says what they check).

Two STM32duino images from one sketch: `before` from the unmodified core
`stm32duino-53560ef`, `after` from a copy of that core with the one patch in
firmware/hq180/ applied. Both link the core's IWatchdog library.
"""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

JOB = "hq180"
FIRMWARE = toolchain.REPO_ROOT / "firmware" / JOB
SKETCH = FIRMWARE / "stm32duino"
PATCH = FIRMWARE / "serial-poll-when-irq-blocked.patch"
LIBRARIES = ("IWatchdog",)


def build_images() -> dict[str, Path]:
    """Every hq180 image, by name; each ELF lies below HILVAL_STATE_DIR."""
    before = toolchain.stm32duino_image(JOB, "before", SKETCH, libraries=LIBRARIES)
    patched = toolchain.patched_copy(JOB, toolchain.source_cache("stm32duino-53560ef"), [PATCH])
    after = toolchain.stm32duino_image(JOB, "after", SKETCH, source=patched, libraries=LIBRARIES)
    return {"before": before, "after": after}
