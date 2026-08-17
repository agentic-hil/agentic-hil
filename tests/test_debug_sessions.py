from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest
from conftest import (
    FAKE_GDB,
    FAKE_STLINK,
    FAKE_STLINK_READ_UNCONFIRMED,
    FAKE_STLINK_SHORT_READ,
    elf_with_symbols,
    write_config,
)

from agentic_hil.backends.common import command_for_log
from agentic_hil.backends.gdbdebug import decode_symbol_value, image_byte_order
from agentic_hil.bench import BenchMutex
from agentic_hil.config import ConfigError, load_config
from agentic_hil.devices import debugger_device
from agentic_hil.elfsymbols import read_elf_byte_order, read_elf_symbol
from agentic_hil.gdbmi import GdbMiCommandResult, intel_hex_record, read_intel_hex_file, write_intel_hex_file
from agentic_hil.report import overall_success, read_last_report
from agentic_hil.tools import AgenticHILToolService

CTC_ARRAY_ADDRESS = 0x200006F0
CTC_ARRAY_SIZE = 408
# A typed 4-byte symbol, so a value read has an integer to decode, at an address
# whose top byte makes the signed and the unsigned reading differ.
BOOT_COUNTER_ADDRESS = 0x20000080
BOOT_COUNTER_SIZE = 4
# The issue's own example: an assembly-defined object the fake GDB refuses for
# both expressions, carried only by the symbol table of the ELF a test builds.
# Eight bytes rather than a real vector table's four hundred, so one test can
# show the fallback resolving it and the value tool decoding what it holds.
VECTOR_TABLE_ADDRESS = 0x08000000
VECTOR_TABLE_SIZE = 8
UNTYPED_SYMBOL_TABLE = [("g_pfnVectors", VECTOR_TABLE_ADDRESS, VECTOR_TABLE_SIZE)]
# A real ELF carrying the typed symbol the fake GDB also answers for, so a test
# can vary the one thing this table exists to vary: the EI_DATA byte in its
# header, which is where a value's integer readings get their byte order (#249).
BOOT_COUNTER_SYMBOL_TABLE = [("boot_counter", BOOT_COUNTER_ADDRESS, BOOT_COUNTER_SIZE)]
# What `image_byte_order` answers for an image that declares each order, for the
# tests that decode bytes without building a file to read it out of.
LITTLE_ENDIAN_IMAGE = {"byte_order": "little", "byte_order_from": "elf_header"}
BIG_ENDIAN_IMAGE = {"byte_order": "big", "byte_order_from": "elf_header"}
START_TIMEOUT_S = 10.0
# The fake gdb answers a hang by never replying, so any cap detects it. What the
# cap must not do is mistake a healthy reply for one, and at 0.1 s it did: on a
# loaded machine the pipe round-trip to the fake exceeded that, an earlier
# command timed out instead of the hanging one, and the run reported the wrong
# phase. These two tests then failed in a full suite run and passed alone.
TIMEOUT_TEST_CAP_S = 2.0


def artifact_bytes(fake_gdb_behavior: str | None = None, elf_symbols: list[tuple] | None = None, big_endian: bool = False) -> bytes:
    """The ELF a test hands the bench.

    Four bytes of magic by default, which is everything the artifact validator
    inspects and everything the fake GDB needs to answer from its own tables.
    `elf_symbols` builds a real one instead, for the tests that make the
    resolution read a symbol table rather than ask GDB (#187) and for the ones
    that need a real EI_DATA to decode a value against (#249); the behaviour
    marker rides along in both, past everything a section header describes.
    """
    trailer = b"" if fake_gdb_behavior is None else f"\nFAKE_GDB_BEHAVIOR={fake_gdb_behavior}\n".encode()
    if elf_symbols is None:
        return b"\x7fELF" + b"\x00" * 12 + trailer
    return elf_with_symbols(elf_symbols, big_endian=big_endian, trailer=trailer)


def debug_service(tmp_path: Path, fake_gdb_behavior: str | None = None, elf_symbols: list[tuple] | None = None, big_endian: bool = False, **config_kwargs) -> AgenticHILToolService:
    config_path = write_config(tmp_path, gdb_executable=FAKE_GDB, **config_kwargs)
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(artifact_bytes(fake_gdb_behavior, elf_symbols, big_endian))
    return AgenticHILToolService(load_config(str(config_path)))


def start_debug_session(service: AgenticHILToolService, mode: str = "load") -> dict:
    return service.call("debug_start_session", {"image_path": "build/app.elf", "mode": mode, "timeout_s": START_TIMEOUT_S})


def test_debug_session_full_cycle_breakpoint_symbol_and_ihex_dump(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        started = start_debug_session(service)
        assert started["ok"] is True, started
        assert started["session"]["status"] == "halted"
        assert started["mode"] == "load"

        status = service.call("debug_get_session_status")
        assert status["active"] is True
        assert status["status"] == "halted"

        breakpoint_result = service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})
        assert breakpoint_result["ok"] is True, breakpoint_result
        assert breakpoint_result["breakpoint"]["backend_id"] == "1"

        listed = service.call("debug_list_breakpoints")
        assert len(listed["breakpoints"]) == 1

        continued = service.call("debug_continue", {"timeout_s": 5})
        assert continued["ok"] is True, continued
        assert continued["stop_reason"] == "breakpoint_hit"
        assert continued["stop"]["breakpoint_id"] == 1
        assert continued["stop"]["frame"]["function"] == "test_done"
        assert continued["stop"]["frame"]["line"] == 123

        stop_reason = service.call("debug_get_stop_reason")
        assert stop_reason["ok"] is True
        assert stop_reason["stop_reason"] == "breakpoint_hit"

        symbol = service.call("debug_symbol_info", {"symbol": "CTC_array"})
        assert symbol["ok"] is True, symbol
        assert symbol["address"] == hex(CTC_ARRAY_ADDRESS)
        assert symbol["size_bytes"] == CTC_ARRAY_SIZE

        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/new/nested/memory.hex"})
        assert dumped["ok"] is True, dumped
        hex_lines = (tmp_path / "build" / "new" / "nested" / "memory.hex").read_text(encoding="ascii").splitlines()
        assert hex_lines[0] == ":020000042000DA"
        assert hex_lines[-1] == ":00000001FF"

        cleared = service.call("debug_clear_breakpoints")
        assert cleared["ok"] is True
        assert service.call("debug_list_breakpoints")["breakpoints"] == []

        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True
        assert stopped["status"] == "stopped"
        assert service.call("debug_get_session_status")["active"] is False
    finally:
        service.close()


def test_debug_halt_records_manual_halt_stop(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        halted = service.call("debug_halt", {"timeout_s": 5})
        assert halted["ok"] is True, halted
        assert halted["stop"]["stop_reason"] == "halted"
        assert halted["stop"]["backend_stop_reason"] == "signal-received"
        assert halted["stop"]["signal"]["name"] == "SIGINT"
    finally:
        service.close()


def test_debug_continue_reports_unexpected_breakpoint(tmp_path: Path) -> None:
    service = debug_service(tmp_path, fake_gdb_behavior="unexpected_breakpoint")
    try:
        assert start_debug_session(service)["ok"] is True
        breakpoint_result = service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})
        assert breakpoint_result["ok"] is True, breakpoint_result

        continued = service.call("debug_continue", {"timeout_s": 5})

        assert continued["ok"] is False, continued
        assert continued["error_type"] == "unexpected_breakpoint"
        assert continued["stop_reason"] == "unexpected_breakpoint"
        assert continued["stop"]["backend_breakpoint_id"] == "99"
        assert continued["stop"]["breakpoint_expected"] is False
        assert continued["stop"]["frame"]["function"] == "assert_failed"
        assert "debug_clear_breakpoints" in " ".join(continued["suggested_actions"])
        assert continued["session"]["status"] == "halted"
        assert service.call("debug_get_session_status")["target_ok"] is False
        assert service.call("debug_get_stop_reason")["target_ok"] is False
        classified = service.call("classify_last_error")
        assert classified["error_type"] == "unexpected_breakpoint"
        assert classified["source_tool"] == "debug_continue"
    finally:
        service.close()


def test_unconfirmed_debug_halt_quarantines_lease(tmp_path: Path) -> None:
    service = debug_service(tmp_path, fake_gdb_behavior="halt_timeout")
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True

        halted = service.call("debug_halt", {"timeout_s": 0.1})

        assert halted["ok"] is False
        assert halted["target_state"] == "unknown"
        assert halted["side_effect_status"] == "unknown"
        assert halted["cleanup_required"] is True
        assert service.coordinator.blocked is True
        cleared = service.call("debug_clear_breakpoints")
        assert cleared["cleanup_required"] is True
        assert service.coordinator.blocked is True
    finally:
        # Service shutdown now catches this closer to the fact than the lease
        # bookkeeping alone did: `close()` re-attempts the halt itself, finds
        # the same `halt_timeout` fake still refusing to confirm one, and
        # refuses to call that a clean stop before the lease release is ever
        # reached.
        with pytest.raises(RuntimeError, match="reconfirming the target was halted"):
            service.close()
        service.coordinator.close()


def test_debug_continue_reports_target_exception_context(tmp_path: Path) -> None:
    service = debug_service(tmp_path, fake_gdb_behavior="hardfault")
    try:
        assert start_debug_session(service)["ok"] is True

        continued = service.call("debug_continue", {"timeout_s": 5})

        assert continued["ok"] is False, continued
        assert continued["error_type"] == "target_exception"
        assert continued["stop_reason"] == "exception"
        assert continued["stop"]["exception_type"] == "hardfault"
        assert continued["stop"]["fault_type"] == "hardfault"
        assert continued["stop"]["signal"] == {"name": "SIGINT", "meaning": "Interrupt"}
        assert continued["stop"]["frame"]["function"] == "HardFault_Handler"
        assert continued["session"]["status"] == "halted"
    finally:
        service.close()


def test_debug_start_records_async_exception_stop(tmp_path: Path) -> None:
    service = debug_service(tmp_path, fake_gdb_behavior="stopped_on_attach_hardfault")
    try:
        started = start_debug_session(service, mode="attach")

        assert started["ok"] is True, started
        assert started["target_ok"] is False
        assert started["target_error_type"] == "target_exception"
        assert started["session"]["stop_reason"]["stop_reason"] == "exception"
        assert started["session"]["stop_reason"]["exception_type"] == "hardfault"
        status = service.call("debug_get_session_status")
        assert status["target_ok"] is False
        assert status["target_error_type"] == "target_exception"
        assert status["session"]["stop_reason"]["stop_reason"] == "exception"
    finally:
        service.close()


def test_debug_second_start_reports_session_already_active(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        second = start_debug_session(service)
        assert second["ok"] is False
        assert second["error_type"] == "session_already_active"
    finally:
        service.close()


def test_failed_debug_start_does_not_poison_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    from agentic_hil.backends import gdbdebug

    original_wait = gdbdebug.wait_for_tcp_port
    attempts = 0

    def fail_once(port: int, timeout_s: float, server) -> bool:
        nonlocal attempts
        attempts += 1
        return False if attempts == 1 else original_wait(port, timeout_s, server)

    monkeypatch.setattr(gdbdebug, "wait_for_tcp_port", fail_once)
    try:
        first = start_debug_session(service, mode="attach")
        second = start_debug_session(service, mode="attach")

        assert first["ok"] is False
        assert first.get("cleanup_required") is not True
        assert second["ok"] is True, second
    finally:
        service.close()


@pytest.mark.parametrize(
    ("behavior", "expected_status", "expected_phase", "timed_out"),
    [
        ("download_error", "partial", "download_started", False),
        ("post_load_reset_error", "partial", "post_load_reset_started", False),
        ("download_timeout", "unknown", "download_started", True),
        ("post_load_reset_timeout", "unknown", "post_load_reset_started", True),
    ],
)
def test_debug_load_failure_retains_quarantined_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
    expected_status: str,
    expected_phase: str,
    timed_out: bool,
) -> None:
    # readonly: a half-written flash is an effect no re-read can settle, so this
    # is the policy under which the incident must survive to the operator. The
    # reset_halt path is covered separately below.
    service = debug_service(tmp_path, fake_gdb_behavior=behavior, auto_recover="readonly")
    if timed_out:
        monkeypatch.setattr("agentic_hil.backends.gdbdebug.GDB_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    try:
        result = start_debug_session(service)

        assert result["ok"] is False, result
        assert result.get("side_effect_committed") is True, result
        assert result["side_effect_status"] == expected_status
        assert result["load_phase"] == expected_phase
        assert result["firmware_load_status"] in {"partial_or_unknown", "committed"}
        assert result["retry_safe"] is False
        assert result["hardware_state"] == "unknown"
        assert result["cleanup_required"] is True
        assert result["lease_state"] == "cleanup_required"
        assert result.get("command_timed_out", False) is timed_out
        assert service.coordinator.blocked is True
        # Was: the probe comes back `resource_quarantined`. It now runs — a
        # probe is the recovery class, and refusing the call that reads what
        # state the board is in was the padlock this release removed. What this
        # test is about survives untouched and is now asserted directly: a
        # failed load leaves a target that may be running a partial image, a
        # read-only re-read does not settle that, and the incident stays open.
        probe = service.call("probe_target")
        assert probe.get("error_type") != "resource_quarantined"
        assert "incident_resolved" not in probe
        assert service.coordinator.blocked is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_reset_halt_policy_settles_a_failed_load_without_running_the_partial_image(tmp_path: Path) -> None:
    service = debug_service(tmp_path, fake_gdb_behavior="download_error", auto_recover="reset_halt")
    modes: list[str] = []
    original_reset = service.backend.reset_target

    def recorded_reset(mode: str = "run") -> dict:
        modes.append(mode)
        return original_reset(mode)

    service.backend.reset_target = recorded_reset  # type: ignore[method-assign]
    try:
        failed = start_debug_session(service)
        assert failed["ok"] is False, failed
        assert failed["lease_state"] == "cleanup_required"

        # A mid-download failure is an ordinary development outcome. Under this
        # policy the owner drives the target into a defined halted state and
        # carries on; the operator is not the recovery mechanism for it.
        recovered = service.call("probe_target")

        assert recovered["ok"] is True, recovered
        assert service.coordinator.blocked is False
        # Halted only. Control is never handed back to a partially written image
        # — flashing and running it again stays an explicit, permissioned act.
        assert modes == ["halt"]
    finally:
        # Teardown is clean here precisely because the incident was settled: the
        # readonly parametrizations above still have to swallow a RuntimeError.
        service.close()


def test_attach_target_connect_timeout_retains_quarantined_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path, fake_gdb_behavior="target_select_timeout")
    monkeypatch.setattr("agentic_hil.backends.gdbdebug.GDB_COMMAND_TIMEOUT_CAP_S", TIMEOUT_TEST_CAP_S)
    try:
        result = start_debug_session(service, mode="attach")

        assert result["ok"] is False
        assert result["load_phase"] == "target_connect_started"
        assert result["side_effect_status"] == "unknown"
        assert result["cleanup_required"] is True
        assert result["lease_state"] == "cleanup_required"
        assert service.coordinator.blocked is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_debug_symbol_info_reports_missing_symbol(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("debug_symbol_info", {"symbol": "missing_symbol"})
        assert result["ok"] is False
        assert result["error_type"] == "symbol_not_found"
        assert result["side_effect_committed"] is False
        assert result.get("cleanup_required") is not True
        assert service.coordinator.blocked is False
    finally:
        service.close()


def test_service_close_persists_debug_shutdown_before_release(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    assert start_debug_session(service)["ok"] is True

    service.close()

    report = read_last_report(service.config)
    assert report["tool"] == "debug_stop_session"
    assert report["status"] == "stopped"
    assert report["lease_state"] == "active"
    assert service._debug_lease is None


def test_debug_symbol_allowlist_blocks_unlisted_symbols(tmp_path: Path) -> None:
    service = debug_service(tmp_path, allowed_symbols=["CTC_array"])
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("debug_symbol_info", {"symbol": "test_done"})
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
        allowed = service.call("debug_symbol_info", {"symbol": "CTC_array"})
        assert allowed["ok"] is True
    finally:
        service.close()


def test_debug_breakpoint_enforces_symbol_allowlist(tmp_path: Path) -> None:
    service = debug_service(tmp_path, allowed_symbols=["test_done"])
    try:
        assert start_debug_session(service)["ok"] is True

        rejected = service.call("debug_set_breakpoint", {"location": "not_allowed"})
        allowed = service.call("debug_set_breakpoint", {"location": {"function": "test_done"}})

        assert rejected["ok"] is False
        assert rejected["error_type"] == "permission_denied"
        assert allowed["ok"] is True
        assert [item["location"] for item in service.call("debug_list_breakpoints")["breakpoints"]] == [
            {"symbol": "test_done"}
        ]
    finally:
        service.close()


def test_debug_file_breakpoint_requires_allow_all_symbols(tmp_path: Path) -> None:
    restricted = debug_service(tmp_path, allowed_symbols=["test_done"])
    unrestricted = debug_service(tmp_path / "unrestricted", allow_all_symbols=True)
    try:
        assert start_debug_session(restricted)["ok"] is True
        rejected = restricted.call("debug_set_breakpoint", {"location": {"file": "main.c", "line": 12}})
        assert rejected["ok"] is False
        assert rejected["error_type"] == "permission_denied"
        assert restricted.call("debug_stop_session")["ok"] is True

        assert start_debug_session(unrestricted)["ok"] is True
        allowed = unrestricted.call("debug_set_breakpoint", {"location": {"file": "main.c", "line": 12}})
        assert allowed["ok"] is True
    finally:
        restricted.close()
        unrestricted.close()


def test_debug_dump_rejects_oversized_symbol(tmp_path: Path) -> None:
    service = debug_service(tmp_path, max_dump_size_bytes=16)
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
        assert result["max_dump_size_bytes"] == 16
    finally:
        service.close()


def test_debug_dump_rejects_output_outside_allowed_roots(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "outside/memory.hex"})
        assert result["ok"] is False
        assert result["error_type"] == "output_validation_failed"
        assert result["validation"]["allowed_root"] is False
    finally:
        service.close()


def test_debug_dump_startup_config_cannot_expand_allowed_roots(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        rejected = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "_CTCPP/memory.hex"})
        assert rejected["ok"] is False
        assert rejected["validation"]["allowed_root"] is False

        config_path = tmp_path / ".agentic-hil" / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace('allowed_roots: ["build"]', 'allowed_roots: ["build", "_CTCPP"]'), encoding="utf-8")

        still_rejected = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "_CTCPP/memory.hex"})
        assert still_rejected["ok"] is False
        assert still_rejected["validation"]["allowed_root"] is False
        assert not (tmp_path / "_CTCPP" / "memory.hex").exists()
    finally:
        service.close()


def test_debug_start_denied_while_raw_debugger_commands_allowed(tmp_path: Path) -> None:
    service = debug_service(tmp_path, permissions={"allow_raw_debugger_commands": True})
    try:
        result = start_debug_session(service, mode="attach")
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
    finally:
        service.close()


def test_debug_load_mode_requires_flash_permission(tmp_path: Path) -> None:
    service = debug_service(tmp_path, permissions={"allow_flash": False})
    try:
        result = start_debug_session(service, mode="load")
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
    finally:
        service.close()


def test_empty_symbol_allowlist_denies_all_symbols(tmp_path: Path) -> None:
    service = debug_service(tmp_path, allowed_symbols=[])
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("debug_symbol_info", {"symbol": "CTC_array"})
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
    finally:
        service.close()


def test_active_debug_log_symlink_is_rejected(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    log_path: Path | None = None
    audit_poisoned = False
    try:
        started = start_debug_session(service)
        assert started["ok"] is True
        log_path = tmp_path / started["log_path"]
        outside = tmp_path / "outside-debug-log.json"
        outside.write_text("unchanged\n", encoding="utf-8")
        log_path.unlink()
        try:
            log_path.symlink_to(outside)
        except OSError as error:
            pytest.skip(f"file symlinks unavailable: {error}")

        result = service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})

        assert result["ok"] is False
        assert result["error_type"] == "audit_broken"
        assert result["side_effect_committed"] is True
        assert result["side_effect_status"] == "unknown"
        assert result["audit_ok"] is False
        assert result["cleanup_required"] is True
        audit_poisoned = True
        assert result["retry_safe"] is False
        assert outside.read_text(encoding="utf-8") == "unchanged\n"
    finally:
        if log_path is not None and log_path.is_symlink():
            log_path.unlink()
        if audit_poisoned:
            with pytest.raises(RuntimeError):
                service.close()
            service.coordinator.close()
        else:
            service.close()


def test_debug_reset_modes_require_reset_permission(tmp_path: Path) -> None:
    service = debug_service(
        tmp_path,
        permissions={"allow_probe": True, "allow_flash": True, "allow_reset": False},
    )
    try:
        for mode in ["reset_halt", "load"]:
            result = start_debug_session(service, mode=mode)
            assert result["ok"] is False
            assert result["error_type"] == "permission_denied"
    finally:
        service.close()


def test_debug_tools_require_active_session(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        for tool, arguments in [
            ("debug_continue", {}),
            ("debug_halt", {}),
            ("debug_set_breakpoint", {"location": "test_done"}),
            ("debug_symbol_info", {"symbol": "CTC_array"}),
        ]:
            result = service.call(tool, arguments)
            assert result["ok"] is False, tool
            assert result["error_type"] == "session_not_active", tool
    finally:
        service.close()


def test_debug_stop_reports_cleanup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    assert start_debug_session(service, mode="attach")["ok"] is True
    session = service.backend._debug.session
    assert session is not None
    assert session.gdb is not None
    original_close = session.gdb.close

    def fail_close(timeout_s: float) -> None:
        raise RuntimeError(f"close failed after {timeout_s}")

    try:
        monkeypatch.setattr(session.gdb, "close", fail_close)
        result = service.call("debug_stop_session")

        assert result["ok"] is False
        assert result["error_type"] == "cleanup_failed"
        assert "close failed" in result["cleanup_error"]
        assert result["active"] is True
        assert result["status"] == "cleanup_required"
        assert result["hardware_state"] == "unknown"
        assert result["quarantined"] is True
        assert service._debug_artifact is not None

        monkeypatch.setattr(session.gdb, "close", original_close)
        retried = service.call("debug_stop_session")
        assert retried["ok"] is True
        assert service._debug_artifact is None
    finally:
        monkeypatch.setattr(session.gdb, "close", original_close)
        service.close()


def test_breakpoint_cleanup_retry_only_deletes_remaining_breakpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": "first"})["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": "second"})["ok"] is True
        debug = service.backend._debug
        original = debug._gdb_command
        failed_once = False
        deletes: list[str] = []

        def command(session, command: str, timeout_s=None, **kwargs):
            nonlocal failed_once
            if command.startswith("-break-delete"):
                deletes.append(command)
                if command.endswith(" 2") and not failed_once:
                    failed_once = True
                    return GdbMiCommandResult(result_class="error", line="", error_message="delete failed")
            return original(session, command, timeout_s, **kwargs)

        monkeypatch.setattr(debug, "_gdb_command", command)
        first = service.call("debug_clear_breakpoints")
        assert first["ok"] is False
        # Reconcile-first drives deletes from the backend list; the advisory local
        # cache is left intact on failure and only cleared once the backend is
        # confirmed empty, so both entries are still present after a failed clear.
        assert sorted(item["backend_id"] for item in debug.session.breakpoints) == ["1", "2"]

        second = service.call("debug_clear_breakpoints")
        assert second["ok"] is True
        assert deletes == ["-break-delete 1", "-break-delete 2", "-break-delete 2"]
        assert debug.session.breakpoints == []
        assert service.hardware_lease_status()["blocked"] is False
    finally:
        service.close()


def test_service_close_reports_debug_cleanup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    assert start_debug_session(service, mode="attach")["ok"] is True
    session = service.backend._debug.session
    assert session is not None
    assert session.gdb is not None
    original_close = session.gdb.close

    def fail_close(timeout_s: float) -> None:
        raise RuntimeError(f"close failed after {timeout_s}")

    monkeypatch.setattr(session.gdb, "close", fail_close)
    with pytest.raises(RuntimeError, match="Debug session cleanup failed"):
        service.close()

    # The first failure left the session status cleanup_required, so the
    # retry does not re-demand a halt reconfirmation through a connection
    # `_close_unlocked`'s own process-registry sweep already reaped out from
    # under it in the meantime: it goes straight to tidying up, exactly as a
    # retry after any other cleanup failure does.
    monkeypatch.setattr(session.gdb, "close", original_close)
    service.close()


def test_intel_hex_record_matches_reference_vectors() -> None:
    assert intel_hex_record(0, 0x04, bytes([0x20, 0x00])) == ":020000042000DA"


def test_write_intel_hex_file_emits_extended_address_and_eof(tmp_path: Path) -> None:
    output = tmp_path / "memory.hex"
    write_intel_hex_file(output, 0x200006F0, bytes(range(20)), workspace=tmp_path)
    lines = output.read_text(encoding="ascii").splitlines()
    assert lines[0] == ":020000042000DA"
    assert lines[1].startswith(":10" + "06F0" + "00")
    assert lines[-1] == ":00000001FF"


def test_write_intel_hex_file_refuses_a_path_outside_the_workspace(tmp_path: Path) -> None:
    # The dump write is the one primitive an agent aims at a path it chose, so
    # containment must hold at the write itself, not only in the caller's
    # earlier directory check.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    escaped = tmp_path / "outside.hex"

    with pytest.raises(ConfigError) as rejected:
        write_intel_hex_file(escaped, 0x20000000, b"\x00\x01", workspace=workspace)

    assert rejected.value.error_type == "unsafe_configured_path"
    assert not escaped.exists()


# --- reading a dump back ----------------------------------------------------
#
# STM32CubeProgrammer's only memory read writes a file, so the backend that has
# no debug session answers `debug_symbol_value` by parsing its own dump (#249).
# What these pin is that the parse is held to the window that was asked for:
# every record carries an address, so the bytes are taken by address, and a file
# that does not cover the window is a failed read rather than a short answer.


def test_a_dump_reads_back_as_exactly_the_bytes_it_was_written_from(tmp_path: Path) -> None:
    """Round trip through the writer beside it, across the 64 KiB boundary the
    extended-address record exists for."""
    output = tmp_path / "memory.hex"
    data = bytes((0x5A + index) & 0xFF for index in range(300))
    write_intel_hex_file(output, 0x0800FF00, data, workspace=tmp_path)

    assert read_intel_hex_file(output, 0x0800FF00, len(data)) == data


def test_a_dump_answers_the_window_that_was_asked_for_and_not_the_file(tmp_path: Path) -> None:
    """A CLI that rounds a one-byte read up to a word has still read that byte.

    Bytes outside the requested window are somebody else's business, so they are
    ignored rather than refused; what may not be ignored is a byte inside it.
    """
    output = tmp_path / "memory.hex"
    write_intel_hex_file(output, 0x20000080, b"\xde\xad\xbe\xef", workspace=tmp_path)

    assert read_intel_hex_file(output, 0x20000081, 2) == b"\xad\xbe"
    assert read_intel_hex_file(output, 0x20000080, 8) is None


@pytest.mark.parametrize(
    "lines",
    [
        [":00000001FF"],
        # A hole in the middle: the first and last bytes of the window arrive and
        # the one between them never does.
        [":020000042000DA", intel_hex_record(0x0080, 0x00, b"\x01"), intel_hex_record(0x0082, 0x00, b"\x03"), ":00000001FF"],
        # The same records, with the base record that puts them at 0x2000xxxx
        # dropped, so the whole window is missing rather than misread.
        [intel_hex_record(0x0080, 0x00, b"\x01\x02\x03"), ":00000001FF"],
        # One byte claimed twice, with two different values.
        [":020000042000DA", intel_hex_record(0x0080, 0x00, b"\x01\x02\x03"), intel_hex_record(0x0081, 0x00, b"\xff"), ":00000001FF"],
        # A record type that is none of the ones this understands: it may be
        # carrying the very bytes that were asked for, so it is refused rather
        # than skipped.
        [":020000042000DA", intel_hex_record(0x0080, 0x00, b"\x01\x02\x03"), ":0100000600F9", ":00000001FF"],
        # A checksum that does not add up, on the record holding the window.
        [":020000042000DA", ":03008000010203FF", ":00000001FF"],
        ["not a record at all"],
    ],
    ids=["empty", "hole", "wrong_address", "conflicting_bytes", "unknown_record_type", "bad_checksum", "not_intel_hex"],
)
def test_a_dump_that_is_not_the_whole_window_is_no_answer_at_all(tmp_path: Path, lines: list[str]) -> None:
    output = tmp_path / "memory.hex"
    output.write_text("\n".join(lines) + "\n", encoding="ascii")

    assert read_intel_hex_file(output, 0x20000080, 3) is None


def test_a_dump_that_is_not_there_is_no_answer_either(tmp_path: Path) -> None:
    assert read_intel_hex_file(tmp_path / "nothing.hex", 0x20000080, 4) is None


def record_mi_commands(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from agentic_hil.gdbmi import GdbMiClient

    recorded: list[str] = []
    original = GdbMiClient.command

    def wrapper(self, mi_command: str, timeout_s: float):
        recorded.append(mi_command)
        return original(self, mi_command, timeout_s)

    monkeypatch.setattr(GdbMiClient, "command", wrapper)
    return recorded


def latch_audit_break(service: AgenticHILToolService) -> None:
    service.backend._debug._audit_broken = OSError("injected audit failure")


INIT_COMMAND_PREFIXES = [
    "-gdb-set pagination off",
    "-gdb-set confirm off",
    "-file-exec-and-symbols",
    "-target-select",
    "-interpreter-exec",
    "-target-download",
    "-interpreter-exec",
]


@pytest.mark.parametrize("fail_at", list(range(1, len(INIT_COMMAND_PREFIXES) + 1)))
def test_audit_fault_after_each_init_command_stops_all_later_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int) -> None:
    service = debug_service(tmp_path)
    recorded = record_mi_commands(monkeypatch)
    from agentic_hil.backends import gdbdebug

    original_write = gdbdebug.write_audit_log
    write_calls = {"count": 0}

    def failing_write(config, path, text):
        write_calls["count"] += 1
        if write_calls["count"] >= fail_at:
            return OSError("injected audit write failure")
        return original_write(config, path, text)

    monkeypatch.setattr(gdbdebug, "write_audit_log", failing_write)
    try:
        result = start_debug_session(service)

        assert result["ok"] is False, result
        assert result["audit_ok"] is False
        assert result["cleanup_required"] is True
        executed = [command for command in recorded if not command.startswith("-gdb-exit")]
        assert len(executed) == fail_at, executed
        for command, prefix in zip(executed, INIT_COMMAND_PREFIXES[:fail_at], strict=False):
            assert command.startswith(prefix), (command, prefix)
        assert service.coordinator.blocked is True
        assert service.hardware_lease_status()["blocked"] is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_audit_break_blocks_status_effects_and_new_sessions_but_not_containment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        recorded = record_mi_commands(monkeypatch)
        latch_audit_break(service)

        status = service.call("debug_get_session_status")
        assert status["ok"] is False
        assert status["error_type"] == "audit_broken"
        assert status["audit_ok"] is False
        assert status["quarantined"] is True

        listed = service.call("debug_list_breakpoints")
        assert listed["ok"] is False
        assert listed["audit_ok"] is False

        continued = service.call("debug_continue", {"timeout_s": 1})
        assert continued["ok"] is False
        assert continued["error_type"] == "resource_quarantined"
        assert not any(command.startswith("-exec-continue") for command in recorded)

        symbol = service.call("debug_symbol_info", {"symbol": "CTC_array"})
        assert symbol["ok"] is False
        assert not any(command.startswith("-data-evaluate-expression") for command in recorded)

        halted = service.call("debug_halt", {"timeout_s": 1})
        assert halted["audit_ok"] is False
        assert overall_success(halted) is False
        assert any(command.startswith("-exec-interrupt") for command in recorded), "containment halt must still reach the target"
        assert service.coordinator.blocked is True, "containment must not lift the audit quarantine"

        restarted = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach"})
        assert restarted["ok"] is False
        assert not any(command.startswith("-file-exec-and-symbols") for command in recorded)
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_audit_break_during_continue_still_halts_the_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # F1 regression: if `-exec-continue` ACKs running and THEN the audit write
    # fails, the target must not be abandoned free-running. The session must
    # reach a halted stop (via the normal stop or the interrupt containment),
    # report audit_ok False, quarantine, and must NOT wedge in status "error".
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        from agentic_hil.backends import gdbdebug

        original_write = gdbdebug.write_audit_log
        armed = {"fail": False}

        def failing_write(config, path, text):
            if armed["fail"]:
                return OSError("injected audit write failure during continue")
            return original_write(config, path, text)

        monkeypatch.setattr(gdbdebug, "write_audit_log", failing_write)
        armed["fail"] = True

        continued = service.call("debug_continue", {"timeout_s": 5})

        assert continued["audit_ok"] is False
        assert overall_success(continued) is False
        debug = service.backend._debug
        # Not abandoned: the session settled on a real stop, never wedged in "error".
        assert debug.session.status == "halted"
        assert debug.session.stop_reason is not None
        assert service.coordinator.blocked is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_breakpoint_insert_ack_loss_requires_backend_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        debug = service.backend._debug
        original = debug._gdb_command

        def ack_losing_command(session, command: str, timeout_s=None, **kwargs):
            response = original(session, command, timeout_s, **kwargs)
            if command.startswith("-break-insert"):
                return GdbMiCommandResult(result_class="timeout", line="", timed_out=True, error_message="GDB/MI command timed out.")
            return response

        monkeypatch.setattr(debug, "_gdb_command", ack_losing_command)
        lost = service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})
        assert lost["ok"] is False
        assert lost["side_effect_status"] == "unknown"
        assert lost["cleanup_required"] is True
        assert lost["provisional_breakpoint"]["backend_id"] is None
        assert service.coordinator.blocked is True
        monkeypatch.setattr(debug, "_gdb_command", original)

        cleared = service.call("debug_clear_breakpoints")
        assert cleared["ok"] is True, cleared
        assert cleared["backend_reconciled"] is True
        # Reconciliation deletes exactly the one number the backend still reports;
        # the provisional local entry is dropped only after that confirms empty.
        assert cleared["cleared"] == 1
        assert service.coordinator.blocked is False
        assert service.call("debug_list_breakpoints")["breakpoints"] == []

        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()


def test_clear_breakpoints_tolerates_already_deleted_number_like_real_gdb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Real GDB answers `-break-delete N` for an already-removed N with an error,
    # unlike the idempotent fake. Reconciliation must treat that as deletion, not
    # a wedge: clear must still succeed once the backend list is empty.
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        debug = service.backend._debug
        original = debug._gdb_command
        listed_once = {"done": False}

        def real_gdb_semantics(session, command: str, timeout_s=None, **kwargs):
            if command.startswith("-break-list"):
                # First list still shows the breakpoint; after the (failed) delete,
                # report it gone so the confirmation pass sees an empty backend.
                if not listed_once["done"]:
                    listed_once["done"] = True
                    return original(session, command, timeout_s, **kwargs)
                return GdbMiCommandResult(result_class="done", line='^done,BreakpointTable={nr_rows="0",body=[]}')
            if command.startswith("-break-delete"):
                return GdbMiCommandResult(result_class="error", line="", error_message="No breakpoint number 1.")
            return original(session, command, timeout_s, **kwargs)

        monkeypatch.setattr(debug, "_gdb_command", real_gdb_semantics)
        cleared = service.call("debug_clear_breakpoints")
        assert cleared["ok"] is True, cleared
        assert cleared["backend_reconciled"] is True
        assert service.coordinator.blocked is False
        monkeypatch.setattr(debug, "_gdb_command", original)
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()


def test_breakpoint_done_without_parsable_backend_id_is_not_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        debug = service.backend._debug
        original = debug._gdb_command

        def unparsable_ack(session, command: str, timeout_s=None, **kwargs):
            response = original(session, command, timeout_s, **kwargs)
            if command.startswith("-break-insert"):
                return GdbMiCommandResult(result_class="done", line="^done")
            return response

        monkeypatch.setattr(debug, "_gdb_command", unparsable_ack)
        result = service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})
        assert result["ok"] is False
        assert result["side_effect_status"] == "unknown"
        assert result["cleanup_required"] is True
        assert result["provisional_breakpoint"]["backend_id"] is None
        monkeypatch.setattr(debug, "_gdb_command", original)

        cleared = service.call("debug_clear_breakpoints")
        assert cleared["ok"] is True, cleared
        assert cleared["backend_reconciled"] is True
        assert service.coordinator.blocked is False
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()


def test_breakpoint_delete_ack_loss_keeps_quarantine_until_reconciled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        debug = service.backend._debug
        original = debug._gdb_command
        lost_once = {"done": False}

        def delete_ack_loss(session, command: str, timeout_s=None, **kwargs):
            response = original(session, command, timeout_s, **kwargs)
            if command.startswith("-break-delete") and not lost_once["done"]:
                lost_once["done"] = True
                return GdbMiCommandResult(result_class="timeout", line="", timed_out=True, error_message="GDB/MI command timed out.")
            return response

        monkeypatch.setattr(debug, "_gdb_command", delete_ack_loss)
        first = service.call("debug_clear_breakpoints")
        assert first["ok"] is False
        assert first["cleanup_required"] is True
        assert service.coordinator.blocked is True
        monkeypatch.setattr(debug, "_gdb_command", original)

        second = service.call("debug_clear_breakpoints")
        assert second["ok"] is True, second
        assert second["backend_reconciled"] is True
        assert service.coordinator.blocked is False
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()


def test_audit_fault_between_breakpoint_deletes_stops_remaining_deletes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        recorded = record_mi_commands(monkeypatch)
        from agentic_hil.backends import gdbdebug

        original_write = gdbdebug.write_audit_log
        write_state = {"fail": False}

        def failing_write(config, path, text):
            if write_state["fail"]:
                return OSError("injected audit write failure")
            return original_write(config, path, text)

        monkeypatch.setattr(gdbdebug, "write_audit_log", failing_write)
        write_state["fail"] = True

        result = service.call("debug_clear_breakpoints")
        assert result["ok"] is False
        assert result["audit_ok"] is False
        # Reconcile-first reads `-break-list` before any delete; its post-write
        # audit break refuses that read, so no `-break-delete` ever runs.
        deletes = [command for command in recorded if command.startswith("-break-delete")]
        assert len(deletes) == 0, "no delete may run once the reconciliation read breaks audit"
        assert service.coordinator.blocked is True
    finally:
        with pytest.raises(RuntimeError):
            service.close()
        service.coordinator.close()


def test_debug_stop_release_persist_fault_converges_on_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A transient persist fault during a debug lease release must leave the lease
    # retryable, and a second debug_stop_session must converge (not stick until
    # process restart).
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        coordinator = service.coordinator
        original_write = coordinator._write_record
        faults = {"remaining": 1}

        def flaky_write(resource: str, record: dict) -> None:
            if record.get("state") == "released" and faults["remaining"] > 0:
                faults["remaining"] -= 1
                raise OSError("injected release persist fault")
            original_write(resource, record)

        monkeypatch.setattr(coordinator, "_write_record", flaky_write)
        first = service.call("debug_stop_session")
        assert first["ok"] is False
        assert coordinator.blocked is True

        second = service.call("debug_stop_session")
        assert second["ok"] is True, second
        assert coordinator.blocked is False
    finally:
        service.close()


def test_clear_breakpoints_missing_delete_does_not_poison_next_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A tolerated "No breakpoint number N" delete must not clobber the target's
    # stop reason to debugger_error, which would spuriously fail the next continue.
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        continued = service.call("debug_continue", {"timeout_s": 5})
        assert continued["ok"] is True
        assert continued["stop_reason"] == "breakpoint_hit"

        debug = service.backend._debug
        original = debug._gdb_command
        listed_once = {"done": False}

        def real_gdb_semantics(session, command: str, timeout_s=None, **kwargs):
            if command.startswith("-break-list"):
                if not listed_once["done"]:
                    listed_once["done"] = True
                    return original(session, command, timeout_s, **kwargs)
                return GdbMiCommandResult(result_class="done", line='^done,BreakpointTable={nr_rows="0",body=[]}')
            if command.startswith("-break-delete"):
                return GdbMiCommandResult(result_class="error", line="", error_message="No breakpoint number 1.")
            return original(session, command, timeout_s, **kwargs)

        monkeypatch.setattr(debug, "_gdb_command", real_gdb_semantics)
        cleared = service.call("debug_clear_breakpoints")
        assert cleared["ok"] is True, cleared
        monkeypatch.setattr(debug, "_gdb_command", original)

        # The stop reason must be preserved, not poisoned to debugger_error.
        assert str(debug.session.stop_reason.get("stop_reason")) != "debugger_error"
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()


# Every class of unusable `timeout_s`, and why each one is worth a case.
# The numeric string and the bool are the load-bearing pair: `float("5")` is 5.0
# and `float(True)` is 1.0, so plain coercion accepts both without complaint, and
# the point here is that the gate does not. `1e400` is the one the stdio JSON
# hook does not catch — it parses to `inf` rather than firing `parse_constant` —
# so refusing it is `find_nonfinite`'s job, and the case is what says so.
INVALID_TIMEOUT_VALUES = [
    ("non_numeric_string", "abc"),
    ("numeric_string", "5"),
    ("bool", True),
    ("null", None),
    ("negative", -1),
    ("overflow_to_infinity", float("1e400")),
    ("nan", float("nan")),
    ("list", [1]),
]

TIMEOUT_TOOLS = ("debug_start_session", "debug_stop_session", "debug_continue", "debug_halt")


def timeout_arguments(tool: str, value: object) -> dict:
    arguments: dict = {"timeout_s": value}
    if tool == "debug_start_session":
        # This tool additionally requires exactly one of image_path/artifact_id.
        # That failure sits at the document root, sorts ahead of one at
        # `timeout_s`, and would leave the refusal naming a different field, so
        # the artifact is supplied and the timeout is the only thing wrong.
        arguments["image_path"] = "build/app.elf"
    return arguments


@pytest.mark.parametrize(("label", "value"), INVALID_TIMEOUT_VALUES, ids=[label for label, _ in INVALID_TIMEOUT_VALUES])
def test_invalid_timeout_is_refused_as_invalid_argument(tmp_path: Path, label: str, value: object) -> None:
    """A `timeout_s` that cannot be used is refused, on every tool that takes one.

    This pins behaviour that was already correct rather than changing it
    (the report read the swallowing coercion in `number_argument` as
    the shipped answer). The refusal comes from the schema gate in `call`,
    before `number_argument` is reached, so no unusable value ever reaches a
    backend as a dropped `None` — which a backend cannot tell apart from the
    argument having been omitted, and would answer by silently substituting the
    configured default for the cap the caller asked for.

    Driven through `call` because that is the only entry point an MCP host can
    reach, and the three fields asserted are what it reads.
    """
    service = debug_service(tmp_path)
    try:
        for tool in TIMEOUT_TOOLS:
            result = service.call(tool, timeout_arguments(tool, value))
            assert result["ok"] is False, (tool, label, result)
            assert result["error_type"] == "invalid_argument", (tool, label, result)
            assert result["field"] == "timeout_s", (tool, label, result)
    finally:
        service.close()


def test_usable_timeout_is_not_refused_as_invalid_argument(tmp_path: Path) -> None:
    """The positive control for the refusals above.

    Without it a gate that rejected every `timeout_s` would satisfy them. These
    three still fail on their own terms — there is no session to stop, continue
    or halt — but not for the shape of the argument. `debug_start_session` is
    left out on purpose: the only way it gets past this point is by starting a
    real session, which is not what this test is for.
    """
    service = debug_service(tmp_path)
    try:
        for tool in ("debug_stop_session", "debug_continue", "debug_halt"):
            result = service.call(tool, {"timeout_s": 5})
            assert result.get("error_type") != "invalid_argument", (tool, result)
    finally:
        service.close()


def test_direct_debug_call_reenters_validation_for_invalid_timeout(tmp_path: Path) -> None:
    """Calling the method directly is refused exactly as calling through `call` is.

    Each of these four opens with a `_dispatch_depth == 0` guard that routes a
    call arriving from outside back through `call`, which is where arguments are
    validated. That guard is the whole reason the schema gate cannot be walked
    around, and nothing else pins it: without it the payload would reach
    `number_argument` unchecked and `"abc"` would become `None` on its way to the
    backend.
    """
    service = debug_service(tmp_path)
    try:
        results = {
            "debug_start_session": service.debug_start_session({"image_path": "build/app.elf", "timeout_s": "abc"}),
            "debug_stop_session": service.debug_stop_session({"timeout_s": "abc"}),
            "debug_continue": service.debug_continue({"timeout_s": "abc"}),
            "debug_halt": service.debug_halt({"timeout_s": "abc"}),
        }
    finally:
        service.close()
    for tool, result in results.items():
        assert result["ok"] is False, (tool, result)
        assert result["error_type"] == "invalid_argument", (tool, result)
        assert result["field"] == "timeout_s", (tool, result)


# --- the stlink backend's route to the same dump ---------------------------
#
# STM32CubeProgrammer reads target memory without a GDB session, so
# `debug_dump_symbol_ihex` is served on that backend too. What these pin is that
# a caller cannot tell which backend answered: the same arguments, the same
# refusals, the same success fields. The differences that remain are the ones
# honesty forces — where the symbol table comes from, and that a read whose
# confirmation line never printed says so.


def stlink_dump_service(tmp_path: Path, *, debugger_executable: Path = FAKE_STLINK, elf_symbols: list[tuple] | None = None, big_endian: bool = False, **config_kwargs) -> AgenticHILToolService:
    config_path = write_config(
        tmp_path,
        debugger_type="stlink",
        debugger_executable=debugger_executable,
        gdb_executable=FAKE_GDB,
        **config_kwargs,
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(artifact_bytes(elf_symbols=elf_symbols, big_endian=big_endian))
    return AgenticHILToolService(load_config(str(config_path)))


def flash_symbol_source(service: AgenticHILToolService) -> dict:
    return service.call("flash_firmware", {"image_path": "build/app.elf"})


def test_stlink_dump_reads_target_memory_and_writes_intel_hex(tmp_path: Path) -> None:
    """The whole route, end to end, on the backend that has no debug session.

    The ELF is flashed first because that is what makes a symbol table describe
    the board: the address this resolves is read out of the image the target is
    running, not out of some file that happened to be lying in the workspace.
    """
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/new/nested/memory.hex"})
    finally:
        service.close()

    assert dumped["ok"] is True, dumped
    assert dumped["backend"] == "stlink"
    assert dumped["symbol"] == "CTC_array"
    assert dumped["address"] == hex(CTC_ARRAY_ADDRESS)
    assert dumped["size_bytes"] == CTC_ARRAY_SIZE
    assert dumped["summary"] == "Symbol memory dumped as Intel HEX."
    # Which image answered the symbol, so an operator reading the result can
    # tell whether the address belongs to the build on the board.
    assert dumped["symbol_source"]["path"] == "build/app.elf"
    hex_lines = (tmp_path / "build" / "new" / "nested" / "memory.hex").read_text(encoding="ascii").splitlines()
    assert hex_lines[0] == ":020000042000DA"
    assert hex_lines[-1] == ":00000001FF"


def test_stlink_dump_sends_the_measured_read_invocation(tmp_path: Path) -> None:
    """`-r <address> <size> <file>` behind the reset family's connect arguments.

    The address is hex and the size decimal because that is the form the CLI was
    measured accepting; the connect block is `mode=NORMAL` because a read is
    addressed to a target the way a reset is, not with the HOTPLUG connect a
    bare probe uses.
    """
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert dumped["ok"] is True, dumped
    logged = json.loads((tmp_path / dumped["log_path"]).read_text(encoding="utf-8"))["command"]
    assert logged.endswith(
        command_for_log(["-c", "port=SWD", "mode=NORMAL", "-r", hex(CTC_ARRAY_ADDRESS), str(CTC_ARRAY_SIZE), str(tmp_path / "build" / "memory.hex")])
    )


def test_stlink_dump_confirms_success_only_on_the_measured_read_line(tmp_path: Path) -> None:
    """A CLI that connected, printed its banner and never confirmed the read.

    `Data read successfully` is the only line that means the bytes arrived. The
    connect banner is not a substitute: this fixture prints the ST-Link serial,
    the device name and the whole UPLOADING block, and none of that may be read
    as a confirmed read. The result is also not allowed to call itself harmless
    — the probe was attached to a live core and the CLI never said where it
    stopped — so the side effect is unknown rather than not_started.
    """
    service = stlink_dump_service(tmp_path, debugger_executable=FAKE_STLINK_READ_UNCONFIRMED)
    try:
        assert flash_symbol_source(service)["ok"] is True
        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert dumped["ok"] is False, dumped
    assert dumped["error_type"] == "memory_read_failed"
    assert dumped["backend_error_type"] == "memory_read_unconfirmed"
    assert dumped["operation_result"] == {"confirmed": False, "expected_success_text": ["Data read successfully"], "matched_success_text": []}
    assert dumped["side_effect_status"] == "unknown"
    assert dumped["retry_safe"] is False
    assert dumped.get("target_contacted") is not False
    assert not (tmp_path / "build" / "memory.hex").exists()


def test_stlink_dump_refuses_a_symbol_the_allowlist_does_not_carry(tmp_path: Path) -> None:
    """The OpenOCD path's refusal, word for word, before anything is spawned."""
    service = stlink_dump_service(tmp_path, allowed_symbols=["other_symbol"])
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied"
    assert refused["summary"] == "Symbol is not allowed by debug.allowed_symbols."
    assert refused["symbol"] == "CTC_array"
    assert not (tmp_path / "build" / "memory.hex").exists()


def test_stlink_dump_refuses_a_symbol_larger_than_the_configured_cap(tmp_path: Path) -> None:
    """`debug.max_dump_size_bytes` is enforced on the resolved size, not on a guess.

    The cap is checked after the offline query and before the probe is opened,
    so an oversized symbol costs a refusal rather than a partial read.
    """
    service = stlink_dump_service(tmp_path, max_dump_size_bytes=CTC_ARRAY_SIZE - 1)
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied"
    assert refused["summary"] == "Symbol dump exceeds debug.max_dump_size_bytes."
    assert refused["size_bytes"] == CTC_ARRAY_SIZE
    assert refused["max_dump_size_bytes"] == CTC_ARRAY_SIZE - 1
    assert not (tmp_path / "build" / "memory.hex").exists()


def test_stlink_dump_names_the_gdb_it_needs_when_the_bench_has_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bench with no GDB is told which field to set, not that the symbol is bad.

    The offline query is the one thing this route needs that the vendor CLI does
    not provide, so its absence has to be named. With `debug.gdb_executable`
    unset and nothing autodetectable on PATH, config load pins the disabled
    placeholder and the dump refuses before it opens the probe.
    """
    monkeypatch.setenv("PATH", "")
    config_path = write_config(tmp_path, debugger_type="stlink", gdb_executable=None)
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    service = AgenticHILToolService(load_config(str(config_path)))
    try:
        service._symbol_elf = service.artifacts.validate_local_path("build/app.elf")["artifact"]
        refused = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "gdb_not_found"
    assert refused["summary"] == "No GDB executable could be found."
    assert "set debug.gdb_executable in the authoritative project config" in refused["likely_causes"]
    assert refused["symbol"] == "CTC_array"
    assert refused["target_contacted"] is False
    assert refused["side_effect_status"] == "not_started"
    assert not (tmp_path / "build" / "memory.hex").exists()


def test_stlink_dump_refuses_before_a_symbol_table_describes_the_target(tmp_path: Path) -> None:
    """No flashed ELF, no answer — and the refusal says what is missing.

    A session gets its symbol table by loading the image; this backend has no
    session, so the only image it may resolve against is the one it put on the
    board. Guessing at a file in the workspace would answer with an address that
    is right about a build and wrong about the target.
    """
    service = stlink_dump_service(tmp_path)
    try:
        refused = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "symbol_source_not_available"
    assert "flash_firmware" in refused["summary"]
    assert refused["target_contacted"] is False
    assert not (tmp_path / "build" / "memory.hex").exists()


def test_remember_symbol_elf_keeps_only_the_source_still_proven_on_the_target(tmp_path: Path) -> None:
    """The decision table a flash leaves behind for the offline symbol source.

    A confirmed ELF flash is the only thing that makes a symbol table describe
    the board, so it is the only thing that becomes the source. A confirmed
    `.hex`/`.bin` puts an image with no symbols on the board and drops it; an
    unconfirmed flash that may have written part of a new image drops it too. The
    one failure that keeps the previous ELF is a flash that provably never
    reached the target, because then the board still runs what it ran before.
    """
    service = stlink_dump_service(tmp_path)
    try:
        elf = service.artifacts.validate_local_path("build/app.elf")["artifact"]
        binary = {"resolved_path": str(tmp_path / "build" / "app.bin"), "path": "build/app.bin"}

        service._remember_symbol_elf(elf, {"ok": True})
        assert service._symbol_elf is elf

        # A confirmed non-ELF flash replaces the image with one carrying no symbols.
        service._remember_symbol_elf(binary, {"ok": True})
        assert service._symbol_elf is None

        # An unconfirmed flash may have landed part of a new image: drop it.
        service._remember_symbol_elf(elf, {"ok": True})
        service._remember_symbol_elf(elf, {"ok": False, "side_effect_status": "unknown"})
        assert service._symbol_elf is None

        # A flash that provably never reached the target leaves the board — and
        # so the remembered ELF — as it was.
        service._remember_symbol_elf(elf, {"ok": True})
        service._remember_symbol_elf(binary, {"ok": False, "side_effect_status": "not_started"})
        assert service._symbol_elf is elf
    finally:
        service.close()


def test_stlink_dump_refuses_a_source_that_no_longer_matches_the_flashed_image(tmp_path: Path) -> None:
    """A rebuild that overwrites the flashed ELF must not be read as the target.

    The remembered path is the caller's own workspace file, and a build system
    is free to replace it between the flash and this read. Resolving against the
    new bytes while reporting the flashed image's provenance would answer with an
    address that is right about the new build and wrong about the board, so the
    dump refuses until the current build is flashed.
    """
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        # The ELF is rebuilt after it was flashed: same path, different bytes.
        (tmp_path / "build" / "app.elf").write_bytes(b"\x7fELF" + b"\x01" * 64)
        refused = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "symbol_source_changed"
    assert "flash_firmware" in refused["summary"]
    assert refused["target_contacted"] is False
    assert refused["side_effect_status"] == "not_started"
    assert not (tmp_path / "build" / "memory.hex").exists()


def test_flash_that_raises_drops_the_previously_trusted_symbol_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A flash that raises may have changed the image, so the old ELF is dropped.

    ``_remember_symbol_elf`` runs only on a returned result; a backend that raises
    after it may have contacted or partly written the target never reaches it. The
    coordination wrapper reports the effect as unknown, so the previously trusted
    ELF — which a later ST-Link dump would resolve a symbol against — must not
    survive a flash whose outcome is unproven, or a symbol could resolve against a
    build the failed flash may have replaced.
    """
    service = stlink_dump_service(tmp_path)
    try:
        # A prior confirmed ELF flash makes it this bench's symbol source.
        assert flash_symbol_source(service)["ok"] is True
        assert service._symbol_elf is not None

        def raising_flash(artifact: dict, reset_after_flash: bool = False) -> dict:
            raise RuntimeError("link lost mid-write")

        monkeypatch.setattr(service.backend, "flash_firmware", raising_flash)
        result = service.call("flash_firmware", {"image_path": "build/app.elf"})
    finally:
        service.close()

    assert result["ok"] is False, result
    # The dispatch wrapper reports the physical effect as unknown, exactly the
    # state in which the old source must not be left trusted.
    assert result["side_effect_status"] == "unknown"
    assert service._symbol_elf is None


def test_stlink_dump_resolves_from_an_immutable_copy_not_the_racing_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rebuild landing after the digest check must not reach the GDB query.

    The flashed ELF is hashed and then a separately spawned GDB is pointed at it;
    a rebuild replacing the workspace file in that window would make GDB answer
    from bytes the digest never covered while the result still carried the flashed
    image's digest. The resolution copies the verified bytes into a private file
    first, so a replacement of the caller's path lands too late to be read.
    """
    import agentic_hil.backends.stlink as stlink_module

    service = stlink_dump_service(tmp_path)
    workspace_elf = tmp_path / "build" / "app.elf"
    flashed_bytes = workspace_elf.read_bytes()
    captured: dict = {}

    real_sha256_file = stlink_module.sha256_file

    def racing_sha256_file(path: Path) -> str:
        digest = real_sha256_file(path)
        # A rebuild lands in the window the digest check used to leave open.
        workspace_elf.write_bytes(b"\x7fELF" + b"\x22" * 64)
        return digest

    real_spawn_command = stlink_module.spawn_command

    def capturing_spawn_command(command, *args, **kwargs):
        elf_arg = next((item for item in command if isinstance(item, str) and item.endswith(".elf")), None)
        if elf_arg is not None:
            captured["gdb_elf_path"] = Path(elf_arg)
            captured["gdb_elf_bytes"] = Path(elf_arg).read_bytes()
        return real_spawn_command(command, *args, **kwargs)

    try:
        assert flash_symbol_source(service)["ok"] is True
        # Only the offline resolution, not the flash, races the rebuild.
        monkeypatch.setattr(stlink_module, "sha256_file", racing_sha256_file)
        monkeypatch.setattr(stlink_module, "spawn_command", capturing_spawn_command)
        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert dumped["ok"] is True, dumped
    # GDB read the flashed image, not the rebuild that raced the resolution.
    assert captured["gdb_elf_bytes"] == flashed_bytes
    # And it was handed a private copy, not the mutable workspace path.
    assert captured["gdb_elf_path"] != workspace_elf
    # The provenance the result reports is now honest about what GDB read.
    assert dumped["symbol_source"]["sha256"] == hashlib.sha256(flashed_bytes).hexdigest()
    # The rebuild really did land; the copy is what made it harmless.
    assert workspace_elf.read_bytes() != flashed_bytes


def test_stlink_dump_does_not_open_the_typed_debug_session_family(tmp_path: Path) -> None:
    """One tool crossed over; the rest of the family still refuses and says why."""
    service = stlink_dump_service(tmp_path)
    try:
        results = {
            "debug_start_session": service.call("debug_start_session", {"image_path": "build/app.elf"}),
            "debug_set_breakpoint": service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}}),
            "debug_symbol_info": service.call("debug_symbol_info", {"symbol": "CTC_array"}),
        }
    finally:
        service.close()

    for tool, result in results.items():
        assert result["ok"] is False, (tool, result)
        assert result["error_type"] == "not_supported", (tool, result)
        assert result["summary"] == "Typed debug sessions require the OpenOCD backend.", (tool, result)


def test_openocd_dump_still_resolves_through_its_own_session(tmp_path: Path) -> None:
    """The service now carries a flashed ELF; the OpenOCD path must ignore it.

    A session has the image loaded, so its symbol table is the target's by
    construction. Resolving against the service's ELF instead would answer out
    of whatever was flashed last, which is not necessarily what the session
    attached to.
    """
    service = debug_service(tmp_path)
    try:
        assert service.call("flash_firmware", {"image_path": "build/app.elf"})["ok"] is True
        assert service._symbol_elf is not None
        assert start_debug_session(service)["ok"] is True
        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
        assert service.call("debug_stop_session")["ok"] is True
    finally:
        service.close()

    assert dumped["ok"] is True, dumped
    assert dumped["backend"] == "openocd"
    assert dumped["address"] == hex(CTC_ARRAY_ADDRESS)
    assert dumped["size_bytes"] == CTC_ARRAY_SIZE
    assert dumped["summary"] == "Symbol memory dumped as Intel HEX."
    # The session answered, so no ELF of the service's travels in the result.
    assert "symbol_source" not in dumped
    assert (tmp_path / "build" / "memory.hex").read_text(encoding="ascii").splitlines()[0] == ":020000042000DA"


# --- the symbol table behind the debug information --------------------------
#
# Offline resolution asks GDB's expression evaluator for `&symbol` and
# `sizeof(symbol)`, and both need a typed symbol. An object the assembler
# defined with `.global`/`.type`/`.size` and no DWARF has no type for either,
# while its address and size sit in the ELF's symbol table (#187). What these
# pin is that the second route answers where the first cannot, that it narrows
# no permission on the way, and that a symbol neither route carries is still
# refused.


def fake_target_bytes(address: int, size: int) -> bytes:
    """What the fake GDB returns for a memory read: one byte per address, the low
    byte of the address itself."""
    return bytes((address + index) & 0xFF for index in range(size))


def read_elf_symbol_from_bytes(tmp_path: Path, data: bytes, symbol: str) -> dict:
    path = tmp_path / "symbols.elf"
    path.write_bytes(data)
    return read_elf_symbol(path, symbol)


def test_elf_symbol_table_answers_address_and_size(tmp_path: Path) -> None:
    table = elf_with_symbols([("g_pfnVectors", 0x08000000, 428), ("main", 0x080001C0, 64)])

    assert read_elf_symbol_from_bytes(tmp_path, table, "g_pfnVectors") == {"ok": True, "address": 0x08000000, "size_bytes": 428}
    assert read_elf_symbol_from_bytes(tmp_path, table, "main") == {"ok": True, "address": 0x080001C0, "size_bytes": 64}


@pytest.mark.parametrize(("bits", "big_endian"), [(32, False), (32, True), (64, False), (64, True)])
def test_elf_symbol_table_is_read_in_the_class_and_order_the_file_declares(tmp_path: Path, bits: int, big_endian: bool) -> None:
    """Four layouts, one answer. ELF32 and ELF64 order a symbol record
    differently and either can be written LSB or MSB first, so the reader is held
    to the header's own claim rather than to the one shape a Cortex-M toolchain
    happens to emit."""
    table = elf_with_symbols([("g_pfnVectors", 0x08000000, 428)], bits=bits, big_endian=big_endian)

    assert read_elf_symbol_from_bytes(tmp_path, table, "g_pfnVectors") == {"ok": True, "address": 0x08000000, "size_bytes": 428}


@pytest.mark.parametrize(
    ("entries", "symbol", "reason"),
    [
        ([("main", 0x080001C0, 64)], "g_pfnVectors", "not_found"),
        # A bare assembler label: in the table, with no `.size` behind it. A
        # zero-length read is not an answer, so this stays a refusal.
        ([("g_pfnVectors", 0x08000000, 0)], "g_pfnVectors", "no_size"),
        # An undefined symbol has a name and no definition; answering from it
        # would hand back an address that is nowhere on the target.
        ([("g_pfnVectors", 0x08000000, 428, 0)], "g_pfnVectors", "not_found"),
        # Two definitions of one name: a file-order first match would be a board
        # address chosen by luck.
        ([("g_pfnVectors", 0x08000000, 428), ("g_pfnVectors", 0x08010000, 428)], "g_pfnVectors", "ambiguous"),
    ],
    ids=["absent", "no_size_directive", "undefined", "two_definitions"],
)
def test_elf_symbol_table_refuses_what_it_cannot_answer(tmp_path: Path, entries: list[tuple], symbol: str, reason: str) -> None:
    assert read_elf_symbol_from_bytes(tmp_path, elf_with_symbols(entries), symbol) == {"ok": False, "reason": reason}


@pytest.mark.parametrize(
    "data",
    [b"", b"\x7fELF", b"not an elf at all", b"\x7fELF" + b"\x00" * 12, b"\x7fELF\x01\x01\x01" + b"\xff" * 60],
    ids=["empty", "magic_only", "not_elf", "magic_and_zeroes", "unparsable_section_table"],
)
def test_elf_symbol_table_reports_a_file_it_cannot_parse_rather_than_raising(tmp_path: Path, data: bytes) -> None:
    """Including the four-byte stand-in the rest of this suite writes. A file this
    cannot read has to come back as a reason, never as a traceback: the caller is
    holding GDB's own refusal and has to be able to report it."""
    assert read_elf_symbol_from_bytes(tmp_path, data, "g_pfnVectors") == {"ok": False, "reason": "unreadable"}


def test_elf_symbol_table_reports_a_missing_file_rather_than_raising(tmp_path: Path) -> None:
    assert read_elf_symbol(tmp_path / "nothing.elf", "g_pfnVectors") == {"ok": False, "reason": "unreadable"}


def test_elf_symbol_table_rejects_a_string_table_size_the_file_cannot_hold(tmp_path: Path) -> None:
    """A hostile `sh_size` on the linked string table must never reach `read()`.

    CPython raises `OverflowError` for a `read()` size past `sys.maxsize`, and a
    smaller-but-still-enormous size can force a process-sized allocation before
    the file is even found too short to hold it. `sh_size` is ELF-controlled — a
    64-bit field can claim up to 2**64-1 — so the bound has to sit before the
    read, not in the exception it would otherwise raise.
    """
    table = elf_with_symbols([("g_pfnVectors", 0x08000000, 428)], bits=64)
    # The string table is the last of the four section headers this helper
    # writes (null, .text, .symtab, .strtab); a 64-bit entry is 64 bytes wide
    # and `sh_size` is the fourth 8-byte field in it, at offset 32.
    strtab_entry_offset = len(table) - 64
    size_field_offset = strtab_entry_offset + 32
    hostile = struct.pack("<Q", 2**64 - 1)
    hostile_table = table[:size_field_offset] + hostile + table[size_field_offset + 8 :]

    assert read_elf_symbol_from_bytes(tmp_path, hostile_table, "g_pfnVectors") == {"ok": False, "reason": "unreadable"}


@pytest.mark.parametrize(
    ("data", "declared"),
    [
        (elf_with_symbols([("main", 0x080001C0, 64)]), "little"),
        (elf_with_symbols([("main", 0x080001C0, 64)], big_endian=True), "big"),
        (elf_with_symbols([("main", 0x080001C0, 64)], bits=64, big_endian=True), "big"),
        # EI_DATA zero is ELFDATANONE, which is not a third order but the absence
        # of one, and it is what the four-byte stand-in this suite writes carries.
        (b"\x7fELF" + b"\x00" * 12, None),
        (b"\x7fELF", None),
        (b"not an elf at all", None),
    ],
    ids=["lsb", "msb", "msb_64_bit", "no_encoding", "truncated", "not_elf"],
)
def test_the_image_is_asked_which_order_its_integers_are_read_in(tmp_path: Path, data: bytes, declared: str | None) -> None:
    """EI_DATA, in the words `int.from_bytes` takes.

    A file that declares neither LSB nor MSB declares nothing, and is refused
    here exactly as the symbol table refuses it: an image that states no order
    cannot be made to state one."""
    path = tmp_path / "image.elf"
    path.write_bytes(data)

    assert read_elf_byte_order(path) == declared


def test_a_missing_image_declares_no_order_rather_than_raising(tmp_path: Path) -> None:
    assert read_elf_byte_order(tmp_path / "nothing.elf") is None


def test_the_order_a_value_is_decoded_in_says_where_it_came_from(tmp_path: Path) -> None:
    """Read off the image, or assumed. Both are the answer; only one is a reading.

    Nothing with a firmware image in front of it reaches the default, because a
    file that declares no order is one the symbol-table parser refuses outright,
    so the field exists to keep the fallback from ever passing itself off as the
    image's own statement."""
    declared = tmp_path / "declared.elf"
    declared.write_bytes(elf_with_symbols([("main", 0x080001C0, 64)], big_endian=True))
    silent = tmp_path / "silent.elf"
    silent.write_bytes(b"\x7fELF" + b"\x00" * 12)

    assert image_byte_order(declared) == {"byte_order": "big", "byte_order_from": "elf_header"}
    assert image_byte_order(silent) == {"byte_order": "little", "byte_order_from": "default"}


def test_symbol_info_falls_back_to_the_symbol_table_for_an_untyped_symbol(tmp_path: Path) -> None:
    """The issue's own case, end to end through a session.

    The fake GDB answers both expressions with the message real GDB gives for a
    symbol with no debug type, and carries no address or size for it at all, so
    what comes back was read out of the ELF this test built. `resolved_from` says
    which route answered rather than leaving a caller to guess.
    """
    service = debug_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert start_debug_session(service)["ok"] is True
        typed = service.call("debug_symbol_info", {"symbol": "CTC_array"})
        untyped = service.call("debug_symbol_info", {"symbol": "g_pfnVectors"})
    finally:
        service.close()

    assert untyped["ok"] is True, untyped
    assert untyped["address"] == hex(VECTOR_TABLE_ADDRESS)
    assert untyped["size_bytes"] == VECTOR_TABLE_SIZE
    assert untyped["resolved_from"] == "elf_symbol_table"
    # The route that still works is unchanged, and says which one it was.
    assert typed["ok"] is True, typed
    assert typed["resolved_from"] == "debug_info"
    assert typed["address"] == hex(CTC_ARRAY_ADDRESS)


def test_symbol_dump_falls_back_to_the_symbol_table_for_an_untyped_symbol(tmp_path: Path) -> None:
    """The dump reaches the same address by the same second route: the fallback
    is in the resolution, so every tool built on it gains it at once."""
    service = debug_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert start_debug_session(service)["ok"] is True
        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "g_pfnVectors", "output_path": "build/vectors.hex"})
    finally:
        service.close()

    assert dumped["ok"] is True, dumped
    assert dumped["address"] == hex(VECTOR_TABLE_ADDRESS)
    assert dumped["size_bytes"] == VECTOR_TABLE_SIZE
    assert dumped["resolved_from"] == "elf_symbol_table"
    written = (tmp_path / "build" / "vectors.hex").read_text(encoding="ascii").splitlines()
    assert written[-1] == ":00000001FF"
    assert fake_target_bytes(VECTOR_TABLE_ADDRESS, VECTOR_TABLE_SIZE).hex().upper() in "".join(written)


def test_the_fallback_still_refuses_a_symbol_no_route_carries(tmp_path: Path) -> None:
    """The refusal that has to survive. GDB's classification is kept word for
    word, and what the symbol table was asked and answered is added to it rather
    than replacing it."""
    service = debug_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert start_debug_session(service)["ok"] is True
        refused = service.call("debug_symbol_info", {"symbol": "missing_symbol"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "symbol_not_found"
    assert refused["symbol"] == "missing_symbol"
    assert refused["symbol_table_lookup"] == "not_found"


def test_the_fallback_narrows_no_permission(tmp_path: Path) -> None:
    """`debug.allowed_symbols` is checked before either route runs, so a symbol
    the operator did not list is refused whether or not the ELF describes it."""
    service = debug_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE, allowed_symbols=["CTC_array"])
    try:
        assert start_debug_session(service)["ok"] is True
        refused = service.call("debug_symbol_info", {"symbol": "g_pfnVectors"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied"
    assert refused["summary"] == "Symbol is not allowed by debug.allowed_symbols."
    assert "symbol_table_lookup" not in refused


def test_an_untyped_symbol_lookup_does_not_poison_the_next_continue(tmp_path: Path) -> None:
    """The `^error` GDB answers for an untyped symbol is not a target fault.

    `_gdb_command` records every failed command as a debugger_error stop, and
    `debug_continue` short-circuits on one. A resolution that recovered through
    the symbol table therefore leaves the stop reason where it found it, or a
    plan that reads the vector table would find the target unable to run
    afterwards.
    """
    service = debug_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert start_debug_session(service)["ok"] is True
        assert service.call("debug_set_breakpoint", {"location": {"symbol": "test_done"}})["ok"] is True
        assert service.call("debug_symbol_info", {"symbol": "g_pfnVectors"})["ok"] is True
        continued = service.call("debug_continue", {"timeout_s": 5})
    finally:
        service.close()

    assert continued["ok"] is True, continued
    assert continued["stop_reason"] == "breakpoint_hit"


def test_stlink_resolution_falls_back_to_the_symbol_table_for_an_untyped_symbol(tmp_path: Path) -> None:
    """The same second route on the backend that has no session.

    The offline query runs against a private copy of the flashed ELF whose digest
    was checked before GDB saw it, and the fallback reads that same copy, so the
    address it answers is covered by the digest the result carries.
    """
    service = stlink_dump_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert flash_symbol_source(service)["ok"] is True
        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "g_pfnVectors", "output_path": "build/vectors.hex"})
    finally:
        service.close()

    assert dumped["ok"] is True, dumped
    assert dumped["backend"] == "stlink"
    assert dumped["address"] == hex(VECTOR_TABLE_ADDRESS)
    assert dumped["size_bytes"] == VECTOR_TABLE_SIZE
    assert dumped["resolved_from"] == "elf_symbol_table"
    assert dumped["symbol_source"]["path"] == "build/app.elf"


def test_stlink_resolution_still_refuses_a_symbol_the_flashed_elf_lacks(tmp_path: Path) -> None:
    service = stlink_dump_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = service.call("debug_dump_symbol_ihex", {"symbol": "missing_symbol", "output_path": "build/memory.hex"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "symbol_not_found"
    assert refused["summary"] == "Symbol was not found in the flashed ELF."
    assert refused["symbol_table_lookup"] == "not_found"
    assert refused["target_contacted"] is False
    assert not (tmp_path / "build" / "memory.hex").exists()


# --- the value a symbol holds -----------------------------------------------
#
# `debug_symbol_info` answers where a symbol is, `debug_dump_symbol_ihex` answers
# with a file, and the bytes the backend read on the way to writing that file
# reached nobody who wanted to compare them (#174). `debug_symbol_value` is that
# same read ending differently: nothing written anywhere, and the bytes returned.


def test_symbol_value_returns_the_bytes_and_both_integer_readings(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        value = service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()

    expected = fake_target_bytes(BOOT_COUNTER_ADDRESS, BOOT_COUNTER_SIZE)
    assert value["ok"] is True, value
    assert value["backend"] == "openocd"
    assert value["symbol"] == "boot_counter"
    assert value["address"] == hex(BOOT_COUNTER_ADDRESS)
    assert value["size_bytes"] == BOOT_COUNTER_SIZE
    assert value["resolved_from"] == "debug_info"
    assert value["hex"] == expected.hex()
    assert value["byte_order"] == "little"
    # The four-byte stand-in this service flashes declares no EI_DATA at all, so
    # the order is the supported Cortex-M one and the result says it was assumed
    # rather than read off the image.
    assert value["byte_order_from"] == "default"
    # This width has one integer reading and the two signs disagree about it,
    # which is the whole reason both are returned instead of one.
    assert value["value_unsigned"] == int.from_bytes(expected, "little", signed=False)
    assert value["value_signed"] == int.from_bytes(expected, "little", signed=True)
    assert value["value_unsigned"] != value["value_signed"]
    assert value["summary"] == "Symbol value read from target memory."
    # A read, and nothing else: no file appeared anywhere under the workspace.
    assert list((tmp_path / "build").iterdir()) == [tmp_path / "build" / "app.elf"]


def test_symbol_value_states_when_a_width_has_no_integer_reading(tmp_path: Path) -> None:
    """408 bytes is a structure, not a number. The bytes still come back; what is
    withheld is the reading that would have been invented."""
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        value = service.call("debug_symbol_value", {"symbol": "CTC_array"})
    finally:
        service.close()

    assert value["ok"] is True, value
    assert value["hex"] == fake_target_bytes(CTC_ARRAY_ADDRESS, CTC_ARRAY_SIZE).hex()
    assert "value_unsigned" not in value
    assert "value_signed" not in value
    assert value["value_not_decoded"] == "size_bytes is not 1, 2, 4 or 8, so the bytes carry no single integer reading."


@pytest.mark.parametrize(
    ("data", "unsigned", "signed"),
    [
        (b"\xff", 255, -1),
        (b"\x00\x80", 32768, -32768),
        (b"\x01\x00\x00\x00", 1, 1),
        (b"\xff\xff\xff\xff\xff\xff\xff\xff", 2**64 - 1, -1),
    ],
    ids=["one_byte", "two_bytes", "four_bytes", "eight_bytes"],
)
def test_symbol_value_decoding_covers_every_integer_width(data: bytes, unsigned: int, signed: int) -> None:
    decoded = decode_symbol_value(data, LITTLE_ENDIAN_IMAGE)

    assert decoded["hex"] == data.hex()
    assert decoded["byte_order"] == "little"
    assert decoded["value_unsigned"] == unsigned
    assert decoded["value_signed"] == signed


@pytest.mark.parametrize("size", [0, 3, 5, 6, 7, 9, 408], ids=str)
def test_symbol_value_decoding_offers_no_number_at_any_other_width(size: int) -> None:
    decoded = decode_symbol_value(bytes(size), LITTLE_ENDIAN_IMAGE)

    assert decoded["hex"] == bytes(size).hex()
    assert "value_unsigned" not in decoded and "value_signed" not in decoded
    assert "value_not_decoded" in decoded


def test_symbol_value_decoding_reads_the_order_it_was_handed() -> None:
    """The same four bytes, two images, two numbers, and one hex string.

    The raw bytes are memory order on every target, so they are the reading that
    cannot change; the order decides only what number they spell, and the result
    carries which order that was and where it came from."""
    data = b"\x01\x02\x03\x04"

    little = decode_symbol_value(data, LITTLE_ENDIAN_IMAGE)
    big = decode_symbol_value(data, BIG_ENDIAN_IMAGE)

    assert little["hex"] == big["hex"] == data.hex()
    assert little["value_unsigned"] == 0x04030201
    assert big["value_unsigned"] == 0x01020304
    assert (little["byte_order"], little["byte_order_from"]) == ("little", "elf_header")
    assert (big["byte_order"], big["byte_order_from"]) == ("big", "elf_header")


@pytest.mark.parametrize("big_endian", [False, True], ids=["little_endian_image", "big_endian_image"])
def test_symbol_value_decodes_in_the_order_the_image_declares(tmp_path: Path, big_endian: bool) -> None:
    """A big-endian build's counter reads big-endian, with nothing configured.

    The target cannot say which way its bytes are read (GDB answers a memory
    read in memory order and stops there), while the ELF the session loaded
    states it in EI_DATA the way every ELF must. So the image is asked, and the
    same four bytes come back as two different numbers on two builds that differ
    in nothing else (#249).
    """
    service = debug_service(tmp_path, elf_symbols=BOOT_COUNTER_SYMBOL_TABLE, big_endian=big_endian)
    try:
        assert start_debug_session(service)["ok"] is True
        value = service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()

    order = "big" if big_endian else "little"
    expected = fake_target_bytes(BOOT_COUNTER_ADDRESS, BOOT_COUNTER_SIZE)
    assert value["ok"] is True, value
    assert value["byte_order"] == order
    assert value["byte_order_from"] == "elf_header"
    # The bytes themselves are untouched by any of this.
    assert value["hex"] == expected.hex()
    assert value["value_unsigned"] == int.from_bytes(expected, order, signed=False)
    assert value["value_signed"] == int.from_bytes(expected, order, signed=True)
    # And the two orders really do disagree about this symbol, so the assertion
    # above is not passing on a palindrome.
    assert int.from_bytes(expected, "big") != int.from_bytes(expected, "little")


def test_symbol_value_reads_a_symbol_only_the_symbol_table_describes(tmp_path: Path) -> None:
    """The two changes meeting: the value of a symbol GDB's expression evaluator
    cannot resolve at all. Neither half answers this on its own."""
    service = debug_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert start_debug_session(service)["ok"] is True
        value = service.call("debug_symbol_value", {"symbol": "g_pfnVectors"})
    finally:
        service.close()

    expected = fake_target_bytes(VECTOR_TABLE_ADDRESS, VECTOR_TABLE_SIZE)
    assert value["ok"] is True, value
    assert value["resolved_from"] == "elf_symbol_table"
    assert value["address"] == hex(VECTOR_TABLE_ADDRESS)
    assert value["hex"] == expected.hex()
    assert value["value_unsigned"] == int.from_bytes(expected, "little", signed=False)


def test_symbol_value_enforces_the_allowlist(tmp_path: Path) -> None:
    """The refusal `debug_symbol_info` gives, word for word. The allowlist is the
    operator's statement about what may leave this bench, and a new way of
    leaving it must not be a way around it."""
    service = debug_service(tmp_path, allowed_symbols=["CTC_array"])
    try:
        assert start_debug_session(service)["ok"] is True
        refused = service.call("debug_symbol_value", {"symbol": "boot_counter"})
        allowed = service.call("debug_symbol_value", {"symbol": "CTC_array"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied"
    assert refused["summary"] == "Symbol is not allowed by debug.allowed_symbols."
    assert "hex" not in refused
    assert allowed["ok"] is True, allowed


def test_symbol_value_enforces_the_configured_size_cap(tmp_path: Path) -> None:
    """`debug.max_dump_size_bytes` bounds what may be read, not only what may be
    written: this tool writes nothing and is held to the same ceiling, checked on
    the resolved size before any memory is read."""
    service = debug_service(tmp_path, max_dump_size_bytes=16)
    try:
        assert start_debug_session(service)["ok"] is True
        refused = service.call("debug_symbol_value", {"symbol": "CTC_array"})
        within = service.call("debug_symbol_value", {"symbol": "boot_counter"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied"
    assert refused["size_bytes"] == CTC_ARRAY_SIZE
    assert refused["max_dump_size_bytes"] == 16
    assert within["ok"] is True, within


def test_symbol_value_requires_a_session_and_a_symbol(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        without_session = service.call("debug_symbol_value", {"symbol": "boot_counter"})
        assert start_debug_session(service)["ok"] is True
        without_symbol = service.call("debug_symbol_value", {"symbol": "   "})
    finally:
        service.close()

    assert without_session["ok"] is False, without_session
    assert without_session["error_type"] == "session_not_active"
    # Refused by the tool's own schema before the service is reached: `symbol`
    # carries the identifier pattern every symbol tool shares, so a name that is
    # not one costs nothing and never becomes an address.
    assert without_symbol["ok"] is False, without_symbol
    assert without_symbol["error_type"] == "invalid_argument"
    assert without_symbol["summary"] == "Tool arguments failed schema validation."


# --- the stlink backend's route to the same value ---------------------------
#
# The second tool of the typed family this backend serves. `-r` is a memory read
# and this is that read, ending with the bytes instead of with a file the caller
# asked for; the file it does write is this call's own, in a private directory,
# and is gone before the result is built (#249). What these pin is the same
# thing the dump's block pins: a caller cannot tell which backend answered.


def stlink_symbol_value(service: AgenticHILToolService, symbol: str = "boot_counter") -> dict:
    return service.call("debug_symbol_value", {"symbol": symbol})


def logged_command(tmp_path: Path, result: dict) -> str:
    return json.loads((tmp_path / result["log_path"]).read_text(encoding="utf-8"))["command"]


def test_stlink_symbol_value_returns_the_bytes_the_openocd_path_would(tmp_path: Path) -> None:
    """The whole route on the backend that has no debug session.

    The ELF is flashed first for the reason the dump's own test gives: that is
    what makes a symbol table describe the board. What comes back is the OpenOCD
    path's result: the same fields, the same summary, the same readings of the
    same bytes, plus the `symbol_source` that says which image answered, which
    a session would not need.
    """
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        value = stlink_symbol_value(service)
    finally:
        service.close()

    expected = fake_target_bytes(BOOT_COUNTER_ADDRESS, BOOT_COUNTER_SIZE)
    assert value["ok"] is True, value
    assert value["backend"] == "stlink"
    assert value["symbol"] == "boot_counter"
    assert value["address"] == hex(BOOT_COUNTER_ADDRESS)
    assert value["size_bytes"] == BOOT_COUNTER_SIZE
    assert value["resolved_from"] == "debug_info"
    assert value["hex"] == expected.hex()
    assert value["value_unsigned"] == int.from_bytes(expected, "little", signed=False)
    assert value["value_signed"] == int.from_bytes(expected, "little", signed=True)
    assert value["summary"] == "Symbol value read from target memory."
    assert value["symbol_source"]["path"] == "build/app.elf"
    # The read was the measured `-r` invocation, behind the same connect block
    # the dump uses: one command, two endings.
    assert command_for_log(["-c", "port=SWD", "mode=NORMAL", "-r", hex(BOOT_COUNTER_ADDRESS), str(BOOT_COUNTER_SIZE)]) in logged_command(tmp_path, value)


def test_stlink_symbol_value_leaves_no_file_and_names_none(tmp_path: Path) -> None:
    """The objection this tool used to refuse over, answered.

    A file is written, because `-r` writes one and there is no other way to read
    this target's memory. It is not the caller's: it is created in a private
    directory outside the workspace, it is removed before the result is built,
    and nothing in the result names it. The log names it, because the log is
    what was executed and that stays honest.
    """
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        value = stlink_symbol_value(service)
    finally:
        service.close()

    assert value["ok"] is True, value
    private_file = Path(logged_command(tmp_path, value).split()[-1].strip('"'))
    assert private_file.name.endswith(".hex")
    assert not private_file.exists()
    assert not private_file.parent.exists()
    assert tmp_path not in private_file.parents
    assert private_file.name not in json.dumps(value)
    # And the workspace still holds exactly what the test put there.
    assert list((tmp_path / "build").iterdir()) == [tmp_path / "build" / "app.elf"]


def test_stlink_symbol_value_decodes_in_the_order_the_flashed_image_declares(tmp_path: Path) -> None:
    """The byte order comes off the digest-verified copy of the flashed ELF.

    Same rule as the session path and the same reason: STM32CubeProgrammer's
    upload is memory order and says nothing about how to read it, while the
    image on the board states it. The copy is what is read, because the caller's
    own path is one a rebuild may already have replaced.
    """
    service = stlink_dump_service(tmp_path, elf_symbols=BOOT_COUNTER_SYMBOL_TABLE, big_endian=True)
    try:
        assert flash_symbol_source(service)["ok"] is True
        value = stlink_symbol_value(service)
    finally:
        service.close()

    expected = fake_target_bytes(BOOT_COUNTER_ADDRESS, BOOT_COUNTER_SIZE)
    assert value["ok"] is True, value
    assert value["byte_order"] == "big"
    assert value["byte_order_from"] == "elf_header"
    assert value["hex"] == expected.hex()
    assert value["value_unsigned"] == int.from_bytes(expected, "big", signed=False)


def test_stlink_symbol_value_reads_a_symbol_only_the_symbol_table_describes(tmp_path: Path) -> None:
    """The offline fallback composes with this tool as it does with the dump: an
    assembly-defined object's value is readable here too."""
    service = stlink_dump_service(tmp_path, elf_symbols=UNTYPED_SYMBOL_TABLE)
    try:
        assert flash_symbol_source(service)["ok"] is True
        value = stlink_symbol_value(service, "g_pfnVectors")
    finally:
        service.close()

    assert value["ok"] is True, value
    assert value["resolved_from"] == "elf_symbol_table"
    assert value["address"] == hex(VECTOR_TABLE_ADDRESS)
    assert value["hex"] == fake_target_bytes(VECTOR_TABLE_ADDRESS, VECTOR_TABLE_SIZE).hex()


@pytest.mark.parametrize(
    ("config_kwargs", "summary"),
    [
        ({"allowed_symbols": ["other_symbol"]}, "Symbol is not allowed by debug.allowed_symbols."),
        ({"max_dump_size_bytes": BOOT_COUNTER_SIZE - 1}, "Symbol read exceeds debug.max_dump_size_bytes."),
    ],
    ids=["allowlist", "size_cap"],
)
def test_stlink_symbol_value_is_held_to_the_same_gates_as_the_session_path(tmp_path: Path, config_kwargs: dict, summary: str) -> None:
    """Both refusals word for word, and both before a probe is opened.

    A new way of getting bytes off this bench must not be a way around what the
    operator said may leave it, on any backend.
    """
    service = stlink_dump_service(tmp_path, **config_kwargs)
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = stlink_symbol_value(service)
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "permission_denied"
    assert refused["summary"] == summary
    assert "hex" not in refused
    # Nothing was spawned for the read, so it never reached a log of its own.
    assert "log_path" not in refused


def test_stlink_symbol_value_refuses_before_a_symbol_table_describes_the_target(tmp_path: Path) -> None:
    """No flashed ELF, no answer: the dump's refusal, unchanged."""
    service = stlink_dump_service(tmp_path)
    try:
        refused = stlink_symbol_value(service)
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "symbol_source_not_available"
    assert "flash_firmware" in refused["summary"]
    assert refused["target_contacted"] is False


def test_stlink_symbol_value_confirms_the_read_before_it_reads_the_file(tmp_path: Path) -> None:
    """A CLI that connected, printed its banner and never confirmed the read.

    The bytes are not asked for at all in that case: `Data read successfully` is
    the only line that means they arrived, and a result that decoded whatever
    was on disk would be reporting a number for a read that never happened.
    """
    service = stlink_dump_service(tmp_path, debugger_executable=FAKE_STLINK_READ_UNCONFIRMED)
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = stlink_symbol_value(service)
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "memory_read_failed"
    assert refused["backend_error_type"] == "memory_read_unconfirmed"
    assert refused["operation_result"] == {"confirmed": False, "expected_success_text": ["Data read successfully"], "matched_success_text": []}
    assert refused["side_effect_status"] == "unknown"
    assert refused["retry_safe"] is False
    assert "hex" not in refused


def test_stlink_symbol_value_refuses_a_confirmed_read_that_is_short(tmp_path: Path) -> None:
    """The confirmation line is about the transfer; the file is its own question.

    This fixture prints every line a successful read prints and writes half the
    bytes. Half of a counter is a different number rather than a smaller one, so
    the answer is a failed read and not a truncated value.
    """
    service = stlink_dump_service(tmp_path, debugger_executable=FAKE_STLINK_SHORT_READ)
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = stlink_symbol_value(service)
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "memory_read_failed"
    assert refused["summary"] == "STM32CubeProgrammer confirmed the read but left no parseable Intel HEX covering the requested bytes."
    assert refused["target_contacted"] is True
    assert "success_confirmed" not in refused
    assert "hex" not in refused


def test_stlink_symbol_value_still_refuses_the_rest_of_the_typed_family(tmp_path: Path) -> None:
    """Two tools have crossed over now. The family's refusal is unchanged for the
    ones that need a session, and it is the same sentence as before."""
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = service.call("debug_symbol_info", {"symbol": "boot_counter"})
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "not_supported"
    assert refused["summary"] == "Typed debug sessions require the OpenOCD backend."


# --- the sessionless read takes a real one-shot debugger lease ---------------
#
# `-r` attaches to a live core with no session behind it, so the read is a
# standalone hardware interaction and coordination has to treat it as one: it
# holds the machine-wide probe lock while another owner has it, it is declared
# by a run that touches the probe or refused, and a read it could not confirm
# raises the incident a session read would. On OpenOCD the same tool runs on the
# session's own lease instead, and that stays true; here there is none to run on.


def test_stlink_symbol_value_refuses_while_another_owner_holds_the_probe(tmp_path: Path) -> None:
    """The machine-wide lock, taken for the read the way it is for a flash.

    A stranger — another MCP server, another project's run — holds the probe, so
    the read must answer `device_busy` and name the holder rather than attaching
    underneath it. Before this the read reached the board with no lease at all.
    """
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        stranger = BenchMutex(frontend="stranger", label="other-bench-session")
        stranger.acquire(list(debugger_device(service.config).lock_keys))
        try:
            refused = stlink_symbol_value(service)
        finally:
            stranger.release_all()
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "device_busy"
    assert refused["holder"]["label"] == "other-bench-session"
    assert refused["side_effect_committed"] is False
    # It never reached the probe: no lease, no read, no log of one.
    assert "hex" not in refused
    assert "log_path" not in refused


def test_stlink_symbol_value_inside_a_run_that_did_not_declare_the_probe_is_refused(tmp_path: Path) -> None:
    """The read is inside the run model now, so it obeys the run's declaration.

    A run that holds only some other board has not declared this probe, and a
    read that reached it anyway would be touching hardware the run never named.
    """
    service = stlink_dump_service(tmp_path)
    try:
        assert flash_symbol_source(service)["ok"] is True
        service.coordinator.begin_run(["physical:unrelated-board"], label="unrelated-plan")
        try:
            refused = stlink_symbol_value(service)
        finally:
            service.coordinator.end_run()
    finally:
        service.close()

    assert refused["ok"] is False, refused
    assert refused["error_type"] == "undeclared_device"
    assert refused["side_effect_committed"] is False
    assert "hex" not in refused


def test_stlink_symbol_value_unconfirmed_read_enters_the_incident_path(tmp_path: Path) -> None:
    """A read that attached and never confirmed is an incident, not a bare error.

    The CLI opened the probe, printed its banner and never said `Data read
    successfully`, so where the core stopped is unknown. The read now runs on a
    one-shot lease, so that unknown state raises the same incident a session read
    would — recorded here even as it stands down, because the target's state is
    what the next reset into halt and probe will say. Before this the same result
    came back with no lease and no incident behind it at all.
    """
    service = stlink_dump_service(tmp_path, debugger_executable=FAKE_STLINK_READ_UNCONFIRMED)
    try:
        assert flash_symbol_source(service)["ok"] is True
        refused = stlink_symbol_value(service)

        assert refused["ok"] is False, refused
        assert refused["error_type"] == "memory_read_failed"
        assert refused["side_effect_status"] == "unknown"
        # The incident the lease raised, and the stand-down that ends it: the
        # read is read-only, so its unconfirmed target state settles at the next
        # reset and probe rather than padlocking the bench for a person.
        assert refused["cleanup_reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert refused["incident_stood_down"]["reasons"] == ["debugger_readonly_target_state_unconfirmed"]
        assert refused["quarantined"] is False
        assert service.coordinator.blocked is False
    finally:
        service.close()
