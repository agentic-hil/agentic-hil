"""Registered plans refuse invalid CAN participant views before taking a lock."""

from __future__ import annotations

from pathlib import Path

from conftest import write_config

from agentic_hil import reactorrun
from agentic_hil.config import load_config


def test_unknown_shared_can_participant_is_reported_before_plan_lock_or_hardware(
    tmp_path: Path, monkeypatch
) -> None:
    bus = "shared_can"
    config_path = write_config(
        tmp_path,
        can_buses_yaml=f'''can_buses:
  {bus}:
    adapter: process
    channel: fake-can
    executable: fake-bridge
    shares:
      ecu_a:
        permissions:
          allow_read: true
          allow_write: true
''',
    )
    config = load_config(str(config_path))
    workspace = Path(config.workspace_root)
    plan_path = workspace / ".agentic-hil" / "testconfig.yaml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "version: 6\nsteps:\n"
        f"  - {{action: can_open, bus_id: {bus}, participant: misspelled, clear_rx_queue: false}}\n",
        encoding="utf-8",
    )

    class CoordinatorSpy:
        begin_calls = 0

        def begin_run(self, *args, **kwargs):
            self.begin_calls += 1
            raise AssertionError("preflight refusal must not acquire a hardware lock")

    coordinator = CoordinatorSpy()

    class ServiceSpy:
        def __init__(self, bound_config, **kwargs):
            self.config = bound_config
            self.coordinator = coordinator
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(reactorrun, "AgenticHILToolService", ServiceSpy)
    junit_path = tmp_path / "preflight.xml"

    result = reactorrun.run_plan(config, str(plan_path), junit_xml=str(junit_path))

    assert result["ok"] is False
    assert result["error_type"] == "test_config_invalid"
    assert result["failed_step"] == 1
    assert result["validation_error"]["field"] == "steps[0].participant"
    assert "no such participant" in result["validation_error"]["summary"].lower()
    assert result["validation_error"]["configured_participants"] == ["ecu_a"]
    assert coordinator.begin_calls == 0
    assert junit_path.is_file()
    assert "test_config_invalid" in junit_path.read_text(encoding="utf-8")
