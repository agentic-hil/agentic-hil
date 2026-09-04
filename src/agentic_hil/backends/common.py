from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agentic_hil.knowledge import remediation_fields
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
# prove its abort point reuses it, because the alternative (quarantining
# hardware a call provably never touched) demands a physical inspection
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

# What each backend without typed debug sessions would have to become to serve
# one, in the words its own configuration uses. Both entries end in the same
# place, because both probes are probes OpenOCD drives: the way out of this
# refusal is a `debuggers.<name>.type` change and the two scripts that come with
# it, not a different probe and not a different bench.
DEBUG_SESSION_WAY_OUT: dict[str, str] = {
    "stlink": (
        "the same ST-Link runs under `type: openocd` with `interface_cfg: interface/stlink.cfg` and the "
        "`target_cfg` for this part"
    ),
    "pyocd": (
        "the same probe runs under `type: openocd` with the `interface_cfg` for it ("
        "`interface/stlink.cfg` for an ST-Link, `interface/cmsis-dap.cfg` for a CMSIS-DAP probe) and the `target_cfg` "
        "for this part"
    ),
}


def debug_session_unsupported(backend_name: str, tool: str) -> JsonObject:
    """Refuse a typed-debug session tool, and say what to change to get one.

    Breakpoints, continue, halt-with-stop-reason and the session lifecycle are
    GDB operations, and neither STM32CubeProgrammer's CLI nor pyOCD's commander
    is a GDB server this project drives as one. That much was always true; what
    was wrong with the refusal is that it named the backend the bench does not
    run and stopped there (#342). A caller reading it learned that the capability
    exists somewhere and nothing about how to reach it, so the reasonable next
    move looked like buying a different probe, when the probe already plugged in
    is one OpenOCD drives.

    So the refusal names the configuration change instead: the same physical
    probe under `type: openocd`, with the interface and target scripts that
    backend reaches a target through. The catalogue entry behind
    `not_supported:<backend>` carries the rest, including what the change costs,
    because a way out that hides its price is a different kind of dead end.

    `error_type` stays `not_supported`. It is the project's word for a capability
    this configuration does not have, the service layer reads it as a refusal
    that started nothing, and a new spelling would have moved this branch out of
    those lists for no gain. NOT_CONTACTED for the same reason it is on the
    reset-mode refusal: this returns before anything is spawned.
    """
    way_out = DEBUG_SESSION_WAY_OUT.get(backend_name)
    reach = f" To run them on this bench, {way_out}." if way_out else ""
    return {
        "ok": False,
        "tool": tool,
        "backend": backend_name,
        "error_type": "not_supported",
        "summary": (
            f"Typed debug sessions require the OpenOCD backend; the {backend_name} backend has no debug session to "
            f"set breakpoints, continue, halt or report a stop reason through.{reach}"
        ),
        **remediation_fields("not_supported", backend_name),
        **NOT_CONTACTED,
    }


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


def failure_text_lines(output: str) -> list[str]:
    """The lines of a capture that carry the words a failure is read out of.

    The same predicate `contains_failure_text` answers over a whole transcript,
    asked line by line, so a caller that keeps such lines and a caller that
    classifies on them are looking at exactly the same words.

    Verbatim and in capture order, with nothing summarised away, for the reason
    `programmer_output_fields` gives: the line is the evidence, and a shortened
    or reworded copy of it is a second account of the run that nobody can check
    against the first. Order is the order the streams were captured in, not a
    reconstruction of the order the tool wrote them: stdout and stderr are read
    as two separate pipes, so their interleaving is not recoverable from the
    capture and no caller should read one into it.
    """
    return [line for line in output.splitlines() if contains_failure_text(line)]


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


def programmer_output_fields(completed: CompletedCommand) -> JsonObject:
    """The tool's own words about the run that failed, for the result to carry.

    A failure carries its diagnosis, and for a debugger failure the diagnosis is
    a line the tool wrote. Until #334 only the erase refusal carried one and
    every other classified failure named a log file by path instead, so a result
    relayed to a person held the classification, the summary and the likely
    causes, and not the sentence all three were derived from.

    Nested under `programmer_output` with the process's own return code, in the
    shape `stdout` and `stderr` are captured everywhere else in this project
    (#327). The human rendering prints a member under either of those names as a
    literal block rather than as one flattened row (#314), and the stream
    redaction covers them by key name at any depth (#317), so both surfaces come
    for free at any nesting.

    Nothing is summarised away and nothing is cut here. The machine document
    carries the capture whole, exactly as the log file the same result names by
    path holds it, and the renderer's caps decide how much of it a person reads;
    truncating at capture time would put a shorter transcript in the report than
    in the log, and leave nobody able to tell which one was short.
    """
    return {"programmer_output": {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}}


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
