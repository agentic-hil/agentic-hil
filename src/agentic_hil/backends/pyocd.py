from __future__ import annotations

import difflib
import json
import shutil
import string
import tempfile
import time
from pathlib import Path

from agentic_hil.backends.common import (
    FAILURE_WORDS,
    NOT_CONTACTED,
    READ_ONLY_TOOLS,
    CompletedCommand,
    command_for_log,
    contains_any,
    contains_failure_text,
    debug_session_unsupported,
    invocation,
    programmer_output_fields,
    reports_reset_failure,
    reset_init_unsupported,
    spawn_command,
    which,
)
from agentic_hil.backends.gdbdebug import (
    decode_symbol_value,
    offline_symbol_info,
    resolve_symbol_offline,
    validate_debug_symbol,
)
from agentic_hil.config import (
    ConfigError,
    display_path,
    probe_selector_key,
    resolve_work_path,
    safe_configured_directory,
    safe_write_text,
)
from agentic_hil.gdbmi import write_intel_hex_file
from agentic_hil.knowledge import (
    exclusive_permission_fields,
    exclusive_permission_summary,
    permission_denied_fields,
    permission_denied_summary,
    permission_key,
    remediation_fields,
)
from agentic_hil.report import (
    classify_failure_report,
    logs_directory,
    mark_audit_failure,
    mark_side_effect,
    merge_audit_status,
    overall_success,
    timestamp_for_filename,
    utc_now_iso,
    write_report,
)
from agentic_hil.types import AgenticHILConfig, JsonObject

PYOCD_NOT_FOUND: JsonObject = {
    "ok": False,
    "backend": "pyocd",
    "error_type": "debugger_not_found",
    "backend_error_type": "pyocd_not_found",
    "summary": "pyOCD executable could not be found.",
    "likely_causes": [
        "debuggers.<name>.executable is not configured",
        "pyOCD is not installed (install agentic-hil[pyocd] or pip install pyocd)",
        "pyocd is not in PATH",
    ],
    # No executable means no process, so this call is not an unconfirmed outcome
    # on the bench: nothing was ever started that could have touched it.
    **NOT_CONTACTED,
}

BACKEND_ERROR_TO_PUBLIC_ERROR = {
    "pyocd_not_found": "debugger_not_found",
    "probe_not_found": "adapter_not_found",
}

# Legal characters in a pyOCD target type name, from pyocd.target.
# normalise_target_type_name.
_TARGET_TYPE_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "_")

# Phrases that identify pyOCD refusing a target type. Taken from observed output
# and from pyocd/board/board.py, not from memory: 0.45.1 emits "Target type
# <name> not recognized. Use 'pyocd list --targets' to see currently available
# target types. See <https://pyocd.io/docs/target_support.html> for how to
# install additional target support.", which none of the first three phrases
# matched, so the whole remediation was unreachable on the one message that
# should produce it.
#
# "pyocd list --targets" is deliberately NOT a marker: pyOCD prints it in an
# unrelated warning about defaulting to the generic cortex_m target, so matching
# on it would call a successful run a failure. "not recognized" alone is not
# either, because pyOCD says the same of an unknown board ID.
TARGET_TYPE_INVALID_PHRASES = ("unknown target type", "no target type", "target type is not")
TARGET_TYPE_INVALID_DOC = "target_support.html"

# pyOCD's own words for an erase that did not take: the FlashEraseFailure its
# flash sequencer raises reaches the log as `Failed to erase sector at 0x...`, with
# the `[flash]` logger name after it. One measured phrase, for the same reason
# the ST-Link and OpenOCD marker lists hold one each: pyOCD is the backend
# likeliest to print `Resetting target` beside a flash failure, so the phrase has
# to be the erase line itself and not a family somebody assumed.
PYOCD_ERASE_FAILURE_MARKERS = ["failed to erase sector"]

# The connect the typed-debug reads use, and the whole reason they are allowed to
# exist on this backend (#344). pyOCD reads target memory, so refusing the read
# half outright was wider than this hardware's own limits; what stood in the way
# was that pyOCD has four connect modes and three of them touch the core, so a
# read taken under the wrong one would answer with a halted or freshly reset
# board's bytes and call it a measurement.
#
# pyOCD documents the four and what each does. Its command line lists them
# (`pyocd/subcommands/base.py`, 0.45.1):
#
#     -M MODE, --connect MODE
#                           Select connect mode from one of (halt, pre-reset, under-reset, attach).
#
# its session option carries the same list with the default that makes the
# choice matter (`pyocd/core/options.py`, 0.45.1):
#
#     OptionInfo('connect_mode', str, "halt",
#         "One of 'halt', 'pre-reset', 'under-reset', 'attach'. Default is 'halt'."),
#
# and its user documentation (`docs/options.md`, published as
# <https://pyocd.io/docs/options.html>) says what each one does to the target:
#
#     'halt': immediately halt all accessible cores upon connect.
#     'pre-reset': perform a hardware reset prior to connect and halt.
#     'under-reset': assert hardware reset during the connect sequence, then deassert after the cores are halted.
#     'attach': connect to a running target without halting cores.
#
# `attach` is therefore the only one of the four whose documented behaviour is
# neither a halt nor a reset, and pyOCD's own connect sequence says the same in
# code (`pyocd/coresight/coresight_target.py`, 0.45.1): `pre_connect` acts only
# for `pre-reset` and `under-reset`, `perform_halt_on_connect` opens with
# `if mode != 'attach':` before it halts anything, and `post_connect` deasserts
# reset only for `under-reset`. Under `attach` all three are no-ops.
#
# It is sent explicitly rather than left to the commander's default, even though
# `pyocd/commands/commander.py` picks the same value when nothing else is given
# ("Otherwise default to attach"). A default is a decision somebody else may
# change: `--halt`, a `connect_mode` in a project's own `pyocd.yaml`, or a later
# release choosing differently would all reach this read silently, and the
# argument on the command line is what the log can be read back for. It is the
# same reason the ST-Link reads stopped riding on `mode=NORMAL` (#342), where
# the default was the connect that reset.
#
# Nothing that resets or halts travels with it: no `--halt`, no `-O
# connect_mode=`, no `reset` command in the commander line. `debuggers.<name>.
# connect_mode` does not reach it either, and could not: this backend refuses
# `under_reset` at load, because holding a target in reset and then reading its
# RAM measures nothing the firmware did.
#
# Documented, and since 2026-08-30 also measured: issue #349 holds the bench
# evidence, five SysTick pairs on a NUCLEO-F446RE tracking wall clock through
# this connect, a reset control the counter detected, and a halted-session
# control in which the same counter froze, which is what makes it a witness.
# Ten read logs carry this argv with --connect attach and nothing that halts.
PYOCD_READ_CONNECT_MODE = "attach"
PYOCD_READ_CONNECT_ARGS = ["--connect", PYOCD_READ_CONNECT_MODE]
# The typed-debug reads this backend answers with no session behind them, and so
# the ones the coordination layer must lease as one-shots rather than run on a
# session lease that does not exist here. `debug_symbol_info` is deliberately not
# among them: it opens no probe at all.
SESSIONLESS_DEBUG_READS = frozenset({"debug_dump_symbol_ihex", "debug_symbol_value"})


class PyOCDBackend:
    backend_name = "pyocd"

    def __init__(self, config: AgenticHILConfig):
        self.config = config
        self._resolved_probe_uid: str | None = None
        self._target_types: JsonObject | None = None

    def reconfigure(self, config: AgenticHILConfig) -> None:
        if self._probe_selector_map(config) != self._probe_selector_map(self.config):
            self._resolved_probe_uid = None
        if config.debugger is None or self.config.debugger is None or config.debugger.executable != self.config.debugger.executable:
            self._target_types = None
        self.config = config

    @staticmethod
    def _probe_selector_map(config: AgenticHILConfig) -> dict[str, tuple[str, str | None]]:
        """Every configured debugger's own (type, selector) pair, exactly as
        `_cross_debugger_identity_collision` reads it: `probe_selector_key`
        strips a `<type>:` prefix when *that* entry is itself type `pyocd`, but
        the value it returns carries no trace of `type` afterwards, so an
        OpenOCD peer with `probe_id: "123"` and a pyOCD peer with the same
        `probe_id` fold to the identical key `"123"`. `type` is paired with the
        key explicitly here so the two are distinguishable.

        `_resolved_probe_uid` is cached on the claim that its resolution
        collides with no *other* configured debugger's selector under *that
        debugger's own* matching rule (substring for pyOCD, exact for anyone
        else). A reload that changes only a peer's `type` -- keeping its
        `probe_id` byte-for-byte the same -- switches which rule applies to it
        and can make that claim stop holding just as surely as a change to the
        selector text can, so both must invalidate the cache the same way. A
        map of every debugger's (type, selector), not only this one's selector,
        is what makes `reconfigure` see either kind of change.
        """
        return {name: (debugger.type, probe_selector_key(debugger)) for name, debugger in config.debuggers.items()}

    def info(self) -> JsonObject:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": "debugger_info", **resolved}
        command = [*invocation(str(resolved["executable_path"])), "--version"]
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), min(self.config.debugger.timeout_s, 10))
        if completed.not_found:
            return {"tool": "debugger_info", **PYOCD_NOT_FOUND}
        if completed.timed_out:
            return {"ok": False, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "error_type": "timeout", "summary": "Debugger version check timed out."}
        output = f"{completed.stdout}{completed.stderr}".strip()
        if completed.returncode != 0:
            backend_error_type = self._classify_output(output)
            error_type = self._public_error_type(backend_error_type)
            return {"ok": False, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type)}
        version = output.splitlines()[0] if output else "pyOCD version output was empty."
        return {"ok": True, "tool": "debugger_info", "backend": self.backend_name, "executable": resolved["executable"], "probe_id": self.config.debugger.probe_id, "target_type": self.config.debugger.target_type, "version": version, "summary": "pyOCD is available."}

    def list_probes(self) -> JsonObject:
        tool = "debugger_probes_list"
        if not self.config.probe_allowed():
            return self._permission_denied(tool, "Debugger probe discovery is disabled by the authoritative config.", self._permission_key("allow_probe"))
        return self._enumerate_probes(tool)

    def _enumerate_probes(self, tool: str) -> JsonObject:
        """List connected probes. Split out of list_probes because selector
        canonicalization needs it before flashing or resetting, which a config
        may grant without granting probe discovery.

        `pyocd json --probes --no-config` enumerates USB probes and never
        connects to a target, so every failure of it (including the timeout,
        whose child spawn_command reaped before returning) is a failed call
        with the bench untouched, and carries the markers that say so."""
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, **resolved}
        command = [*invocation(str(resolved["executable_path"])), "json", "--probes", "--no-config"]
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), self.config.debugger.timeout_s)
        if completed.not_found:
            return {"tool": tool, **PYOCD_NOT_FOUND}
        if completed.timed_out:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "timeout", "summary": "Debugger probe discovery timed out.", **NOT_CONTACTED}
        if completed.returncode != 0:
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "probe_discovery_failed", "summary": "pyOCD probe discovery command failed.", **NOT_CONTACTED}
        parsed = parse_pyocd_probes(completed.stdout)
        if not parsed["ok"]:
            return {"tool": tool, "backend": self.backend_name, **parsed, **NOT_CONTACTED}
        probes = parsed["probes"]
        return {
            "ok": True,
            "tool": tool,
            "backend": self.backend_name,
            "probes": probes,
            "summary": f"{len(probes)} connected debugger probe(s) detected.",
        }

    def probe_target(self) -> JsonObject:
        if not self.config.probe_allowed():
            return self._permission_denied("probe_target", "Probing is disabled by the authoritative config.", self._permission_key("allow_probe"))
        selected = self._resolve_probe_selector("probe_target")
        if not selected["ok"]:
            return selected
        result = self._run_pyocd("probe_target", ["commander", "--command", "status", *self._connection_args()])
        if result.get("ok"):
            result["target_detected"] = True
            result["summary"] = "Target detected through pyOCD."
        return self._write_action_report(result)

    def flash_firmware(self, artifact: JsonObject, reset_after_flash: bool = False) -> JsonObject:
        if not self.config.debugger.permissions.allow_flash:
            return self._permission_denied("flash_firmware", "Flashing is disabled by the authoritative config.", self._permission_key("allow_flash"))
        if self.config.debugger.permissions.allow_raw_debugger_commands:
            return self._exclusive_permission_denied("flash_firmware", "Flashing", "allow_raw_debugger_commands")
        if self.config.debugger.permissions.allow_mass_erase:
            return self._exclusive_permission_denied("flash_firmware", "Flashing", "allow_mass_erase")

        artifact_path = str(artifact["resolved_path"])
        address_args: list[str] = []
        if Path(artifact_path).suffix.lower() == ".bin":
            if self.config.debugger.flash_address is None:
                return {"ok": False, "tool": "flash_firmware", "backend": self.backend_name, "error_type": "invalid_argument", "summary": "Flashing .bin artifacts with pyOCD requires debuggers.<name>.flash_address.", "artifact": self._artifact_summary(artifact)}
            address_args = ["--base-address", self.config.debugger.flash_address]

        selected = self._resolve_probe_selector("flash_firmware")
        if not selected["ok"]:
            return selected
        result = self._run_pyocd("flash_firmware", ["flash", "--no-reset", *self._connection_args(), *address_args, artifact_path])
        result["artifact"] = self._artifact_summary(artifact)
        result["verify"] = True
        if not overall_success(result):
            result["reset_after_flash"] = False
            if result.get("ok") is True:
                result["reset_skipped_reason"] = "audit_failed" if result.get("audit_ok") is False else "target_failed"
            return self._write_action_report(result)
        if not reset_after_flash:
            result["reset_after_flash"] = False
            result["summary"] = "Firmware flashed and verified. Target was not reset."
            return self._write_action_report(result)

        reset = self._run_pyocd("flash_firmware", ["commander", "--command", "reset", *self._connection_args()])
        if not reset.get("ok"):
            reset["artifact"] = self._artifact_summary(artifact)
            reset["verify"] = True
            reset["reset_after_flash"] = False
            reset["side_effect_committed"] = True
            reset["side_effect_status"] = "partial"
            reset["retry_safe"] = False
            reset["error_type"] = "reset_failed"
            reset["summary"] = "Firmware flashed, but the post-flash reset failed."
            return self._write_action_report(merge_audit_status(reset, result, reset))
        result["reset_after_flash"] = reset_after_flash
        result["summary"] = "Firmware flashed, verified, and target reset."
        return self._write_action_report(merge_audit_status(result, result, reset))

    def reset_target(self, mode: str = "run") -> JsonObject:
        allowed_modes = ["run", "halt", "init"]
        if mode not in allowed_modes:
            return {"ok": False, "tool": "reset_target", "error_type": "invalid_argument", "summary": "Invalid reset mode.", "allowed_values": allowed_modes}
        if mode == "init":
            return reset_init_unsupported(self.backend_name, "pyOCD's commander has no equivalent")
        commander_command = "reset" if mode == "run" else "reset halt"
        selected = self._resolve_probe_selector("reset_target")
        if not selected["ok"]:
            return selected
        result = self._run_pyocd("reset_target", ["commander", "--command", commander_command, *self._connection_args()])
        result["mode"] = mode
        if result.get("ok"):
            result["summary"] = f"Target reset with mode '{mode}'."
        return self._write_action_report(result)

    def debug_start_session(self, artifact: JsonObject | None = None, mode: str = "attach", timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_start_session")

    def debug_stop_session(self, timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_stop_session")

    def debug_get_session_status(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_get_session_status")

    def debug_set_breakpoint(self, location: JsonObject | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_set_breakpoint")

    def debug_list_breakpoints(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_list_breakpoints")

    def debug_clear_breakpoints(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_clear_breakpoints")

    def debug_continue(self, timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_continue")

    def debug_halt(self, timeout_s: float | None = None) -> JsonObject:
        return self._unsupported_debug_tool("debug_halt")

    def debug_get_stop_reason(self) -> JsonObject:
        return self._unsupported_debug_tool("debug_get_stop_reason")

    def debug_symbol_info(self, symbol: str = "", symbol_elf: JsonObject | None = None) -> JsonObject:
        """The shared offline resolution, and nothing of this backend's own.

        Where a symbol lives and how large it is are properties of the image, and
        the image is on disk, so this answer never needed pyOCD at all. It
        refused here only because it was filed with the session family, which is
        the same mistake #342 found on the ST-Link backend and left standing on
        this one (#344).

        The same call that backend makes, so the two answer identically: a GDB
        run in batch mode against the ELF a confirmed `flash_firmware` put on the
        board, with no probe opened, no lease taken and no programmer log
        written. `debug.gdb_executable` is what it needs, and `gdb_not_found`
        names that field when the bench has not set one.
        """
        return offline_symbol_info(self.config, self.backend_name, symbol, symbol_elf)

    def debug_symbol_value(self, symbol: str = "", symbol_elf: JsonObject | None = None) -> JsonObject:
        """One allowed symbol's bytes, read through pyOCD's own memory read.

        The OpenOCD path's result, from a backend with no session: same
        allowlist refusal word for word, same `debug.max_dump_size_bytes` cap on
        the resolved size before a probe is opened, same `hex` and integer
        readings and summary, plus the `symbol_source` naming the flashed ELF
        the symbol was resolved against, because there is no session that loaded
        it.

        `pyocd commander --command "savemem ADDR LEN FILE"` is what does the
        reading: pyOCD's own documentation of it is "Save a range of memory to a
        binary file", it is served with no gdbserver in the picture, and it
        connects with `--connect attach`, the one mode pyOCD documents as
        leaving a running core alone. The file it writes is this call's own
        business, created in a private directory, read back and deleted before
        the result is built, and named nowhere in it.

        The bytes are held to exactly what was asked for: `savemem` writes the
        window it read and nothing else, so a file that is not `size_bytes` long
        is a failed read rather than a short answer.
        """
        tool = "debug_symbol_value"
        prepared = self._prepare_symbol_read(tool, symbol, symbol_elf)
        if not prepared["ok"]:
            return prepared["result"]
        resolved = prepared["resolved"]
        read = self._read_target_memory(tool, int(resolved["address_value"]), int(resolved["size_bytes"]))
        result = self._finish_symbol_read(read, symbol, resolved)
        if result.get("ok"):
            result.update(decode_symbol_value(read["data"], resolved["image_byte_order"]))
            result["summary"] = "Symbol value read from target memory."
        return self._write_action_report(result)

    def debug_dump_symbol_ihex(self, symbol: str = "", output: JsonObject | None = None, symbol_elf: JsonObject | None = None) -> JsonObject:
        """Read one allowed symbol out of target memory and write Intel HEX.

        The same `savemem` read as the value tool, ending differently, and the
        ending is where this backend differs from the ST-Link one: pyOCD's
        memory read writes a raw binary file, so the Intel HEX a caller was
        promised is written here, by the same `write_intel_hex_file` the OpenOCD
        path uses on the bytes its session read. The caller's path is therefore
        never handed to pyOCD, and the file that appears at it is written through
        the workspace-guarded writer or not at all.
        """
        tool = "debug_dump_symbol_ihex"
        prepared = self._prepare_symbol_read(tool, symbol, symbol_elf)
        if not prepared["ok"]:
            return prepared["result"]
        if output is None:
            # The service validates the output path before it gets here, so this
            # is unreachable through a tool call. It is a refusal rather than an
            # assert because an assert is stripped under `python -O`, and what
            # would follow it is a probe opened for a read with nowhere to put
            # what it read.
            return {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "invalid_argument", "summary": "output_path must be a non-empty string.", **NOT_CONTACTED}
        resolved = prepared["resolved"]
        address_value = int(resolved["address_value"])
        read = self._read_target_memory(tool, address_value, int(resolved["size_bytes"]))
        result = self._finish_symbol_read(read, symbol, resolved)
        result["output"] = output
        if result.get("ok"):
            try:
                # The writer will not create a missing parent, so the directory
                # is made through the same guarded helper the OpenOCD path uses,
                # and a dump cannot mkdir its way out of the workspace.
                safe_configured_directory(self.config, str(Path(str(output["resolved_path"])).parent), f"{tool}.output_path")
                write_intel_hex_file(Path(str(output["resolved_path"])), address_value, read["data"], workspace=self.config.work_dir)
            except (ConfigError, OSError) as error:
                # The bytes did leave the target, so this failure is about the
                # file and says so: the read itself is not in doubt and the
                # contact is not either.
                result.update({"ok": False, "error_type": "output_write_failed", "summary": "Intel HEX output file could not be written.", "backend_error": str(error), "target_contacted": True})
                return self._write_action_report(result)
            result["summary"] = "Symbol memory dumped as Intel HEX."
        return self._write_action_report(result)

    def sessionless_debug_tools(self) -> frozenset[str]:
        """The two reads this backend serves with no debug session behind them.

        `savemem` attaches to a live core the same way a reset does, so a symbol
        read here is a standalone hardware interaction rather than a call inside
        a session that already holds the lease. The coordination layer takes a
        one-shot debugger lease for exactly these: machine-wide ownership, run
        declaration and the incident path for an unconfirmed read, which a
        session backend's own lease would carry instead."""
        return SESSIONLESS_DEBUG_READS

    def target_support(self) -> JsonObject:
        """Whether this pyOCD resolves the configured target_type, before anything is driven.

        Three answers, and keeping them apart is the whole point. `unsupported`
        is a fact about this host: pyOCD enumerated its target types and the
        configured one is not among them, so no flash can work and the pack
        command is the fix. `undetermined` means this host could not answer
        (pyOCD is not installed, the enumeration failed, the output was not
        readable), which says nothing about the configuration and must not turn
        a healthy `doctor` red. A check that conflated the two would fail every
        bench that has no toolchain installed yet, and `setup` rolls back on a
        red `doctor`.
        """
        report: JsonObject = {"ok": True, "tool": "debugger_target_support", "backend": self.backend_name}
        target_type = self.config.debugger.target_type if self.config.debugger else None
        if not target_type:
            return {
                **report,
                "status": "not_configured",
                "target_type": None,
                "summary": "No debuggers.<name>.target_type is configured, so pyOCD guesses the target from the probe's board ID. Set it to the MCU part for a board pyOCD does not recognise.",
            }
        report["target_type"] = target_type
        listed = self._enumerate_target_types()
        if not listed["ok"]:
            return {
                **report,
                "status": "undetermined",
                "undetermined_reason": listed["reason"],
                "summary": f"Whether pyOCD resolves target_type '{target_type}' could not be determined on this host: {listed['reason']} This is not a fault in the configuration.",
            }
        targets = listed["targets"]
        entry = targets.get(normalise_target_type(target_type))
        if entry is not None:
            source = entry.get("source")
            provenance = "an installed CMSIS pack" if source == "pack" else "pyOCD's built-in list"
            return {
                **report,
                "status": "supported",
                "source": source,
                "vendor": entry.get("vendor"),
                "part_number": entry.get("part_number"),
                "summary": f"pyOCD resolves target_type '{target_type}' from {provenance}.",
            }
        return {
            **report,
            "ok": False,
            "status": "unsupported",
            "error_type": "target_type_invalid",
            "summary": f"pyOCD does not resolve target_type '{target_type}'. Most vendor parts, including the whole STM32F4 family, exist only after a CMSIS device-family pack is installed.",
            "likely_causes": self._likely_causes("target_type_invalid"),
            **remediation_fields("target_type_invalid", self.backend_name),
            "install_commands": pack_install_commands(target_type),
            "close_matches": difflib.get_close_matches(normalise_target_type(target_type), sorted(targets), n=5, cutoff=0.7),
            "resolved_target_types": len(targets),
        }

    def classify_last_error(self) -> JsonObject:
        return classify_failure_report(self.config, self._likely_causes)

    def close(self) -> None:
        return None

    def _enumerate_target_types(self) -> JsonObject:
        """Every target type this pyOCD resolves, keyed by pyOCD's normalised name.

        `{"ok": True, "targets": {...}}`, or `{"ok": False, "reason": "..."}` when
        the question could not be answered here. Never guesses: an output that
        does not parse exactly as pyOCD's documented `json` envelope is a reason,
        not an empty target list, because an empty list would read as "nothing is
        supported" and condemn a working configuration.
        """
        if self._target_types is not None:
            return self._target_types
        self._target_types = self._read_target_types()
        return self._target_types

    def _read_target_types(self) -> JsonObject:
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"ok": False, "reason": "the pyOCD executable could not be found."}
        command = [*invocation(str(resolved["executable_path"])), "json", "--targets", "--no-config"]
        # Enumerating target types is a host query that never reaches the probe,
        # so a short hardware timeout must not turn a determinable answer into
        # "undetermined"; a first run that populates the pack cache is slow.
        timeout_s = max(self.config.debugger.timeout_s, 30) if self.config.debugger else 30
        completed = spawn_command(command, str(Path(str(resolved["executable_path"])).parent), timeout_s)
        if completed.not_found:
            return {"ok": False, "reason": "the pyOCD executable could not be found."}
        if completed.timed_out:
            return {"ok": False, "reason": f"`pyocd json --targets` did not finish within {timeout_s:g}s."}
        if completed.returncode != 0:
            return {"ok": False, "reason": "`pyocd json --targets` failed, so the list of resolvable target types is unknown."}
        return parse_pyocd_targets(completed.stdout)

    def _confirm_target_support(self, backend_error_type: str) -> str:
        """Reclassify an unexplained failure when the target type provably does not resolve.

        pyOCD reports a bad target type as prose on stderr and exits 1 like every
        other error, so the classifier above has to match text, and text changes
        between releases: it already did, which is how this stayed invisible.
        `pyocd json --targets` answers the same question in a machine-readable
        form, so a failure nothing else explains is checked against it once.

        Only a definite absence reclassifies. An enumeration that could not be
        run or read leaves the classification exactly as it was, because "cannot
        tell" must never be reported as "the target type is wrong".
        """
        if backend_error_type != "unknown_debugger_error":
            return backend_error_type
        target_type = self.config.debugger.target_type if self.config.debugger else None
        if not target_type:
            return backend_error_type
        listed = self._enumerate_target_types()
        if not listed["ok"]:
            return backend_error_type
        return "target_type_invalid" if normalise_target_type(target_type) not in listed["targets"] else backend_error_type

    def _resolve_executable(self) -> JsonObject:
        configured = self.config.debugger.executable
        if configured:
            has_path_separator = "/" in configured or "\\" in configured
            if Path(configured).is_absolute() or has_path_separator:
                resolved = Path(resolve_work_path(self.config, configured))
                if not resolved.is_file():
                    return dict(PYOCD_NOT_FOUND)
                return {"ok": True, "executable": str(resolved), "executable_path": str(resolved)}
            found = which(configured)
            if found is None:
                return dict(PYOCD_NOT_FOUND)
            return {"ok": True, "executable": found, "executable_path": found}
        found = which("pyocd")
        if found is None:
            return dict(PYOCD_NOT_FOUND)
        return {"ok": True, "executable": found, "executable_path": found}

    def _run_pyocd(self, tool: str, action_args: list[str]) -> JsonObject:
        started_at = utc_now_iso()
        start = time.perf_counter()
        resolved = self._resolve_executable()
        if not resolved["ok"]:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **resolved, "finished_at": utc_now_iso(), "elapsed_ms": int((time.perf_counter() - start) * 1000)}
        args = [*invocation(str(resolved["executable_path"])), *action_args]
        log_path = str(Path(logs_directory(self.config)) / f"pyocd-{timestamp_for_filename()}-{tool}.log")
        completed = spawn_command(args, str(Path(str(resolved["executable_path"])).parent), self.config.debugger.timeout_s)
        finished_at = utc_now_iso()
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if completed.not_found:
            return {"tool": tool, "backend": self.backend_name, "started_at": started_at, **PYOCD_NOT_FOUND, "finished_at": finished_at, "elapsed_ms": elapsed_ms}
        audit_error = self._write_log(log_path, args, completed.stdout, completed.stderr, completed.returncode, completed.timed_out)
        if completed.timed_out:
            return self._finish_log_audit({"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": "timeout", "summary": "Debugger command timed out.", "likely_causes": self._likely_causes("timeout"), "log_path": display_path(self.config, log_path)}, audit_error)
        output = f"{completed.stdout}{completed.stderr}"
        if completed.returncode == 0:
            backend_error_type = self._backend_error_from_output(output, tool)
            if backend_error_type is not None:
                return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, backend_error_type, log_path, completed), audit_error)
            return self._finish_log_audit({"ok": True, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "summary": "pyOCD command completed successfully.", "log_path": display_path(self.config, log_path)}, audit_error)
        return self._finish_log_audit(self._failure_result(tool, started_at, finished_at, elapsed_ms, self._confirm_target_support(self._classify_output(output, tool)), log_path, completed), audit_error)

    def _prepare_symbol_read(self, tool: str, symbol: str, symbol_elf: JsonObject | None) -> JsonObject:
        """Everything both memory reads settle before a probe is opened.

        In the order the OpenOCD and ST-Link paths settle it, because the order
        is part of the answer: the permission first, then the allowlist, then the
        offline resolution, then the size cap against the size that resolution
        returned. A symbol nobody allowed must not leak an address, and a read
        larger than `debug.max_dump_size_bytes` must be refused before pyOCD is
        spawned rather than after it has read the bytes.
        """
        if not self.config.probe_allowed():
            return {"ok": False, "result": self._permission_denied(tool, "Symbol reads require allow_probe in the authoritative config.", self._permission_key("allow_probe"))}
        validated = validate_debug_symbol(self.config, self.backend_name, tool, symbol)
        if not validated["ok"]:
            return {"ok": False, "result": validated}
        resolved = resolve_symbol_offline(self.config, self.backend_name, tool, symbol, symbol_elf)
        if not resolved["ok"]:
            return {"ok": False, "result": resolved}
        size_bytes = int(resolved["size_bytes"])
        if size_bytes > self.config.debug.max_dump_size_bytes:
            noun = "read" if tool == "debug_symbol_value" else "dump"
            return {"ok": False, "result": {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "permission_denied", "summary": f"Symbol {noun} exceeds debug.max_dump_size_bytes.", "symbol": symbol, "size_bytes": size_bytes, "max_dump_size_bytes": self.config.debug.max_dump_size_bytes}}
        selected = self._resolve_probe_selector(tool)
        if not selected["ok"]:
            return {"ok": False, "result": selected}
        return {"ok": True, "resolved": resolved}

    def _read_target_memory(self, tool: str, address_value: int, size_bytes: int) -> JsonObject:
        """`savemem` into a private file, and the bytes it left there.

        The file is private and outside the workspace on purpose, for both
        reads: the value tool's caller asked for bytes and named no path, and the
        dump's caller named a path for Intel HEX, which is not what pyOCD writes.
        So neither caller's workspace is where this lands, and the file is gone
        before the result is built.

        `data` is the bytes only when the run succeeded and the file holds
        exactly the window that was asked for. Anything else is `None`, and the
        caller reports a failed read rather than a short answer, because half of
        a status word is a different number rather than a smaller one.
        """
        try:
            staging_dir = Path(tempfile.mkdtemp(prefix="agentic-hil-pyocd-read-"))
        except OSError as error:
            return {"result": {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "memory_read_failed", "summary": "The private file this read needs could not be created.", "backend_error": str(error), **NOT_CONTACTED}, "data": None}
        try:
            memory_path = staging_dir / "symbol-memory.bin"
            argument = pyocd_command_path(memory_path)
            if argument is None:
                # Refused rather than sent, because pyOCD would read a different
                # path than the one meant: its command tokenizer treats an
                # unquoted backslash as an escape and ends a single-quoted word
                # at the next quote, so a path it cannot carry has to stop the
                # read instead of silently redirecting it.
                return {"result": {"ok": False, "tool": tool, "backend": self.backend_name, "error_type": "memory_read_failed", "summary": "The private file this read needs cannot be named on pyOCD's command line.", "backend_error": f"temporary directory path contains a quote character: {memory_path}", **NOT_CONTACTED}, "data": None}
            result = self._run_pyocd(tool, ["commander", *PYOCD_READ_CONNECT_ARGS, "--command", f"savemem {hex(address_value)} {size_bytes} {argument}", *self._connection_args()])
            data: bytes | None = None
            if result.get("ok"):
                try:
                    data = memory_path.read_bytes()
                except OSError:
                    data = None
                if data is not None and len(data) != size_bytes:
                    data = None
            return {"result": result, "data": data}
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def _finish_symbol_read(self, read: JsonObject, symbol: str, resolved: JsonObject) -> JsonObject:
        """The fields both reads add, and the two verdicts neither may skip.

        A failure that did not prove where it stopped is unconfirmed rather than
        harmless: a read writes nothing, but it was a probe attached to a live
        core, and a pyOCD that stopped part-way through leaves no evidence of
        what it did to it. Branches that did prove their abort point, such as a
        probe pyOCD never found, arrive carrying `not_started` and keep it.

        A run that succeeded and left no usable file is a failed read that did
        reach the target, which is the one case the exit code alone would report
        as success.
        """
        result = read["result"]
        result.update({"symbol": symbol, "address": resolved["address"], "size_bytes": int(resolved["size_bytes"]), "resolved_from": resolved["resolved_from"], "symbol_source": resolved["symbol_source"]})
        if not result.get("ok") and "side_effect_status" not in result:
            result.update({"side_effect_status": "unknown", "retry_safe": False})
        if result.get("ok") and read["data"] is None:
            result.update({"ok": False, "error_type": "memory_read_failed", "summary": "pyOCD reported a completed run but left no file holding the requested bytes.", "target_contacted": True})
        return result

    def _connection_args(self) -> list[str]:
        args: list[str] = []
        # The canonical full UID when one was resolved, so pyOCD's substring
        # match can only land on the probe the operator named.
        selector = self._resolved_probe_uid or (self.config.debugger.probe_id if self.config.debugger else None)
        if selector is not None:
            args.extend(["--uid", selector])
        if self.config.debugger.target_type is not None:
            args.extend(["--target", self.config.debugger.target_type])
        return args

    def _resolve_probe_selector(self, tool: str) -> JsonObject:
        """Resolve the configured probe_id to the one full probe UID it selects.

        pyOCD matches --uid as a case-insensitive SUBSTRING of a probe's unique
        ID and strips an optional `<type>:` prefix first (pyocd.probe.aggregator).
        So a configured value can select a board the operator never named, and
        two different values can select one board. Enumerating and then passing
        the full UID is the only way a configured name means one probe.

        Config load cannot do this (it must work with no hardware attached),
        so it happens here, once per bound probe, before anything is driven.

        Enumerating also lets this catch what config load provably cannot: two
        selectors that share no substring relationship with each other
        (config.validate_debuggers rejects only that relationship, the one
        static text can decide) can still both resolve, uniquely, to the same
        attached probe once real hardware answers (`OCD1` and `123` both
        landing on `PYOCD123`, neither a substring of the other). That
        collision is checked below, against every other configured debugger,
        before this resolution is trusted or cached."""
        debugger = self.config.debugger
        if debugger is None or debugger.probe_id is None:
            return {"ok": True, "uid": None}
        if self._resolved_probe_uid is not None:
            return {"ok": True, "uid": self._resolved_probe_uid}

        listed = self._enumerate_probes(tool)
        if not listed["ok"]:
            return listed
        available = [str(probe.get("probe_id", "")) for probe in listed["probes"]]
        needle = debugger.probe_id.split(":", 1)[1] if ":" in debugger.probe_id else debugger.probe_id
        matches = [uid for uid in available if needle.lower() in uid.lower()]
        if not matches:
            return self._probe_selector_error(tool, "no connected probe matches the configured probe_id.", debugger.probe_id, available)
        if len(matches) > 1:
            return self._probe_selector_error(tool, "the configured probe_id matches more than one connected probe.", debugger.probe_id, matches)
        # The resolved UID is passed back through the same substring match, so
        # it must be unambiguous in its own right: one full UID can be a
        # substring of a longer one.
        reselected = [uid for uid in available if matches[0].lower() in uid.lower()]
        if len(reselected) > 1:
            return self._probe_selector_error(tool, "the resolved probe UID is itself a prefix of another connected probe's UID, so it cannot address one board.", debugger.probe_id, reselected)
        collision = self._cross_debugger_identity_collision(matches[0], available)
        if collision is not None:
            return self._probe_identity_collision_error(tool, debugger.probe_id, matches[0], *collision)
        self._resolved_probe_uid = matches[0]
        return {"ok": True, "uid": matches[0]}

    def _cross_debugger_identity_collision(self, resolved_uid: str, available: list[str]) -> tuple[str, str] | None:
        """The other configured debugger, if any, whose own probe_id resolves to
        this same attached probe.

        Two selectors can each be sound on their own (neither a no-match, a
        multi-match, nor a self-prefix) and still name one physical probe for
        two logical debuggers, which is exactly what `validate_debuggers`
        cannot see from the text alone and defers to here. Every other
        configured entry with a probe_id is a candidate, not only the other
        pyocd ones: `probe_selector_key` already carries the right rule for
        each (the `<type>:` prefix pyOCD strips applies only when that entry
        is itself type pyocd), and a probe pyOCD can see is a probe some other
        backend's entry may also have been given the serial of. Resolved
        against the same attached hardware this entry just enumerated, so an
        entry naming a probe that is not actually here never collides with
        one that is.

        Matched by the *peer's own* backend rule, not pyOCD's. Substring
        containment is pyOCD's own quirk (`pyocd.probe.aggregator` matching
        `--uid` as a case-insensitive substring); OpenOCD and ST-Link hand
        their configured `probe_id` straight to their own tool as an exact
        serial (`adapter serial <id>`, `sn=<id>`) and neither does substring
        selection at all. Applying pyOCD's rule to an OpenOCD or ST-Link
        entry can report a collision neither backend would ever produce --
        `123` is a substring of `PYOCD123` but is not an OpenOCD serial that
        resolves to it -- and falsely blocking a bound pyOCD probe over an
        absent peer's unrelated, similarly-named one is exactly the failure
        mode `validate_debuggers` already lets through to here."""
        debugger_id = self.config.debugger_id
        for name, other in self.config.debuggers.items():
            if name == debugger_id or other.probe_id is None:
                continue
            needle = probe_selector_key(other)
            if needle is None:
                continue
            if other.type == "pyocd":
                other_matches = [uid for uid in available if needle in uid.lower()]
            else:
                other_matches = [uid for uid in available if needle == uid.lower()]
            if len(other_matches) == 1 and other_matches[0] == resolved_uid:
                return name, other.probe_id
        return None

    def _probe_selector_error(self, tool: str, reason: str, configured: str, candidates: list[str]) -> JsonObject:
        return {
            "ok": False,
            "tool": tool,
            "backend": self.backend_name,
            "error_type": "adapter_not_found",
            "summary": f"Probe selection is not unambiguous: {reason}",
            **remediation_fields("adapter_not_found", self.backend_name),
            "configured_probe_id": configured,
            "candidate_probe_ids": candidates,
            **NOT_CONTACTED,
            # Deliberately not retry_safe: retrying with the same configuration
            # cannot resolve the ambiguity, only a config or bench change can.
            "retry_safe": False,
        }

    def _probe_identity_collision_error(self, tool: str, configured: str, resolved_uid: str, other_debugger: str, other_probe_id: str) -> JsonObject:
        # Same envelope every other unresolvable selector returns from this
        # method, plus the two fields naming the other side of the collision:
        # config.py's own collision refusals (validate_debuggers,
        # validate_resource_ids) carry an `other_debugger` for the same reason:
        # a caller told two boards apart by name needs the other name, not just
        # a probe string, to know which entry to fix.
        error = self._probe_selector_error(
            tool,
            (
                f"debuggers.{self.config.debugger_id}.probe_id ({configured!r}) and "
                f"debuggers.{other_debugger}.probe_id ({other_probe_id!r}) both resolve to the same "
                f"connected probe ({resolved_uid}), so they would drive one physical probe as if it were two."
            ),
            configured,
            [resolved_uid],
        )
        error["other_debugger"] = other_debugger
        error["other_probe_id"] = other_probe_id
        return error

    def _artifact_summary(self, artifact: JsonObject) -> JsonObject:
        return {"source": artifact.get("source", "path"), "path": artifact.get("path"), "sha256": artifact.get("sha256")}

    # Classifications drawn from pyOCD's own output that name where its run
    # stopped, each of them short of driving the target. The connect failure is
    # limited to the reads, whose command drives nothing of its own: a flash or a
    # reset that lost the target part-way through says the same words.
    PRE_CONTACT_BACKEND_ERRORS = frozenset({"probe_not_found", "target_type_invalid"})
    READ_ONLY_PRE_CONTACT_BACKEND_ERRORS = frozenset({"target_not_detected"})

    def _proves_no_contact(self, tool: str, backend_error_type: str) -> bool:
        if backend_error_type in self.PRE_CONTACT_BACKEND_ERRORS:
            return True
        # SESSIONLESS_DEBUG_READS beside the older READ_ONLY_TOOLS: a `savemem`
        # read drives nothing of its own either, so pyOCD reporting it never
        # connected, no ACK, not responding, unable to connect, is the same
        # proof of no contact it is for a probe listing. Without them here a
        # sessionless read that failed before `savemem` keeps no NOT_CONTACTED
        # fields, and `_finish_symbol_read` turns a provably untouched bench
        # into `side_effect_status: unknown` and a cleanup-required lease.
        return tool in (READ_ONLY_TOOLS | SESSIONLESS_DEBUG_READS) and backend_error_type in self.READ_ONLY_PRE_CONTACT_BACKEND_ERRORS

    def _failure_result(self, tool: str, started_at: str, finished_at: str, elapsed_ms: int, backend_error_type: str, log_path: str, completed: CompletedCommand) -> JsonObject:
        # likely_causes says what may be wrong; remediation says what to check
        # next, scoped to this backend. target_type_invalid is the case that
        # cost the most: without the fix in the result, the caller has to work
        # out from pyOCD's own sources that the value comes from a CMSIS pack.
        error_type = self._public_error_type(backend_error_type)
        # `programmer_output` on every classified failure (#334). pyOCD logs what
        # it was doing as it does it, so the line it stopped on arrives with the
        # lines that led up to it, and that is what the classification, the
        # summary and the causes were all read out of.
        result = {"ok": False, "tool": tool, "backend": self.backend_name, "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms, "error_type": error_type, "backend_error_type": backend_error_type, "summary": self._summary_for_error(error_type), "likely_causes": self._likely_causes(error_type), **remediation_fields(error_type, self.backend_name), "log_path": display_path(self.config, log_path), **programmer_output_fields(completed)}
        target_type = self.config.debugger.target_type if self.config.debugger else None
        if error_type == "target_type_invalid" and target_type:
            # The refusal carries the command that fixes it, with the configured
            # value already substituted, so nobody has to reconstruct it.
            result["install_commands"] = pack_install_commands(target_type)
        if self._proves_no_contact(tool, backend_error_type):
            # Each names a point at which the target was not driven: the probe is
            # the only channel to it, and pyOCD reporting that it found no probe
            # or could not open one is the report that the channel never existed;
            # an unresolvable target type is refused while the session is being
            # built, before anything is driven; and a connect sequence pyOCD says
            # failed (no ACK, not responding, unable to connect) never brought
            # a core under debug control. A failed call over a channel that never
            # carried anything must refuse, not quarantine. All
            # three are pyOCD's own words about its own run, which is what makes
            # them evidence rather than this layer's inference.
            result.update(NOT_CONTACTED)
        return result

    def _backend_error_from_output(self, output: str, tool: str) -> str | None:
        backend_error_type = self._classify_output(output, tool)
        if backend_error_type != "unknown_debugger_error":
            return backend_error_type
        if contains_failure_text(output):
            return backend_error_type
        return None

    def _write_action_report(self, result: JsonObject) -> JsonObject:
        return write_report(self.config, mark_side_effect(result))

    def _write_log(self, log_path: str, args: list[str], stdout: str, stderr: str, returncode: int | None, timed_out: bool) -> Exception | None:
        try:
            safe_write_text(self.config, log_path, json.dumps({"command": command_for_log(args), "returncode": returncode, "timed_out": timed_out, "stdout": stdout, "stderr": stderr}, indent=2) + "\n")
        except (ConfigError, OSError) as error:
            return error
        return None

    def _finish_log_audit(self, result: JsonObject, error: Exception | None) -> JsonObject:
        return mark_audit_failure(result, error) if error is not None else result

    def _permission_denied(self, tool: str, summary: str, permission: str | None = None) -> JsonObject:
        """A refusal one permission caused, carrying which one and what to do.

        `permission` is the dotted key the file uses and `agentic-hil grant`
        takes, built off the bound entry's own name. Named in the summary as
        well as carried as a field: an agent asked to report the permission it
        was denied reads the summary out (#443)."""
        result: JsonObject = {"ok": False, "tool": tool, "error_type": "permission_denied", "summary": summary}
        if permission:
            result["summary"] = permission_denied_summary(summary, permission)
            result.update(permission_denied_fields(permission))
            result.update(remediation_fields("permission_denied", permission=permission))
        return result

    def _permission_key(self, key: str) -> str:
        return permission_key("debuggers", self.config.debugger_id, key)

    def _exclusive_permission_denied(self, tool: str, action: str, blocking: str) -> JsonObject:
        """The other direction: a permission that is granted and blocks this.

        Its own helper because the advice is the opposite one. The unscoped
        `permission_denied` entry says the operator opens the key; here the key
        is already open and opening it is what an operator must not be sent to
        do, so the scoped entry travels on the result rather than being looked
        up later off an `error_type` the two cases share."""
        return {
            "ok": False,
            "tool": tool,
            "error_type": "permission_denied",
            "summary": exclusive_permission_summary(action, blocking, self.config.debugger_id),
            **exclusive_permission_fields(blocking, self.config.debugger_id),
        }

    def _unsupported_debug_tool(self, tool: str) -> JsonObject:
        return debug_session_unsupported(self.backend_name, tool)

    def _classify_output(self, output: str, tool: str | None = None) -> str:
        lower = output.lower()
        if contains_any(lower, ["no available debug probes", "no debug probes are connected", "unable to open probe", "probe not found", "no probe with uid"]):
            return "probe_not_found"
        if contains_any(lower, ["unable to connect", "failed to connect", "target is not responding", "no ack received", "error connecting"]):
            return "target_not_detected"
        if contains_any(lower, TARGET_TYPE_INVALID_PHRASES) or TARGET_TYPE_INVALID_DOC in lower:
            return "target_type_invalid"
        if "target type" in lower and "not recognized" in lower:
            return "target_type_invalid"
        # Ahead of the verify and reset rules and of the broad flash bucket, for
        # the reason #327 put the ST-Link erase rule there: this is the line
        # pyOCD wrote about the operation that stopped, and it used to be read
        # either as a generic flash failure or, whenever pyOCD had logged
        # `Resetting target` first, as a failed reset (#333).
        if contains_any(lower, PYOCD_ERASE_FAILURE_MARKERS):
            return "flash_erase_failed"
        if "verify" in lower and contains_any(lower, ["failed", "mismatch", "error"]):
            return "verify_failed"
        if reports_reset_failure(output):
            return "reset_failed"
        if tool == "flash_firmware" and contains_any(lower, FAILURE_WORDS):
            return "flash_failed"
        # The twin of the flash bucket above, anchored on the operation rather
        # than on a word: when the tool is `reset_target`, the operation that
        # reported a failure is a reset, whatever pyOCD's commander wrote about
        # it. That is what keeps a genuine reset failure classified without the
        # rule above having to guess from a stray "reset" somewhere in a
        # transcript, which is what it used to do (#333).
        if tool == "reset_target" and contains_any(lower, FAILURE_WORDS):
            return "reset_failed"
        # The same rule once more, anchored on the operation rather than on a
        # word: when the tool is one of the memory reads, the operation that
        # reported a failure is a read, whatever pyOCD's commander wrote about
        # it. Without it a `savemem` that could not reach the address would land
        # in the unknown bucket and answer "Debugger failed with an unknown
        # error", which tells a caller nothing about what was being attempted.
        if tool in SESSIONLESS_DEBUG_READS and contains_any(lower, FAILURE_WORDS):
            return "memory_read_failed"
        return "unknown_debugger_error"

    def _public_error_type(self, backend_error_type: str) -> str:
        return BACKEND_ERROR_TO_PUBLIC_ERROR.get(backend_error_type, backend_error_type)

    def _summary_for_error(self, error_type: str) -> str:
        return {
            "debugger_not_found": "Debugger executable could not be found.",
            "adapter_not_found": "Debugger probe could not be found or opened.",
            "target_not_detected": "Debugger could not detect the target.",
            "target_type_invalid": "pyOCD does not know the configured debuggers.<name>.target_type.",
            "flash_failed": "Debugger failed to flash the firmware.",
            "flash_erase_failed": "pyOCD could not erase a flash sector this image covers, so the flash contents are unconfirmed.",
            "verify_failed": "Debugger failed to verify the flashed firmware.",
            "reset_failed": "Debugger failed to reset the target.",
            "memory_read_failed": "Debugger failed to read the requested target memory.",
            "timeout": "Debugger command timed out.",
            "unknown_debugger_error": "Debugger failed with an unknown error.",
        }.get(error_type, "Debugger failed with an unknown error.")

    def _likely_causes(self, error_type: str) -> list[str]:
        return {
            "target_not_detected": ["DUT is not powered", "SWD/JTAG wiring issue", "debug probe already in use", "wrong debuggers.<name>.target_type for this device"],
            "adapter_not_found": ["debug probe is not connected", "debuggers.<name>.probe_id does not match a connected probe", "probe driver or udev rule is missing", "debug probe is already in use"],
            "target_type_invalid": ["debuggers.<name>.target_type is misspelled", "the target requires a CMSIS pack (pyocd pack install <type>)"],
            "verify_failed": ["flash write did not persist correctly", "firmware image does not match target memory layout"],
            "flash_failed": ["target flash is locked", "firmware image is invalid for this target", "debuggers.<name>.flash_address is wrong"],
            # The device-side causes #327 measured carry over unchanged; the
            # third is pyOCD's own, because the sector map pyOCD erases by comes
            # from the CMSIS pack behind debuggers.<name>.target_type, and a pack
            # for a near neighbour of this part erases at addresses the device
            # refuses.
            "flash_erase_failed": ["the sectors this image covers are protected (write protection, PCROP, or a read-out protection level that refuses the erase)", "the core was still executing from flash when the erase was issued", "debuggers.<name>.target_type resolves to a device whose flash sector map is not this device's"],
            "reset_failed": ["reset line wiring issue", "target is not responding"],
            # The address is the first suspect and the pack is the second: pyOCD
            # reads memory through the map its CMSIS pack describes, so a symbol
            # that resolved out of the ELF can still sit outside every region
            # this target_type declares.
            "memory_read_failed": ["the symbol's address is outside a memory region this target_type describes", "the core faulted or lost debug access while the read was running", "debuggers.<name>.target_type resolves to a device whose memory map is not this device's"],
            "timeout": ["debugger stopped responding", "debug probe or target is stuck", "timeout_s is too low for this operation"],
            "debugger_not_found": ["debuggers.<name>.executable is not configured", "pyOCD is not installed (install agentic-hil[pyocd] or pip install pyocd)", "pyocd is not in PATH"],
        }.get(error_type, ["inspect the debugger log for details"])


def normalise_target_type(value: str) -> str:
    """pyOCD's own normalisation of a target type name.

    Mirrors pyocd.target.normalise_target_type_name: characters outside
    ``[A-Za-z0-9_]`` collapse to a single underscore and the rest is lowercased,
    so ``STM32F446-RETx`` and ``stm32f446_retx`` are one target type. Comparing
    raw strings would report a value pyOCD resolves as unsupported, which is the
    one mistake this check must not make.
    """
    result: list[str] = []
    in_replace = False
    for character in value:
        if character in _TARGET_TYPE_NAME_CHARS:
            result.append(character.lower())
            in_replace = False
        elif not in_replace:
            result.append("_")
            in_replace = True
    return "".join(result)


def pyocd_command_path(path: Path) -> str | None:
    """A file path pyOCD's commander will read back as the path that was meant.

    `--command` is one string that pyOCD re-splits with its own tokenizer
    (`pyocd.utility.cmdline.split_command`, 0.45.1), not the shell's and not
    argparse's, and that tokenizer has two habits a Windows path walks straight
    into: an unquoted backslash escapes the next character, so `C:\\Users\\bench`
    arrives as `C:Usersbench`, and an unquoted space ends the word. Single
    quotes fix both, and are the right quote of the two the tokenizer accepts:
    inside double quotes it still honours backslash escapes, inside single
    quotes it does not.

    Forward slashes as well as the quotes, because they cost nothing and read
    back the same on both platforms.

    `None` when the path itself carries a single quote, which would end the
    quoted word early and point pyOCD at some other file. The caller refuses the
    read rather than sending it: a read that quietly wrote somewhere else would
    fail as an empty file at best and as somebody else's bytes at worst.
    """
    text = path.as_posix()
    if "'" in text:
        return None
    return f"'{text}'"


def pack_install_commands(target_type: str) -> list[str]:
    """The host setup commands that make a missing target type resolvable.

    Named, never run. `pyocd pack install` fetches from the vendor index over the
    network, and a background download nobody asked for is exactly what went
    wrong when this was undocumented: an agent hunting for the right value ended
    up pulling a .pdsc from a vendor site by hand, twice.
    """
    normalised = normalise_target_type(target_type)
    return [
        f"pyocd pack find {normalised}",
        f"pyocd pack install {normalised}",
        "pyocd pack show",
    ]


def parse_pyocd_targets(output: str) -> JsonObject:
    """Target types from `pyocd json --targets`, keyed by normalised name.

    Anything that is not exactly pyOCD's documented envelope is reported as a
    reason rather than as an empty list, because an empty list is the answer
    "nothing resolves", which would condemn a working configuration.
    """
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "reason": "`pyocd json --targets` returned output that is not JSON."}
    if not isinstance(payload, dict) or payload.get("status") != 0 or not isinstance(payload.get("targets"), list):
        return {"ok": False, "reason": "`pyocd json --targets` did not report a target list."}
    targets: JsonObject = {}
    for target in payload["targets"]:
        if not isinstance(target, dict) or not isinstance(target.get("name"), str) or not target["name"]:
            return {"ok": False, "reason": "`pyocd json --targets` returned a target record without a name."}
        targets[normalise_target_type(target["name"])] = {
            "name": target["name"],
            "vendor": target.get("vendor"),
            "part_number": target.get("part_number"),
            "source": target.get("source"),
        }
    if not targets:
        return {"ok": False, "reason": "`pyocd json --targets` reported no target types at all."}
    return {"ok": True, "targets": targets}


def parse_pyocd_probes(output: str) -> JsonObject:
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "error_type": "probe_discovery_failed", "summary": "pyOCD returned invalid probe-discovery JSON."}
    if not isinstance(payload, dict) or payload.get("status") != 0 or not isinstance(payload.get("boards"), list):
        return {"ok": False, "error_type": "probe_discovery_failed", "summary": "pyOCD reported a probe-discovery failure."}
    probe_ids: list[str] = []
    for board in payload["boards"]:
        if not isinstance(board, dict) or not isinstance(board.get("unique_id"), str) or not board["unique_id"]:
            return {"ok": False, "error_type": "probe_discovery_failed", "summary": "pyOCD returned an invalid probe record."}
        if board["unique_id"] not in probe_ids:
            probe_ids.append(board["unique_id"])
    return {"ok": True, "probes": [{"probe_id": probe_id} for probe_id in probe_ids]}
