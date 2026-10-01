"""Take STM32_Programmer_CLI and ST-LINK_gdbserver out of the STM32CubeCLT installer without running it.

The bench image's optional `bench-tier-cubeclt` stage runs this on the licensed
STM32CubeCLT for Linux archive that tools/bench_in_container.py staged after
checking its digest. The archive is a zip around a makeself installer: a shell
header, then a plain tar that holds the installer scripts and one tar.gz of the
whole tree. Running that header would run `setup.sh`, which installs udev rules
and the stlink-server system service as root. Neither is wanted in an image that
only needs two programs, so nothing here executes a line of it: the header is
read for where the payload starts and how it is packed, and the two directories
the bench drives are extracted from the inner tree with the tarfile `data`
filter, which keeps the owner's executable bits and refuses links that leave the
destination.

    python extract_cubeclt.py ARCHIVE.zip DESTINATION
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

# The archive tools/bench_in_container.py stages for `--cubeclt-archive`, which
# a test holds equal to CUBECLT_ARCHIVE_SHA256 there.
EXPECTED_SHA256 = "8bebfb8811e28dcc26977c058a6109cdea4bcc930b2c4cf833d8309036b93b0d"
# What the bench drives out of the tree: STM32CubeProgrammer for the CLI, and
# STLink-gdb-server for the server it starts with that CLI's directory as `-cp`.
# The rest (the cross compiler, the debugger, the SVD files) is left in the
# archive; the image carries its own toolchain.
PARTS = ("STM32CubeProgrammer", "STLink-gdb-server")
# The 1.22.0 header says where the payload starts as the length of its own first
# lines, and that it is unpacked through `cat`, which is a plain tar.
HEADER_LENGTH = re.compile(r'offset=`head -n (\d+) "\$0"')
PAYLOAD_SIZES = re.compile(r'^filesizes="([^"]*)"')
UNPACKED_THROUGH = re.compile(r'MS_dd\w*\s+"\$0"\s+\$offset\s+\$s\s*\|\s*eval\s+"([^"]*)"')
# The header of 1.22.0 is 524 lines; a file that names no length in its first
# few thousand is not a makeself installer.
HEADER_SEARCH_LINES = 4096
INNER_TREE = re.compile(r"(?:\./)?stm32cubeclt_[^/]*-Lin\.tar\.gz")


class ExtractionRefused(Exception):
    """The archive is not the one this extraction was written against."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def the_installer(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    installers = [member for member in archive.infolist() if member.filename.endswith(".sh") and "/" not in member.filename]
    if len(installers) != 1:
        names = ", ".join(member.filename for member in archive.infolist()) or "nothing"
        raise ExtractionRefused(f"the archive holds {names}, not one makeself installer (.sh) at its top")
    return installers[0]


def read_header(stream: IO[bytes]) -> list[str]:
    """The header's lines, leaving the stream at the first byte of the payload."""
    lines: list[str] = []
    length: int | None = None
    while length is None or len(lines) < length:
        line = stream.readline()
        if not line:
            raise ExtractionRefused("the installer ends inside its header")
        lines.append(line.decode("utf-8", "replace"))
        if length is None:
            found = HEADER_LENGTH.search(lines[-1])
            if found:
                length = int(found.group(1))
                if length < len(lines):
                    raise ExtractionRefused(f"line {len(lines)} of the header says the header is only {length} lines long")
            elif len(lines) >= HEADER_SEARCH_LINES:
                raise ExtractionRefused(f"no `offset=` line in the first {HEADER_SEARCH_LINES} lines; this is not a makeself installer")
    sizes = [found.group(1) for found in map(PAYLOAD_SIZES.match, lines) if found]
    if len(sizes) != 1 or not sizes[0].isdigit():
        raise ExtractionRefused(f"the header names payload sizes {sizes!r}, not one payload")
    packers = {found.group(1) for line in lines for found in UNPACKED_THROUGH.finditer(line)}
    if packers != {"cat"}:
        raise ExtractionRefused(f"the payload is unpacked through {sorted(packers)!r}, not `cat`; only a plain tar payload is taken apart here")
    return lines


def part_of(name: str) -> str | None:
    relative = name[2:] if name.startswith("./") else name
    top = relative.split("/", 1)[0]
    return top if top in PARTS else None


def wanted(tree: tarfile.TarFile, found: set[str]) -> Iterator[tarfile.TarInfo]:
    for member in tree:
        part = part_of(member.name)
        if part is not None:
            found.add(part)
            yield member


def extract(archive: Path, destination: Path, expected_sha256: str = EXPECTED_SHA256) -> list[str]:
    """Extract PARTS into DESTINATION; nothing is left there unless all of it arrived."""
    actual = sha256_of(archive)
    if actual != expected_sha256:
        raise ExtractionRefused(f"{archive.name} has SHA-256 {actual}, expected {expected_sha256}")
    if destination.exists():
        raise ExtractionRefused(f"{destination} already exists")
    partial = destination.with_name(destination.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    found: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as zipped, zipped.open(the_installer(zipped)) as installer:
            read_header(installer)
            with tarfile.open(fileobj=installer, mode="r|") as payload:
                for entry in payload:
                    if not INNER_TREE.fullmatch(entry.name):
                        continue
                    inner = payload.extractfile(entry)
                    if inner is None:
                        raise ExtractionRefused(f"{entry.name} is not a file")
                    partial.mkdir(parents=True)
                    with tarfile.open(fileobj=inner, mode="r|gz") as tree:
                        tree.extractall(partial, members=wanted(tree, found), filter="data")
                    break
                else:
                    raise ExtractionRefused("the payload carries no stm32cubeclt_*-Lin.tar.gz tree")
        missing = [part for part in PARTS if part not in found]
        if missing:
            raise ExtractionRefused(f"the tree carries no {', '.join(missing)}")
        partial.rename(destination)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return sorted(found)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: extract_cubeclt.py ARCHIVE.zip DESTINATION", file=sys.stderr)
        return 2
    try:
        parts = extract(Path(argv[0]), Path(argv[1]))
    except (ExtractionRefused, OSError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"extract_cubeclt: {error}", file=sys.stderr)
        return 1
    print(f"extract_cubeclt: {', '.join(parts)} extracted into {argv[1]}; nothing of the installer ran")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
