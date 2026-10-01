"""Build-time check of the optional STM32CubeCLT image layer.

Selected explicitly by the `bench-tier-cubeclt` target, after the layer has
taken STM32_Programmer_CLI and ST-LINK_gdbserver out of the STM32CubeCLT
installer. It is not named like the container tier's tests, so no other image
collects it. A build has no USB device, so nothing here reaches a probe: it
proves the extracted programs load and answer as they did on the bench, and that
the product finds the GDB server beside the CLI without being told where it is.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml
from support import scaled_time_bound

from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

ROOT = Path(os.environ["AGENTIC_HIL_BENCH_CUBECLT"])
CUBE_CLI = ROOT / "STM32CubeProgrammer" / "bin" / "STM32_Programmer_CLI"
GDB_SERVER = ROOT / "STLink-gdb-server" / "bin" / "ST-LINK_gdbserver"
# Recorded on the bench from the same STM32CubeCLT 1.22.0 tree, its location
# written as this placeholder.
RECORDING = Path(__file__).resolve().parents[1] / "fixtures" / "st_link_gdbserver_7_14_0_linux_recordings.json"
PLACEHOLDER = "<cubeclt>"


def test_only_the_two_parts_the_bench_drives_were_extracted() -> None:
    assert sorted(entry.name for entry in ROOT.iterdir()) == ["STLink-gdb-server", "STM32CubeProgrammer"]
    assert os.access(CUBE_CLI, os.X_OK), CUBE_CLI
    assert os.access(GDB_SERVER, os.X_OK), GDB_SERVER


def test_the_extracted_gdb_server_answers_as_it_did_on_the_bench() -> None:
    recording = json.loads(RECORDING.read_text(encoding="utf-8"))
    for name in ("version", "help"):
        record = recording[name]
        answered = subprocess.run(
            [str(GDB_SERVER), *record["argv_tail"]],
            capture_output=True,
            text=True,
            timeout=scaled_time_bound(60),
            check=False,
        )
        observed = {"returncode": answered.returncode, "stdout": answered.stdout.splitlines(), "stderr": answered.stderr.splitlines()}
        print(json.dumps({"schema": "agentic-hil.st-link-gdbserver-image-smoke/v1", "name": name, **observed}))
        expected = {key: record[key] for key in ("returncode", "stdout", "stderr")}
        expected["stdout"] = [line.replace(PLACEHOLDER, str(ROOT)) for line in expected["stdout"]]
        assert observed == expected, name


def _configuration(workspace: Path, config_file: Path, state_root: Path) -> Path:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "workspace_root": str(workspace),
        "state_root": str(state_root),
        "version": 3,
        "permissions": {
            "allow_config_write": False,
            "allow_config_description_write": False,
            "allow_config_permissions_write": False,
            "allow_recover": False,
            "allow_upgrade": False,
        },
        "target": {"name": "st-link-gdbserver-image-smoke", "controller": "stm32f446ret6"},
        "debuggers": {
            "dut": {
                "type": "stlink",
                "executable": str(CUBE_CLI),
                "probe_id": None,
                "timeout_s": 60,
                "interface": "SWD",
                "permissions": {
                    "allow_flash": False,
                    "allow_reset": False,
                    "allow_debug_execution": False,
                    "allow_raw_debugger_commands": False,
                    "allow_mass_erase": False,
                },
            }
        },
        "debug": {"gdb_executable": None, "allowed_symbols": [], "allow_all_symbols": False, "max_dump_size_bytes": 1048576},
        "artifacts": {
            "allowed_roots": ["build"],
            "upload_directory": ".agentic-hil/artifacts",
            "allowed_extensions": [".elf"],
            "max_upload_size_mb": 1,
            "allow_upload": False,
        },
        "com_ports": {},
        "can_buses": {},
        "reports": {"directory": ".agentic-hil/reports"},
        "logs": {"directory": ".agentic-hil/logs"},
    }
    config_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return config_file


def test_the_product_finds_the_gdb_server_beside_the_cli_and_runs_the_cli(tmp_path: Path) -> None:
    """No `gdb_server_executable` is named: the server is the one beside the configured CLI."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    config = load_config(str(_configuration(workspace, tmp_path / "config" / "config.yaml", tmp_path / "state")))

    assert config.debuggers["dut"].gdb_server_executable == str(GDB_SERVER), config.debuggers["dut"]

    service = AgenticHILToolService(config)
    try:
        version = service.call("debugger_info")
    finally:
        service.close()
    assert version["ok"] is True and "2.23.0" in version["version"], version
