"""Where the build tools are, where builds go, and how one command runs.

Every image a job builds is built by its `hilval/<job>/build.py` with these
helpers. Two rules hold for all of them:

* Nothing a build writes lands in the repository. `build_dir(job)` is below
  `HILVAL_STATE_DIR`, outside the worktree, so a build never shows up as a
  change and never ends up in a pull request.
* What the bench gate sends is published. An image must not name this machine,
  so builds remap the repository, the build directory and the source cache to
  neutral names (`prefix_map_flags`) and strip debug information that would
  carry anything else. The gate refuses an image that still names a user
  directory or the host.

The paths of this machine come from environment variables, never from this
file, because this file is published with the images:

* `HILVAL_STATE_DIR`: the gate's state and every build output.
* `HILVAL_ARM_TOOLCHAIN`: the `bin` directory of a GNU Arm Embedded toolchain
  (`arm-none-eabi-gcc` and friends). Unset, the STM32CubeCLT 1.22.0 default
  location is tried, then PATH.
* `HILVAL_SRC_CACHE`: read-only upstream checkouts (one directory each) that a
  build copies from. Never written to.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# STM32CubeCLT's default install location on Windows; not a user directory.
DEFAULT_ARM_TOOLCHAIN = Path("C:/ST/STM32CubeCLT_1.22.0/GNU-tools-for-STM32/bin")
DEFAULT_CMAKE = Path("C:/ST/STM32CubeCLT_1.22.0/CMake/bin")
DEFAULT_NINJA = Path("C:/ST/STM32CubeCLT_1.22.0/Ninja/bin")

# Cortex-M4F of the STM32F446RE. Soft-float calls keep start-up code free of
# FPU enabling; a job that needs the FPU passes its own flags.
CORTEX_M4_FLAGS = ("-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=soft")


class ToolchainError(RuntimeError):
    """A build tool is missing or a build command failed; the message says which."""


def _absolute_env(variable: str) -> Path | None:
    value = os.environ.get(variable)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ToolchainError(f"{variable} must be an absolute path, not {value!r}")
    return path


def state_dir() -> Path:
    """`HILVAL_STATE_DIR`, which must exist and lie outside the repository."""
    path = _absolute_env("HILVAL_STATE_DIR")
    if path is None:
        raise ToolchainError("HILVAL_STATE_DIR is not set; builds go there, outside the worktree")
    if path.resolve().is_relative_to(REPO_ROOT.resolve()):
        raise ToolchainError("HILVAL_STATE_DIR must lie outside the repository")
    return path


def build_dir(job: str, *parts: str) -> Path:
    """A fresh, empty build directory for `job` (and `parts`) below `HILVAL_STATE_DIR/build`."""
    path = state_dir().joinpath("build", job, *parts)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def source_cache(name: str) -> Path:
    """The read-only upstream checkout `HILVAL_SRC_CACHE/<name>`."""
    root = _absolute_env("HILVAL_SRC_CACHE")
    if root is None:
        raise ToolchainError("HILVAL_SRC_CACHE is not set; it names the directory of upstream checkouts")
    path = root / name
    if not path.is_dir():
        raise ToolchainError(f"the source cache has no {name!r} checkout")
    return path


def _find(program: str, configured: Path | None, default: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    for directory in (configured, default):
        if directory is not None and (directory / f"{program}{suffix}").is_file():
            return directory / f"{program}{suffix}"
    found = shutil.which(program)
    if found:
        return Path(found)
    raise ToolchainError(f"{program} was not found (set HILVAL_ARM_TOOLCHAIN or put it on PATH)")


def arm_tool(name: str) -> Path:
    """`arm-none-eabi-<name>` (gcc, objcopy, size, ...) from the configured toolchain."""
    return _find(f"arm-none-eabi-{name}", _absolute_env("HILVAL_ARM_TOOLCHAIN"), DEFAULT_ARM_TOOLCHAIN)


def cmake() -> Path:
    return _find("cmake", None, DEFAULT_CMAKE)


def ninja() -> Path:
    return _find("ninja", None, DEFAULT_NINJA)


def prefix_map_flags(*directories: Path) -> list[str]:
    """GCC flags that rename the repository and `directories` in everything the compiler records.

    The repository becomes `.`; each further directory becomes `/build/<n>`.
    """
    flags = []
    mapped = [(REPO_ROOT, ".")] + [(directory, f"/build/{index}") for index, directory in enumerate(directories)]
    for directory, name in mapped:
        for spelling in {str(directory), directory.as_posix()}:
            flags += [f"-ffile-prefix-map={spelling}={name}", f"-fmacro-prefix-map={spelling}={name}"]
    return flags


def run(
    command: Sequence[str | Path],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout_s: float = 900,
) -> str:
    """Run one build command; its combined output, or a ToolchainError that quotes the end of it."""
    arguments = [str(part) for part in command]
    try:
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            env={**os.environ, **(environment or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ToolchainError(f"{Path(arguments[0]).name} could not run: {error}") from None
    if completed.returncode != 0:
        raise ToolchainError(
            f"{Path(arguments[0]).name} exited with {completed.returncode}:\n{completed.stdout[-6000:]}"
        )
    return completed.stdout


def gcc_image(
    job: str,
    name: str,
    sources: Sequence[Path],
    *,
    linker_script: Path,
    defines: Sequence[str] = (),
    include_dirs: Sequence[Path] = (),
    cflags: Sequence[str] = (),
) -> Path:
    """Compile and link a bare-metal C image with no C library; the ELF's path.

    Sources, includes and the linker script are paths inside the repository and
    are passed relative to it, so the image records no absolute path. Debug
    information is stripped; the symbol table stays.
    """
    out = build_dir(job, name)
    gcc = arm_tool("gcc")

    def relative(path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
        except ValueError:
            raise ToolchainError(f"{path} is not inside the repository") from None

    elf = out / f"{name}.elf"
    command: list[str | Path] = [
        gcc,
        *CORTEX_M4_FLAGS,
        "-std=c11",
        "-O2",
        "-g0",
        "-ffreestanding",
        "-fno-common",
        "-ffunction-sections",
        "-fdata-sections",
        "-Wall",
        "-Wextra",
        "-Werror",
        *prefix_map_flags(out),
        *(f"-D{define}" for define in defines),
        *(f"-I{relative(directory)}" for directory in include_dirs),
        *cflags,
        *(relative(source) for source in sources),
        "-nostdlib",
        "-nostartfiles",
        f"-T{relative(linker_script)}",
        "-Wl,--gc-sections",
        "-Wl,--build-id=none",
        "-Wl,--strip-debug",
        "-o",
        elf,
        "-lgcc",
    ]
    run(command, cwd=REPO_ROOT)
    return elf


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
