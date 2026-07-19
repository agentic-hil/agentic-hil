from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FAKE_GDB, write_authoritative_config, write_config

from agentic_hil.cli import build_parser, entrypoint
from agentic_hil.config import ConfigError, load_config
from agentic_hil.test_reactor import TestReactor, load_test_config
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
        devices_yaml="devices:\n  dut:\n    debugger: true\n    uart: dut_uart\n",
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


def test_reactor_runs_flash_uart_breakpoint_and_memory_dump(tmp_path: Path) -> None:
    service, com_ports = reactor_service(tmp_path)
    test_path = write_test_config(
        tmp_path,
        """version: 1
name: capture-state
steps:
  - device: dut
    action: flash
    image_path: build/app.elf
  - device: dut
    action: uart_open
  - device: dut
    action: debug_start
    image_path: build/app.elf
    mode: attach
  - device: dut
    action: run_until_breakpoint
    location:
      symbol: test_done
    timeout_s: 5
  - device: dut
    action: dump_memory
    symbol: CTC_array
    output_path: build/new/nested/memory.hex
  - device: dut
    action: debug_stop
  - device: dut
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
        """version: 1
steps:
  - {device: dut, action: uart_open}
  - {device: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {device: dut, action: run_until_breakpoint, location: test_done, timeout_s: 5}
  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/should-not-exist.hex}
""",
    )
    try:
        result = TestReactor(service.config, service).run(load_test_config(str(test_path), str(tmp_path)))
    finally:
        service.close()

    assert result["ok"] is False
    assert result["failed_step"] == 3
    assert result["error_type"] == "unexpected_breakpoint"
    assert [item["action"] for item in result["cleanup"]] == ["debug_stop", "uart_close"]
    assert com_ports.active == set()
    assert not (tmp_path / "build" / "should-not-exist.hex").exists()


def test_test_config_accepts_json_and_rejects_unknown_actions(tmp_path: Path) -> None:
    json_path = tmp_path / "testconfig.json"
    json_path.write_text(
        json.dumps({"version": 1, "steps": [{"device": "dut", "action": "uart_open"}]}),
        encoding="utf-8",
    )
    assert load_test_config(str(json_path), str(tmp_path)).steps[0].action == "uart_open"

    invalid_path = write_test_config(tmp_path, "version: 1\nsteps:\n  - {device: dut, action: shell}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(invalid_path), str(tmp_path))
    assert excinfo.value.error_type == "test_config_invalid"
    assert excinfo.value.details["field"] == "steps[0].action"
    assert excinfo.value.details["value"] == "shell"


def test_test_config_rejects_duplicate_keys_with_location(tmp_path: Path) -> None:
    path = write_test_config(
        tmp_path,
        "version: 1\nsteps:\n  - device: dut\n    action: uart_open\n    action: uart_close\n",
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
        f"version: 1\nname:\n  secret: {secret}\nsteps:\n  - {{device: dut, action: uart_open}}\n",
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
    plan.write_text("version: 1\nsteps:\n  - {device: dut, action: uart_open}\n", encoding="utf-8")

    with pytest.raises(ConfigError) as rejected:
        load_test_config(str(plan), str(workspace))

    assert rejected.value.error_type == "test_config_invalid"
    assert rejected.value.details["workspace_root"] == str(workspace.resolve())


def test_preflight_rejects_late_unknown_device_before_flash(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                devices_yaml="devices:\n  dut:\n    debugger: true\n",
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 1
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: typo, action: flash, image_path: build/app.elf}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["failed_step"] == 2
    assert result["steps"] == []
    assert service.calls == []


def test_preflight_rejects_cross_device_uart_close(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                devices_yaml=(
                    "devices:\n  controller_a:\n    uart: shared\n"
                    "  controller_b:\n    uart: shared\n"
                ),
                com_ports_yaml='com_ports:\n  shared:\n    device: "COM_TEST"\n',
            )
        )
    )
    plan_path = write_test_config(
        tmp_path,
        "version: 1\nsteps:\n  - {device: controller_a, action: uart_open}\n  - {device: controller_b, action: uart_close}\n",
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
        """version: 1
steps:
  - {device: dut, action: debug_start, image_path: build/app.elf, mode: attach}
  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/new/nested/memory.hex}
  - {device: missing, action: flash, image_path: build/app.elf}
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
                devices_yaml="devices:\n  dut:\n    debugger: true\n",
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 1
steps:
  - {device: dut, action: flash, image_path: build/app.elf}
  - {device: dut, action: debug_start, image_path: build/app.elf}
  - {device: dut, action: run_until_breakpoint, location: test_done}
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
                devices_yaml="devices:\n  dut:\n    debugger: true\n    uart: dut_uart\n",
                com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n',
            )
        ),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 1
steps:
  - {device: dut, action: uart_open}
  - {device: dut, action: debug_start, image_path: build/app.elf}
  - {device: dut, action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}
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
        str(write_config(tmp_path, devices_yaml="devices:\n  dut:\n    debugger: true\n")),
    )
    plan_path = write_test_config(tmp_path, "version: 1\nsteps:\n  - {device: dut, action: flash, image_path: build/app.elf}\n")
    service = RecordingService(raise_flash=True)

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "step_exception"
    assert result["steps"][0]["result"]["exception_type"] == "OSError"


def test_reactor_treats_audit_failure_as_failed_step(tmp_path: Path) -> None:
    config = load_config(
        str(write_config(tmp_path, devices_yaml="devices:\n  dut:\n    debugger: true\n    uart: dut_uart\n", com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_TEST"\n')),
    )
    plan_path = write_test_config(
        tmp_path,
        "version: 1\nsteps:\n  - {device: dut, action: flash, image_path: build/app.elf}\n  - {device: dut, action: uart_open}\n",
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
    config = load_config(str(write_config(tmp_path, devices_yaml="devices:\n  dut:\n    debugger: true\n")))
    plan_path = write_test_config(
        tmp_path,
        "version: 1\nsteps:\n  - {device: dut, action: debug_start, image_path: build/app.elf}\n  - {device: dut, action: run_until_breakpoint, location: test_done}\n",
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
        str(write_config(tmp_path, devices_yaml="devices:\n  dut:\n    debugger: true\n")),
    )
    plan_path = write_test_config(
        tmp_path,
        """version: 1
steps:
  - {device: dut, action: debug_start, image_path: build/app.elf}
  - {device: dut, action: run_until_breakpoint, location: first_stop}
  - {device: dut, action: run_until_breakpoint, location: second_stop}
  - {device: dut, action: debug_stop}
""",
    )
    service = RecordingService()

    result = TestReactor(config, service).run(load_test_config(str(plan_path), str(tmp_path)))  # type: ignore[arg-type]

    assert result["ok"] is True, result
    assert [name for name, _ in service.calls].count("debug_clear_breakpoints") == 2


def test_main_config_rejects_device_with_unknown_uart(tmp_path: Path) -> None:
    config_path = write_config(tmp_path, devices_yaml="devices:\n  dut:\n    uart: missing_uart\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(config_path))
    assert excinfo.value.error_type == "config_invalid"
    assert excinfo.value.details["field"] == "devices.dut.uart"


def test_single_config_retains_device_uart_mapping(tmp_path: Path) -> None:
    config = load_config(
        str(
            write_config(
                tmp_path,
                devices_yaml="devices:\n  dut:\n    debugger: true\n    uart: dut_uart\n",
                com_ports_yaml='com_ports:\n  dut_uart:\n    device: "COM_PROJECT"\n',
            )
        )
    )

    assert config.devices["dut"].debugger is True
    assert config.devices["dut"].uart == "dut_uart"


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
    test_path = write_test_config(tmp_path, "version: 1\nsteps:\n  - {device: dut, action: shell}\n")
    monkeypatch.chdir(tmp_path)

    exit_code = entrypoint(["test-reactor", "--test-config", str(test_path)])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["error_type"] == "test_config_invalid"


def test_cli_returns_failure_for_audit_failed_result(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("agentic_hil.cli.run_test_reactor", lambda _path: {"ok": True, "audit_ok": False})

    exit_code = entrypoint(["test-reactor"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["audit_ok"] is False
