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
    spawn_managed_process,
    terminate_process_tree,
)
from agentic_hil.types import JsonObject

# The markers a failure carries when it can prove it never reached the bench:
# no target contact, no side effect, the board where the last call that did
# reach it left it, safe to retry. 0.7.1's `debugger_command_rejected` fix
# introduced this shape for one OpenOCD case; every backend failure that can
# prove its abort point reuses it, because the alternative — quarantining
# hardware a call provably never touched — demands a physical inspection
# nothing justifies. It is the whole of the claim the service layer will act
# on: a result without these fields is not the opposite claim, it is no claim,
# and read-only one-shots quarantine on it rather than infer one.
NOT_CONTACTED: JsonObject = {
    "target_contacted": False,
    "side_effect_committed": False,
    "side_effect_status": "not_started",
    "hardware_state": "unchanged",
    "retry_safe": True,
}

# The tools whose command drives nothing of its own: whatever addressed the
# target did so while the backend was still opening its session. A backend may
# therefore read "no target answered" as a proven abort point for these, and may
# not for a flash or a reset, whose own command drives the target and can produce
# the same words after it has.
READ_ONLY_TOOLS = frozenset({"probe_target", "debugger_probes_list"})


def reset_init_unsupported(backend_name: str, missing: str) -> JsonObject:
    """Refuse `reset_target(mode="init")` on a backend that cannot run it.

    `init` is not a third spelling of `halt`. On OpenOCD, `reset init` halts the
    core and then runs the target's `reset-init` event script, which is where a
    board's clock tree, wait states and watchdog are set up before anything
    reads memory. Neither STM32CubeProgrammer nor pyOCD has that event, and
    through 0.8.0 both took `init` and sent their plain halt in its place -
    `-halt` and `reset halt`, the same argument each already sends for mode
    `halt` - so the reset-init script never ran and the mode was the same call
    twice under two names. pyOCD then reported `Target reset with mode 'init'.`
    over it, which told a caller that asked for an initialised core that it had
    one. One enum, three values, and the third meant a different
    machine depending on which debugger the project happened to configure;
    nothing shipped said so.

    Refusing is the answer these two backends already give for typed debug
    sessions: the capability is OpenOCD's, and a caller that needs it needs an
    OpenOCD debugger rather than a quieter approximation of one. The refusal is
    also cheaper than what it replaces - it returns before the CLI is spawned,
    so it carries NOT_CONTACTED and leaves the board where the last call that
    did reach it left it. `run` and `halt` are unaffected: those two do agree
    across the backends, and a step that only wanted the core stopped names
    `halt` and keeps working.
    """
    return {
        "ok": False,
        "tool": "reset_target",
        "backend": backend_name,
        "error_type": "not_supported",
        "mode": "init",
        **NOT_CONTACTED,
        "summary": (
            f"Reset mode 'init' requires the OpenOCD backend: it runs the target's reset-init event script, and {missing}. "
            "Use mode 'halt' if stopping the core is what this step needs, or configure an openocd debugger to run reset-init."
        ),
        "supported_modes": ["run", "halt"],
    }


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
        except (KeyboardInterrupt, SystemExit):
            # Preserve the interrupt after the best-effort reap above ran, rather
            # than masking it as a generic cleanup RuntimeError.
            raise
        except BaseException as cleanup_error:
            raise RuntimeError(f"Timed-out command cleanup remains unconfirmed: {cleanup_error}") from cleanup_error
    except BaseException as primary_error:
        try:
            terminate_process_tree(child, CHILD_REAP_TIMEOUT_S)
        except BaseException as cleanup_error:
            if isinstance(primary_error, (KeyboardInterrupt, SystemExit)):
                primary_error.args = (*primary_error.args, f"Cleanup error: {cleanup_error}")
                raise primary_error from cleanup_error
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


# The words that make a line a report of a failure rather than a mention of one.
# Deliberately the same two every backend's reset and flash rules already match
# on, so anchoring changes where a rule looks and nothing about what it looks for.
FAILURE_WORDS = ["failed", "error"]


def reports_reset_failure(output: str) -> bool:
    """Whether a line that reports a failure is a line that names a reset.

    The rule this replaces asked whether the word "reset" and a failure word both
    appeared *somewhere* in the transcript, which is true of nearly every failure
    on a tool that mentions resets while it is working. STM32CubeProgrammer opens
    every action with a banner carrying `Reset mode  : Software reset`, OpenOCD
    warns on nearly every `reset halt` that it is only resetting the core, and
    pyOCD logs `Resetting target` beside whatever it does next. Each of those is
    a tool saying what it is doing, and none of them is a tool saying a reset
    failed, so a failure reported lines away was answered with the reset line and
    its wiring (#333, and #327 before it on one backend).

    A reset failure is a reset named where the failure is reported: one line
    carrying both. That keeps every genuine reset failure classified, because a
    tool reporting a failed operation names the operation it failed at: `Error:
    failed to reset the target`, `Error: Unable to reset target` and `E Error
    attempting to reset target` all put the two words on one line. And it stops
    an informational reset line elsewhere in the transcript from deciding what
    the failing line meant.
    """
    return any("reset" in line and contains_any(line, FAILURE_WORDS) for line in output.lower().splitlines())


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
    candidates.extend(cube_clt_programmer_paths(Path("C:/ST")))
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


def cube_clt_programmer_paths(root: Path) -> list[str]:
    try:
        installations = [item for item in root.iterdir() if item.name.startswith("STM32CubeCLT_")]
    except OSError:
        return []
    candidates = [item / "STM32CubeProgrammer" / "bin" / "STM32_Programmer_CLI.exe" for item in installations]
    return [str(candidate) for candidate in sorted(candidates, reverse=True) if candidate.is_file()]
