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
* `HILVAL_ZEPHYR_WORKSPACE`: a Zephyr workspace without west: one directory per
  Zephyr tree a job builds against (a job names its tree), the modules
  `modules/hal/stm32`, `modules/hal/cmsis_6` and `modules/hal/cmsis` (and
  checkouts of other module revisions beside them, such as
  `modules/hal/stm32-c9afc5e`), and a Python environment `.venv` with Zephyr's
  build requirements. Never written to.
* `CARGO_HOME` (optional): where `cargo` and its registry cache are; unset,
  `~/.cargo`. Rust builds are offline, so every crate they need must already be
  in that cache or in a source checkout.

GNU Make (STM32CubeCLT's, then PATH), CMake and Ninja come from the same
STM32CubeCLT installation by default. Make runs its recipes in a POSIX shell, so
`sh` must be on PATH (on Windows, Git for Windows' `usr/bin`).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# STM32CubeCLT's default install location on Windows; not a user directory.
DEFAULT_ARM_TOOLCHAIN = Path("C:/ST/STM32CubeCLT_1.22.0/GNU-tools-for-STM32/bin")
DEFAULT_CMAKE = Path("C:/ST/STM32CubeCLT_1.22.0/CMake/bin")
DEFAULT_NINJA = Path("C:/ST/STM32CubeCLT_1.22.0/Ninja/bin")
DEFAULT_MAKE = Path("C:/ST/STM32CubeCLT_1.22.0/Make/bin")

# Cortex-M4F of the STM32F446RE. Soft-float calls keep start-up code free of
# FPU enabling; a job that needs the FPU passes its own flags.
CORTEX_M4_FLAGS = ("-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=soft")

RUST_TARGET = "thumbv7em-none-eabihf"
ZEPHYR_BOARD = "nucleo_f446re"
ZEPHYR_MODULES = ("hal/stm32", "hal/cmsis_6", "hal/cmsis")
MICROPYTHON_BOARD = "NUCLEO_F446RE"

# STM32duino (Arduino_Core_STM32): the source cache checkouts a build reads, the
# variant directory below the core, and what the core's boards.txt and
# platform.txt give the Nucleo-64 entry NUCLEO_F446RE with the IDE's default
# menu choices (generic Serial, no USB, -Os, no debug symbols, newlib nano).
STM32DUINO_CORE = "stm32duino-53560ef"
STM32DUINO_API = "arduinocore-api-bce6000"
STM32DUINO_CMSIS = "cmsis6-v6.3.0"
STM32DUINO_VARIANT = "STM32F4xx/F446R(C-E)T"
_STM32DUINO_CPU = ("-mcpu=cortex-m4", "-mfpu=fpv4-sp-d16", "-mfloat-abi=hard", "-mthumb")
_STM32DUINO_DEFINES = (
    "VECT_TAB_OFFSET=0x0",
    "USE_HAL_DRIVER",
    "USE_FULL_LL_DRIVER",
    "EXTENDED_PIN_MODE",
    "NDEBUG",
    "STM32F4xx",
    "ARDUINO=10607",
    "ARDUINO_NUCLEO_64",
    "ARDUINO_NUCLEO_F446RE",
    "ARDUINO_ARCH_STM32",
    'BOARD_NAME="NUCLEO_F446RE"',
    'VARIANT_H="variant_NUCLEO_F446RE.h"',
    "STM32F446xx",
    "HAL_UART_MODULE_ENABLED",
)
_STM32DUINO_LINK = (
    "--specs=nano.specs",
    "-Wl,--defsym=LD_FLASH_OFFSET=0x0",
    "-Wl,--defsym=LD_MAX_SIZE=524288",
    "-Wl,--defsym=LD_MAX_DATA_SIZE=131072",
    "-Wl,--check-sections",
    "-Wl,--gc-sections",
    "-Wl,--entry=Reset_Handler",
    "-Wl,--unresolved-symbols=report-all",
    "-Wl,--warn-common",
    "-Wl,--no-warn-rwx-segments",
    "-Wl,--build-id=none",
)

_PATH_TOKEN = re.compile(r"@([A-Z][A-Z0-9_]*)@")
_JOBS = str(min(os.cpu_count() or 4, 8))


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


def make() -> Path:
    return _find("make", None, DEFAULT_MAKE)


def _cargo_home() -> Path:
    return _absolute_env("CARGO_HOME") or Path.home() / ".cargo"


def cargo() -> Path:
    return _find("cargo", _cargo_home() / "bin", _cargo_home() / "bin")


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


def _posix_prefix_maps(mapped: Sequence[tuple[Path, str]]) -> str:
    """Prefix-map flags in forward-slash spelling only, as one string for a build system's CFLAGS."""
    flags = []
    for directory, name in mapped:
        spelling = Path(directory).as_posix()
        flags += [f"-ffile-prefix-map={spelling}={name}", f"-fmacro-prefix-map={spelling}={name}"]
    return " ".join(flags)


def _with_path(*directories: Path) -> dict[str, str]:
    """An environment addition that puts `directories` in front of PATH."""
    return {"PATH": os.pathsep.join([*(str(directory) for directory in directories), os.environ.get("PATH", "")])}


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


def strip_debug(elf: Path, out: Path) -> Path:
    """`elf` without its debug information, written to `out`; the symbol table stays."""
    out = Path(out)
    run([arm_tool("objcopy"), "--strip-debug", Path(elf), out], cwd=out.parent)
    return out


def gcc_image(
    job: str,
    name: str,
    sources: Sequence[Path],
    *,
    linker_script: Path,
    defines: Sequence[str] = (),
    include_dirs: Sequence[Path] = (),
    cflags: Sequence[str] = (),
    system_include_dirs: Sequence[Path] = (),
    library_dirs: Sequence[Path] = (),
    libraries: Sequence[str] = (),
    cpu_flags: Sequence[str] = CORTEX_M4_FLAGS,
) -> Path:
    """Compile and link a bare-metal C image with no C library; the ELF's path.

    Sources, includes and the linker script are paths inside the repository and
    are passed relative to it, so the image records no absolute path. Debug
    information is stripped; the symbol table stays.

    A prebuilt library outside the repository, such as `libopencm3()`, comes in
    through `system_include_dirs` (its headers, `-isystem`, so its own warnings
    do not fail the build), `library_dirs` (searched for `-l` libraries and for
    linker scripts the image's script INCLUDEs) and `libraries` (`opencm3_stm32f4`).
    Those directories are remapped like the build directory. Libraries must be
    built with the same `cpu_flags`.
    """
    out = build_dir(job, name)
    gcc = arm_tool("gcc")

    def relative(path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
        except ValueError:
            raise ToolchainError(f"{path} is not inside the repository") from None

    external = [Path(directory) for directory in (*system_include_dirs, *library_dirs)]
    elf = out / f"{name}.elf"
    command: list[str | Path] = [
        gcc,
        *cpu_flags,
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
        *prefix_map_flags(out, *external),
        *(f"-D{define}" for define in defines),
        *(f"-I{relative(directory)}" for directory in include_dirs),
        *(part for directory in system_include_dirs for part in ("-isystem", Path(directory).as_posix())),
        *cflags,
        *(relative(source) for source in sources),
        "-nostdlib",
        "-nostartfiles",
        *(f"-L{Path(directory).as_posix()}" for directory in library_dirs),
        f"-T{relative(linker_script)}",
        "-Wl,--gc-sections",
        "-Wl,--build-id=none",
        "-Wl,--strip-debug",
        "-o",
        elf,
        *(f"-l{library}" for library in libraries),
        "-lgcc",
    ]
    run(command, cwd=REPO_ROOT)
    return elf


def patched_copy(
    job: str,
    source: Path,
    patches: Sequence[Path] = (),
    *,
    name: str | None = None,
    include: Sequence[str] | None = None,
) -> Path:
    """A copy of the upstream checkout `source` with `patches` applied; the copy's root.

    The copy lies below the job's build directory (`_src/<name>`, by default the
    checkout's directory name) and leaves out `.git`. `include` limits it to
    those paths relative to the checkout (directories or files), for checkouts
    too large to copy whole. Each patch is a unified diff with `a/` and `b/`
    prefixes, as `git diff` writes it, applied in order with `git apply`; a patch
    that does not apply fails the build. The checkout itself is never written to.
    """
    origin = Path(source)
    copy = build_dir(job, "_src", name or origin.name)
    ignore = shutil.ignore_patterns(".git")
    for part in include if include is not None else [""]:
        item = origin / part if part else origin
        target = copy / part if part else copy
        if item.is_dir():
            shutil.copytree(item, target, ignore=ignore, dirs_exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        else:
            raise ToolchainError(f"{origin.name} has no {part!r} to copy")
    git = shutil.which("git")
    if patches and git is None:
        raise ToolchainError("git was not found on PATH; it applies the patches")
    for index, patch in enumerate(patches):
        # The checkouts have LF line endings; a patch saved with CRLF would not apply.
        normalized = copy.parent / f"{copy.name}.{index}.patch"
        normalized.write_bytes(Path(patch).read_bytes().replace(b"\r\n", b"\n"))
        run(
            [git, "apply", "--whitespace=nowarn", normalized],
            cwd=copy,
            # Keeps git from treating a repository above the copy as the one to patch.
            environment={"GIT_CEILING_DIRECTORIES": str(copy.parent)},
        )
    return copy


def zephyr_workspace() -> Path:
    """`HILVAL_ZEPHYR_WORKSPACE`, the Zephyr trees and modules builds read from."""
    path = _absolute_env("HILVAL_ZEPHYR_WORKSPACE")
    if path is None:
        raise ToolchainError("HILVAL_ZEPHYR_WORKSPACE is not set; it names the Zephyr workspace to build against")
    if not path.is_dir():
        raise ToolchainError("HILVAL_ZEPHYR_WORKSPACE is not a directory")
    return path


def zephyr_image(
    job: str,
    name: str,
    app: Path,
    *,
    zephyr: str,
    cmake_args: Sequence[str] = (),
    modules: Sequence[str] = ZEPHYR_MODULES,
) -> Path:
    """Build the Zephyr application `app` for nucleo_f446re against the tree `zephyr`; the stripped ELF's path.

    `zephyr` names a Zephyr tree in `HILVAL_ZEPHYR_WORKSPACE` (for example
    `z121031-head`); `cmake_args` go to the CMake configure call
    (`-DEXTRA_CONF_FILE=...`, `-DDTC_OVERLAY_FILE=...`, `-DCONFIG_FOO=y`). The
    toolchain is the GNU Arm Embedded one (`gnuarmemb`). The CMake build, its
    cache and Zephyr's user cache lie below the job's build directory; the
    workspace is only read.

    `modules` are the module checkouts below the workspace's `modules/`. A tree
    whose `west.yml` pins another revision of a module names that checkout
    instead, for example `("hal/stm32-c9afc5e", "hal/cmsis_6", "hal/cmsis")`.
    """
    workspace = zephyr_workspace()
    base = workspace / zephyr
    if not (base / "VERSION").is_file() or not (base / "CMakeLists.txt").is_file():
        raise ToolchainError(f"the Zephyr workspace has no tree {zephyr!r}")
    for module in modules:
        if Path(module).is_absolute() or ".." in Path(module).parts:
            raise ToolchainError(f"module {module!r} must be a path below the workspace's modules/")
    module_dirs = [workspace / "modules" / module for module in modules]
    for module in module_dirs:
        if not module.is_dir():
            raise ToolchainError(f"the Zephyr workspace has no module {module.relative_to(workspace).as_posix()}")
    python = workspace / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
    if not python.is_file():
        raise ToolchainError("the Zephyr workspace has no Python environment .venv")
    out = build_dir(job, name)
    build = out / "build"
    gcc = arm_tool("gcc")
    maps = _posix_prefix_maps([(REPO_ROOT, "."), (workspace, "/zephyr"), (out, "/build/0")])
    environment = {"ZEPHYR_BASE": str(base), **_with_path(cmake().parent, ninja().parent, gcc.parent)}
    run(
        [
            cmake(),
            "-S",
            Path(app).resolve().as_posix(),
            "-B",
            build.as_posix(),
            "-GNinja",
            f"-DBOARD={ZEPHYR_BOARD}",
            f"-DZEPHYR_MODULES={';'.join(module.as_posix() for module in module_dirs)}",
            "-DZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb",
            f"-DGNUARMEMB_TOOLCHAIN_PATH={gcc.parent.parent.as_posix()}",
            f"-DPython3_EXECUTABLE={python.as_posix()}",
            f"-DUSER_CACHE_DIR={(out / 'cache').as_posix()}",
            f"-DEXTRA_CFLAGS={maps}",
            *cmake_args,
        ],
        cwd=out,
        environment=environment,
    )
    run([cmake(), "--build", build.as_posix()], cwd=out, environment=environment, timeout_s=1800)
    return strip_debug(build / "zephyr" / "zephyr.elf", out / f"{name}.elf")


def cargo_image(
    job: str,
    name: str,
    crate: Path,
    *,
    binary: str,
    paths: Mapping[str, Path],
) -> Path:
    """Build the binary `binary` of the Rust crate `crate` for the F446RE; the stripped ELF's path.

    The crate directory (in the repository) is copied below the job's build
    directory first, so its lock file and target directory never land in the
    repository. In the copied `Cargo.toml` every `@NAME@` is replaced by the
    forward-slash spelling of `paths[NAME]`: that is how a crate names the
    upstream checkouts it depends on, for example
    `embassy-stm32 = { path = "@EMBASSY@/embassy-stm32", ... }` with
    `paths={"EMBASSY": toolchain.source_cache("embassy")}` or a `patched_copy`.

    The build is `cargo build --release --offline` for `thumbv7em-none-eabihf`.
    Linker arguments belong in the crate's `build.rs`
    (`cargo:rustc-link-arg-bins=-Tlink.x`): the helper sets the rustflags, so
    flags from a `.cargo/config.toml` do not apply. Everything rustc records
    from the crate copy, the named checkouts, Cargo's registry and the target
    directory (sources that build scripts generate) is remapped.
    """
    out = build_dir(job, name)
    copy = out / "crate"
    shutil.copytree(crate, copy, ignore=shutil.ignore_patterns("target", ".git"))
    manifest = copy / "Cargo.toml"
    if not manifest.is_file():
        raise ToolchainError(f"{crate} has no Cargo.toml")
    text = manifest.read_text(encoding="utf-8")
    for key in paths:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ToolchainError(f"path name {key!r} must be uppercase letters, digits and '_'")
    unknown = sorted({token for token in _PATH_TOKEN.findall(text) if token not in paths})
    if unknown:
        raise ToolchainError(f"Cargo.toml uses {', '.join('@' + token + '@' for token in unknown)} but no path was given")
    manifest.write_text(
        _PATH_TOKEN.sub(lambda match: Path(paths[match.group(1)]).as_posix(), text),
        encoding="utf-8",
    )
    home = _cargo_home()
    remaps = [
        (home / "registry" / "src", "/cargo/registry"),
        (home / "git" / "checkouts", "/cargo/git"),
        *((Path(path), f"/src/{key.lower()}") for key, path in paths.items()),
        (copy, "/crate"),
        # Build scripts write generated sources (embassy-stm32's `_generated.rs`) below the target directory.
        (out / "target", "/target"),
    ]
    flags = []
    for directory, mapped in remaps:
        for spelling in dict.fromkeys([str(directory), directory.as_posix()]):
            flags.append(f"--remap-path-prefix={spelling}={mapped}")
    run(
        [cargo(), "build", "--release", "--offline", "--target", RUST_TARGET, "--bin", binary],
        cwd=copy,
        environment={
            "CARGO_TARGET_DIR": str(out / "target"),
            "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(flags),
            "CARGO_NET_OFFLINE": "true",
            "CARGO_TERM_COLOR": "never",
        },
        timeout_s=1800,
    )
    return strip_debug(out / "target" / RUST_TARGET / "release" / binary, out / f"{name}.elf")


def micropython_image(
    job: str,
    name: str,
    frozen: Path,
    *,
    source: Path | None = None,
    board: str = MICROPYTHON_BOARD,
    make_args: Sequence[str] = (),
) -> Path:
    """MicroPython's stm32 port for `board` with the `.py` files in `frozen` frozen in; the stripped ELF's path.

    The files of the directory `frozen` are frozen as source text, so no
    mpy-cross is built or needed, and nothing else is frozen. A frozen
    `main.py` runs at boot; its `print` output goes to the REPL on USART2, the
    bench's port. `source` is a MicroPython checkout (default:
    `HILVAL_SRC_CACHE/micropython`, or a `patched_copy`); the build only reads
    it. `make_args` go to the port's make call.
    """
    origin = Path(source) if source is not None else source_cache("micropython")
    out = build_dir(job, name)
    manifest = out / "manifest.py"
    manifest.write_text(f"freeze_as_str({Path(frozen).resolve().as_posix()!r})\n", encoding="utf-8")
    # makemanifest.py insists that mpy-cross exists even when nothing is compiled with it.
    placeholder = out / "no-mpy-cross"
    placeholder.write_bytes(b"")
    gcc = arm_tool("gcc")
    maps = _posix_prefix_maps([(REPO_ROOT, "."), (origin, "/micropython"), (out, "/build/0")])
    run(
        [
            make(),
            "-C",
            (origin / "ports" / "stm32").as_posix(),
            f"-j{_JOBS}",
            f"BOARD={board}",
            f"BUILD={(out / 'build').as_posix()}",
            f"FROZEN_MANIFEST={manifest.as_posix()}",
            "MICROPY_MPYCROSS_DEPENDENCY=",
            f"PYTHON={Path(sys.executable).as_posix()}",
            f"CROSS_COMPILE={(gcc.parent / 'arm-none-eabi-').as_posix()}",
            f"CFLAGS_EXTRA=-DMICROPY_MODULE_FROZEN_STR=1 {maps}",
            *make_args,
        ],
        cwd=out,
        environment={"MICROPY_MPYCROSS": str(placeholder), **_with_path(make().parent, gcc.parent)},
        timeout_s=1800,
    )
    return strip_debug(out / "build" / "firmware.elf", out / f"{name}.elf")


def stm32duino_image(
    job: str,
    name: str,
    sketch: Path,
    *,
    source: Path | None = None,
    libraries: Sequence[str] = (),
    defines: Sequence[str] = (),
) -> Path:
    """The STM32duino sketch in `sketch` built for the NUCLEO_F446RE; the stripped ELF's path.

    The build does what the Arduino IDE does with the core's `boards.txt` and
    `platform.txt` for the Nucleo-64 entry NUCLEO_F446RE and the default menu
    choices: generic `Serial` on USART2 (PA2/PA3), no USB, `-Os`, no debug
    symbols, newlib nano, the variant's clock setup. The core and ArduinoCore-API
    go into an archive; the variant, the libraries and the sketch are linked as
    objects, with the variant's linker script and the core's `.noinit` script.

    `sketch` is a directory in the repository; its `.c`, `.cpp` and `.S` files,
    also in subdirectories, are the sketch, compiled as written. An `.ino` file
    is refused because the IDE's prototype generation is not reproduced: write
    the sketch as `.cpp` that includes `<Arduino.h>`. The directory is on the
    include path, so a `hal_conf_extra.h` there works as in the IDE.
    `libraries` names libraries of the core besides SrcWrapper, which is always
    built (`("IWatchdog",)`). `defines` (`"SERIAL_TX_BUFFER_SIZE=256"`) go to
    every compile, where the IDE would read a `build_opt.h`.

    `source` is the core: by default `HILVAL_SRC_CACHE/stm32duino-53560ef`, or a
    `patched_copy` of it; the build only reads it. Its `cores/arduino/api` is a
    git submodule that is empty there, so the build copies ArduinoCore-API at
    the submodule's commit (`arduinocore-api-bce6000`) into its build directory.
    The Cortex-M headers come from `cmsis6-v6.3.0`, the CMSIS_6 release the
    platform names. The compiler is the configured Arm toolchain, not the IDE's
    xPack GCC.
    """
    origin = Path(source) if source is not None else source_cache(STM32DUINO_CORE)
    cmsis_root = source_cache(STM32DUINO_CMSIS)
    sketch = Path(sketch).resolve()
    if not sketch.is_relative_to(REPO_ROOT.resolve()) or not sketch.is_dir():
        raise ToolchainError(f"the sketch {sketch.name!r} must be a directory inside the repository")
    if any(sketch.rglob("*.ino")):
        raise ToolchainError("the sketch has an .ino file; write it as .cpp that includes <Arduino.h>")
    for library in libraries:
        if not re.fullmatch(r"[A-Za-z0-9_]+", library) or library == "SrcWrapper":
            raise ToolchainError(f"library {library!r} must be the name of one of the core's libraries besides SrcWrapper")
    core = origin / "cores" / "arduino"
    variant = origin / "variants" / STM32DUINO_VARIANT
    system = origin / "system"
    hal = system / "Drivers" / "STM32F4xx_HAL_Driver"
    device = system / "Drivers" / "CMSIS" / "Device" / "ST" / "STM32F4xx"
    wrapper = origin / "libraries" / "SrcWrapper"
    library_dirs = {library: origin / "libraries" / library / "src" for library in ("SrcWrapper", *libraries)}
    cmsis = cmsis_root / "CMSIS" / "Core" / "Include"
    for directory in (core, variant, system / "STM32F4xx", hal, device, wrapper / "inc", cmsis, *library_dirs.values()):
        if not directory.is_dir():
            raise ToolchainError(f"the STM32duino build expects {directory.name!r} and the checkout has none")

    out = build_dir(job, name)
    api_root = out / "api-root"
    api = api_root / "api"
    shutil.copytree(source_cache(STM32DUINO_API), api, ignore=shutil.ignore_patterns(".git", ".github"))

    # In the order platform.txt puts them: the core's extra includes, CMSIS, then
    # the core, the variant and the libraries the IDE adds. `api-root` makes the
    # core's `#include "api/ArduinoAPI.h"` find the copy.
    include_dirs = [
        sketch,
        core / "avr",
        core / "stm32",
        api / "deprecated",
        api / "deprecated-avr-comp",
        wrapper / "inc",
        wrapper / "inc" / "LL",
        hal / "Inc",
        hal / "Src",
        system / "STM32F4xx",
        cmsis,
        device / "Include",
        device / "Source",
        device / "Source" / "Templates" / "gcc",
        core,
        api_root,
        variant,
        *library_dirs.values(),
    ]
    common = [
        *_STM32DUINO_CPU,
        *prefix_map_flags(out, origin, cmsis_root),
        *(f"-D{define}" for define in (*_STM32DUINO_DEFINES, *defines)),
        *(f"-I{directory.as_posix()}" for directory in include_dirs),
    ]
    c_flags = ["-Os", "-w", "-std=gnu17", "-ffunction-sections", "-fdata-sections", "--param", "max-inline-insns-single=500"]
    cpp_flags = [
        "-Os",
        "-w",
        "-std=gnu++17",
        "-ffunction-sections",
        "-fdata-sections",
        "-fno-threadsafe-statics",
        "--param",
        "max-inline-insns-single=500",
        "-fno-rtti",
        "-fno-exceptions",
        "-fno-use-cxa-atexit",
    ]
    gcc, gxx = arm_tool("gcc"), arm_tool("g++")

    def sources(root: Path) -> list[Path]:
        return sorted(
            path
            for path in root.rglob("*")
            if path.suffix in (".c", ".cpp", ".S")
            and path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        )

    groups = {
        "core": [(core, path) for path in sources(core)] + [(api, path) for path in sources(api)],
        "variant": [(variant, path) for path in sources(variant)],
        **{f"lib-{library}": [(directory, path) for path in sources(directory)] for library, directory in library_dirs.items()},
        "sketch": [(sketch, path) for path in sources(sketch)],
    }
    if not groups["sketch"]:
        raise ToolchainError("the sketch has no .c, .cpp or .S file")
    compiles = []
    objects: dict[str, list[Path]] = {}
    for group, files in groups.items():
        for root, path in files:
            relative = "/".join(re.sub(r"[^A-Za-z0-9_.-]", "_", part) for part in path.relative_to(root).parts)
            target = Path("obj", group, f"{relative}.o")
            (out / target).parent.mkdir(parents=True, exist_ok=True)
            flags = cpp_flags if path.suffix == ".cpp" else ["-x", "assembler-with-cpp"] if path.suffix == ".S" else c_flags
            compiles.append([gxx if path.suffix == ".cpp" else gcc, *common, *flags, "-c", path.as_posix(), "-o", target.as_posix()])
            objects.setdefault(group, []).append(target)
    with ThreadPoolExecutor(max_workers=int(_JOBS)) as pool:
        for _ in pool.map(lambda command: run(command, cwd=out), compiles):
            pass

    run([arm_tool("ar"), "rcsD", "core.a", *(target.as_posix() for target in objects["core"])], cwd=out)
    linked = [target for group, targets in objects.items() if group != "core" for target in targets]
    # One argument per line: the object list would otherwise approach Windows' command-line limit.
    (out / "objects.rsp").write_text("\n".join(target.as_posix() for target in linked) + "\n", encoding="utf-8")
    run(
        [
            gcc,
            *_STM32DUINO_CPU,
            "-Os",
            *_STM32DUINO_LINK,
            f"-Wl,--default-script={(variant / 'ldscript.ld').as_posix()}",
            f"-Wl,--script={(system / 'ldscript.ld').as_posix()}",
            "-o",
            "obj/image.elf",
            "-L.",
            "-Wl,--start-group",
            "@objects.rsp",
            "core.a",
            "-lc",
            "-Wl,--end-group",
            "-lm",
            "-lgcc",
            "-lstdc++",
        ],
        cwd=out,
    )
    return strip_debug(out / "obj" / "image.elf", out / f"{name}.elf")


def libopencm3(job: str, *, targets: Sequence[str] = ("stm32/f4",), source: Path | None = None) -> Path:
    """libopencm3 built for `targets` in a copy below the job's build directory; the copy's root.

    The root holds `include/` (for `system_include_dirs`) and `lib/` (for
    `library_dirs`: `libopencm3_stm32f4.a` and the linker scripts such as
    `cortex-m-generic.ld`). The library is built soft-float, like `gcc_image`'s
    default `cpu_flags`. `source` defaults to `HILVAL_SRC_CACHE/libopencm3`; a
    `patched_copy` works too.
    """
    origin = Path(source) if source is not None else source_cache("libopencm3")
    root = build_dir(job, "_libopencm3")
    shutil.copytree(origin, root, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
    # The generator's shebang names python3, which a Windows host may not have; make
    # skips its own call once the generated files are newer than their inputs.
    for target in targets:
        if (root / "include" / "libopencm3" / target / "irq.json").is_file():
            run([sys.executable, "-I", "./scripts/irq2nvic_h", f"./include/libopencm3/{target}/irq.json"], cwd=root)
    gcc = arm_tool("gcc")
    run(
        [make(), f"-j{_JOBS}", f"TARGETS={' '.join(targets)}", "FP_FLAGS=-mfloat-abi=soft"],
        cwd=root,
        environment=_with_path(make().parent, gcc.parent),
        timeout_s=1800,
    )
    return root


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
