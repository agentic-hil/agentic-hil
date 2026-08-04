from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_GDB, FAKE_OPENOCD, write_authoritative_config, write_config

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
    def __init__(self, *, fail_cleanup: bool = False, raise_flash: bool = False, audit_flash_failure: bool = False, audit_failure_call: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_cleanup = fail_cleanup
        self.raise_flash = raise_flash
        self.audit_flash_failure = audit_flash_failure
        self.audit_failure_call = audit_failure_call
        self.breakpoint_id = 0

    def call(self, name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        self.calls.append((name, arguments))
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


def test_preflight_refuses_running_the_target_without_the_execution_grant(tmp_path: Path) -> None:
    """`run_until_breakpoint` continues the target, so it is a write.

    The plan is refused before anything is flashed or attached: preflight is the
    only diagnosis an operator gets, and it names the flag that fired."""
    config = load_config(
        str(
            write_config(
                tmp_path,
                permissions={"allow_probe": True, "allow_flash": True, "allow_reset": True, "allow_debug_execution": False},
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 2
steps:
  - {debugger: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {debugger: dut, action: run_until_breakpoint, location: test_done}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["validation_error"]["field"] == "steps[1].action"
    assert result["validation_error"]["permission"] == "allow_debug_execution"
    assert service.calls == []


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
