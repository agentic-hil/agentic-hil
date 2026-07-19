from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import FAKE_GDB, FAKE_OPENOCD, write_config

from agentic_hil.cli import entrypoint
from agentic_hil.config import ConfigError, load_config
from agentic_hil.hardware_lock import HardwareQuarantinedError
from agentic_hil.test_reactor import (
    ProjectTestLock,
    load_test_config,
)
from agentic_hil.test_reactor import (
    TestReactor as Reactor,
)
from agentic_hil.tools import AgenticHILToolService


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def run_action(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        self.active -= 1


class FakeArtifacts:
    def validate_local_path(self, image_path: str) -> dict:
        return {"ok": True, "artifact": {"path": image_path, "resolved_path": image_path}}

    def validate_output_path(self, output_path: str, tool: str) -> dict:
        return {"ok": True, "output": {"path": output_path, "resolved_path": output_path}}


class RecordingService:
    def __init__(self, tracker: ConcurrencyTracker) -> None:
        self.tracker = tracker
        self.artifacts = FakeArtifacts()
        self.calls: list[str] = []
        self.closed = False

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append(name)
        if name == "flash_firmware":
            self.tracker.run_action()
        return {"ok": True, "tool": name}

    def close(self) -> None:
        self.closed = True

    def hardware_state(self) -> dict:
        return {"active": False, "state_confirmed": True, "active_resources": [], "inspection_errors": []}


class CleanupFailureService(RecordingService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        if name == "debug_stop_session":
            return {"ok": False, "tool": name, "error_type": "cleanup_failed"}
        return result


class StepAndCleanupFailureService(CleanupFailureService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name == "debug_set_breakpoint":
            return {"ok": True, "breakpoint": {"id": "1"}}
        if name == "debug_continue":
            return {"ok": False, "tool": name, "error_type": "target_exception"}
        return super().call(name, arguments)


class FailingFlashService(RecordingService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        if name == "flash_firmware":
            return {"ok": False, "tool": name, "error_type": "flash_failed", "summary": "Flash failed."}
        return result


class CloseFailureService(RecordingService):
    def close(self) -> None:
        self.closed = True
        raise RuntimeError("device close failed")


class ActiveCloseFailureService(CloseFailureService):
    def hardware_state(self) -> dict:
        return {"active": True, "state_confirmed": True, "active_resources": [{"type": "debugger", "id": "default"}], "inspection_errors": []}


class AuditCloseFailureService(CloseFailureService):
    pass


class InterruptingActiveService(RecordingService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        raise KeyboardInterrupt

    def hardware_state(self) -> dict:
        return {"active": True, "state_confirmed": True, "active_resources": [{"type": "debugger", "id": "default"}], "inspection_errors": []}


class InterruptingInactiveService(RecordingService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        raise KeyboardInterrupt


class MalformedStepService(RecordingService):
    def call(self, name: str, arguments: dict | None = None):
        return None


class UnconfirmedStepService(RecordingService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append(name)
        return {"ok": True, "tool": name, "error_type": "hardware_cleanup_failed", "hardware_state_unconfirmed": True}


class ConfirmedExceptionService(RecordingService):
    def __init__(self, tracker: ConcurrencyTracker, error_type: type[BaseException]) -> None:
        super().__init__(tracker)
        self.error_type = error_type

    def call(self, name: str, arguments: dict | None = None) -> dict:
        error = self.error_type("report failed")
        error._agentic_hil_completion_confirmed = True
        raise error


class PolicyChangingService(RecordingService):
    def __init__(self, tracker: ConcurrencyTracker, config_path: str) -> None:
        super().__init__(tracker)
        self.config_path = Path(config_path)
        self.changed = False

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = super().call(name, arguments)
        if name == "flash_firmware" and not self.changed:
            self.changed = True
            self.config_path.write_text(self.config_path.read_text(encoding="utf-8") + "\npermissions:\n  allow_flash: false\n", encoding="utf-8")
        return result


class ResolvedBinArtifacts(FakeArtifacts):
    def validate_local_path(self, image_path: str) -> dict:
        return {"ok": True, "artifact": {"path": image_path, "resolved_path": "build/app.bin"}}


class ResolvedBinService(RecordingService):
    def __init__(self, tracker: ConcurrencyTracker) -> None:
        super().__init__(tracker)
        self.artifacts = ResolvedBinArtifacts()


class ExplodingArtifacts(FakeArtifacts):
    def validate_local_path(self, image_path: str) -> dict:
        raise RuntimeError("preflight exploded")


class ExplodingPreflightService(RecordingService):
    def __init__(self, tracker: ConcurrencyTracker) -> None:
        super().__init__(tracker)
        self.artifacts = ExplodingArtifacts()


def write_plan(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".agentic-hil" / "testconfig.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def multi_device_config(tmp_path: Path, shared_debugger: bool = False) -> Path:
    second_debugger = "probe_a" if shared_debugger else "probe_b"
    return write_config(
        tmp_path,
        debuggers_yaml=f'''debuggers:
  probe_a:
    type: "openocd"
    executable: "{FAKE_OPENOCD.as_posix()}"
    probe_id: "PROBE_A"
  probe_b:
    type: "openocd"
    executable: "{FAKE_OPENOCD.as_posix()}"
    probe_id: "PROBE_B"
''',
        devices_yaml=f'''devices:
  controller_a:
    debugger: "probe_a"
    target:
      name: "controller-a"
      controller: "stm32f4"
  controller_b:
    debugger: "{second_debugger}"
    target:
      name: "controller-b"
      controller: "stm32f4"
''',
    )


def serial_plan(tmp_path: Path) -> Path:
    return write_plan(
        tmp_path,
        """version: 1
tests:
  - name: test-a
    device: controller_a
    steps:
      - {action: flash, image_path: build/a.elf}
  - name: test-b
    device: controller_b
    steps:
      - {action: flash, image_path: build/b.elf}
""",
    )


def test_config_loads_named_debuggers_and_devices(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))

    assert config.devices["controller_a"].debugger == "probe_a"
    assert config.devices["controller_b"].target.name == "controller-b"
    assert config.debuggers["probe_a"].probe_id == "PROBE_A"


def test_device_target_override_inherits_unspecified_project_fields(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        devices_yaml='''devices:
  controller:
    debugger: "default"
    target:
      name: "renamed-controller"
''',
    )

    config = load_config(str(config_path), str(tmp_path))

    assert config.devices["controller"].target.name == "renamed-controller"
    assert config.devices["controller"].target.controller == config.target.controller


def test_test_config_expands_user_home_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plan_path = home / "user-test.yaml"
    plan_path.write_text(
        """version: 1
tests:
  - name: user-test
    device: dut
    steps:
      - {action: flash, image_path: build/app.elf}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    plan = load_test_config("~/user-test.yaml", str(tmp_path))

    assert plan.path == str(plan_path.resolve())


def test_tests_run_serially_even_with_different_devices_and_probes(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))
    tracker = ConcurrencyTracker()

    result = Reactor(config, service_factory=lambda _: RecordingService(tracker)).run(plan)

    assert result["ok"] is True, result
    assert tracker.max_active == 1


def test_shared_probe_also_runs_serially(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path, shared_debugger=True)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))
    tracker = ConcurrencyTracker()

    result = Reactor(config, service_factory=lambda _: RecordingService(tracker)).run(plan)

    assert result["ok"] is True, result
    assert tracker.max_active == 1


def test_project_lock_rejects_a_concurrent_reactor_run(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))
    held_lock = ProjectTestLock(config.config_path)
    assert held_lock.acquire() is True
    try:
        result = Reactor(config, service_factory=lambda _: RecordingService(ConcurrencyTracker())).run(plan)
    finally:
        held_lock.confirm_safe_and_release()

    assert result["ok"] is False
    assert result["error_type"] == "test_reactor_busy"
    assert result["tests"] == []


def test_project_lock_blocks_regular_tool_service_hardware_calls(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path)), str(tmp_path))
    held_lock = ProjectTestLock(config.config_path)
    assert held_lock.acquire() is True
    service = AgenticHILToolService(config)
    try:
        result = service.call("probe_target")
    finally:
        service.close()
        held_lock.confirm_safe_and_release()

    assert result["ok"] is False
    assert result["error_type"] == "hardware_busy"


def test_active_tool_service_debug_session_blocks_reactor(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        gdb_executable=FAKE_GDB,
        devices_yaml='''devices:
  dut:
    debugger: "default"
''',
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    plan_path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: blocked
    device: dut
    steps:
      - {action: flash, image_path: build/app.elf}
""",
    )
    config = load_config(str(config_path), str(tmp_path))
    service = AgenticHILToolService(config)
    try:
        started = service.call("debug_start_session", {"image_path": "build/app.elf", "mode": "attach"})
        assert started["ok"] is True, started
        result = Reactor(config).run(load_test_config(str(plan_path), str(tmp_path)))
        stopped = service.call("debug_stop_session")
        assert stopped["ok"] is True, stopped
    finally:
        service.close()

    assert result["ok"] is False
    assert result["error_type"] == "test_reactor_busy"


def test_device_services_are_created_only_after_lock_and_partial_initialization_is_closed(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))
    services: list[RecordingService] = []

    def service_factory(_: object) -> RecordingService:
        if services:
            raise RuntimeError("second device failed")
        service = RecordingService(ConcurrencyTracker())
        services.append(service)
        return service

    reactor = Reactor(config, service_factory=service_factory)
    assert services == []
    result = reactor.run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "device_initialization_failed"
    assert services[0].closed is True
    lock = ProjectTestLock(config.config_path)
    assert lock.acquire() is True
    lock.confirm_safe_and_release()


def test_lock_io_failure_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))
    invalid_temp_root = tmp_path / "not-a-directory"
    invalid_temp_root.write_text("file", encoding="utf-8")
    monkeypatch.setattr("agentic_hil.hardware_lock.hardware_state_directory", lambda: invalid_temp_root)
    services: list[RecordingService] = []

    def service_factory(_: object) -> RecordingService:
        service = RecordingService(ConcurrencyTracker())
        services.append(service)
        return service

    result = Reactor(config, service_factory=service_factory).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "test_reactor_lock_failed"
    assert services == []


def test_preflight_exception_closes_services_and_releases_project_lock(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))
    services: list[ExplodingPreflightService] = []

    def service_factory(_: object) -> ExplodingPreflightService:
        service = ExplodingPreflightService(ConcurrencyTracker())
        services.append(service)
        return service

    result = Reactor(config, service_factory=service_factory).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "preflight_exception"
    assert all(service.closed for service in services)
    lock = ProjectTestLock(config.config_path)
    assert lock.acquire() is True
    lock.confirm_safe_and_release()


def test_cleanup_failure_aborts_remaining_tests(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: leaves-debugger-dirty
    device: controller_a
    steps:
      - {action: debug_start, image_path: build/app.elf}
  - name: must-not-run
    device: controller_b
    steps:
      - {action: flash, image_path: build/app.elf}
""",
    )
    tracker = ConcurrencyTracker()

    result = Reactor(config, service_factory=lambda _: CleanupFailureService(tracker)).run(load_test_config(str(path), str(tmp_path)))

    assert result["ok"] is False
    assert result["tests"][0]["error_type"] == "cleanup_failed"
    assert result["aborted_tests"] == ["must-not-run"]
    assert result["error_type"] == "unsafe_test_state"
    assert tracker.max_active == 0


def test_step_failure_cannot_mask_cleanup_failure(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: unsafe-failure
    device: controller_a
    steps:
      - {action: debug_start, image_path: build/app.elf}
      - {action: run_until_breakpoint, location: test_done}
  - name: must-not-run
    device: controller_b
    steps:
      - {action: flash, image_path: build/app.elf}
""",
    )

    result = Reactor(config, service_factory=lambda _: StepAndCleanupFailureService(ConcurrencyTracker())).run(load_test_config(str(path), str(tmp_path)))

    assert result["error_type"] == "unsafe_test_state"
    assert result["tests"][0]["error_type"] == "cleanup_failed"
    assert result["tests"][0]["step_error_type"] == "target_exception"
    assert result["aborted_tests"] == ["must-not-run"]


def test_device_close_failure_is_reported_as_unsafe_state(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    result = Reactor(config, service_factory=lambda _: CloseFailureService(ConcurrencyTracker())).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "unsafe_test_state"
    assert result["cleanup_error_type"] == "device_close_failed"
    assert result["failed_tests"] == []


def test_reactor_quarantines_project_when_cleanup_leaves_active_hardware(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    result = Reactor(config, service_factory=lambda _: ActiveCloseFailureService(ConcurrencyTracker())).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "unsafe_test_state"
    assert result["hardware_state_unconfirmed"] is True
    assert result["quarantine"]["reason"] == "hardware_cleanup_failed"
    assert result["active_resources"] == [{"device": "controller_a", "type": "debugger", "id": "default"}, {"device": "controller_b", "type": "debugger", "id": "default"}]
    with pytest.raises(HardwareQuarantinedError):
        ProjectTestLock(config.config_path).acquire()


def test_reactor_does_not_quarantine_when_cleanup_error_has_no_active_resource(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    result = Reactor(config, service_factory=lambda _: AuditCloseFailureService(ConcurrencyTracker())).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "unsafe_test_state"
    assert "quarantine" not in result
    second_lock = ProjectTestLock(config.config_path)
    assert second_lock.acquire() is True
    second_lock.confirm_safe_and_release()


def test_keyboard_interrupt_quarantines_active_reactor_hardware(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    with pytest.raises(KeyboardInterrupt):
        Reactor(config, service_factory=lambda _: InterruptingActiveService(ConcurrencyTracker())).run(plan)

    state = ProjectTestLock(config.config_path).status()
    assert state["quarantined"] is True
    assert state["state"]["reason"] == "hardware_cleanup_failed"
    assert state["state"]["active_resources"] == [{"device": "controller_a", "type": "debugger", "id": "default"}, {"device": "controller_b", "type": "debugger", "id": "default"}]


def test_keyboard_interrupt_without_session_quarantines_reactor_step(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    with pytest.raises(KeyboardInterrupt):
        Reactor(config, service_factory=lambda _: InterruptingInactiveService(ConcurrencyTracker())).run(plan)

    state = ProjectTestLock(config.config_path).status()
    assert state["hardware_state_unconfirmed"] is True
    assert state["state"]["inspection_errors"][0]["action"] == "flash"


def test_malformed_step_result_aborts_and_quarantines_reactor(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    result = Reactor(config, service_factory=lambda _: MalformedStepService(ConcurrencyTracker())).run(plan)

    assert result["error_type"] == "unsafe_test_state"
    assert result["aborted_tests"] == ["test-b"]
    state = ProjectTestLock(config.config_path).status()
    assert state["hardware_state_unconfirmed"] is True
    assert state["state"]["inspection_errors"][0]["error_type"] == "TypeError"


def test_unconfirmed_step_result_aborts_remaining_devices(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))
    services: list[UnconfirmedStepService] = []

    def service_factory(_):
        service = UnconfirmedStepService(ConcurrencyTracker())
        services.append(service)
        return service

    result = Reactor(config, service_factory=service_factory).run(plan)

    assert result["error_type"] == "unsafe_test_state"
    assert result["tests"][0]["error_type"] == "hardware_state_unconfirmed"
    assert result["aborted_tests"] == ["test-b"]
    assert services[1].calls == []
    state = ProjectTestLock(config.config_path).status()
    assert state["hardware_state_unconfirmed"] is True
    assert state["state"]["inspection_errors"][0]["action"] == "flash"
    assert state["state"]["inspection_errors"][0]["device"] == "controller_a"


def test_confirmed_step_exception_fails_without_quarantine(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    result = Reactor(config, service_factory=lambda _: ConfirmedExceptionService(ConcurrencyTracker(), RuntimeError)).run(plan)

    assert result["ok"] is False
    assert result["tests"][0]["error_type"] == "step_exception"
    assert result.get("error_type") != "unsafe_test_state"
    assert ProjectTestLock(config.config_path).status()["hardware_state_unconfirmed"] is False


def test_confirmed_step_base_exception_is_reraised_without_quarantine(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    with pytest.raises(KeyboardInterrupt):
        Reactor(config, service_factory=lambda _: ConfirmedExceptionService(ConcurrencyTracker(), KeyboardInterrupt)).run(plan)

    assert ProjectTestLock(config.config_path).status()["hardware_state_unconfirmed"] is False


def test_policy_change_blocks_remaining_hardware_steps_and_tests(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: policy-change
    device: controller_a
    steps:
      - {action: flash, image_path: build/app.elf}
      - {action: flash, image_path: build/app.elf}
  - name: must-not-run
    device: controller_b
    steps:
      - {action: flash, image_path: build/app.elf}
""",
    )
    services: list[PolicyChangingService] = []

    def service_factory(device_config: object) -> PolicyChangingService:
        service = PolicyChangingService(ConcurrencyTracker(), config.config_path)
        services.append(service)
        return service

    result = Reactor(config, service_factory=service_factory).run(load_test_config(str(path), str(tmp_path)))

    assert result["ok"] is False
    assert result["error_type"] == "policy_changed"
    assert result["failed_tests"] == ["policy-change"]
    assert result["aborted_tests"] == ["must-not-run"]
    assert services[0].calls.count("flash_firmware") == 1


def test_failed_reactor_result_is_authoritative_last_report(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: failing-flash
    device: controller_a
    steps:
      - {action: flash, image_path: build/app.elf}
""",
    )

    result = Reactor(config, service_factory=lambda _: FailingFlashService(ConcurrencyTracker())).run(load_test_config(str(path), str(tmp_path)))
    report = json.loads((tmp_path / ".agentic-hil" / "reports" / "last-report.json").read_text(encoding="utf-8"))

    assert result["ok"] is False
    assert result["error_type"] == "test_failed"
    assert result["failed_tests"] == ["failing-flash"]
    assert report["tool"] == "test_reactor"
    assert report["error_type"] == "test_failed"
    assert report["failed_tests"] == ["failing-flash"]
    service = AgenticHILToolService(config)
    try:
        classified = service.call("classify_last_error")
    finally:
        service.close()
    assert classified["error_type"] == "test_failed"


def test_bin_flash_without_backend_address_fails_preflight(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, debugger_type="stlink", devices_yaml='''devices:\n  dut:\n    debugger: "default"\n''')), str(tmp_path))
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: binary
    device: dut
    steps:
      - {action: flash, image_path: build/app.bin}
""",
    )
    tracker = ConcurrencyTracker()

    result = Reactor(config, service_factory=lambda _: RecordingService(tracker)).run(load_test_config(str(path), str(tmp_path)))

    assert result["ok"] is False
    assert "flash_address" in result["validation_errors"][0]["summary"]
    assert tracker.max_active == 0


def test_preflight_checks_resolved_artifact_extension(tmp_path: Path) -> None:
    config = load_config(str(write_config(tmp_path, devices_yaml='''devices:\n  dut:\n    debugger: "default"\n''')), str(tmp_path))
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: disguised-binary
    device: dut
    steps:
      - {action: debug_start, image_path: build/app.elf}
""",
    )

    result = Reactor(config, service_factory=lambda _: ResolvedBinService(ConcurrencyTracker())).run(load_test_config(str(path), str(tmp_path)))

    assert result["ok"] is False
    assert "ELF" in result["validation_errors"][0]["summary"]


def test_preflight_rejects_unknown_device_before_any_test_runs(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: typo
    device: missing
    steps:
      - {action: flash, image_path: build/app.elf}
""",
    )
    tracker = ConcurrencyTracker()

    result = Reactor(config, service_factory=lambda _: RecordingService(tracker)).run(load_test_config(str(path), str(tmp_path)))

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert tracker.max_active == 0


def test_reactor_runs_debug_breakpoint_and_ihex_dump(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        gdb_executable=FAKE_GDB,
        devices_yaml='''devices:
  dut:
    debugger: "default"
''',
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    plan_path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: capture
    device: dut
    steps:
      - {action: debug_start, image_path: build/app.elf, mode: attach}
      - {action: run_until_breakpoint, location: test_done, timeout_s: 5}
      - {action: dump_memory, symbol: CTC_array, output_path: build/memory.hex}
      - {action: debug_stop}
""",
    )

    result = Reactor(load_config(str(config_path), str(tmp_path))).run(load_test_config(str(plan_path), str(tmp_path)))

    assert result["ok"] is True, result
    lines = (tmp_path / "build" / "memory.hex").read_text(encoding="ascii").splitlines()
    assert lines[0] == ":020000042000DA"
    assert lines[-1] == ":00000001FF"


def test_reactor_cli_runs_explicit_test_configuration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = write_config(
        tmp_path,
        gdb_executable=FAKE_GDB,
        devices_yaml='''devices:
  dut:
    debugger: "default"
''',
    )
    elf_path = tmp_path / "build" / "app.elf"
    elf_path.parent.mkdir(parents=True, exist_ok=True)
    elf_path.write_bytes(b"\x7fELF" + b"\x00" * 12)
    plan_path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: cli-capture
    device: dut
    steps:
      - {action: debug_start, image_path: build/app.elf, mode: attach}
      - {action: run_until_breakpoint, location: test_done, timeout_s: 5}
      - {action: dump_memory, symbol: CTC_array, output_path: build/cli-memory.hex}
      - {action: debug_stop}
""",
    )

    exit_code = entrypoint(["test-reactor", "--config", str(config_path), "--test-config", str(plan_path)])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["ok"] is True
    assert response["tests"][0]["name"] == "cli-capture"
    assert (tmp_path / "build" / "cli-memory.hex").is_file()


def test_test_config_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: duplicate
    device: dut
    device: other
    steps:
      - {action: uart_open}
""",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_test_config(str(path), str(tmp_path))

    assert excinfo.value.error_type == "test_config_invalid"
    assert "duplicate key 'device'" in excinfo.value.details["backend_error"]


class AuditOnlyCloseService(RecordingService):
    def close(self) -> None:
        from agentic_hil.report import AuditWriteError

        self.closed = True
        raise AuditWriteError("stop log", None, "confirmed")


def test_reactor_audit_only_cleanup_failure_is_not_unsafe_state(tmp_path: Path) -> None:
    config = load_config(str(multi_device_config(tmp_path)), str(tmp_path))
    plan = load_test_config(str(serial_plan(tmp_path)), str(tmp_path))

    result = Reactor(config, service_factory=lambda _: AuditOnlyCloseService(ConcurrencyTracker())).run(plan)

    assert result["ok"] is False
    assert result["error_type"] == "audit_write_failed"
    assert result.get("hardware_state_unconfirmed") is not True
    assert "quarantine" not in result
    lock = ProjectTestLock(config.config_path)
    assert lock.acquire() is True
    lock.confirm_safe_and_release()


class UnsafeStepAuditCleanupService(RecordingService):
    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.calls.append(name)
        if name == "flash_firmware":
            return {"ok": False, "tool": name, "error_type": "hardware_state_unconfirmed", "hardware_state_unconfirmed": True, "summary": "Stimulus completion was not confirmed."}
        if name == "com_session_stop":
            return {"ok": False, "tool": name, "error_type": "audit_write_failed", "completion_confirmed": True, "resources_stopped": True, "summary": "Stop log could not be written."}
        return {"ok": True, "tool": name}


def test_unsafe_step_with_audit_only_cleanup_still_aborts_the_plan(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        devices_yaml="""devices:
  controller_a:
    debugger: "default"
    uart: "dut"
    target:
      name: "controller-a"
      controller: "stm32f4"
""",
        com_ports_yaml='com_ports:\n  dut:\n    device: "/dev/ttyAGENTIC_HILTEST"\n',
    )
    config = load_config(str(config_path), str(tmp_path))
    plan_path = write_plan(
        tmp_path,
        """version: 1
tests:
  - name: test-a
    device: controller_a
    steps:
      - {action: uart_open}
      - {action: flash, image_path: build/a.elf}
  - name: test-b
    device: controller_a
    steps:
      - {action: flash, image_path: build/a.elf}
""",
    )
    services: list[UnsafeStepAuditCleanupService] = []

    def factory(_config):
        service = UnsafeStepAuditCleanupService(ConcurrencyTracker())
        services.append(service)
        return service

    result = Reactor(config, service_factory=factory).run(load_test_config(str(plan_path), str(tmp_path)))

    assert result["ok"] is False
    assert result["error_type"] == "unsafe_test_state"
    assert result["aborted_tests"] == ["test-b"]
    assert len(result["tests"]) == 1
    assert result["tests"][0]["error_type"] == "hardware_state_unconfirmed"
    assert result["tests"][0]["cleanup_audit_write_failed"] is True
    flash_calls = sum(service.calls.count("flash_firmware") for service in services)
    assert flash_calls == 1, "no further hardware action may run after an unconfirmed step"
