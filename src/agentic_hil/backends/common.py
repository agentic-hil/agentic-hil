from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agentic_hil.process import (
    CHILD_REAP_TIMEOUT_S,
    process_group_kwargs,
    spawn_managed_process,
    terminate_process_tree,
)


@dataclass(frozen=True)
class CompletedCommand:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    not_found: bool


def spawn_command(command: list[str], cwd: str, timeout_seconds: float) -> CompletedCommand:
    try:
        child = spawn_managed_process(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **process_group_kwargs(),
        )
    except FileNotFoundError:
        return CompletedCommand(stdout="", stderr="", returncode=None, timed_out=False, not_found=True)
    try:
        stdout, stderr = child.communicate(timeout=max(0.0, timeout_seconds))
        terminate_process_tree(child, CHILD_REAP_TIMEOUT_S)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            terminate_process_tree(child, CHILD_REAP_TIMEOUT_S)
            stdout, stderr = child.communicate(timeout=CHILD_REAP_TIMEOUT_S)
            timed_out = True
        except BaseException as cleanup_error:
            raise RuntimeError(f"Timed-out command cleanup remains unconfirmed: {cleanup_error}") from cleanup_error
    except BaseException as primary_error:
        try:
            terminate_process_tree(child, CHILD_REAP_TIMEOUT_S)
        except BaseException as cleanup_error:
            raise RuntimeError(f"Command failed and cleanup remains unconfirmed: {cleanup_error}") from primary_error
        raise
    return CompletedCommand(
        stdout=decode_output(stdout),
        stderr=decode_output(stderr),
        returncode=child.returncode,
        timed_out=timed_out,
        not_found=False,
    )


def decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def invocation(executable_path: str) -> list[str]:
    suffix = Path(executable_path).suffix.lower()
    if suffix == ".py":
        return [sys.executable, executable_path]
    return [executable_path]


def which(executable: str) -> str | None:
    found = shutil.which(executable)
    if found:
        return found
    if os.name != "nt" and Path(executable).exists() and Path(executable).is_file():
        return str(Path(executable).resolve())
    return None


def command_for_log(args: list[str]) -> str:
    return " ".join(f'"{escape_command_log_arg(arg)}"' if re.search(r'[\s"\\]', arg) else arg for arg in args)


def escape_command_log_arg(arg: str) -> str:
    return arg.replace("\\", "\\\\").replace('"', '\\"')


def contains_any(value: str, needles: list[str]) -> bool:
    return any(needle in value for needle in needles)


def contains_failure_text(output: str) -> bool:
    return contains_any(output.lower(), ["error:", "failed", "failure", "mismatch"])


def find_stm32_programmer_cli() -> str | None:
    for candidate in ["STM32_Programmer_CLI", "STM32_Programmer_CLI.exe"]:
        found = which(candidate)
        if found:
            return found
    for candidate in common_stm32_programmer_paths():
        if Path(candidate).is_file():
            return candidate
    return None


def common_stm32_programmer_paths() -> list[str]:
    candidates: list[str] = []
    for env_name in ["ProgramFiles", "ProgramFiles(x86)"]:
        root = os.environ.get(env_name)
        if root:
            candidates.append(str(Path(root) / "STMicroelectronics" / "STM32Cube" / "STM32CubeProgrammer" / "bin" / "STM32_Programmer_CLI.exe"))
    candidates.extend(cube_ide_bundled_programmer_paths(Path("C:/ST")))
    return candidates


def cube_ide_bundled_programmer_paths(root: Path) -> list[str]:
    candidates: list[str] = []
    try:
        cube_ide_dirs = [item for item in root.iterdir() if item.name.startswith("STM32CubeIDE_")]
    except OSError:
        return candidates
    for cube_ide_dir in cube_ide_dirs:
        plugin_root = cube_ide_dir / "STM32CubeIDE" / "plugins"
        if not plugin_root.exists():
            continue
        try:
            plugin_dirs = [item for item in plugin_root.iterdir() if item.name.startswith("com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.win32_")]
        except OSError:
            continue
        for plugin_dir in plugin_dirs:
            candidates.append(str(plugin_dir / "tools" / "bin" / "STM32_Programmer_CLI.exe"))
    return sorted(candidates, reverse=True)
