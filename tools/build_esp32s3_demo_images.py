"""Build the Nano ESP32-S3 demo's RAM images and reproducibility manifest."""

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

FIRMWARE = Path(__file__).resolve().parents[1] / "examples" / "nano-esp32s3_demo"
SOURCES = ("Src/banner.c", "esp32s3_ram.ld")
IMAGES = {"banner_a.bin": "A", "banner_b.bin": "B"}
TOOLCHAIN_RELEASE = "esp-16.1.0_20260609"
VERSION_LINE = re.compile(r"\(crosstool-NG ([^)]+)\) (\d+\.\d+\.\d+)\s*$")
CFLAGS = (
    "-std=c11", "-Os", "-Wall", "-Wextra", "-Werror", "-ffreestanding",
    "-fno-builtin", "-nostdlib", "-mlongcalls", "-ffunction-sections",
    "-fdata-sections", "-Wl,--gc-sections",
)


def run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise SystemExit(f"{Path(command[0]).name} failed:\n{completed.stdout}{completed.stderr}")
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True, help="directory containing xtensa-esp32s3-elf-gcc")
    args = parser.parse_args(argv)
    gcc = shutil.which("xtensa-esp32s3-elf-gcc", path=str(args.toolchain))
    if gcc is None or Path(gcc).resolve().parent != args.toolchain.resolve():
        parser.error("--toolchain must contain xtensa-esp32s3-elf-gcc")
    first_line = run([gcc, "--version"]).partition("\n")[0]
    compiler = VERSION_LINE.search(first_line)
    if compiler is None or compiler[1] != TOOLCHAIN_RELEASE:
        parser.error(f"--toolchain must be the crosstool-NG release {TOOLCHAIN_RELEASE}")

    source, linker = (FIRMWARE / name for name in SOURCES)
    esptool = version("esptool")
    sources = {name: hashlib.sha256((FIRMWARE / name).read_bytes()).hexdigest() for name in SOURCES}
    images = {}
    # Finish both conversions before replacing anything in the checkout.
    with tempfile.TemporaryDirectory() as scratch:
        temporary = Path(scratch)
        for name, letter in IMAGES.items():
            image = temporary / name
            elf = image.with_suffix(".elf")
            run([gcc, *CFLAGS, "-T", str(linker), f'-DIMAGE="{letter}"', "-o", str(elf), str(source)])
            run([sys.executable, "-I", "-m", "esptool", "--chip", "esp32s3", "elf2image", "--flash-mode", "dio", "-o", str(image), str(elf)])
            data = image.read_bytes()
            images[name] = {"image": letter, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

        manifest = {
            "chip": "esp32s3",
            "flash_address": "0x0",
            "images": images,
            "sources": sources,
            "toolchain": {"release": compiler[1], "gcc": compiler[2], "cflags": list(CFLAGS)},
            "esptool": esptool,
        }
        for name in IMAGES:
            shutil.copyfile(temporary / name, FIRMWARE / name)
        (FIRMWARE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
