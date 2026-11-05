"""Install the reviewed F446 pack without consulting the CMSIS pack index."""

from __future__ import annotations

import hashlib
import http.client
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

PACK_VERSION = "3.1.1"
PACK_URL = f"https://www.keil.com/pack/Keil.STM32F4xx_DFP.{PACK_VERSION}.pack"
PACK_SIZE = 2085496
PACK_SHA256 = "345231106fe697df24bbe9133aeeec1b0383d7891c4adf4f47bbb8b478e9f2f0"
DOWNLOAD_TIMEOUT_S = 60
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_S = 10
# A connection dropped in the middle of the body can end the read without an
# error, so a file that fails the check is a failed attempt like a refused
# connection is, and it costs a retry rather than the image.
DOWNLOAD_FAILURES = (OSError, http.client.HTTPException, ValueError)


def verify(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != PACK_SHA256 or path.stat().st_size != PACK_SIZE:
        raise ValueError("The CMSIS pack does not match the reviewed SHA-256 and size.")


def download(destination: Path) -> None:
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(PACK_URL, timeout=DOWNLOAD_TIMEOUT_S) as response, destination.open("wb") as output:
                shutil.copyfileobj(response, output)
            verify(destination)
            return
        except DOWNLOAD_FAILURES:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(DOWNLOAD_RETRY_S)


def install() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "download.pack"
        download(source)
        with zipfile.ZipFile(source) as archive:
            (description,) = [name for name in archive.namelist() if name.endswith(".pdsc")]
            pdsc = Path(temporary) / Path(description).name
            pdsc.write_bytes(archive.read(description))

        # The cache indexes a local PDSC without fetching the vendor index. A
        # pack filename alone adds no devices, so its description goes first.
        import cmsis_pack_manager

        cache = cmsis_pack_manager.Cache(True, True)
        # An image's home has no store yet, and the cache does not make one: it
        # writes its index into the directory it finds and adds nothing without.
        Path(cache.data_path).mkdir(parents=True, exist_ok=True)
        cache.add_pack_from_path(str(pdsc))
        names = {pack.get_pack_name() for pack in cache.packs_for_devices(cache.index.values())}
        (name,) = names
        destination = Path(cache.data_path) / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def main() -> int:
    try:
        install()
    except (*DOWNLOAD_FAILURES, zipfile.BadZipFile) as error:
        print(f"CMSIS pack installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
