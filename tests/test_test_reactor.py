from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import DEFAULT_TEST_PERMISSIONS, FAKE_GDB, FAKE_OPENOCD, write_authoritative_config, write_config

from agentic_hil.cli import build_parser, entrypoint
from agentic_hil.config import ConfigError, load_config
from agentic_hil.test_reactor import TestReactor, load_test_config, merge_result_status
from agentic_hil.tools import AgenticHILToolService


class FakeComPortService:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.closed = False

    def reconfigure(self, config) -> None:
        pass

    def session_start(self, port_id: str, clear_buffer: bool = True) -> dict:
        already_active = port_id in self.active
        self.active.add(port_id)
        return {"ok": True, "tool": "com_session_start", "port_id": port_id, "already_active": already_active}

    def session_stop(self, port_id: str) -> dict:
        was_active = port_id in self.active
        self.active.discard(port_id)
        return {"ok": True, "tool": "com_session_stop", "port_id": port_id, "was_active": was_active}

    def close(self) -> None:
        self.closed = True
        self.active.clear()


class RecordingService:
    def __init__(
        self,
        *,
        fail_cleanup: bool = False,
        raise_flash: bool = False,
        audit_flash_failure: bool = False,
        audit_failure_call: str | None = None,
        fail_call: str | None = None,
        uart_reads: list[bytes] | None = None,
        uart_encoding: str = "utf-8",
        reset_result: dict | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_cleanup = fail_cleanup
        self.raise_flash = raise_flash
        self.audit_flash_failure = audit_flash_failure
        self.audit_failure_call = audit_failure_call
        self.fail_call = fail_call
        # What the line says, one entry per com_read, in the order it says it. An
        # exhausted list is a silent port — which is exactly what a board that
        # never printed its banner looks like from here.
        self.uart_reads = list(uart_reads or [])
        self.uart_encoding = uart_encoding
        self.reset_result = reset_result
        self.breakpoint_id = 0
        self.recovery_calls: list[list[str]] = []

    def recover_after_failed_run(self, devices: list[str] | None = None) -> dict:
        # Part of the service contract the reactor drives, so the double carries
        # it: every abort calls this, and a double that lacked it would only
        # prove the seam is never reached.
        self.recovery_calls.append(sorted(devices or []))
        return {"attempted": True, "actions": ["reap_processes", "reset_halt", "probe_target"], "outcome": "recovered", "incident_resolved": False}

    def call(self, name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == self.fail_call:
            return {"ok": False, "tool": name, "error_type": "can_read_failed", "summary": "adapter would not read"}
        if name == "can_session_start":
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "already_active": False}
        if name == "can_session_stop":
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "was_active": True}
        if name == "can_read":
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "frames": [], "frames_read": 0}
        if name == "can_send":
            return {"ok": True, "tool": name, "bus_id": arguments.get("bus_id"), "frame": {"id": arguments.get("frame_id")}}
        if name == "flash_firmware" and self.raise_flash:
            raise OSError("flash transport failed")
        if name == "flash_firmware" and self.audit_flash_failure:
            return {
                "ok": True,
                "audit_ok": False,
                "audit_error": {"error_type": "unsafe_configured_path"},
                "side_effect_committed": True,
                "retry_safe": False,
            }
        if name == self.audit_failure_call:
            result = {"ok": True, "audit_ok": False, "audit_error": {"error_type": "audit_failed"}, "side_effect_committed": True, "retry_safe": False}
            if name == "debug_set_breakpoint":
                self.breakpoint_id += 1
                result["breakpoint"] = {"id": self.breakpoint_id}
            return result
        if name == "com_session_start":
            return {"ok": True, "already_active": False}
        if name == "com_session_stop":
            return {"ok": True}
        if name == "com_read":
            chunk = self.uart_reads.pop(0) if self.uart_reads else b""
            # The shape com_read really returns: raw bytes as hex beside the
            # decoded text, so a caller that reassembles across reads has
            # something to reassemble that a split character cannot corrupt.
            return {
                "ok": True,
                "tool": name,
                "port_id": arguments.get("port_id"),
                "bytes_read": len(chunk),
                "buffer_remaining_bytes": 0,
                "data": {"hex": chunk.hex(), "text": chunk.decode(self.uart_encoding, errors="replace"), "encoding": self.uart_encoding},
            }
        if name == "reset_target":
            return self.reset_result if self.reset_result is not None else {"ok": True, "tool": name, "mode": arguments.get("mode")}
        if name == "debug_start_session":
            return {"ok": True, "target_ok": True}
        if name == "debug_stop_session":
            if self.fail_cleanup:
                raise OSError("debugger would not stop")
            return {"ok": True}
        if name == "debug_dump_symbol_ihex":
            return {"ok": False, "error_type": "memory_read_failed", "summary": "read failed"}
        if name == "debug_set_breakpoint":
            self.breakpoint_id += 1
            return {"ok": True, "breakpoint": {"id": self.breakpoint_id}}
        if name == "debug_continue":
            return {"ok": True, "stop_reason": "breakpoint_hit", "stop": {"breakpoint_id": self.breakpoint_id}}
        if name == "debug_clear_breakpoints":
            return {"ok": True, "cleared": 1}
        return {"ok": True}


def reactor_service(tmp_path: Path, behavior: str | None = None) -> tuple[AgenticHILToolService, FakeComPortService]:
    config_path = write_config(
        tmp_path,
        gdb_executable=FAKE_GDB,
        com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_data = b"\x7fELF" + b"\x00" * 12
    if behavior is not None:
        elf_data += f"\nFAKE_GDB_BEHAVIOR={behavior}\n".encode()
    elf_path.write_bytes(elf_data)
    com_ports = FakeComPortService()
    service = AgenticHILToolService(load_config(str(config_path)), com_ports=com_ports)  # type: ignore[arg-type]
    return service, com_ports


def write_test_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_reactor_flattens_and_deduplicates_all_audit_errors() -> None:
    first = {"audit_ok": False, "audit_error": {"error_type": "first"}, "audit_errors": [{"error_type": "first"}, {"error_type": "second"}]}
    cleanup = {"audit_ok": False, "audit_error": {"error_type": "cleanup"}, "audit_errors": [{"error_type": "second"}, {"error_type": "cleanup"}]}

    result = merge_result_status({"ok": False}, first, cleanup)

    assert result["audit_error"] == {"error_type": "first"}
    assert result["audit_errors"] == [{"error_type": "first"}, {"error_type": "second"}, {"error_type": "cleanup"}]
    assert result["retry_safe"] is False


def test_reactor_runs_flash_uart_breakpoint_and_memory_dump(tmp_path: Path) -> None:
    service, com_ports = reactor_service(tmp_path)
    test_path = write_test_config(
        tmp_path,
        """version: 2
name: capture-state
steps:
  - debugger: dut
    action: flash
    image_path: build/app.elf
  - port_id: dut_uart
    action: uart_open
  - debugger: dut
    action: debug_start
    image_path: build/app.elf
    mode: attach
  - debugger: dut
    action: run_until_breakpoint
    location:
      symbol: test_done
    timeout_s: 5
  - debugger: dut
    action: dump_memory
    symbol: CTC_array
    output_path: build/new/nested/memory.hex
  - debugger: dut
    action: debug_stop
  - port_id: dut_uart
    action: uart_close
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(test_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == [
        "flash",
        "uart_open",
        "debug_start",
        "run_until_breakpoint",
        "dump_memory",
        "debug_stop",
        "uart_close",
    ]
    assert result["cleanup"] == []
    assert com_ports.active == set()
    hex_lines = (tmp_path / "build" / "new" / "nested" / "memory.hex").read_text(encoding="ascii").splitlines()
    assert hex_lines[0] == ":020000042000DA"
    assert hex_lines[-1] == ":00000001FF"


def test_reactor_stops_debug_and_uart_after_failed_step(tmp_path: Path) -> None:
    service, com_ports = reactor_service(tmp_path, "unexpected_breakpoint")
    test_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: run_until_breakpoint, location: test_done, timeout_s: 5}
  - {debugger: dut, action: dump_memory, symbol: CTC_array, output_path: build/should-not-exist.hex}
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(test_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is False
    assert result["failed_step"] == 3
    assert result["error_type"] == "unexpected_breakpoint"
    assert sorted(item["action"] for item in result["cleanup"]) == ["debug_stop", "uart_close"]
    assert com_ports.active == set()
    assert not (tmp_path / "build" / "should-not-exist.hex").exists()


def test_test_config_accepts_json_and_rejects_unknown_actions(tmp_path: Path) -> None:
    json_path = tmp_path / "testconfig.json"
    json_path.write_text(
        json.dumps({"version": 2, "steps": [{"port_id": "dut_uart", "action": "uart_open"}]}),
        encoding="utf-8",
    )
    assert load_test_config(str(json_path), str(tmp_path)).steps[0].action == "uart_open"

    invalid_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: shell}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(invalid_path), str(tmp_path))
    assert excinfo.value.error_type == "test_config_invalid"
    assert excinfo.value.details["field"] == "steps[0].action"
    assert excinfo.value.details["value"] == "shell"


def test_test_config_rejects_duplicate_keys_with_location(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - port_id: dut_uart\n    action: uart_open\n    action: uart_close\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(path), str(tmp_path))
    assert excinfo.value.error_type == "test_config_invalid"
    assert excinfo.value.details["line"] == 5
    assert "duplicate key 'action'" in excinfo.value.details["backend_error"]


def test_test_config_schema_error_does_not_echo_structured_input(tmp_path: Path) -> None:
    secret = "operator-secret-must-not-leak"
    path = write_test_config(
        tmp_path,
        f"version: 2\nname:\n  secret: {secret}\nsteps:\n  - {{port_id: dut_uart, action: uart_open}}\n",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(path), str(tmp_path))

    assert excinfo.value.error_type == "test_config_invalid"
    assert secret not in json.dumps(excinfo.value.details)
    assert "value" not in excinfo.value.details


def test_test_plan_must_remain_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = tmp_path / "outside.yaml"
    plan.write_text("version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_test_config(str(plan), str(workspace))

    assert rejected.value.error_type == "test_config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())


def test_reactor_schema_rejects_traversal_breakpoint_file(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: run_until_breakpoint, location: {file: ../evil.c, line: 10}}\n",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(path), str(tmp_path))
    assert excinfo.value.error_type == "test_config_invalid"


def test_reactor_schema_accepts_windows_drive_breakpoint_file(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: run_until_breakpoint, location: {file: 'C:/src/main.c', line: 10}}\n",
    )
    loaded = load_test_config(str(path), str(tmp_path))
    assert loaded.steps[0].arguments["location"] == {"file": "C:/src/main.c", "line": 10}


def test_preflight_contract_rejects_bad_step_before_any_backend_call(tmp_path: Path) -> None:
    from agentic_hil.test_reactor import TestConfig, TestStep

    config = load_config(str(write_config(tmp_path, allow_all_symbols=True)))
    service = RecordingService()
    # Bypass the schema loader to prove the preflight contract validators are an
    # independent gate: a traversal breakpoint file must fail before debug_start.
    plan = TestConfig(
        path=str(tmp_path / "plan.yaml"),
        name="direct",
        steps=[
            TestStep("debug_start", {"image_path": "build/app.elf"}, debugger="dut"),
            TestStep("run_until_breakpoint", {"location": {"file": "../evil.c", "line": 10}}, debugger="dut"),
        ],
    )

    result = TestReactor(config, service).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["failed_step"] == 2
    assert service.calls == []


def test_preflight_rejects_late_unknown_debugger_before_flash(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {debugger: typo, action: flash, image_path: build/app.elf}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["failed_step"] == 2
    assert result["steps"] == []
    assert service.calls == []


def test_preflight_rejects_closing_a_uart_the_plan_never_opened(tmp_path: Path) -> None:
    # A plan may only close the session it opened, so a close on a second
    # configured port fails before any step runs.
    config = load_config(
        str(
            write_config(
                tmp_path,
                com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n  other_uart:\n    device: "COM_OTHER"\n',
            )
        )
    )
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_open}\n  - {port_id: other_uart, action: uart_close}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["steps"] == []
    assert service.calls == []


def test_preflight_does_not_create_dump_output_directories(tmp_path: Path) -> None:
    service, _ = reactor_service(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: dump_memory, symbol: CTC_array, output_path: build/new/nested/memory.hex}
  - {debugger: missing, action: flash, image_path: build/app.elf}
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(plan_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is False
    assert result["steps"] == []
    assert not (tmp_path / "build" / "new").exists()


def test_preflight_enforces_symbol_allowlist_before_hardware_actions(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                allowed_symbols=["capture_buffer"],
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: run_until_breakpoint, location: test_done}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[2].location"
    assert service.calls == []


def test_reactor_cleanup_continues_after_debug_stop_exception(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}
""",
    )
    service = RecordingService(fail_cleanup=True)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "cleanup_failed"
    assert result["step_error_type"] == "memory_read_failed"
    assert result["cleanup_ok"] is False
    assert [item["action"] for item in result["cleanup"]] == ["debug_stop", "uart_close"]
    assert ("com_session_stop", {"port_id": "dut_uart"}) in service.calls


def test_reactor_converts_step_exception_to_structured_failure(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path)),
    )
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n")
    service = RecordingService(raise_flash=True)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "step_exception"
    assert result["steps"][0]["result"]["exception_type"] == "OSError"


def test_reactor_treats_audit_failure_as_failed_step(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')),
    )
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n  - {port_id: dut_uart, action: uart_open}\n",
    )
    service = RecordingService(audit_flash_failure=True)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 1
    assert result["error_type"] == "audit_failed"
    assert result["audit_ok"] is False
    assert result["retry_safe"] is False
    assert service.calls == [("flash_firmware", {"image_path": "build/app.elf"})]


@pytest.mark.parametrize(
    ("failed_call", "expected_calls"),
    [
        ("debug_set_breakpoint", ["debug_start_session", "debug_set_breakpoint", "debug_clear_breakpoints", "debug_stop_session"]),
        ("debug_continue", ["debug_start_session", "debug_set_breakpoint", "debug_continue", "debug_clear_breakpoints", "debug_stop_session"]),
        ("debug_clear_breakpoints", ["debug_start_session", "debug_set_breakpoint", "debug_continue", "debug_clear_breakpoints", "debug_stop_session"]),
    ],
)
def test_run_until_breakpoint_propagates_internal_audit_failures(tmp_path: Path, failed_call: str, expected_calls: list[str]) -> None:
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: debug_start, image_path: build/app.elf}\n  - {debugger: dut, action: run_until_breakpoint, location: test_done}\n",
    )
    service = RecordingService(audit_failure_call=failed_call)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["audit_ok"] is False
    assert result["retry_safe"] is False
    assert [name for name, _ in service.calls] == expected_calls


def test_run_until_breakpoint_removes_each_owned_breakpoint(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path)),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: run_until_breakpoint, location: first_stop}
  - {debugger: dut, action: run_until_breakpoint, location: second_stop}
  - {debugger: dut, action: debug_stop}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [name for name, _ in service.calls].count("debug_clear_breakpoints") == 2


def test_plan_rejects_a_com_port_the_config_does_not_declare(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {port_id: missing_uart, action: uart_open}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].port_id"
    assert result["validation_error"]["configured_com_ports"] == ["dut_uart"]
    assert service.calls == []


def test_single_configured_debugger_is_bound_and_needs_no_name(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))

    assert config.debugger_id == "dut"
    assert config.debugger is config.debuggers["dut"]


def test_several_configured_debuggers_leave_the_config_unbound(tmp_path: Path) -> None:
    # With more than one probe there is no board a bare call could mean, so
    # nothing is bound and the plan must name the probe itself.
    config = load_config(
        str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))
    )

    assert config.debugger_id is None
    assert config.debugger is None
    assert sorted(config.debuggers) == ["dut", "probe_b"]


def test_plan_must_name_the_debugger_when_several_are_configured(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))
    )
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {action: flash, image_path: build/app.elf}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].debugger"
    assert result["validation_error"]["configured_debuggers"] == ["dut", "probe_b"]
    assert service.calls == []


def test_debugger_target_override_inherits_unspecified_project_fields(tmp_path: Path) -> None:
    # A per-debugger `target` overrides only the fields it names; the rest fall
    # back to the project target so a board cannot silently lose the controller.
    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml=(
                    "debuggers:\n  probe_t:\n    type: openocd\n    resource_id: rt\n"
                    '    target:\n      name: "renamed-controller"\n'
                ),
            )
        )
    )

    assert config.debuggers["probe_t"].target is not None
    assert config.debuggers["probe_t"].target.name == "renamed-controller"
    assert config.debuggers["probe_t"].target.controller == config.target.controller


def test_cli_parses_test_reactor_command() -> None:
    args = build_parser().parse_args(["test-reactor", "--test-config", "test.json"])
    assert args.command == "test-reactor"
    assert args.test_config == "test.json"


def test_cli_uses_authoritative_config_and_repository_local_test_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_authoritative_config(tmp_path, monkeypatch)
    test_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: shell}\n")
    monkeypatch.chdir(tmp_path)

    exit_code = entrypoint(["test-reactor", "--test-config", str(test_path)])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["error_type"] == "test_config_invalid"


def test_cli_returns_failure_for_audit_failed_result(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("agentic_hil.cli.run_test_reactor", lambda _path, **_kwargs: {"ok": True, "audit_ok": False})

    exit_code = entrypoint(["test-reactor"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["audit_ok"] is False


def test_reactor_builds_one_service_per_named_debugger(tmp_path: Path) -> None:
    # Multi-board: the bound probe shares the base service; every other named
    # probe drives its own service built for that debugger, sharing the base
    # coordinator for project-level exclusivity.
    from agentic_hil.config import bind_debugger
    from agentic_hil.test_reactor import TestReactor

    config = bind_debugger(
        load_config(str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))),
        "dut",
    )

    class ClosableRecordingService(RecordingService):
        def close(self) -> None:
            self.closed = True

    base = ClosableRecordingService()
    built: list[ClosableRecordingService] = []

    def factory(bound_config) -> ClosableRecordingService:
        svc = ClosableRecordingService()
        svc.built_for = bound_config.debugger
        built.append(svc)
        return svc

    reactor = TestReactor(config, base, service_factory=factory)  # type: ignore[arg-type]
    try:
        assert reactor.runner("dut").service is base
        assert reactor.runner("dut").debugger is config.debuggers["dut"]
        assert reactor.runner("probe_b").service is not base
        assert reactor.runner("probe_b").debugger.resource_id == "rb"
        assert len(built) == 1
        assert built[0].built_for.resource_id == "rb"
        assert built[0].built_for.type == "openocd"
    finally:
        reactor.close()
    assert built[0].closed is True


def test_config_rejects_named_debuggers_that_share_a_physical_probe(tmp_path: Path) -> None:
    # Two names, one probe serial: both entries would drive the same board while
    # a plan believes it addressed two.
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_name="probe_x",
                    probe_id="0669FF505153",
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n    probe_id: "0669FF505153"\n',
                )
            )
        )
    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["other_debugger"] == "probe_x"


def test_multi_probe_config_without_probe_ids_still_loads(tmp_path: Path) -> None:
    # Discovering the ids is exactly what an operator does before they can write
    # one down, and loading must work with no hardware attached, so the demand
    # for a probe_id cannot live at config load.
    config = load_config(
        str(
            write_config(
                tmp_path,
                debugger_name="probe_x",
                auto_probe_ids=False,
                debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n',
            )
        )
    )

    assert sorted(config.debuggers) == ["probe_x", "probe_y"]
    assert config.debuggers["probe_y"].probe_id is None


def test_driving_an_unnamed_probe_is_refused_when_several_are_configured(tmp_path: Path) -> None:
    # The demand belongs at the hardware boundary, where picking the wrong board
    # is the failure that matters.
    from agentic_hil.config import bind_debugger

    config = bind_debugger(
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_name="probe_x",
                    auto_probe_ids=False,
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: openocd\n',
                )
            )
        ),
        "probe_x",
    )
    service = AgenticHILToolService(config)
    try:
        driven = service.call("reset_target", {"mode": "run"})
        listed = service.call("debugger_probes_list")
    finally:
        service.close()

    assert driven["ok"] is False
    assert driven["error_type"] == "not_supported"
    assert driven["side_effect_committed"] is False
    assert "debugger-probes" in driven["summary"]
    # Discovery is the way out of that state, so the unnamed-probe guard must
    # not swallow it. (OpenOCD answers not_supported on its own — it cannot
    # enumerate — but the refusal is the backend's, not the guard's.)
    assert "configured_debuggers" not in listed
    assert listed["backend"] == "openocd"


def test_config_rejects_a_pyocd_probe_id_contained_in_another(tmp_path: Path) -> None:
    # pyOCD matches --uid as a case-insensitive substring, so the shorter
    # selector also selects the board the longer one names.
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_type="pyocd",
                    debugger_name="probe_x",
                    probe_id="0669FF505153",
                    auto_probe_ids=False,
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "0669FF"\n',
                )
            )
        )

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["other_debugger"] == "probe_x"


def test_config_rejects_a_pyocd_probe_id_that_only_differs_by_its_type_prefix(tmp_path: Path) -> None:
    # pyOCD strips an optional `<type>:` prefix before matching, so these two
    # spellings are one selector.
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            str(
                write_config(
                    tmp_path,
                    debugger_type="pyocd",
                    debugger_name="probe_x",
                    probe_id="0669FF505153",
                    auto_probe_ids=False,
                    debuggers_yaml='debuggers:\n  probe_y:\n    type: pyocd\n    probe_id: "stlink:0669ff505153"\n',
                )
            )
        )

    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["other_debugger"] == "probe_x"


def test_single_debugger_needs_no_probe_id(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)))

    assert config.debuggers["dut"].probe_id is None
    assert config.debugger_id == "dut"


def test_reactor_requires_factory_for_an_unbound_debugger(tmp_path: Path) -> None:
    from agentic_hil.config import bind_debugger
    from agentic_hil.test_reactor import TestReactor

    config = bind_debugger(
        load_config(str(write_config(tmp_path, debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n"))),
        "dut",
    )
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: probe_b, action: flash, image_path: build/app.elf}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].debugger"
    assert result["validation_error"]["bound_debugger"] == "dut"
    assert service.calls == []


def test_preflight_gates_debug_actions_on_the_named_debuggers_type(tmp_path: Path) -> None:
    # probe_b is pyocd while the other configured probe is openocd: typed debug
    # steps must be judged by the debugger the STEP names.
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: pyocd\n    resource_id: rb\n",
            )
        )
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: probe_b, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: probe_b, action: debug_stop}
""",
    )

    class ClosableService(RecordingService):
        def close(self) -> None:
            pass

    reactor = TestReactor(config, RecordingService(), service_factory=lambda bound_config: ClosableService())  # type: ignore[arg-type]
    try:
        result = reactor.run(load_test_config(str(plan_path), str(tmp_path)))
    finally:
        reactor.close()

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["validation_error"]["debugger_type"] == "pyocd"


def test_debug_steps_execute_on_the_named_debugger_service(tmp_path: Path) -> None:
    # Inverse direction: the bound probe is pyocd (no typed debug), but the probe
    # the step NAMES is openocd — preflight must pass and the steps must run on
    # that probe's own service, not the base service.
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debugger_type="pyocd",
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n",
            )
        )
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: probe_b, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: probe_b, action: debug_stop}
""",
    )

    class ClosableService(RecordingService):
        def close(self) -> None:
            pass

    base = RecordingService()
    built: list[ClosableService] = []

    def factory(bound_config) -> ClosableService:
        svc = ClosableService()
        built.append(svc)
        return svc

    reactor = TestReactor(config, base, service_factory=factory)  # type: ignore[arg-type]
    try:
        result = reactor.run(load_test_config(str(plan_path), str(tmp_path)))
    finally:
        reactor.close()

    assert result["ok"] is True, result
    assert base.calls == []
    assert [name for name, _ in built[0].calls] == ["debug_start_session", "debug_stop_session"]


def test_cli_reports_reactor_construction_failure_and_closes_service(monkeypatch: pytest.MonkeyPatch) -> None:
    # If building the per-device services fails inside TestReactor.__init__, the
    # CLI must still write a structured report, close the base service, and only
    # then re-raise the constructor error.
    from types import SimpleNamespace

    from agentic_hil.cli import run_test_reactor

    class FakeService:
        def __init__(self, config, frontend: str) -> None:
            assert frontend == "reactor"
            self.config = config
            self.coordinator = object()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services: list[FakeService] = []

    def make_service(config, frontend: str = "", **kwargs) -> FakeService:
        svc = FakeService(config, frontend=frontend or kwargs.get("frontend", ""))
        services.append(svc)
        return svc

    written: list[dict] = []

    def fake_reactor(config, service, service_factory=None):
        raise RuntimeError("device service failed to build")

    monkeypatch.setattr("agentic_hil.cli.load_authoritative_config", lambda workspace: SimpleNamespace(work_dir="."))
    monkeypatch.setattr(
        "agentic_hil.cli.load_test_config", lambda path, work_dir: SimpleNamespace(name="plan", path="plan.yaml", steps=[])
    )
    monkeypatch.setattr("agentic_hil.cli.AgenticHILToolService", lambda config, frontend: make_service(config, frontend=frontend))
    monkeypatch.setattr("agentic_hil.cli.TestReactor", fake_reactor)
    monkeypatch.setattr("agentic_hil.cli.write_report", lambda config, result: written.append(result) or result)

    with pytest.raises(RuntimeError, match="device service failed to build"):
        run_test_reactor("plan.yaml")

    assert len(services) == 1
    assert services[0].closed is True
    assert written and written[0]["error_type"] == "reactor_exception"
    assert written[0]["ok"] is False


def test_reactor_close_releases_services_built_before_a_factory_failure(tmp_path: Path) -> None:
    # Runners are built on first use, so a later probe's service can fail after
    # an earlier one already exists. close() must still release what was built.
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n  probe_c:\n    type: openocd\n    resource_id: rc\n",
            )
        )
    )

    class ClosableService(RecordingService):
        closed = False

        def close(self) -> None:
            self.closed = True

    built: list[ClosableService] = []

    def factory(bound_config) -> ClosableService:
        if built:
            raise RuntimeError("second probe service failed to build")
        svc = ClosableService()
        built.append(svc)
        return svc

    reactor = TestReactor(config, RecordingService(), service_factory=factory)  # type: ignore[arg-type]
    reactor.runner("probe_b")
    with pytest.raises(RuntimeError, match="second probe service failed to build"):
        reactor.runner("probe_c")

    reactor.close()

    assert len(built) == 1
    assert built[0].closed is True


def test_reactor_close_aggregates_all_per_debugger_errors(tmp_path: Path) -> None:
    from agentic_hil.test_reactor import TestReactor

    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml="debuggers:\n  probe_b:\n    type: openocd\n    resource_id: rb\n  probe_c:\n    type: openocd\n    resource_id: rc\n",
            )
        )
    )

    class FailingCloseService(RecordingService):
        def __init__(self, message: str) -> None:
            super().__init__()
            self.message = message

        def close(self) -> None:
            raise OSError(self.message)

    names = iter(["close-b failed", "close-c failed"])

    def factory(bound_config) -> FailingCloseService:
        return FailingCloseService(next(names))

    reactor = TestReactor(config, RecordingService(), service_factory=factory)  # type: ignore[arg-type]
    reactor.runner("probe_b")
    reactor.runner("probe_c")
    with pytest.raises(RuntimeError) as excinfo:
        reactor.close()
    assert "close-b failed" in str(excinfo.value)
    assert "close-c failed" in str(excinfo.value)
    # close() drained the owned services; a second close must not re-raise.
    reactor.close()


def test_single_probe_target_override_is_applied_on_the_bound_config(tmp_path: Path) -> None:
    # The override is documented as moving from devices.<name>.target to
    # debuggers.<name>.target. Auto-binding the only configured probe has to
    # honour it, or it would work on multi-probe projects and be silently
    # dropped on the supported single-probe path.
    config = load_config(
        str(
            write_config(
                tmp_path,
                debuggers_yaml='debuggers:\n  dut:\n    target:\n      name: "sensor-node"\n',
            )
        )
    )

    assert config.debugger_id == "dut"
    assert config.target.name == "sensor-node"
    # Unnamed fields still fall back to the project target.
    assert config.target.controller == "stm32f4"


def test_bind_debugger_applies_the_probes_own_target(tmp_path: Path) -> None:
    from agentic_hil.config import bind_debugger

    config = load_config(
        str(
            write_config(
                tmp_path,
                debugger_name="board_a",
                probe_id="PROBE-A",
                debuggers_yaml=(
                    'debuggers:\n  board_b:\n    type: openocd\n    probe_id: "PROBE-B"\n'
                    f'    executable: "{FAKE_OPENOCD.as_posix()}"\n'
                    '    target:\n      name: "sensor-node"\n'
                ),
            )
        )
    )

    assert bind_debugger(config, "board_b").target.name == "sensor-node"
    assert bind_debugger(config, "board_a").target.name == config.target.name


def test_version_1_plan_is_refused_with_the_key_that_replaced_device(tmp_path: Path) -> None:
    # Version 1 steps addressed a `device:` the config model no longer has.
    # Without a version boundary every old plan would fail on a bare const
    # mismatch with nothing pointing at the replacement.
    plan_path = write_test_config(
        tmp_path,
        "version: 1\nsteps:\n  - {device: dut, action: flash, image_path: build/app.elf}\n",
    )

    with pytest.raises(ConfigError) as rejected:
        load_test_config(str(plan_path), str(tmp_path))

    assert rejected.value.error_type == "test_config_invalid"
    assert rejected.value.details["field"] == "version"
    migration = rejected.value.details["migration"]
    assert any("debugger:" in value for value in migration.values())
    assert any("port_id:" in value for value in migration.values())


CAN_BUS_YAML = """can_buses:
  dut_can:
    adapter: "socketcan"
    channel: "can0"
    bitrate: 500000
"""


def can_config(tmp_path: Path, *, listen_only: bool = False, allow_write: bool = True, allow_read: bool = True):
    """A project with one configured CAN bus, and the flags that decide what a
    plan may do with it."""
    permissions = {**DEFAULT_TEST_PERMISSIONS, "allow_can_write": allow_write, "allow_can_read": allow_read}
    bus = CAN_BUS_YAML + ("    listen_only: true\n" if listen_only else "")
    return load_config(str(write_config(tmp_path, can_buses_yaml=bus, permissions=permissions)))


def test_reactor_runs_a_can_read_and_send_plan_end_to_end(tmp_path: Path) -> None:
    # A CAN bus is a device a plan drives like any other: it opens, it is read
    # from and written to, and it closes.
    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
name: bus-traffic
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_read, max_frames: 4, wait_timeout_s: 0.5}
  - {bus_id: dut_can, action: can_send, frame_id: "0x123", data_hex: "01ff"}
  - {bus_id: dut_can, action: can_close}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["can_open", "can_read", "can_send", "can_close"]
    assert [step["route"] for step in result["steps"]] == ["dut_can"] * 4
    # Every call names the entry the step routed to, supplied by the device
    # rather than taken from the plan's own arguments.
    assert service.calls == [
        ("can_session_start", {"clear_rx_queue": True, "bus_id": "dut_can"}),
        ("can_read", {"max_frames": 4, "wait_timeout_s": 0.5, "bus_id": "dut_can"}),
        ("can_send", {"frame_id": "0x123", "data_hex": "01ff", "bus_id": "dut_can"}),
        ("can_session_stop", {"bus_id": "dut_can"}),
    ]
    # The plan closed what it opened, so cleanup has nothing left to do.
    assert result["cleanup"] == []


def test_preflight_rejects_a_can_bus_the_config_does_not_declare(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {bus_id: missing_can, action: can_open}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].bus_id"
    assert result["validation_error"]["configured_can_buses"] == ["dut_can"]
    assert service.calls == []


def test_preflight_refuses_a_can_send_on_a_bus_that_may_not_be_written(tmp_path: Path) -> None:
    # The permission the backend would raise, moved to where the plan can still
    # be corrected: nothing is opened for a send that was never going to happen.
    config = can_config(tmp_path, allow_write=False)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_send, frame_id: 291}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["validation_error"]["field"] == "steps[1].action"
    assert result["validation_error"]["permission"] == "allow_write"
    assert result["steps"] == []
    assert service.calls == []


def test_preflight_refuses_a_can_send_on_a_listen_only_bus(tmp_path: Path) -> None:
    # Writing is permitted here; the bus itself is what refuses. `listen_only`
    # is the claim that observing this bus sends nothing, so a plan that also
    # sends on it contradicts the bus it declared — and that is a fault in the
    # plan, findable before anything reaches the bench.
    config = can_config(tmp_path, listen_only=True, allow_write=True)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_send, frame_id: "0x7ff"}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    validation = result["validation_error"]
    assert validation["field"] == "steps[1].action"
    assert validation["listen_only"] is True
    assert validation["config_field"] == "can_buses.dut_can.listen_only"
    assert "listen_only" in validation["summary"]
    assert service.calls == []


def test_reactor_closes_an_open_can_session_after_a_failed_step(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_read}
  - {bus_id: dut_can, action: can_close}
""",
    )
    service = RecordingService(fail_call="can_read")

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "can_read_failed"
    # The plan never reached its own can_close, so the run closes the session it
    # opened, exactly as it does for a UART.
    assert [item["action"] for item in result["cleanup"]] == ["can_close"]
    assert result["cleanup"][0]["bus_id"] == "dut_can"
    assert result["cleanup_ok"] is True
    assert [name for name, _ in service.calls] == ["can_session_start", "can_read", "can_session_stop"]


def test_preflight_refuses_can_traffic_before_the_bus_is_open(tmp_path: Path) -> None:
    config = can_config(tmp_path)
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {bus_id: dut_can, action: can_read}\n")
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[0].action"
    assert result["validation_error"]["summary"] == "CAN bus session must be opened before this action."
    assert service.calls == []


def test_a_can_session_may_only_be_closed_by_the_plan_that_opened_it(tmp_path: Path) -> None:
    # Preflight refuses this plan, so the ownership guard underneath it is
    # reached only by a step that got past preflight — which is exactly what it
    # is there for.
    from agentic_hil.test_reactor import TestStep

    config = can_config(tmp_path)
    service = RecordingService()

    result = TestReactor(config, service).execute_step(TestStep("can_close", {}, bus_id="dut_can"))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "can_session_not_owned"
    assert result["bus_id"] == "dut_can"
    assert service.calls == []


def test_a_plan_declares_and_locks_the_can_bus_it_drives(tmp_path: Path) -> None:
    # A device kind the reactor can drive is a device kind the run holds: the
    # bus is in what the mutex takes, or an observation from outside could reach
    # it between two of this plan's steps.
    from agentic_hil.devices import can_device
    from agentic_hil.test_reactor import declared_devices

    config = can_config(tmp_path)
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {bus_id: dut_can, action: can_open}
  - {bus_id: dut_can, action: can_close}
""",
    )

    devices = declared_devices(config, load_test_config(str(plan_path), str(tmp_path)))

    assert can_device(config, "dut_can").lock_key in devices
    # One entry per physical unit, not one per step — where a unit is every key
    # its device holds, not one: an executable debugger carries its legacy
    # `probe:<path>` alias beside `probe-exe:` so old and new processes collide
    # during an upgrade window, and counting keys would break each time a
    # device gains an alias. The set equality is the real claim.
    from agentic_hil.devices import debugger_device

    expected = {can_device(config, "dut_can").lock_key}
    expected.update(debugger_device(config).lock_keys)
    assert set(devices) == expected


def test_every_device_kind_answers_for_its_own_actions() -> None:
    # The one authority: a step action belongs to the class that serves it, and
    # the schema map, the routing keys and the tools are all read off those
    # classes. A kind naming a tool its device does not own, or a schema entry
    # the bundled schema does not define, would be a second answer.
    from importlib import resources

    from agentic_hil.test_reactor import ACTION_SCHEMAS, ROUTE_FIELDS, STEP_DEVICE_CLASSES, step_device_class

    schema = json.loads(resources.files("agentic_hil").joinpath("schemas/testconfig.schema.json").read_text(encoding="utf-8"))
    claimed: set[str] = set()
    for device_class in STEP_DEVICE_CLASSES:
        assert device_class.route_field in ROUTE_FIELDS
        for action, schema_key in device_class.step_actions.items():
            assert action not in claimed, f"{action} is claimed by two device kinds"
            claimed.add(action)
            assert schema_key in schema["$defs"], f"{action} names a schema $def that does not exist"
            assert step_device_class(action) is device_class
        for action, tool in device_class.step_tools.items():
            assert action in device_class.step_actions
            assert tool in device_class.device_class.tools, f"{tool} is not a tool {device_class.device_class.__name__} owns"

    assert claimed == set(ACTION_SCHEMAS)
    assert {"can_open", "can_close", "can_send", "can_read"} <= claimed


def test_reset_and_uart_expect_reproduce_the_demo_flow(tmp_path: Path) -> None:
    # The demo loop, declared: flash, open the line, reset into the firmware,
    # wait for its banner, close. The banner arrives in three pieces and no
    # single read contains it, so this only passes if the step matches against
    # everything read so far.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
name: hello-world
steps:
  - {debugger: dut, action: flash, image_path: build/app.elf}
  - {port_id: dut_uart, action: uart_open}
  - {debugger: dut, action: reset}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(uart_reads=[b"Hel", b"lo Wo", b"rld\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [step["action"] for step in result["steps"]] == ["flash", "uart_open", "reset", "uart_expect", "uart_close"]
    assert result["cleanup"] == []
    # The mode the schema documents reaches the tool rather than being left to it.
    assert ("reset_target", {"mode": "run"}) in service.calls
    expected = result["steps"][3]["result"]
    assert expected["ok"] is True
    assert expected["reads"] == 3
    assert expected["bytes_received"] == 13
    assert [arguments["port_id"] for name, arguments in service.calls if name == "com_read"] == ["dut_uart"] * 3


def test_uart_expect_reassembles_a_character_split_across_two_reads(tmp_path: Path) -> None:
    # The seam a decoded-text buffer cannot survive: one code point arrives half
    # in one read and half in the next. Decoding each read on its own turns both
    # halves into replacement characters, and text that did arrive would never
    # be found.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, text: "Grüße", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    banner = "Grüße\r\n".encode()
    service = RecordingService(uart_reads=[banner[:3], banner[3:]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert result["steps"][1]["result"]["reads"] == 2
    assert result["steps"][1]["result"]["bytes_received"] == len(banner)


def test_uart_expect_timeout_fails_the_run_and_reports_what_the_port_said(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 0.2}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(uart_reads=[b"boot: stage 1\r\n", b"HardFault at 0x08000abc\r\n"])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "uart_expect_timeout"
    # A failed step aborts the run like any other: the close never executed, and
    # the session the plan opened is closed by cleanup instead.
    assert [step["action"] for step in result["steps"]] == ["uart_open", "uart_expect"]
    assert [item["action"] for item in result["cleanup"]] == ["uart_close"]
    assert service.recovery_calls, "a failed run must reach the recovery seam"

    failed = result["steps"][1]["result"]
    assert failed["error_type"] == "uart_expect_timeout"
    assert failed["expected_text"] == "Hello World"
    # The point of the named error: a red result says what the port actually
    # said, so nobody has to reconnect to the board to find out.
    assert "HardFault at 0x08000abc" in failed["received_tail"]["text"]
    assert failed["received_tail"]["hex"] == b"boot: stage 1\r\nHardFault at 0x08000abc\r\n".hex()
    assert failed["received_tail_truncated"] is False
    assert failed["bytes_received"] == 40


# A short expectation and one long enough that finding it needs more bytes kept
# than the report is allowed to show. The cap is on what a red step reports, not
# on what it matched against, so both must come back the same size.
@pytest.mark.parametrize("expected_text", ["Hello World", "Hello World " * 20])
def test_uart_expect_timeout_caps_the_reported_tail(tmp_path: Path, expected_text: str) -> None:
    # A chatty port must not turn one red step into an unbounded result, and the
    # tail it does report is the end of the traffic — the part nearest whatever
    # went wrong.
    from agentic_hil.test_reactor import UART_EXPECT_TAIL_BYTES

    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        f"""version: 2
steps:
  - {{port_id: dut_uart, action: uart_open}}
  - {{port_id: dut_uart, action: uart_expect, text: "{expected_text}", timeout_s: 0.2}}
  - {{port_id: dut_uart, action: uart_close}}
""",
    )
    noise = bytes((index % 10) + 48 for index in range(4000))
    service = RecordingService(uart_reads=[noise[:1500], noise[1500:]])

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    failed = result["steps"][1]["result"]
    assert failed["error_type"] == "uart_expect_timeout"
    assert failed["bytes_received"] == 4000
    assert len(bytes.fromhex(failed["received_tail"]["hex"])) == UART_EXPECT_TAIL_BYTES
    assert failed["received_tail"]["hex"] == noise[-UART_EXPECT_TAIL_BYTES:].hex()
    assert failed["received_tail_truncated"] is True


def test_uart_expect_reports_a_failed_read_as_the_read_refused_it(tmp_path: Path) -> None:
    # A read that was refused is not an expectation that went unmet: answering it
    # with uart_expect_timeout would send an operator to look at firmware that
    # never got the chance to print anything.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 5}
  - {port_id: dut_uart, action: uart_close}
""",
    )
    service = RecordingService(fail_call="com_read")

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["failed_step"] == 2
    assert result["error_type"] == "can_read_failed"
    assert result["steps"][1]["result"]["tool"] == "com_read"
    assert len([name for name, _ in service.calls if name == "com_read"]) == 1


def test_reset_step_reports_a_backend_mode_refusal_unchanged(tmp_path: Path) -> None:
    # `init` is OpenOCD's reset-init event and nothing else runs it. A backend
    # that says so must have that answer reach the plan, not a reworded one.
    from agentic_hil.backends.common import reset_init_unsupported

    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: dut, action: reset, mode: init}\n")
    service = RecordingService(reset_result=reset_init_unsupported("pyocd", "pyOCD has no such event"))

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "not_supported"
    assert result["steps"][0]["result"]["supported_modes"] == ["run", "halt"]
    assert ("reset_target", {"mode": "init"}) in service.calls


def test_reset_step_is_refused_without_the_reset_permission(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, permissions={**DEFAULT_TEST_PERMISSIONS, "allow_reset": False})))
    plan_path = write_test_config(
        tmp_path,
        "version: 2\nsteps:\n  - {debugger: dut, action: flash, image_path: build/app.elf}\n  - {debugger: dut, action: reset}\n",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["validation_error"]["field"] == "steps[1].action"
    # Named, so the operator is sent to the grant and not to the bench.
    assert result["validation_error"]["summary"] == "Target reset is disabled for this debugger by the authoritative config."
    # Refused before the run, so the flash the plan would have done never happened.
    assert result["steps"] == []
    assert service.calls == []


def test_reset_step_is_refused_while_a_debug_session_is_open(tmp_path: Path) -> None:
    # `reset_target` is a one-shot and cannot take the lease a live debug session
    # holds, so this plan could only ever come back busy half way through.
    config = load_config(str(write_config(tmp_path)))
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: debug_start, image_path: build/app.elf}
  - {debugger: dut, action: reset}
  - {debugger: dut, action: debug_stop}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[1].action"
    assert result["validation_error"]["debug_session_debugger"] == "dut"
    assert service.calls == []


def test_new_steps_are_refused_when_they_name_a_device_the_config_does_not_declare(tmp_path: Path) -> None:
    # A route field naming nothing configured is answered by the name, not by a
    # lock failure about a device nobody declared — for the new steps as for the
    # old ones, because each is answered by the class that serves it.
    config = load_config(str(write_config(tmp_path, com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')))
    expect_plan = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {port_id: dut_uart, action: uart_open}
  - {port_id: typo_uart, action: uart_expect, text: "Hello World", timeout_s: 5}
""",
    )
    service = RecordingService()

    refused = TestReactor(config, service).run(load_test_config(str(expect_plan), str(tmp_path)))  # type: ignore[arg-type]

    assert refused["ok"] is False
    assert refused["validation_error"]["field"] == "steps[1].port_id"
    assert refused["validation_error"]["configured_com_ports"] == ["dut_uart"]
    assert service.calls == []

    reset_plan = write_test_config(tmp_path, "version: 2\nsteps:\n  - {debugger: typo, action: reset}\n")
    reset_service = RecordingService()

    reset_refused = TestReactor(config, reset_service).run(load_test_config(str(reset_plan), str(tmp_path)))  # type: ignore[arg-type]

    assert reset_refused["validation_error"]["field"] == "steps[0].debugger"
    assert reset_refused["validation_error"]["configured_debuggers"] == ["dut"]
    assert reset_service.calls == []


@pytest.mark.parametrize(
    ("step", "field"),
    [
        ("{port_id: dut_uart, action: uart_expect, timeout_s: 5}", "steps[0].text"),
        ('{port_id: dut_uart, action: uart_expect, text: "", timeout_s: 5}', "steps[0].text"),
        ('{port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: 0}', "steps[0].timeout_s"),
        ('{port_id: dut_uart, action: uart_expect, text: "Hello World", timeout_s: -1}', "steps[0].timeout_s"),
        ('{port_id: dut_uart, action: uart_expect, text: "Hello World"}', "steps[0].timeout_s"),
        ('{port_id: dut_uart, debugger: dut, action: uart_expect, text: "x", timeout_s: 5}', "steps[0].debugger"),
        ("{port_id: dut_uart, action: reset}", "steps[0].port_id"),
        ("{debugger: dut, action: reset, mode: warm}", "steps[0].mode"),
    ],
)
def test_new_steps_are_refused_by_the_bundled_schema(tmp_path: Path, step: str, field: str) -> None:
    # Both new steps answer to the same schema pass every other step does: a
    # missing expectation, a timeout that waits for no time at all, a route field
    # belonging to another device kind, and a reset mode no backend has.
    path = write_test_config(tmp_path, f"version: 2\nsteps:\n  - {step}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.error_type == "test_config_invalid"
    assert refused.value.details["field"] == field


def test_unknown_reactor_action_still_names_the_two_new_ones(tmp_path: Path) -> None:
    # The allowed set is read off the device classes, so an action nobody serves
    # is refused with a list that grew by exactly the two steps that were added.
    path = write_test_config(tmp_path, "version: 2\nsteps:\n  - {port_id: dut_uart, action: uart_wait, text: hi}\n")

    with pytest.raises(ConfigError) as refused:
        load_test_config(str(path), str(tmp_path))

    assert refused.value.details["field"] == "steps[0].action"
    assert {"reset", "uart_expect"} <= set(refused.value.details["allowed_values"])
    assert "uart_wait" not in refused.value.details["allowed_values"]
