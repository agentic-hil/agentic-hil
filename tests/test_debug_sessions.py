from __future__ import annotations

from pathlib import Path

from conftest import FAKE_GDB, write_config

from agentic_hil.config import load_config
from agentic_hil.gdbmi import intel_hex_record, write_intel_hex_file
from agentic_hil.tools import AgenticHILToolService

CTC_ARRAY_ADDRESS = 0x200006F0
CTC_ARRAY_SIZE = 408
START_TIMEOUT_S = 10.0


def debug_service(tmp_path: Path, fake_gdb_behavior: str | None = None, **config_kwargs) -> AgenticHILToolService:
    config_path = write_config(tmp_path, gdb_executable=FAKE_GDB, **config_kwargs)
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    if fake_gdb_behavior is not None:
        (tmp_path / "fake_gdb_behavior.txt").write_text(fake_gdb_behavior, encoding="utf-8")
    return AgenticHILToolService(load_config(str(config_path), str(tmp_path)))


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

        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
        assert dumped["ok"] is True, dumped
        hex_lines = (tmp_path / "build" / "memory.hex").read_text(encoding="ascii").splitlines()
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
    finally:
        service.close()


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


def test_debug_symbol_info_reports_missing_symbol(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("debug_symbol_info", {"symbol": "missing_symbol"})
        assert result["ok"] is False
        assert result["error_type"] == "symbol_not_found"
    finally:
        service.close()


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


def test_debug_dump_reloads_allowed_roots_without_restarting_session(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        rejected = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "_CTCPP/memory.hex"})
        assert rejected["ok"] is False
        assert rejected["validation"]["allowed_root"] is False

        config_path = tmp_path / ".agentic-hil" / "config.yaml"
        config_text = config_path.read_text(encoding="utf-8")
        config_path.write_text(config_text.replace('allowed_roots: ["build"]', 'allowed_roots: ["build", "_CTCPP"]'), encoding="utf-8")

        dumped = service.call("debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "_CTCPP/memory.hex"})
        assert dumped["ok"] is True, dumped
        assert dumped["session"]["status"] == "halted"
        assert (tmp_path / "_CTCPP" / "memory.hex").is_file()
    finally:
        service.close()


def test_debug_start_denied_while_raw_debugger_commands_allowed(tmp_path: Path) -> None:
    service = debug_service(tmp_path, permissions_yaml="permissions:\n  allow_raw_debugger_commands: true\n")
    try:
        result = start_debug_session(service, mode="attach")
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
    finally:
        service.close()


def test_debug_load_mode_requires_flash_permission(tmp_path: Path) -> None:
    service = debug_service(tmp_path, permissions_yaml="permissions:\n  allow_flash: false\n")
    try:
        result = start_debug_session(service, mode="load")
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


def test_intel_hex_record_matches_reference_vectors() -> None:
    assert intel_hex_record(0, 0x04, bytes([0x20, 0x00])) == ":020000042000DA"


def test_write_intel_hex_file_emits_extended_address_and_eof(tmp_path: Path) -> None:
    output = tmp_path / "memory.hex"
    write_intel_hex_file(output, 0x200006F0, bytes(range(20)))
    lines = output.read_text(encoding="ascii").splitlines()
    assert lines[0] == ":020000042000DA"
    assert lines[1].startswith(":10" + "06F0" + "00")
    assert lines[-1] == ":00000001FF"
