from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from agentic_hil.backends.common import find_stm32_programmer_cli, invocation, spawn_command
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids, stlink_target_info
from agentic_hil.comports import list_available_com_ports
from agentic_hil.config import generated_permissions
from agentic_hil.types import JsonObject, fold_hardware_id

PROJECT_PROFILE = "agentic-hil.config.example.yaml"

# The probe flags a generated configuration decides, and the value it writes for
# each, both read from `generated_permissions` — the one place that also drives
# `grant_every_permission` and the skeleton. A second list here would be the one
# that quietly stops deciding a flag somebody added to the schema, and a second
# set of *defaults* here was hardci-hq#107.

# The profile a generated configuration is filled from when the workspace has no
# `agentic-hil.config.example.yaml` of its own. Names and transport defaults, and
# deliberately no permission: every flag it leaves unnamed follows the skeleton's
# open default, so this decides entry names and baudrates and nothing about what
# the board may be told to do.
#
# It is one constant because it answers a question both generation paths ask.
# `agentic-hil init` and `project_config_create` write the same file for the same
# machine, so a default that lived on one of them would be a second answer waiting
# to differ from the first.
DEFAULT_PROJECT_PROFILE: JsonObject = {
    "target": {"name": "detected-target"},
    "debuggers": {"dut": {"timeout_s": 60, "permissions": {}}},
    "com_ports": {"dut_uart": {"baudrate": 115200, "permissions": {}}},
}


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

    The identity is ``fold_hardware_id`` — the same rule ``select_probe_id``, the
    lock key and the mismatch check use, and for the same reason. This used to
    strip punctuation as well, on the theory that a correlation keys nothing and
    can therefore afford to be loose. It cannot: the port it picks is written
    into ``com_ports.<name>.device`` by adoption, so a probe serial ``AB-CD``
    matching a *sole* host port whose serial is really ``ABCD`` puts another
    device's port into an entry whose `allow_write` may already be true — a
    single false match, which the tie guard is by construction blind to. One
    identity for one physical unit, everywhere, is the only version of this that
    cannot silently name the wrong device.

    A serial the debugger and the enumerator spell differently is therefore not
    correlated at all, and the caller reports the COM port as unavailable with
    the host inventory attached, so the operator names it. That is a worse
    experience than a lucky guess and a better one than a wrong file.
    """
    ports = available.get("ports") if available.get("ok") is True else None
    if not isinstance(ports, list):
        return None
    identity = fold_hardware_id(probe_id)
    matches = [
        port
        for port in ports
        if isinstance(port, dict) and fold_hardware_id(str(port.get("serial_number", ""))) == identity
    ]
    return dict(matches[0]) if len(matches) == 1 else None


def port_device_name(matched_port: JsonObject) -> str:
    """How a discovered port should be written down: stably, where that exists.

    Linux publishes ``/dev/serial/by-id/usb-<vendor>_<product>_<serial>-ifNN``,
    which names the board rather than the order it was enumerated in; the
    inventory already resolved it. Windows publishes no openable equivalent, so
    there the kernel name is written and ``serial_number`` carries the identity
    instead — see ``comports.serial_by_id_links``."""
    stable = matched_port.get("stable_device")
    if isinstance(stable, str) and stable:
        return stable
    return str(matched_port["device"])


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
            #
            # Each flag defaults to what the skeleton this fills in states, which
            # is granted except for the two that block flashing while they are
            # true (hardci-hq#96, corrected by hardci-hq#107). The default comes
            # from `generated_permissions` rather than being written here, so
            # this path cannot disagree with the skeleton again — it did, and the
            # discovery path is the one a bench with a board attached takes, so
            # it was the copy that mattered.
            #
            # A profile that names one is still honoured, which can now only
            # narrow: a person who wrote `allow_mass_erase: false` into their
            # project's example configuration meant it, and a default that
            # overrode it would be the same silent widening the carried-over
            # permissions on the regeneration path exist to prevent. Over MCP
            # this profile decides nothing at all — `project_config_create`
            # rewrites every permission afterwards — because there it is
            # repository-controlled data rather than a file a person ran `init`
            # against.
            "permissions": {flag: bool(requested_permissions.get(flag, default)) for flag, default in generated_permissions("debuggers").items()},
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
                "device": port_device_name(matched_port),
                "baudrate": int(profile_port.get("baudrate", 115200)),
                # The port was correlated by this serial in the first place, so
                # recording it costs nothing and is what makes the entry survive
                # a replug: `device` is how the port is opened, this is which
                # board it is (hardci-hq#100).
                "serial_number": str(matched_port.get("serial_number") or discovery["probe_id"]),
                # `allow_read` went the same way as `allow_probe`: reading a
                # port needs no grant at version 2, and the key is refused there.
                "permissions": {
                    "allow_write": bool(requested_io.get("allow_write", True)),
                },
            }
        }
    return template


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
