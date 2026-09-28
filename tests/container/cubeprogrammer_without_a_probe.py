"""Optional build-only smoke check for the licensed CubeProgrammer image layer.

This file is selected explicitly by the optional Docker target. It is not named
like the ordinary container-tier tests, so the default CI image neither skips
nor needs the licensed tool. Docker build does not receive USB device nodes;
these calls exercise the real CLI and Agentic HIL's refusal handling without a
target attached.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml
from support import scaled_time_bound

from agentic_hil.config import load_config
from agentic_hil.tools import AgenticHILToolService

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "stm32cubeprogrammer_2_23_0_recordings.json"
CUBE_CLI = Path("/opt/st/cubeprogrammer-2.23.0/bin/STM32_Programmer_CLI")
NO_STLINK_NOTICE = "ST-LINK error (DEV_NO_STLINK)"


def output_matches_recording(name: str, observed: str) -> bool:
    """Match the recording, with one exact VM-observed no-probe notice variant.

    The VM's CubeProgrammer build adds this one line immediately before its
    existing no-probe message. It is accepted only for probe listing and only
    at that exact position; all other transcript differences remain visible.
    """
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))["recordings"][name]["stdout"]
    if observed == expected:
        return True
    if name != "list_no_probe" or expected.count("No ST-Link detected!") != 1:
        return False
    with_notice = expected.replace(
        "No ST-Link detected!",
        f"{NO_STLINK_NOTICE}\nNo ST-Link detected!",
        1,
    )
    return observed == with_notice


def run_cli_smoke(records: dict) -> dict:
    """Capture every real CLI result before checking any one transcript."""
    observed = {}
    for name, record in records.items():
        process = subprocess.run(
            [str(CUBE_CLI), *record["argv"]],
            capture_output=True,
            text=True,
            timeout=scaled_time_bound(60),
            check=False,
        )
        observed[name] = {
            "argv": record["argv"],
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
    print(json.dumps({"schema": "agentic-hil.cubeprogrammer-probe-free-smoke/v1", "recordings": observed}))

    for name, record in records.items():
        result = observed[name]
        assert result["returncode"] == record["returncode"], (record["argv"], result)
        assert output_matches_recording(name, result["stdout"]), (record["argv"], result["stdout"])
        assert result["stderr"] == record["stderr"], (record["argv"], result["stderr"])
    return observed


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
        "target": {"name": "cubeprogrammer-probe-free-smoke", "controller": "stm32f446ret6"},
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


def test_installed_cubeprogrammer_matches_recorded_usb_free_results_and_product_refusals(tmp_path: Path) -> None:
    """Re-run exact version/list/connect commands, then exercise all three product paths."""
    captured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = captured["recordings"]
    assert captured["provenance"]["version"] == "2.23.0"
    assert CUBE_CLI.is_file(), f"optional CubeProgrammer target did not install {CUBE_CLI}"

    observed = run_cli_smoke(records)

    workspace = tmp_path / "project"
    workspace.mkdir()
    config = load_config(str(_configuration(workspace, tmp_path / "config" / "config.yaml", tmp_path / "state")))
    service = AgenticHILToolService(config)
    try:
        version = service.call("debugger_info")
        listing = service.call("debugger_probes_list")
        refusal = service.call("probe_target")
    finally:
        service.close()

    assert version["ok"] is True and "2.23.0" in version["version"], version
    assert listing["ok"] is True and listing["probes"] == [], listing
    assert listing.get("target_contacted") is not True, listing
    assert listing["side_effect_status"] == "not_started" and listing["retry_safe"] is True, listing
    assert refusal["ok"] is False, refusal
    assert refusal["backend_error_type"] == "probe_not_found", refusal
    assert refusal["error_type"] == "adapter_not_found", refusal
    assert refusal["programmer_output"]["stdout"] == observed["connect_no_probe"]["stdout"], refusal
    assert refusal["target_contacted"] is False, refusal
    assert refusal["side_effect_status"] == "not_started" and refusal["retry_safe"] is True, refusal
    log_path = workspace / refusal["log_path"]
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert log["stdout"] == observed["connect_no_probe"]["stdout"], log
