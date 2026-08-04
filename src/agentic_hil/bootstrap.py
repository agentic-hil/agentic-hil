from __future__ import annotations

import re
from pathlib import Path

import yaml

from agentic_hil.backends.common import find_stm32_programmer_cli, invocation, spawn_command
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids, stlink_target_info
from agentic_hil.comports import list_available_com_ports
from agentic_hil.types import JsonObject

PROJECT_PROFILE = "agentic-hil.config.example.yaml"


def load_project_profile(workspace: Path) -> JsonObject | None:
    path = workspace / PROJECT_PROFILE
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def discover_attached_hardware(timeout_s: float = 10.0) -> JsonObject:
    """Discover one attached STM32 bench before runtime policy exists.

    Commands are fixed here rather than supplied by project data. Discovery may
    enumerate ST-Link probes and connect in HOTPLUG mode, but cannot reset,
    halt, erase, flash, or open a serial port.
    """
    executable = find_stm32_programmer_cli()
    com_ports = list_available_com_ports("bootstrap_com_ports")
    if executable is None:
        return _discovery_failure("debugger_not_found", "STM32CubeProgrammer CLI was not found.", com_ports=com_ports)

    cwd = str(Path(executable).parent)
    listed = spawn_command([*invocation(executable), "-q", "-l", "st-link-only"], cwd, timeout_s)
    if listed.not_found:
        return _discovery_failure("debugger_not_found", "STM32CubeProgrammer CLI disappeared during discovery.", executable=executable, com_ports=com_ports)
    if listed.timed_out:
        return _discovery_failure("timeout", "ST-Link enumeration timed out after its process was reaped.", executable=executable, com_ports=com_ports)
    output = f"{listed.stdout}{listed.stderr}"
    probe_ids = stlink_probe_ids(output)
    if listed.returncode != 0 or (not probe_ids and not stlink_empty_result(output)):
        return _discovery_failure("probe_discovery_failed", "STM32CubeProgrammer returned an invalid probe listing.", executable=executable, com_ports=com_ports)
    if not probe_ids:
        return _discovery_failure("adapter_not_found", "No ST-Link probe is attached.", executable=executable, com_ports=com_ports)
    if len(probe_ids) != 1:
        return _discovery_failure(
            "ambiguous_hardware",
            "More than one ST-Link probe is attached; setup will not choose a board.",
            executable=executable,
            probes=[{"probe_id": probe_id} for probe_id in probe_ids],
            com_ports=com_ports,
        )

    probe_id = probe_ids[0]
    probed = spawn_command(
        [*invocation(executable), "-q", "-c", "port=SWD", "mode=HOTPLUG", f"sn={probe_id}"],
        cwd,
        timeout_s,
    )
    if probed.not_found:
        return _discovery_failure("debugger_not_found", "STM32CubeProgrammer CLI disappeared during target discovery.", executable=executable, probe_id=probe_id, com_ports=com_ports)
    if probed.timed_out:
        return _discovery_failure("timeout", "Target discovery timed out after its process was reaped.", executable=executable, probe_id=probe_id, com_ports=com_ports)
    target = stlink_target_info(f"{probed.stdout}{probed.stderr}")
    if probed.returncode != 0 or target is None:
        return _discovery_failure(
            "target_not_detected",
            "ST-Link was found, but no target identity was detected. Runtime permissions are not required for bootstrap discovery.",
            executable=executable,
            probe_id=probe_id,
            com_ports=com_ports,
        )

    matched_port = correlate_com_port(probe_id, com_ports)
    return {
        "ok": True,
        "tool": "bootstrap_hardware_discovery",
        "backend": "stlink",
        "executable": executable,
        "probe_id": probe_id,
        "target": target,
        "com_port": matched_port,
        "available_com_ports": com_ports,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "summary": "One attached STM32 target was identified through ST-Link HOTPLUG discovery.",
    }


def correlate_com_port(probe_id: str, available: JsonObject) -> JsonObject | None:
    ports = available.get("ports") if available.get("ok") is True else None
    if not isinstance(ports, list):
        return None
    identity = _hardware_identity(probe_id)
    matches = [
        port
        for port in ports
        if isinstance(port, dict) and _hardware_identity(str(port.get("serial_number", ""))) == identity
    ]
    return dict(matches[0]) if len(matches) == 1 else None


def apply_discovery_to_template(template: JsonObject, profile: JsonObject, discovery: JsonObject) -> JsonObject:
    target_profile = profile.get("target") if isinstance(profile.get("target"), dict) else {}
    detected_target = discovery.get("target") if isinstance(discovery.get("target"), dict) else {}
    profile_controller = str(target_profile.get("controller", ""))
    template["target"] = {
        "name": str(target_profile.get("name") or "detected-target"),
        "controller": profile_controller if profile_controller and "unknown" not in profile_controller.lower() else str(detected_target.get("controller") or "unknown-controller").lower(),
    }

    profile_debuggers = profile.get("debuggers") if isinstance(profile.get("debuggers"), dict) else {}
    profile_debugger = next((value for value in profile_debuggers.values() if isinstance(value, dict)), {})
    requested_permissions = profile_debugger.get("permissions") if isinstance(profile_debugger.get("permissions"), dict) else {}
    template["debuggers"] = {
        "dut": {
            "type": "stlink",
            "executable": discovery["executable"],
            "probe_id": discovery["probe_id"],
            "interface": "SWD",
            "timeout_s": profile_debugger.get("timeout_s", 60),
            # The template this fills in is a version 2 configuration, where
            # reading needs no grant and `allow_probe` is refused by name. A
            # profile written against version 1 may still request it; that
            # request is already satisfied, so it is dropped rather than
            # written into a file that would be refused on load.
            "permissions": {
                "allow_flash": bool(requested_permissions.get("allow_flash", False)),
                "allow_reset": bool(requested_permissions.get("allow_reset", False)),
                "allow_raw_debugger_commands": False,
                "allow_mass_erase": False,
                "allow_debug_execution": bool(requested_permissions.get("allow_debug_execution", False)),
            },
        }
    }

    profile_artifacts = profile.get("artifacts")
    if isinstance(profile_artifacts, dict):
        template["artifacts"].update({key: value for key, value in profile_artifacts.items() if key in {"allowed_roots", "allowed_extensions", "allow_upload", "max_upload_size_mb", "upload_directory"}})

    matched_port = discovery.get("com_port")
    profile_ports = profile.get("com_ports") if isinstance(profile.get("com_ports"), dict) else {}
    profile_port = next((value for value in profile_ports.values() if isinstance(value, dict)), None)
    if isinstance(matched_port, dict) and profile_port is not None:
        requested_io = profile_port.get("permissions") if isinstance(profile_port.get("permissions"), dict) else {}
        template["com_ports"] = {
            "dut_uart": {
                "device": str(matched_port["device"]),
                "baudrate": int(profile_port.get("baudrate", 115200)),
                # `allow_read` went the same way as `allow_probe`: reading a
                # port needs no grant at version 2, and the key is refused there.
                "permissions": {
                    "allow_write": bool(requested_io.get("allow_write", False)),
                },
            }
        }
    return template


def _hardware_identity(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def _discovery_failure(error_type: str, summary: str, **details: object) -> JsonObject:
    return {
        "ok": False,
        "tool": "bootstrap_hardware_discovery",
        "error_type": error_type,
        "summary": summary,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        **details,
    }
