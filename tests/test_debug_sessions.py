from __future__ import annotations

from pathlib import Path

from conftest import FAKE_GDB, write_config

from hardci.config import load_config
from hardci.gdbmi import intel_hex_record, write_intel_hex_file
from hardci.tools import HardCIToolService

CTC_ARRAY_ADDRESS = 0x200006F0
CTC_ARRAY_SIZE = 408
START_TIMEOUT_S = 10.0


def debug_service(tmp_path: Path, **config_kwargs) -> HardCIToolService:
    config_path = write_config(tmp_path, gdb_executable=FAKE_GDB, **config_kwargs)
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    return HardCIToolService(load_config(str(config_path), str(tmp_path)))


def start_debug_session(service: HardCIToolService, mode: str = "load") -> dict:
    return service.call("hardci_debug_start_session", {"image_path": "build/app.elf", "mode": mode, "timeout_s": START_TIMEOUT_S})


def test_debug_session_full_cycle_breakpoint_symbol_and_ihex_dump(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        started = start_debug_session(service)
        assert started["ok"] is True, started
        assert started["session"]["status"] == "halted"
        assert started["mode"] == "load"

        status = service.call("hardci_debug_get_session_status")
        assert status["active"] is True
        assert status["status"] == "halted"

        breakpoint_result = service.call("hardci_debug_set_breakpoint", {"location": {"symbol": "test_done"}})
        assert breakpoint_result["ok"] is True, breakpoint_result
        assert breakpoint_result["breakpoint"]["backend_id"] == "1"

        listed = service.call("hardci_debug_list_breakpoints")
        assert len(listed["breakpoints"]) == 1

        continued = service.call("hardci_debug_continue", {"timeout_s": 5})
        assert continued["ok"] is True, continued
        assert continued["stop_reason"] == "breakpoint_hit"
        assert continued["stop"]["breakpoint_id"] == 1
        assert continued["stop"]["frame"]["function"] == "test_done"
        assert continued["stop"]["frame"]["line"] == 123

        stop_reason = service.call("hardci_debug_get_stop_reason")
        assert stop_reason["ok"] is True
        assert stop_reason["stop_reason"] == "breakpoint_hit"

        symbol = service.call("hardci_debug_symbol_info", {"symbol": "CTC_array"})
        assert symbol["ok"] is True, symbol
        assert symbol["address"] == hex(CTC_ARRAY_ADDRESS)
        assert symbol["size_bytes"] == CTC_ARRAY_SIZE

        dumped = service.call("hardci_debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
        assert dumped["ok"] is True, dumped
        hex_lines = (tmp_path / "build" / "memory.hex").read_text(encoding="ascii").splitlines()
        assert hex_lines[0] == ":020000042000DA"
        assert hex_lines[-1] == ":00000001FF"

        cleared = service.call("hardci_debug_clear_breakpoints")
        assert cleared["ok"] is True
        assert service.call("hardci_debug_list_breakpoints")["breakpoints"] == []

        stopped = service.call("hardci_debug_stop_session")
        assert stopped["ok"] is True
        assert stopped["status"] == "stopped"
        assert service.call("hardci_debug_get_session_status")["active"] is False
    finally:
        service.close()


def test_debug_halt_records_signal_stop(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service, mode="attach")["ok"] is True
        halted = service.call("hardci_debug_halt", {"timeout_s": 5})
        assert halted["ok"] is True, halted
        assert halted["stop"]["stop_reason"] == "fault"
        assert halted["stop"]["backend_stop_reason"] == "signal-received"
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
        result = service.call("hardci_debug_symbol_info", {"symbol": "missing_symbol"})
        assert result["ok"] is False
        assert result["error_type"] == "symbol_not_found"
    finally:
        service.close()


def test_debug_symbol_allowlist_blocks_unlisted_symbols(tmp_path: Path) -> None:
    service = debug_service(tmp_path, allowed_symbols=["CTC_array"])
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("hardci_debug_symbol_info", {"symbol": "test_done"})
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
        allowed = service.call("hardci_debug_symbol_info", {"symbol": "CTC_array"})
        assert allowed["ok"] is True
    finally:
        service.close()


def test_debug_dump_rejects_oversized_symbol(tmp_path: Path) -> None:
    service = debug_service(tmp_path, max_dump_size_bytes=16)
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("hardci_debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "build/memory.hex"})
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
        assert result["max_dump_size_bytes"] == 16
    finally:
        service.close()


def test_debug_dump_rejects_output_outside_allowed_roots(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        assert start_debug_session(service)["ok"] is True
        result = service.call("hardci_debug_dump_symbol_ihex", {"symbol": "CTC_array", "output_path": "outside/memory.hex"})
        assert result["ok"] is False
        assert result["error_type"] == "output_validation_failed"
        assert result["validation"]["allowed_root"] is False
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


def test_debug_reset_halt_mode_requires_reset_permission(tmp_path: Path) -> None:
    service = debug_service(tmp_path, permissions_yaml="permissions:\n  allow_reset: false\n")
    try:
        result = start_debug_session(service, mode="reset_halt")
        assert result["ok"] is False
        assert result["error_type"] == "permission_denied"
    finally:
        service.close()


def test_debug_tools_require_active_session(tmp_path: Path) -> None:
    service = debug_service(tmp_path)
    try:
        for tool, arguments in [
            ("hardci_debug_continue", {}),
            ("hardci_debug_halt", {}),
            ("hardci_debug_set_breakpoint", {"location": "test_done"}),
            ("hardci_debug_symbol_info", {"symbol": "CTC_array"}),
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
