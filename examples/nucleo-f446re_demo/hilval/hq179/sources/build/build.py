"""Builds the hq179 images: Zephyr PR #119508's STM32 Pulse IO driver, unpatched and patched.

Both images are one Zephyr application (firmware/hq179/zephyr) built against the
workspace tree z119508-head (PR head f7165025ba97992bd7367d6873f986b4cdca407f,
hal_stm32 c9afc5e80f977e48f9de14c7f6cce1d193ec7181). prj.conf switches the
in-tree backend off and the driver enters as application source: a copy of
drivers/pulse_io/pulse_io_stm32_timer_dma.c taken from the tree, unpatched for
`before` and with firmware/hq179/pulse_io_stm32_timer_dma.patch applied for
`after`, so the two images differ only by that patch. Nothing here sets
BUILD_VERSION; the image prints Zephyr's own and tests/hq179 checks it.
"""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

JOB = "hq179"
FIRMWARE = toolchain.REPO_ROOT / "firmware" / JOB
APP = FIRMWARE / "zephyr"
PATCH = FIRMWARE / "pulse_io_stm32_timer_dma.patch"
ZEPHYR_TREE = "z119508-head"
MODULES = ("hal/stm32-c9afc5e", "hal/cmsis_6", "hal/cmsis")
DRIVER = "drivers/pulse_io/pulse_io_stm32_timer_dma.c"
IMAGES = {"before": (), "after": (PATCH,)}


def build_images() -> dict[str, Path]:
    """The two ELFs, keyed `before` and `after`, below HILVAL_STATE_DIR."""
    tree = toolchain.zephyr_workspace() / ZEPHYR_TREE
    overlay = (APP / "app.overlay").resolve().as_posix()
    images: dict[str, Path] = {}
    for name, patches in IMAGES.items():
        copy = toolchain.patched_copy(JOB, tree, patches, name=f"driver-{name}", include=[DRIVER])
        driver = (copy / DRIVER).as_posix()
        images[name] = toolchain.zephyr_image(
            JOB,
            name,
            APP,
            zephyr=ZEPHYR_TREE,
            modules=MODULES,
            cmake_args=[f"-DDTC_OVERLAY_FILE={overlay}", f"-DHQ179_DRIVER_SRC={driver}"],
        )
    return images
