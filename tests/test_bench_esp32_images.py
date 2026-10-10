"""Software-only checks on the two ESP32 images the bench's ESP32 stage flashes.

The ESP32 stage (``tests/bench/esp32_recordings.py``) puts ``banner_a.bin`` and
``banner_b.bin`` on an ESP32 through esptool and reads on the board's console
which of the two runs. The bench receives them as they are committed, because
the bench image carries no Xtensa toolchain, so nothing on the bench says
whether they are what their source says. This module does, on every host and
without the toolchain:

* the manifest beside them names each image's SHA-256 and size and the SHA-256
  of the sources they were built from, so a source edited without running
  ``tools/build_esp32_bench_images.py`` again fails here rather than on a board;
* each image is the format the ESP32 ROM loads from flash offset 0x1000: its
  magic, DIO, the ESP32's chip id, every segment inside a region of the linker
  script, the entry point in IRAM, and the ROM's checksum and the appended
  SHA-256 where the ROM and esptool look for them;
* the two differ in the image letter and in nothing else the ROM loads, so the
  letter on the console is the only thing that tells them apart.

The format is esptool's ``bin_image.py`` (``ESP32FirmwareImage``), read here
independently of esptool, which this test does not import.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import build_esp32_bench_images as build  # noqa: E402

from tests.bench.esp32_recordings import TICK_LINE  # noqa: E402

FIRMWARE = REPOSITORY_ROOT / "tests" / "bench" / "firmware" / "esp32"
MANIFEST = json.loads((FIRMWARE / "manifest.json").read_text(encoding="utf-8"))
IMAGE_NAMES = sorted(MANIFEST["images"])

# The common header, then the extended header every chip after the ESP8266 has:
# the WP pin, three bytes of SPI drive strengths, the chip id, the minimum chip
# revision twice over, the maximum, four reserved bytes and whether a SHA-256 of
# the image is appended. Then each segment's load address and length.
HEADER = struct.Struct("<BBBBI")
EXTENDED_HEADER = struct.Struct("<BBBBHBHH4sB")
SEGMENT_HEADER = struct.Struct("<II")
IMAGE_MAGIC = 0xE9
FLASH_MODE_DIO = 2
ESP32_CHIP_ID = 0
# The seed of the ROM's XOR checksum over every segment's data.
CHECKSUM_SEED = 0xEF
DIGEST_SIZE = 32

# What the image prints, `%s` its letter and `%u` its tick. The bench stage reads
# these lines back with a pattern of its own, held against this string below.
FORMAT = b"agentic-hil esp32 image %s tick %u\n"

REGION = re.compile(r"^\s*(?P<name>\w+)\s*\([A-Z]+\)\s*:\s*org\s*=\s*(?P<origin>0x[0-9A-Fa-f]+),\s*len\s*=\s*(?P<length>0x[0-9A-Fa-f]+)", re.MULTILINE)


@dataclass(frozen=True)
class Region:
    start: int
    end: int

    def holds(self, address: int, length: int = 1) -> bool:
        return self.start <= address and address + length <= self.end


@dataclass(frozen=True)
class Image:
    data: bytes
    magic: int
    flash_mode: int
    entry: int
    chip_id: int
    hash_appended: int
    segments: tuple[tuple[int, bytes], ...]
    checksum_index: int


def linker_regions() -> dict[str, Region]:
    """The memory regions ``esp32_ram.ld`` places the image in, by name."""
    script = (FIRMWARE / "esp32_ram.ld").read_text(encoding="utf-8")
    regions = {match["name"]: Region(int(match["origin"], 16), int(match["origin"], 16) + int(match["length"], 16)) for match in REGION.finditer(script)}
    assert set(regions) == {"iram_seg", "dram_seg"}, regions
    return regions


def parsed(name: str) -> Image:
    data = (FIRMWARE / name).read_bytes()
    magic, count, flash_mode, _size_and_frequency, entry = HEADER.unpack_from(data, 0)
    extended = EXTENDED_HEADER.unpack_from(data, HEADER.size)
    offset = HEADER.size + EXTENDED_HEADER.size
    segments = []
    for _ in range(count):
        address, length = SEGMENT_HEADER.unpack_from(data, offset)
        offset += SEGMENT_HEADER.size
        segments.append((address, data[offset : offset + length]))
        offset += length
    # esptool pads to the last byte of a 16-byte block and puts the checksum there.
    return Image(
        data=data,
        magic=magic,
        flash_mode=flash_mode,
        entry=entry,
        chip_id=extended[4],
        hash_appended=extended[9],
        segments=tuple(segments),
        checksum_index=offset + 15 - offset % 16,
    )


@pytest.mark.parametrize("name", IMAGE_NAMES)
def test_each_image_is_the_one_its_manifest_names(name: str) -> None:
    data = (FIRMWARE / name).read_bytes()

    assert hashlib.sha256(data).hexdigest() == MANIFEST["images"][name]["sha256"], name
    assert len(data) == MANIFEST["images"][name]["size"], name


def test_the_images_were_built_from_the_committed_sources() -> None:
    """A source that moved since the build means the committed images are not its."""
    stale = [name for name, digest in sorted(MANIFEST["sources"].items()) if hashlib.sha256((FIRMWARE / name).read_bytes()).hexdigest() != digest]

    assert not stale, f"{', '.join(stale)} changed since the images were built; run tools/build_esp32_bench_images.py and commit what it writes"


def test_the_manifest_is_the_build_scripts_own() -> None:
    """A flag, a release or an image changed in the script without a build since."""
    assert MANIFEST["chip"] == "esp32"
    assert MANIFEST["flash_address"] == build.FLASH_ADDRESS
    assert {name: entry["image"] for name, entry in MANIFEST["images"].items()} == build.IMAGES
    assert set(MANIFEST["sources"]) == set(build.SOURCES)
    assert MANIFEST["toolchain"]["release"] == build.TOOLCHAIN_RELEASE
    assert MANIFEST["toolchain"]["cflags"] == list(build.CFLAGS)


@pytest.mark.parametrize("name", IMAGE_NAMES)
def test_each_image_is_what_the_esp32_rom_loads(name: str) -> None:
    image = parsed(name)
    regions = linker_regions()

    assert image.magic == IMAGE_MAGIC
    assert image.flash_mode == FLASH_MODE_DIO
    assert image.chip_id == ESP32_CHIP_ID
    assert regions["iram_seg"].holds(image.entry), hex(image.entry)
    assert image.segments
    for address, payload in image.segments:
        assert any(region.holds(address, len(payload)) for region in regions.values()), (hex(address), len(payload))
    checksum = CHECKSUM_SEED
    for _address, payload in image.segments:
        for byte in payload:
            checksum ^= byte
    assert image.data[image.checksum_index] == checksum
    assert image.hash_appended == 1
    assert len(image.data) == image.checksum_index + 1 + DIGEST_SIZE
    assert image.data[image.checksum_index + 1 :] == hashlib.sha256(image.data[: image.checksum_index + 1]).digest()


@pytest.mark.parametrize("name", IMAGE_NAMES)
def test_each_image_prints_its_own_letter(name: str) -> None:
    image = parsed(name)
    dram = linker_regions()["dram_seg"]
    strings = b"".join(payload for address, payload in image.segments if dram.holds(address, len(payload))).split(b"\0")

    assert FORMAT in strings, strings
    assert MANIFEST["images"][name]["image"].encode("ascii") in strings, strings


def test_the_two_images_differ_in_their_letter_alone() -> None:
    """Everything the ROM loads, up to the checksum, is the same but one byte."""
    a, b = (parsed(name) for name in IMAGE_NAMES)

    assert len(a.data) == len(b.data)
    assert a.checksum_index == b.checksum_index
    differing = [index for index in range(a.checksum_index) if a.data[index] != b.data[index]]
    assert len(differing) == 1, differing
    assert {a.data[differing[0]], b.data[differing[0]]} == {ord(entry["image"]) for entry in MANIFEST["images"].values()}


@pytest.mark.parametrize("ending", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_the_bench_stage_reads_each_line_the_images_print(ending: bytes) -> None:
    """Both endings: the line the console hands over may carry a carriage return before its line feed."""
    printed = ((b"A", 0), (b"A", 1), (b"B", 4294967295))
    stream = b"".join((FORMAT % line).replace(b"\n", ending) for line in printed)

    assert [(match["image"], int(match["tick"])) for match in TICK_LINE.finditer(stream)] == [(letter, tick) for letter, tick in printed]
    assert TICK_LINE.search((FORMAT % (b"A", 12)).removesuffix(b"\n")) is None, "a line still arriving is not read as one"
