"""Build the bench's two ESP32 images and write down what they are.

The ESP32 stage of the bench tier (``tests/bench/esp32_recordings.py``) flashes
two images onto an ESP32 through esptool and reads on the board's console which
one the chip runs. The Nucleo's images are built by the tier itself, inside the
bench container, with the ARM toolchain that image carries. These are not: the
bench image has no Xtensa toolchain, and the bench receives binaries rather than
building them. So the two images are built here, on a developer machine, and
committed beside their source, and this script is the record of how:

* ``tests/bench/firmware/esp32/banner.c`` is compiled twice, once per image
  letter, against ``esp32_ram.ld`` beside it, with Espressif's GCC from the
  crosstool-NG release named in ``TOOLCHAIN_RELEASE``
  (github.com/espressif/crosstool-NG, checked against that release's own
  checksum file before use);
* ``esptool elf2image`` turns each ELF into the image the ROM loads from flash
  offset 0x1000; it reads a file and writes a file, and opens no serial port;
* ``manifest.json`` beside them names each image's SHA-256 and size, the SHA-256
  of the two sources they were built from, the compiler release and version,
  the compiler flags and the esptool version.

``tests/test_bench_esp32_images.py`` holds the committed images against that
manifest and against the image format the ROM reads, on every host and without
the toolchain, and fails when a source changed without the images being built
again. The same toolchain release writes the same bytes, so ``git diff`` after a
run says whether anything moved.

Usage:

    python tools/build_esp32_bench_images.py --toolchain <directory holding xtensa-esp32-elf-gcc>

Run it with the Python environment esptool is installed in.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = REPOSITORY_ROOT / "tests" / "bench" / "firmware" / "esp32"
SOURCES = ("banner.c", "esp32_ram.ld")
MANIFEST = FIRMWARE / "manifest.json"

TOOLCHAIN_RELEASE = "esp-16.1.0_20260609"
IMAGES = {"banner_a.bin": "A", "banner_b.bin": "B"}
FLASH_ADDRESS = "0x1000"
CFLAGS = (
    "-std=c11",
    "-Os",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-ffreestanding",
    "-fno-builtin",
    "-nostdlib",
    "-mlongcalls",
    "-ffunction-sections",
    "-fdata-sections",
    "-Wl,--gc-sections",
)
VERSION_LINE = re.compile(r"\(crosstool-NG (?P<release>[^)]+)\) (?P<gcc>\S+)$")


def run(command: list[str]) -> str:
    """One build command's standard output; its whole output and status when it fails."""
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"{Path(command[0]).name} exited {completed.returncode}:\n{completed.stdout}{completed.stderr}")
    return completed.stdout


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the bench's ESP32 images and their manifest.")
    parser.add_argument("--toolchain", type=Path, required=True, help="the directory holding xtensa-esp32-elf-gcc")
    arguments = parser.parse_args(argv)

    found = shutil.which("xtensa-esp32-elf-gcc", path=str(arguments.toolchain))
    if found is None:
        raise SystemExit(f"there is no xtensa-esp32-elf-gcc in {arguments.toolchain}")
    gcc = Path(found)
    stated = VERSION_LINE.search(run([str(gcc), "--version"]).splitlines()[0])
    if stated is None or stated["release"] != TOOLCHAIN_RELEASE:
        raise SystemExit(f"this script builds with the {TOOLCHAIN_RELEASE} toolchain, and {gcc} is not that one")

    images: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory() as scratch:
        for name, letter in IMAGES.items():
            elf = Path(scratch) / f"{Path(name).stem}.elf"
            image = FIRMWARE / name
            run([str(gcc), *CFLAGS, "-T", str(FIRMWARE / "esp32_ram.ld"), f'-DIMAGE="{letter}"', "-o", str(elf), str(FIRMWARE / "banner.c")])
            run([sys.executable, "-I", "-m", "esptool", "--chip", "esp32", "elf2image", "--flash-mode", "dio", "-o", str(image), str(elf)])
            images[name] = {"image": letter, "sha256": sha256_of(image), "size": image.stat().st_size}

    manifest = {
        "chip": "esp32",
        "flash_address": FLASH_ADDRESS,
        "images": images,
        "sources": {name: sha256_of(FIRMWARE / name) for name in SOURCES},
        "toolchain": {"release": stated["release"], "gcc": stated["gcc"], "cflags": list(CFLAGS)},
        "esptool": version("esptool"),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
