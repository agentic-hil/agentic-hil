"""Builds the hq172 images: one Zephyr application against the head and the base tree of PR #121031.

The trees are the workspace directories named in TREES. The gate in tests/hq172 freezes the
revisions those directories must hold and checks them three ways: it compares each image's
`zephyr` field (Zephyr's generated BUILD_VERSION, git describe of ZEPHYR_BASE) with them, it
replaces `toolchain.zephyr_image` by a stand-in and asserts that build_images() calls it with
exactly TREES, this application directory and CMAKE_ARGS, and it reads the workspace's tree and
module checkouts with git in the session that built the images. REVISIONS here names the same
hashes for readers of the published sources; the gate asserts the two agree. Rules this module
keeps for that: the helper is called as the module attribute `toolchain.zephyr_image`, never
imported by name; nothing else from toolchain is called; -DBUILD_VERSION is never passed, and
CMAKE_ARGS is the complete list of extra CMake arguments.
"""

from __future__ import annotations

from pathlib import Path

from hilval import toolchain

JOB = "hq172"
FIRMWARE = toolchain.REPO_ROOT / "firmware" / JOB
APP = FIRMWARE / "zephyr"
TREES = {"head": "z121031-head", "base": "z121031-base"}
REVISIONS = {
    "head": "90ccb4f9d4efc6a2886a8c910f7129856667ae04",
    "base": "3c61a12bdd8b2975b9180abcb80779ad3724a40d",
}
CMAKE_ARGS = (f"-DDTC_OVERLAY_FILE={(APP / 'app.overlay').as_posix()}",)


def build_images() -> dict[str, Path]:
    """Every hq172 image, by name; each ELF lies below HILVAL_STATE_DIR."""
    return {
        name: toolchain.zephyr_image(JOB, name, APP, zephyr=tree, cmake_args=CMAKE_ARGS)
        for name, tree in TREES.items()
    }
