from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import yaml

from agentic_hil.backends.common import find_stm32_programmer_cli, invocation, spawn_command
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids, stlink_target_info
from agentic_hil.comports import list_available_com_ports
from agentic_hil.types import JsonObject, fold_hardware_id

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


def discover_attached_hardware(
    timeout_s: float = 10.0,
    *,
    probe_id: str | None = None,
    before_connect: Callable[[str], JsonObject | None] | None = None,
) -> JsonObject:
    """Discover one attached STM32 bench before runtime policy exists.

    Commands are fixed here rather than supplied by project data. Discovery may
    enumerate ST-Link probes and connect in HOTPLUG mode, but cannot reset,
    halt, erase, flash, or open a serial port.

    ``probe_id`` names which of several attached probes this is about. It selects
    among what is enumerated and never adds to it: a serial that is not attached
    is refused naming the ones that are. Without it, more than one probe is
    ``ambiguous_hardware`` rather than a silent choice — picking one is how the
    wrong board ends up in a configuration. Selection folds case with
    ``fold_hardware_id``, which is the identity every device lock key and every
    probe comparison in this repository uses, so the probe this selects, the probe
    that gets locked and the probe a mismatch check names are one probe.

    ``before_connect`` is called with the enumerated spelling of the selected
    serial after enumeration and before the HOTPLUG connect — the last point at
    which nothing has been said to that particular board yet. Returning a result
    from it aborts discovery and that result is the answer, which is how a caller
    takes the machine-wide lock on the probe it is about to talk to.
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
    if probe_id is not None:
        selected = select_probe_id(probe_id, probe_ids)
        if selected is None:
            return _discovery_failure(
                "adapter_not_found",
                f"No attached ST-Link probe has the serial '{probe_id}'. Selection chooses among the probes that are attached; it does not add one.",
                executable=executable,
                requested_probe_id=probe_id,
                probes=[{"probe_id": found} for found in probe_ids],
                com_ports=com_ports,
            )
    elif len(probe_ids) != 1:
        return _discovery_failure(
            "ambiguous_hardware",
            "More than one ST-Link probe is attached; discovery will not choose a board.",
            executable=executable,
            probes=[{"probe_id": found} for found in probe_ids],
            next_step="Ask which board this is about and name its serial as probe_id; every attached serial is listed under `probes`.",
            com_ports=com_ports,
        )
    else:
        selected = probe_ids[0]

    # The enumerated spelling from here on, never the caller's: it is what
    # STM32CubeProgrammer is given below and what ends up in the configuration,
    # so the file names the probe the way the toolchain does.
    probe_id = selected
    if before_connect is not None:
        refusal = before_connect(probe_id)
        if refusal is not None:
            return refusal
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


def select_probe_id(requested: str, enumerated: list[str]) -> str | None:
    """The enumerated spelling of a requested probe, or None if it is not attached.

    Matched with ``fold_hardware_id`` and with nothing else, because selection is
    the same question as locking and comparing: *which physical unit is this*. One
    ST-Link is ``0669FF…`` to STM32CubeProgrammer and ``0669ff…`` to udev, so case
    has to fold or exact membership answers ``adapter_not_found`` — "plug the
    board in" — for a board that is plugged in.

    It folds case and no more. A looser rule here than the one the lock key and
    the mismatch check use is worse than a strict one: a request for ``AB-CD``
    would select the attached ``ABCD``, take the lock for a *different* key, and
    HOTPLUG-connect to a board the rest of the system does not think this call is
    about. The later mismatch check would catch it, but only after something had
    already been said to the wrong physical board."""
    identity = fold_hardware_id(requested)
    matches = [found for found in enumerated if fold_hardware_id(found) == identity]
    return matches[0] if len(matches) == 1 else None


def correlate_com_port(probe_id: str, available: JsonObject) -> JsonObject | None:
    """The one host serial port that carries this probe's serial, or None.

    This is a correlation between two host inventories, not an identity the rest
    of the system keys anything on, and it deliberately does not use the lock
    rule: a USB descriptor reaches the enumerator with separators the debugger's
    own listing does not have (``06:6A-FF30`` against ``066AFF30``), and a probe
    serial is what is being looked for in a field somebody else formatted. So
    punctuation is ignored here — and only here, where nothing is being locked,
    connected to or compared for sameness. A tie still resolves to None rather
    than to a guess, so the looseness can never pick a port; it can only fail to.
    """
    ports = available.get("ports") if available.get("ok") is True else None
    if not isinstance(ports, list):
        return None
    identity = _com_serial_identity(probe_id)
    matches = [
        port
        for port in ports
        if isinstance(port, dict) and _com_serial_identity(str(port.get("serial_number", ""))) == identity
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


def _com_serial_identity(value: str) -> str:
    """A serial number as written into a host port inventory, for correlation only.

    Not an identity anything is keyed on — ``fold_hardware_id`` is that, and
    ``select_probe_id`` uses it. Kept separate and named for its one use so the
    two rules cannot be confused for each other again."""
    return fold_hardware_id(re.sub(r"[^A-Za-z0-9]", "", value))


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
