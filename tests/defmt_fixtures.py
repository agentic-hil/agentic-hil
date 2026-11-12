"""Build the defmt firmware images used by the acceptance tests."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

TARGET = "thumbv7em-none-eabihf"
FIRMWARE = Path(__file__).parent / "fixtures" / "defmt" / "firmware"
VARIANTS = {"rzcobs": "ts", "raw": "raw"}


def missing_toolchain_prerequisite() -> str | None:
    """Return a named missing prerequisite, without starting a firmware build."""
    cargo = shutil.which("cargo")
    if cargo is None:
        return "cargo not found on PATH"
    rustc = shutil.which("rustc")
    if rustc is None:
        return "rustc not found on PATH"
    result = subprocess.run(
        [rustc, "--print", "target-libdir", "--target", TARGET],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not Path(result.stdout.strip()).is_dir():
        return f"Rust target {TARGET} is not installed"
    return None


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold one byte of a lock file across processes on Windows and POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        descriptor = lock_file.fileno()
        if os.name == "nt":
            import msvcrt

            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


def build_images(target_dir: Path) -> dict[str, Path]:
    """Build both locked variants below target_dir and return their image paths."""
    cargo = shutil.which("cargo")
    if cargo is None:
        raise FileNotFoundError("cargo not found on PATH")
    target_dir.mkdir(parents=True, exist_ok=True)
    image_dir = target_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    images: dict[str, Path] = {}
    for name, feature in VARIANTS.items():
        cargo_target = target_dir / "firmware" / name
        environment = {
            **os.environ,
            "CARGO_TARGET_DIR": str(cargo_target),
            "DEFMT_LOG": "trace",
        }
        subprocess.run(
            [cargo, "build", "--release", "--locked", "--features", feature],
            cwd=FIRMWARE,
            env=environment,
            check=True,
        )
        built = cargo_target / TARGET / "release" / "defmt-fixture"
        image = image_dir / f"defmt-fixture-{name}.elf"
        shutil.copyfile(built, image)
        images[name] = image
    return images
