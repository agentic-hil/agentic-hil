"""Contract for the opt-in CubeProgrammer bench setup and its evidence."""

from __future__ import annotations

import json
from pathlib import Path

from tests.bench.cubeprogrammer_recordings import configure_cubeprogrammer, programmer_recording


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, name: str, payload: dict | None = None) -> tuple[bool, dict]:
        self.calls.append((name, payload or {}))
        return False, {"ok": True}


class FakeBench:
    def __init__(self, root: Path, connect_mode: str) -> None:
        self.project = root / "project"
        self.config_root = root / "config"
        self.state_root = root / "state"
        self._configuration = {
            "debuggers": {"dut": {"probe_id": "STLINK123", "connect_mode": connect_mode}},
            "com_ports": {"uart": {"device": "/dev/ttyACM0"}},
        }

    def configuration(self) -> dict:
        return self._configuration


def test_cube_bench_configures_only_its_temporary_debugger_for_under_reset() -> None:
    server = FakeServer()

    configure_cubeprogrammer(server, "dut", Path("/opt/st/STM32_Programmer_CLI"))

    assert server.calls == [
        (
            "project_config_set",
            {
                "changes": [
                    {"key": "debuggers.dut.type", "value": "stlink"},
                    {"key": "debuggers.dut.executable", "value": str(Path("/opt/st/STM32_Programmer_CLI"))},
                    {"key": "debuggers.dut.connect_mode", "value": "under_reset"},
                ]
            },
        ),
        ("project_config_reload_description", {}),
    ]


def test_cube_recording_names_effective_flash_connect_mode(tmp_path: Path) -> None:
    bench = FakeBench(tmp_path, "under_reset")

    encoded = programmer_recording(bench, "dut", "uart", Path("/opt/st/STM32_Programmer_CLI"), {"ok": True})

    assert json.loads(encoded)["connect_mode"] == "under_reset"
