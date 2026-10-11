"""Builds the hq176 validation images (reference/hq176/README.md says what they check).

Two MicroPython images for the NUCLEO_F446RE with the same frozen measurement
script: `baseline` from the unmodified source cache (the reproduction of
MicroPython #8794 / #8646) and `patched` from a copy of that cache with the
documentation patch applied. The helpers are called as attributes of
`hilval.toolchain`, so the gate can stand them in and check the wiring offline.
"""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

JOB = "hq176"
FIRMWARE = toolchain.REPO_ROOT / "firmware" / JOB
FROZEN = FIRMWARE / "micropython"
PATCH = FIRMWARE / "uart-sendbreak-docs.patch"


def build_images() -> dict[str, Path]:
    """Every hq176 image, by name; each ELF lies below HILVAL_STATE_DIR."""
    baseline = toolchain.micropython_image(JOB, "baseline", FROZEN)
    cache = toolchain.source_cache("micropython")
    copy = toolchain.patched_copy(JOB, cache, [PATCH], name="micropython")
    patched = toolchain.micropython_image(JOB, "patched", FROZEN, source=copy)
    return {"baseline": baseline, "patched": patched}
