from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import yaml

from agentic_hil.backends.common import find_stm32_programmer_cli, invocation, spawn_command
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids, stlink_target_info
from agentic_hil.comports import list_available_com_ports
from agentic_hil.config import GDB_AUTODETECT_CANDIDATES, ConfigError, generated_permissions
from agentic_hil.types import JsonObject, fold_hardware_id

PROJECT_PROFILE = "agentic-hil.config.example.yaml"

# The whole of what `apply_discovery_to_template` reads off a profile. A key that
# is not one of these is read past without a word, which is tolerable for a file
# an operator wrote and not for one this project ships: the demo profile opened
# with `workspace_root` and `state_root`, both of which look exactly like
# settings and neither of which is one. Both roots are decided by the caller that
# generates the configuration (`agentic-hil init` binds the workspace it ran in,
# `project_config_create` does the same over MCP) and written over whatever a
# profile said, so an operator editing either placeholder got no effect, no
# warning and no line saying the value had been read and discarded. The keys are
# named here so a test can hold every shipped profile to them, and so the
# silence has an edge a reader can see.
PROFILE_KEYS_READ = ("target", "debuggers", "artifacts", "com_ports")

# The probe flags a generated configuration decides, and the value it writes for
# each, both read from `generated_permissions` — the one place that also drives
# `grant_every_permission` and the skeleton. A second list here would be the one
# that quietly stops deciding a flag somebody added to the schema, and a second
# set of *defaults* here is what once left a generated bench unable to flash.

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

# The one backend bootstrap knows. Named once so that every caller reporting
# which backend answered reports the same one this module actually ran.
BOOTSTRAP_BACKEND = "stlink"


def load_project_profile(workspace: Path) -> JsonObject | None:
    path = workspace / PROJECT_PROFILE
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def enumerate_attached_probes(timeout_s: float = 10.0) -> JsonObject:
    """List the attached ST-Link probes, and stop there.

    The first half of ``discover_attached_hardware``, on its own, because two
    callers ask the same question and one answer has to serve both. Discovery
    asks it before it picks a board to connect to. `agentic-hil debugger-probes`
    asks it on a project that has no configuration yet, which is precisely the
    moment before the first `setup` when an operator wants to know whether the
    board is visible and whether there is exactly one of it.

    The command is the fixed read-only one package code owns, the same one
    discovery has always run: it enumerates and does nothing else, so no HOTPLUG
    connect, no reset, no halt, no erase, no flash and no serial port is reached
    from here.

    An empty listing is a success, exactly as it is for the configured backend.
    Whether nothing attached is a failure belongs to the caller: discovery needs
    a board and says `adapter_not_found`, while a probe listing that found none
    has answered the question it was asked.
    """
    executable = find_stm32_programmer_cli()
    if executable is None:
        return _discovery_failure("debugger_not_found", "STM32CubeProgrammer CLI was not found.")
    cwd = str(Path(executable).parent)
    listed = spawn_command([*invocation(executable), "-q", "-l", "st-link-only"], cwd, timeout_s)
    if listed.not_found:
        return _discovery_failure("debugger_not_found", "STM32CubeProgrammer CLI disappeared during discovery.", executable=executable)
    if listed.timed_out:
        return _discovery_failure("timeout", "ST-Link enumeration timed out after its process was reaped.", executable=executable)
    output = f"{listed.stdout}{listed.stderr}"
    probe_ids = stlink_probe_ids(output)
    if listed.returncode != 0 or (not probe_ids and not stlink_empty_result(output)):
        return _discovery_failure("probe_discovery_failed", "STM32CubeProgrammer returned an invalid probe listing.", executable=executable)
    return {
        "ok": True,
        "tool": "bootstrap_hardware_discovery",
        "backend": BOOTSTRAP_BACKEND,
        "executable": executable,
        "probes": [{"probe_id": found} for found in probe_ids],
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "summary": f"{len(probe_ids)} connected debugger probe(s) detected.",
    }


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
    com_ports = list_available_com_ports("bootstrap_com_ports")
    listed = enumerate_attached_probes(timeout_s)
    if not listed["ok"]:
        # The host serial inventory travels with every enumeration failure, as
        # it always has: an operator whose probe did not enumerate still needs
        # to see what the host does have.
        return {**listed, "com_ports": com_ports}
    executable = str(listed["executable"])
    cwd = str(Path(executable).parent)
    probe_ids = [str(found["probe_id"]) for found in listed["probes"]]
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
        "backend": BOOTSTRAP_BACKEND,
        "executable": executable,
        "gdb_executable": autodetected_gdb(),
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


def autodetected_gdb() -> str | None:
    """The GDB this host answers with when a configuration names none, or None.

    Reported beside the programmer binary because it is the same kind of fact:
    a toolchain that lives on this host, at a location only this host can state,
    which is exactly what a configuration written on another machine or before
    the toolchain was installed cannot know. Nothing is said to a board for it.

    The candidates and their order come from the loader rather than from a
    second list here, so what discovery offers to write into
    `debug.gdb_executable` is the very GDB the runtime fallback would have
    picked while the key stayed null. Pinning it is still worth doing: the
    fallback is resolved against whatever PATH the server happens to be started
    with, and the file is the only place that survives a different one.
    """
    for candidate in GDB_AUTODETECT_CANDIDATES:
        found = shutil.which(candidate)
        if found is not None:
            return found
    return None


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


def profile_baudrate(profile_port: JsonObject, port_name: str) -> int:
    """The baudrate a workspace profile asks for, or a refusal naming the key.

    `agentic-hil.config.example.yaml` is hand-written and reaches this with no
    schema in front of it, unlike the file it helps generate. A bare `int()` over
    it turned `baudrate: fast` into a `ValueError` out of a generation that had
    already read the board — a traceback where the project's own rule is that a
    refusal names the field, says what was expected and is safe to act on.

    `bool` is refused rather than converted for the reason `validated_wait`
    refuses it: `int(True)` is `1`, which the configuration schema accepts as a
    baudrate, so a `baudrate: true` nobody meant would be written into the file
    and opened on the port.
    """
    requested = profile_port.get("baudrate", 115200)
    if isinstance(requested, bool):
        raise ConfigError(
            "invalid_argument",
            f"{PROJECT_PROFILE} names a boolean baudrate; a baudrate is a positive whole number of bits per second, such as 115200.",
            {"field": f"com_ports.{port_name}.baudrate", "value": requested, "profile": PROJECT_PROFILE},
        )
    try:
        return int(requested)
    except (TypeError, ValueError) as error:
        raise ConfigError(
            "invalid_argument",
            f"{PROJECT_PROFILE} names a baudrate that is not a number; a baudrate is a positive whole number of bits per second, such as 115200.",
            {"field": f"com_ports.{port_name}.baudrate", "value": requested, "profile": PROJECT_PROFILE},
        ) from error


def apply_discovery_to_template(template: JsonObject, profile: JsonObject, discovery: JsonObject) -> JsonObject:
    """Fill a configuration skeleton from one discovered bench and one profile.

    The profile is consulted for the keys in ``PROFILE_KEYS_READ`` and for
    nothing else. Everything the caller decides is decided by the caller: both
    roots, the backend, the probe serial, the executable and the port device all
    come from the discovery and the workspace this ran in, so a profile naming
    one of them is read past rather than honoured. That is why the shipped
    profile carries none, and why a test keeps it that way.
    """
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
            # The template this fills in is a version 3 configuration, and from
            # version 2 on reading needs no grant and `allow_probe` is refused by name. A
            # profile written against version 1 may still request it; that
            # request is already satisfied, so it is dropped rather than
            # written into a file that would be refused on load.
            #
            # Each flag defaults to what the skeleton this fills in states, which
            # is granted except for the two that block flashing while they are
            # true. The default comes
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

    # The GDB this host answers with, written down rather than left to be
    # resolved again on whatever PATH the server is next started with. It costs
    # nothing to state: with the key null the loader runs this very lookup at
    # every load and holds the result to the same file checks, so naming it
    # changes what the file *says* and not what it resolves to. What it buys is
    # that a bench generated with the toolchain installed keeps naming that GDB
    # when the server is started from a shell that has lost it, and that
    # `adopt-hardware` on a freshly generated file has nothing left to carry:
    # both paths read this machine through this one function.
    gdb_executable = discovery.get("gdb_executable")
    if isinstance(gdb_executable, str) and gdb_executable:
        template["debug"] = {**template.get("debug", {}), "gdb_executable": gdb_executable}

    profile_artifacts = profile.get("artifacts")
    if isinstance(profile_artifacts, dict):
        template["artifacts"].update({key: value for key, value in profile_artifacts.items() if key in {"allowed_roots", "allowed_extensions", "allow_upload", "max_upload_size_mb", "upload_directory"}})

    matched_port = discovery.get("com_port")
    profile_ports = profile.get("com_ports") if isinstance(profile.get("com_ports"), dict) else {}
    profile_entry = next(((key, value) for key, value in profile_ports.items() if isinstance(value, dict)), None)
    profile_port = None if profile_entry is None else profile_entry[1]
    if isinstance(matched_port, dict) and profile_port is not None:
        requested_io = profile_port.get("permissions") if isinstance(profile_port.get("permissions"), dict) else {}
        # The vendor and product ids off the same enumeration record, because a
        # USB serial is unique only within a vendor: without them a matching
        # serial on another kind of adapter passes the check. Written only when
        # the host reported them.
        usb_ids = {field: value for field in ("vid", "pid") if isinstance(value := matched_port.get(field), int) and not isinstance(value, bool)}
        # The port was correlated by this serial in the first place, so recording
        # it costs nothing and is what makes the entry survive a replug: `device`
        # is how the port is opened, this is which board it is.
        serial_number = str(matched_port.get("serial_number") or discovery["probe_id"] or "")
        template["com_ports"] = {
            "dut_uart": {
                "device": port_device_name(matched_port),
                "baudrate": profile_baudrate(profile_port, str(profile_entry[0] if profile_entry else "<name>")),
                **({"serial_number": serial_number} if serial_number else {}),
                **usb_ids,
                # The template this fills in says `version: 3`, where an entry
                # carrying no serial has to declare what identifies it instead.
                # A serial is what this path normally writes — the port was found
                # by it — so this is the adapter that published none, and the
                # file says which of the two it is rather than leaving a reader
                # to guess.
                **({} if serial_number else {"identity_source": "_".join(usb_ids) or "device"}),
                # `allow_read` went the same way as `allow_probe`: reading a
                # port needs no grant from version 2 on, and the key is refused there.
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
