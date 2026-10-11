"""Builds the hq175 validation images (reference/hq175/README.md says what they check)."""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

JOB = "hq175"
FIRMWARE = toolchain.REPO_ROOT / "firmware" / JOB
CRATE = FIRMWARE / "embassy"
# The upstream change under test: the API addition this job proposes for
# embassy-rs/embassy#3710, applied to a copy of the source cache's checkout.
PATCH = FIRMWARE / "embassy-independent-outputs.patch"


def build_images() -> dict[str, Path]:
    """Every hq175 image, by name; each ELF lies below HILVAL_STATE_DIR.

    `baseline` builds against the unmodified Embassy checkout of the source cache
    and shows today's behaviour, where `enable()` and `disable()` switch a
    channel's main and complementary outputs together. `patched` builds against a
    copy of the same checkout with `embassy-independent-outputs.patch` applied,
    which adds the independent output enables to `ComplementaryPwm`.

    Both binaries live in one crate and cargo builds only the one it is asked for,
    so `baseline` never compiles the code that calls the new methods and is a real
    build against the untouched upstream revision.
    """
    stm32_data = toolchain.source_cache("stm32-data-generated")
    embassy = toolchain.source_cache("embassy")
    baseline = toolchain.cargo_image(
        JOB,
        "baseline",
        CRATE,
        binary="baseline",
        paths={"EMBASSY": embassy, "STM32_DATA": stm32_data},
    )
    # A patch that does not apply fails here, before the patched image is built.
    patched_embassy = toolchain.patched_copy(JOB, embassy, [PATCH])
    patched = toolchain.cargo_image(
        JOB,
        "patched",
        CRATE,
        binary="patched",
        paths={"EMBASSY": patched_embassy, "STM32_DATA": stm32_data},
    )
    return {"baseline": baseline, "patched": patched}
