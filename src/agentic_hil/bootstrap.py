from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import yaml

from agentic_hil.backends.common import find_stm32_programmer_cli, invocation, spawn_command
from agentic_hil.backends.stlink import stlink_empty_result, stlink_probe_ids, stlink_target_info
from agentic_hil.comports import (
    DISCOVERED_BY_USB_INVENTORY,
    list_available_com_ports,
    usb_stlink_ports,
    usb_stlink_probe_ids,
)
from agentic_hil.config import (
    ADOPT_HARDWARE_COMMAND,
    GDB_AUTODETECT_CANDIDATES,
    ConfigError,
    generated_permissions,
    skeleton_debugger_scripts,
)
from agentic_hil.knowledge import remediation_fields
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
# each, both read from `generated_permissions`: the one place that also drives
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

# The backend bootstrap runs when STM32CubeProgrammer's CLI is on the host: its
# `-l st-link-only` listing enumerates the probes and its HOTPLUG connect names
# the part. Named once so that every caller reporting which backend answered
# reports the same one this module actually ran.
BOOTSTRAP_BACKEND = "stlink"

# The backend bootstrap runs when that CLI is absent. OpenOCD cannot enumerate
# probes itself (`debugger_probes_list` answers `not_supported` on it), so the
# probe serials come from the host's own USB serial inventory and OpenOCD is
# only the toolchain the generated entry will drive. Both halves are on a plain
# Ubuntu host with `apt install openocd` and nothing from ST, which is the
# supported first path this project documents and the one that used to refuse.
BOOTSTRAP_FALLBACK_BACKEND = "openocd"

# The binary the fallback backend is, looked up on PATH exactly as the OpenOCD
# backend's own `executable: null` fallback looks it up.
OPENOCD_EXECUTABLE = "openocd"

# Which enumeration answered, carried in every discovery result under
# `discovered_by`. A caller reading a probe serial has to be able to tell which
# of the two produced it: the CLI reads the probe, the inventory reads the USB
# descriptor the probe published to the host, and only the first of those has
# also spoken to a target. The inventory's own spelling is
# `DISCOVERED_BY_USB_INVENTORY`, which lives beside the enumeration that
# produces it because the configured OpenOCD backend reports it too.
DISCOVERED_BY_STLINK_CLI = "stm32cubeprogrammer_cli"

# What a probe bound off the USB serial inventory says about the count it was
# bound from, spelled once so the discovery result, the generated configuration
# and every sentence written about either use the same word. The inventory
# reaches an ST-Link only through the virtual COM port a V2-1 or a V3 publishes,
# so it is a real reading and never a complete one, and the honest word for that
# is `incomplete` rather than a silence or a refusal (#423).
PROBE_INVENTORY_INCOMPLETE = "incomplete"

# Who named the probe a discovery was asked to select, which is what decides
# whether the sole-probe caveat is made. Two spellings, because a serial arriving
# at `discover_attached_hardware` says nothing on its own about where it came
# from, and only one of the two places it comes from is a statement about the
# bench. `caller` is an operator at `agentic-hil adopt-hardware --probe-id`, or an
# MCP caller passing `probe_id`: somebody looked at the bench and said which board
# this is, so an inventory that cannot prove its count cannot have picked the
# wrong one. `configuration` is a regeneration handing back the serial the file it
# is about to rewrite already binds: nobody looked at anything, that serial is a
# reading this same inventory produced earlier, and the count is exactly as
# incomplete as it was when the file was first written (#423).
PROBE_NAMED_BY_CALLER = "caller"
PROBE_NAMED_BY_CONFIGURATION = "configuration"


def probe_inventory_caveat(probe_id: str) -> JsonObject:
    """The caveat a sole visible probe is bound under, as result and file fields.

    The USB serial inventory shows exactly one ST-Link and discovery binds it,
    because that is the whole of the supported first path on an ordinary Linux
    workstation: OpenOCD is one `apt install`, STM32CubeProgrammer is a
    registration wall, and an `init` from the project root has to reach a working
    bench without anybody reading a 24-character serial off a sticker. What the
    inventory cannot do is prove the count: a standalone ST-LINK/V2, or any probe
    that publishes no VCP, can be attached beside the one it saw and never appear.

    So the caveat travels rather than blocking. It travels as data on the
    discovery result *and* into the generated configuration, which is the half a
    disclosure on a successful answer alone used to miss: the file is what a later
    flash or reset reads, so the file is where a reader has to be able to see
    which enumeration chose this board and what that enumeration cannot see.
    """
    return {
        "discovered_by": DISCOVERED_BY_USB_INVENTORY,
        "probe_inventory": PROBE_INVENTORY_INCOMPLETE,
        "probe_inventory_note": (
            f"'{probe_id}' is the one ST-Link this host's USB serial inventory shows, and it is bound. That inventory "
            "reaches an ST-Link only through the virtual COM port a V2-1 or a V3 publishes, so a standalone "
            "ST-LINK/V2 -- or any probe with no VCP -- would not appear in it even if it were attached beside this "
            "one. If that is this bench, name the intended board with `agentic-hil adopt-hardware --probe-id "
            "<serial>`, or install STM32CubeProgrammer, whose own listing reads the serial off the probe itself and "
            "is an authoritative count."
        ),
    }


def bound_off_incomplete_inventory(discovery: JsonObject) -> str | None:
    """The sentence a bench bound off the USB serial inventory is reported with, or None.

    Discovery binds the one ST-Link that inventory shows rather than refusing to
    choose, because binding it is what lets `agentic-hil init` on an ordinary
    Linux workstation reach a working bench with nobody reading a 24-character
    serial off a probe (#423). What that reading cannot do is prove the probe is
    the only one attached, so the caveat travels: it is on the discovery result,
    it is written into the generated configuration beside the `probe_id` it
    qualifies, and this is the half a person is told, in the `agentic-hil init`
    summary and next steps and in the `project_config_create` next steps.

    None whenever there is no caveat to make: a board the caller named with
    `probe_id`, an authoritative STM32CubeProgrammer count, or a read that bound
    nothing at all."""
    if discovery.get("ok") is not True or discovery.get("probe_inventory") != PROBE_INVENTORY_INCOMPLETE:
        return None
    note = str(discovery.get("probe_inventory_note") or "").strip()
    return note or None


# One row of OpenOCD's `targets` table: an index, an optional `*` for the
# current target, then the target's own name. The header and its rule of dashes
# carry no leading number and are passed over by the same rule.
OPENOCD_TARGET_ROW = re.compile(r"^\s*\d+\*?\s+(\S+)\s+\S+\s+\S+\s+\S+\s+\S+", re.MULTILINE)


def load_project_profile(workspace: Path) -> JsonObject | None:
    path = workspace / PROJECT_PROFILE
    if not path.is_file():
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def find_openocd() -> str | None:
    """The OpenOCD this host answers with, or None.

    The sibling of ``find_stm32_programmer_cli``, and a named function for the
    same two reasons: it is the one place the fallback backend's binary is
    looked up, and it is a name a test can replace so that what the suite
    discovers does not depend on what the machine running it happens to have
    installed. PATH only, which is the same lookup the OpenOCD backend's own
    ``executable: null`` fallback makes, so a bench generated here names the
    OpenOCD that bench would have resolved anyway."""
    return shutil.which(OPENOCD_EXECUTABLE)


def _tools_searched(stlink_cli: str | None, openocd: str | None) -> list[JsonObject]:
    """What discovery looked for and where it landed, as a result field.

    Both tools every time, found or not, because "we looked for this and it was
    not there" is the half an operator cannot reconstruct from a refusal that
    names only what is missing."""
    return [
        {"name": "STM32_Programmer_CLI", "provided_by": "STM32CubeProgrammer", "path": stlink_cli, "found": stlink_cli is not None},
        {"name": OPENOCD_EXECUTABLE, "provided_by": "OpenOCD", "path": openocd, "found": openocd is not None},
    ]


def _usb_enumeration(available: JsonObject, timeout_s: float) -> JsonObject:
    """Enumerate ST-Link probes with no STM32CubeProgrammer on the host.

    The half of `enumerate_attached_probes` that answers when ST's CLI is not
    installed, which on a Linux workstation is the ordinary case: OpenOCD is one
    `apt install` and STM32CubeProgrammer is a registration wall. The probe
    serial is read out of the USB descriptor the probe published to the host,
    which is the same string `adapter serial` takes, so nothing is invented
    here and nothing is said to a board.

    ``timeout_s`` is accepted and unused: no process is spawned. It is in the
    signature so both halves are called the same way and a caller does not have
    to know which one will answer.
    """
    del timeout_s
    openocd = find_openocd()
    tools = _tools_searched(None, openocd)
    if openocd is None:
        return _discovery_failure(
            "debugger_not_found",
            "Neither STM32CubeProgrammer's CLI (STM32_Programmer_CLI) nor OpenOCD (openocd) was found on this host, so "
            "there is no toolchain to drive an ST-Link with and no way to enumerate one. Install OpenOCD (`apt install "
            "openocd`, `brew install open-ocd`, or the Windows build on PATH) or STM32CubeProgrammer, then run this again.",
            discovered_by=DISCOVERED_BY_USB_INVENTORY,
            tools_searched=tools,
        )
    if available.get("ok") is not True:
        # Without the CLI, the host's serial inventory *is* the enumeration.
        # A listing that failed is therefore a discovery that could not run,
        # and it says so rather than reporting an empty bench.
        return _discovery_failure(
            "probe_discovery_failed",
            "STM32CubeProgrammer is not installed, so ST-Link probes are enumerated from this host's USB serial "
            f"inventory, and that inventory could not be read: {available.get('summary', 'the serial backend did not answer')}",
            discovered_by=DISCOVERED_BY_USB_INVENTORY,
            executable=openocd,
            backend=BOOTSTRAP_FALLBACK_BACKEND,
            tools_searched=tools,
        )
    probe_ids = usb_stlink_probe_ids(available)
    return {
        "ok": True,
        "tool": "bootstrap_hardware_discovery",
        "backend": BOOTSTRAP_FALLBACK_BACKEND,
        "discovered_by": DISCOVERED_BY_USB_INVENTORY,
        "executable": openocd,
        "tools_searched": tools,
        "probes": [{"probe_id": found} for found in probe_ids],
        "stlink_ports": usb_stlink_ports(available),
        # Not an authoritative count, exactly as the configured OpenOCD backend's
        # own listing says of the same reading (`openocd.debugger_probes_list`).
        # The USB serial inventory reaches an ST-Link only through the virtual COM
        # port a V2-1 or a V3 publishes, so a standalone ST-LINK/V2 -- or any probe
        # OpenOCD drives that exposes no VCP -- can be attached and never appear
        # here: an empty reading is not proof no probe is connected, and a nonempty
        # one is not proof it lists every probe attached. Carried so the no-config
        # `debugger-probes` listing this feeds fails `conclusive_success` rather
        # than exiting 0 over a partial inventory (round 0, finding 1).
        "complete": False,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "summary": (
            f"{len(probe_ids)} connected debugger probe(s) read from this host's USB serial inventory; "
            "STM32CubeProgrammer is not installed, so nothing was said to a board to find them. That inventory "
            "reaches an ST-Link only through the virtual COM port a V2-1 or a V3 publishes, so a standalone "
            "ST-LINK/V2 -- or any probe with no VCP -- would not appear here even if attached: this is not "
            "necessarily every probe connected. Read the ids off the probes or the adapter vendor's own tool for "
            "an authoritative count."
        ),
    }


def enumerate_attached_probes(timeout_s: float = 10.0, *, com_ports: JsonObject | None = None) -> JsonObject:
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

    Two enumerations, and which one runs is decided by one fact about the host:
    whether STM32CubeProgrammer's CLI is installed. With it, everything below is
    exactly what it has always been, down to the command line. Without it, the
    probes are read out of the host's USB serial inventory and the toolchain is
    OpenOCD (``_usb_enumeration``), because a missing vendor CLI is not a missing
    bench: on the Nucleo + ST-Link + OpenOCD path this project documents as its
    supported first one, a Linux host normally has OpenOCD and never has
    STM32CubeProgrammer, and refusing there refused the documented bench. The
    answer says which of the two produced it under ``discovered_by``, so nothing
    downstream has to infer it from the presence of a field.

    ``com_ports`` is the host inventory the caller has already taken, passed in
    so that the fallback enumeration and the COM port correlation read one
    reading of the host rather than two takes of it between which a board can be
    unplugged. Omitted, this takes its own.
    """
    executable = find_stm32_programmer_cli()
    if executable is None:
        return _usb_enumeration(com_ports if com_ports is not None else list_available_com_ports("bootstrap_com_ports"), timeout_s)
    cwd = str(Path(executable).parent)
    found_by = {"discovered_by": DISCOVERED_BY_STLINK_CLI, "backend": BOOTSTRAP_BACKEND}
    listed = spawn_command([*invocation(executable), "-q", "-l", "st-link-only"], cwd, timeout_s)
    if listed.not_found:
        return _discovery_failure("debugger_not_found", "STM32CubeProgrammer CLI disappeared during discovery.", executable=executable, **found_by)
    if listed.timed_out:
        return _discovery_failure("timeout", "ST-Link enumeration timed out after its process was reaped.", executable=executable, **found_by)
    output = f"{listed.stdout}{listed.stderr}"
    probe_ids = stlink_probe_ids(output)
    if listed.returncode != 0 or (not probe_ids and not stlink_empty_result(output)):
        return _discovery_failure("probe_discovery_failed", "STM32CubeProgrammer returned an invalid probe listing.", executable=executable, **found_by)
    return {
        "ok": True,
        "tool": "bootstrap_hardware_discovery",
        "backend": BOOTSTRAP_BACKEND,
        "discovered_by": DISCOVERED_BY_STLINK_CLI,
        "executable": executable,
        "tools_searched": _tools_searched(executable, find_openocd()),
        "probes": [{"probe_id": found} for found in probe_ids],
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "summary": f"{len(probe_ids)} connected debugger probe(s) detected.",
    }


def profile_target_controller(profile: JsonObject | None) -> str | None:
    """The controller a workspace profile names for its own board, or None.

    The profile is authoritative for the board it describes: it was written by
    somebody holding that Nucleo, and it names the exact part
    (``stm32f446ret6``), which is more than any read of the hardware produces.
    The skeleton's own ``unknown-controller`` is not a name, and neither is a
    profile that left the key at some other spelling of unknown, so both fall
    through to the probe below. Exactly the test `apply_discovery_to_template`
    already applies when it decides whether the profile or the detected
    controller goes into the file, kept in one place so the two cannot disagree
    about which of them is speaking."""
    target = profile.get("target") if isinstance(profile, dict) else None
    named = str(target.get("controller", "")) if isinstance(target, dict) else ""
    return named if named and "unknown" not in named.lower() else None


def profile_openocd_scripts(profile: JsonObject | None) -> tuple[str, str]:
    """The interface and target scripts to read a target through, profile first.

    The scripts are how OpenOCD reaches a board at all, so a profile that names
    them for its own board is followed and the shipped skeleton's pair
    (``interface/stlink.cfg``, ``target/stm32f4x.cfg``) is the fallback. Read
    off the skeleton rather than restated here, so the read below and the entry
    that gets written name the same two files."""
    interface_cfg, target_cfg = skeleton_debugger_scripts()
    debuggers = profile.get("debuggers") if isinstance(profile, dict) else None
    entry = next((value for value in debuggers.values() if isinstance(value, dict)), {}) if isinstance(debuggers, dict) else {}
    return str(entry.get("interface_cfg") or interface_cfg), str(entry.get("target_cfg") or target_cfg)


def openocd_target_name(output: str) -> str | None:
    """The one target OpenOCD's ``targets`` table names, or None.

    What OpenOCD reports is the name of the target the script created
    (``stm32f4x.cpu``), which is a family rather than a part number: it is what
    this host can say about the board and it is less than a profile says, which
    is why the profile is asked first. The ``.cpu`` suffix is the target's role
    within the chip and not part of the chip's name, so it comes off.

    More than one row is more than one core (a dual-core part, or a script that
    declares an AP alongside the CPU), and this refuses to pick: naming one of
    them would put a guess in the file where a caller can read it as a fact."""
    names = list(dict.fromkeys(match.group(1) for match in OPENOCD_TARGET_ROW.finditer(output)))
    if len(names) != 1:
        return None
    name = names[0]
    return name[: -len(".cpu")] if name.endswith(".cpu") else name


def _openocd_target_identity(
    executable: str,
    probe_id: str,
    profile: JsonObject | None,
    timeout_s: float,
) -> tuple[JsonObject | None, JsonObject]:
    """What the board behind this probe is, without STM32CubeProgrammer.

    Two sources, asked in that order, and the answer says which one spoke.

    The workspace profile first, because it is a person's statement about the
    board this project drives and it names the part exactly. Nothing is said to
    the board for it, which is the other reason it goes first: the cheapest read
    of a target is the one that does not happen.

    OpenOCD second, and read-only: `init`, `targets`, `shutdown`, with the
    probe selected by `adapter serial`, through the same reaped-with-a-timeout
    spawn every other process in this repository goes through. No flash, no
    erase, no reset and no `reset halt`; `init` examines the target the scripts
    declared and stops. It is the same class of contact the ST-Link HOTPLUG
    connect is, which is why the caller has already taken the probe's lock
    before this runs.

    Returns the target, or None with a note under ``target_discovery`` saying
    why the part could not be named. None is not a failure: an ST-Link that
    answered is a probe worth writing down, and a controller nobody could name
    is a field an operator fills in, not a reason to withhold the serial, the
    toolchain path and the COM port that were found.
    """
    named = profile_target_controller(profile)
    if named is not None:
        return {"probe_id": probe_id, "controller": named, "source": "workspace_profile"}, {
            "source": "workspace_profile",
            "summary": f"The target was named by this workspace's {PROJECT_PROFILE} rather than read off the board.",
        }
    interface_cfg, target_cfg = profile_openocd_scripts(profile)
    probed = spawn_command(
        [
            *invocation(executable),
            "-f",
            interface_cfg,
            "-c",
            f"adapter serial {probe_id}",
            "-f",
            target_cfg,
            "-c",
            "init",
            "-c",
            "targets",
            "-c",
            "shutdown",
        ],
        str(Path(executable).parent),
        timeout_s,
    )
    attempt: JsonObject = {
        "source": "openocd",
        "interface_cfg": interface_cfg,
        "target_cfg": target_cfg,
        "timed_out": probed.timed_out,
        "returncode": probed.returncode,
    }
    if probed.not_found:
        # The OpenOCD that resolved on PATH was gone by the time this spawned, so
        # nothing was said to the board: a pre-contact failure, the same one the
        # ST-Link path reports when its own CLI disappears. The bench is
        # untouched, so the state stays `unchanged`, but the read did not happen
        # and this is not a probe that answered with no name.
        return None, _discovery_failure(
            "debugger_not_found",
            f"OpenOCD (`{executable}`) disappeared before the target could be read. Nothing was said to the board.",
            **attempt,
        )
    if probed.timed_out:
        # A reaped read, not an answered one. OpenOCD's `init` attaches over SWD
        # and can halt the target before `targets` and `shutdown` are ever
        # reached, and the process was killed rather than allowed to run its own
        # `shutdown`, so neither the point it stopped at nor the state it left the
        # core in is known. `hardware_state: unknown` fails `overall_success` (so
        # no configuration is generated or adopted from it) and is what the leased
        # caller quarantines the probe on; collapsing this into "no target named"
        # released a board that may be sitting halted.
        return None, _discovery_failure(
            "timeout",
            (
                f"OpenOCD did not finish its read-only init/targets/shutdown through `{interface_cfg}` and "
                f"`{target_cfg}` before it was reaped, and its `init` attaches to the target, so what it left on the "
                "board is not known."
            ),
            hardware_state="unknown",
            side_effect_status="unknown",
            **attempt,
        )
    controller = openocd_target_name(f"{probed.stdout}{probed.stderr}")
    if controller is None:
        return None, {
            **attempt,
            "summary": (
                f"The ST-Link answered and OpenOCD named no target through `{interface_cfg}` and `{target_cfg}`, so the "
                "controller was not identified. Nothing was written to the board."
            ),
        }
    return {"probe_id": probe_id, "controller": controller, "source": "openocd"}, {
        **attempt,
        "summary": f"OpenOCD reported the target as '{controller}' through a read-only init/targets/shutdown.",
    }


def discover_attached_hardware(
    timeout_s: float = 10.0,
    *,
    probe_id: str | None = None,
    probe_named_by: str = PROBE_NAMED_BY_CALLER,
    before_connect: Callable[[str], JsonObject | None] | None = None,
    profile: JsonObject | None = None,
) -> JsonObject:
    """Discover one attached STM32 bench before runtime policy exists.

    Commands are fixed here rather than supplied by project data. Discovery may
    enumerate ST-Link probes and connect in HOTPLUG mode, but cannot reset,
    halt, erase, flash, or open a serial port.

    ``probe_id`` names which of several attached probes this is about. It selects
    among what is enumerated and never adds to it: a serial that is not attached
    is refused naming the ones that are. Without it, more than one probe is
    ``ambiguous_hardware`` rather than a silent choice. Picking one is how the
    wrong board ends up in a configuration. Selection folds case with
    ``fold_hardware_id``, which is the identity every device lock key and every
    probe comparison in this repository uses, so the probe this selects, the probe
    that gets locked and the probe a mismatch check names are one probe.

    ``probe_named_by`` says where that serial came from, and it changes nothing
    about the selection: the named probe has to be in the enumeration either way,
    and one that is not is `adapter_not_found` either way. What it decides is the
    sole-probe caveat below. `caller` is somebody stating which board this is, so
    a count that could not rule out a second probe cannot have picked the wrong
    one and no caveat is made. `configuration` is a regeneration carrying back the
    serial the file already binds, which is this same partial inventory's earlier
    reading rather than a statement about the bench, so the caveat is made exactly
    as it was when that file was first written. The default is `caller`, because a
    direct call naming a serial is a caller naming it; a wrapper that carries one
    out of a file says so. Any other spelling is treated as not-the-caller, so a
    value nobody recognises discloses rather than silently suppresses (#423).

    ``before_connect`` is called with the enumerated spelling of the selected
    serial after enumeration and before the HOTPLUG connect: the last point at
    which nothing has been said to that particular board yet. Returning a result
    from it aborts discovery and that result is the answer, which is how a caller
    takes the machine-wide lock on the probe it is about to talk to.

    ``profile`` is the workspace's own ``agentic-hil.config.example.yaml``, and
    it is read for one thing: what the board is. A profile that names its
    controller settles the target with nothing said to the board at all, which is
    the ordinary case on a project that ships one. It is consulted only on the
    path that has no STM32CubeProgrammer to ask; where that CLI answers, the
    board keeps naming itself exactly as before.
    """
    com_ports = list_available_com_ports("bootstrap_com_ports")
    listed = enumerate_attached_probes(timeout_s, com_ports=com_ports)
    if not listed["ok"]:
        # The host serial inventory travels with every enumeration failure, as
        # it always has: an operator whose probe did not enumerate still needs
        # to see what the host does have.
        return {**listed, "com_ports": com_ports}
    executable = str(listed["executable"])
    backend = str(listed["backend"])
    # Carried onto every refusal below so that a reader of a failure knows which
    # enumeration produced the probes it names, and which ST-Link-shaped ports
    # the host has: "no bench was found" beside a listed ST-Link VCP is the
    # combination that sent the reported operator looking in the wrong place.
    found_by: JsonObject = {
        "backend": backend,
        "discovered_by": listed.get("discovered_by"),
        "tools_searched": listed.get("tools_searched", []),
        **({"stlink_ports": listed["stlink_ports"]} if "stlink_ports" in listed else {}),
    }
    probe_ids = [str(found["probe_id"]) for found in listed["probes"]]
    # Whether the enumeration that produced these ids is an authoritative count.
    # STM32CubeProgrammer's own listing is; the USB serial inventory the OpenOCD
    # fallback reads is not (`complete: false`), because it reaches an ST-Link
    # only through the virtual COM port a V2-1 or a V3 publishes and a VCP-less
    # ST-LINK/V2 can be attached beside the ones it saw. It decides what a chosen
    # board is chosen *with*: a sole visible probe is bound and carries the caveat
    # into the answer and into the file, while a reading that saw nothing at all
    # is a blind spot rather than an empty bench (#423).
    inventory_authoritative = listed.get("complete") is not False
    # Captured before the caller's `probe_id` is folded into the enumerated
    # spelling below, so the answer can say whether the board it bound was one it
    # chose off a partial reading or one the operator named. Both halves matter: a
    # serial regeneration carried out of the file it is about to rewrite is not
    # anybody naming a board, it is this same inventory's earlier reading handed
    # back, and treating it as an explicit choice dropped the caveat off every
    # `agentic-hil init --force` and every `project_config_create` over a bound
    # bench, on the exact hosts the caveat exists for (#423).
    caller_named_probe = probe_id is not None and probe_named_by == PROBE_NAMED_BY_CALLER
    if probe_id is not None:
        # An explicit selection is exact whatever the enumeration's completeness:
        # the operator named the board, so a serial among the ones this host can
        # see is bound and one that is not is refused rather than added.
        selected = select_probe_id(probe_id, probe_ids)
        if selected is None:
            return _discovery_failure(
                "adapter_not_found",
                f"No attached ST-Link probe has the serial '{probe_id}'. Selection chooses among the probes that are attached; it does not add one.",
                executable=executable,
                requested_probe_id=probe_id,
                probes=[{"probe_id": found} for found in probe_ids],
                com_ports=com_ports,
                **found_by,
            )
    elif not probe_ids:
        # An empty probe reading, and which "empty" it is decides the answer.
        # STM32CubeProgrammer's own listing is an authoritative count, so an empty
        # one is a definitive "no board attached": `adapter_not_found`, and `init`
        # tells the operator to attach the bench. The USB serial inventory is not,
        # because it reaches an ST-Link only through the virtual COM port a V2-1 or
        # a V3 publishes:
        #   * an ST-Link serial port that is visible but published no probe serial
        #     is a real, addressable finding the operator can see, so it stays
        #     `adapter_not_found` and the account names the port; but
        #   * a reading with no ST-Link port at all cannot rule out a VCP-less
        #     ST-LINK/V2 attached right now, so declaring the bench empty and
        #     sending the operator to attach one would point them at hardware that
        #     may already be there. It is the one incomplete-inventory refusal
        #     there is, because it is the only one with nothing to bind: `init`
        #     writes an unbound placeholder whose next step is not "attach the
        #     bench" and `project_config_create` refuses rather than reporting an
        #     absent bench off a blind spot (round 2, finding 3).
        if inventory_authoritative or listed.get("stlink_ports"):
            return _discovery_failure(
                "adapter_not_found",
                (
                    "No ST-Link probe is attached."
                    if inventory_authoritative
                    else (
                        "This host's USB serial inventory shows an ST-Link serial port but read no probe serial off it "
                        "to select, so there is no probe id to bind. Check the ST-Link is a genuine ST unit with its "
                        "driver installed, or install STM32CubeProgrammer, which reads the serial off the probe itself."
                    )
                ),
                executable=executable,
                com_ports=com_ports,
                **found_by,
            )
        return _incomplete_inventory_refusal(executable=executable, com_ports=com_ports, **found_by)
    elif len(probe_ids) != 1:
        return _discovery_failure(
            "ambiguous_hardware",
            "More than one ST-Link probe is attached; discovery will not choose a board.",
            executable=executable,
            probes=[{"probe_id": found} for found in probe_ids],
            next_step="Ask which board this is about and name its serial as probe_id; every attached serial is listed under `probes`.",
            com_ports=com_ports,
            **found_by,
        )
    else:
        # Exactly one probe visible. It is bound, whether or not the reading that
        # found it is an authoritative count, and the caveat travels with it: on
        # the host this path exists for -- OpenOCD from a package manager, no
        # STM32CubeProgrammer -- this is what makes `agentic-hil init` from the
        # project root reach a working bench with nobody retyping a 24-character
        # serial, which is the whole of #423. Refusing here to guard against a
        # VCP-less probe nobody has attached took that away from every newcomer to
        # protect the rarer bench, and the rarer bench is served by a caveat that
        # travels instead: `probe_inventory: incomplete` on this answer and in the
        # generated file, and `--probe-id` to name the other board.
        selected = probe_ids[0]

    # The enumerated spelling from here on, never the caller's: it is what the
    # toolchain is given below and what ends up in the configuration, so the file
    # names the probe the way the host does.
    probe_id = selected
    if before_connect is not None:
        refusal = before_connect(probe_id)
        if refusal is not None:
            return refusal
    if backend == BOOTSTRAP_BACKEND:
        target, target_discovery = _stlink_target_identity(executable, probe_id, timeout_s)
        if target is None:
            return {**target_discovery, "executable": executable, "probe_id": probe_id, "com_ports": com_ports, **found_by}
        summary = "One attached STM32 target was identified through ST-Link HOTPLUG discovery."
    else:
        target, target_discovery = _openocd_target_identity(executable, probe_id, profile, timeout_s)
        if target is None and target_discovery.get("ok") is False:
            # A read that failed -- OpenOCD gone before it spawned, or reaped
            # mid-attach -- is not a probe that answered with no target, and the
            # ST-Link path above returns exactly this for the same case. Letting
            # it fall through would build an `ok: true` bench from a read that
            # never finished (and, on a timeout, from a board whose state is not
            # known); the failure is the whole answer.
            return {**target_discovery, "executable": executable, "probe_id": probe_id, "com_ports": com_ports, **found_by}
        summary = (
            f"One attached ST-Link was identified from this host's USB serial inventory and its target is "
            f"'{target['controller']}'."
            if target is not None
            else (
                f"An attached ST-Link ('{probe_id}') was identified from this host's USB serial inventory and its "
                "target was not identified, so the configuration written names the probe, the toolchain and the port "
                "and leaves the controller for you to name."
            )
        )

    # A board this call chose without the caller naming it, off an inventory that
    # is not an authoritative count, is bound and says so. The caveat is data
    # rather than only prose -- `probe_inventory: incomplete` beside
    # `discovered_by: usb_serial_inventory` and the sentence naming what the
    # inventory cannot see -- because `apply_discovery_to_template` carries those
    # same fields into the generated configuration, which is what a later flash or
    # reset actually reads. When the caller named the probe, or the enumeration was
    # STM32CubeProgrammer's own authoritative listing, the selection is exact and
    # no caveat is added (#423).
    caveat = probe_inventory_caveat(probe_id) if not caller_named_probe and not inventory_authoritative else {}
    if caveat:
        summary = f"{summary} {caveat['probe_inventory_note']}"
    matched_port = correlate_com_port(probe_id, com_ports)
    return {
        "ok": True,
        "tool": "bootstrap_hardware_discovery",
        "backend": backend,
        "discovered_by": listed.get("discovered_by"),
        "tools_searched": listed.get("tools_searched", []),
        **({"stlink_ports": listed["stlink_ports"]} if "stlink_ports" in listed else {}),
        "executable": executable,
        "gdb_executable": autodetected_gdb(),
        "probe_id": probe_id,
        "target": target,
        "target_discovery": target_discovery,
        "com_port": matched_port,
        "available_com_ports": com_ports,
        **caveat,
        "side_effect_committed": False,
        "side_effect_status": "not_started",
        "hardware_state": "unchanged",
        "cleanup_required": False,
        "summary": summary,
    }


def _stlink_target_identity(executable: str, probe_id: str, timeout_s: float) -> tuple[JsonObject | None, JsonObject]:
    """The board behind this probe, read by STM32CubeProgrammer's HOTPLUG connect.

    Unchanged from what discovery has always done, lifted out only so that the
    two ways of naming a target sit side by side. Its second element is a
    refusal rather than a note: with this CLI present, a probe that answers and a
    target that does not is the failure it has always been, because the same call
    reads both."""
    probed = spawn_command(
        [*invocation(executable), "-q", "-c", "port=SWD", "mode=HOTPLUG", f"sn={probe_id}"],
        str(Path(executable).parent),
        timeout_s,
    )
    if probed.not_found:
        return None, _discovery_failure("debugger_not_found", "STM32CubeProgrammer CLI disappeared during target discovery.")
    if probed.timed_out:
        return None, _discovery_failure("timeout", "Target discovery timed out after its process was reaped.")
    target = stlink_target_info(f"{probed.stdout}{probed.stderr}")
    if probed.returncode != 0 or target is None:
        return None, _discovery_failure(
            "target_not_detected",
            "ST-Link was found, but no target identity was detected. Runtime permissions are not required for bootstrap discovery.",
        )
    return target, {"source": BOOTSTRAP_BACKEND, "summary": "The target named itself through an ST-Link HOTPLUG connect."}


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
    has to fold or exact membership answers ``adapter_not_found`` ("plug the
    board in") for a board that is plugged in.

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

    The identity is ``fold_hardware_id``: the same rule ``select_probe_id``, the
    lock key and the mismatch check use, and for the same reason. This used to
    strip punctuation as well, on the theory that a correlation keys nothing and
    can therefore afford to be loose. It cannot: the port it picks is written
    into ``com_ports.<name>.device`` by adoption, so a probe serial ``AB-CD``
    matching a *sole* host port whose serial is really ``ABCD`` puts another
    device's port into an entry whose `allow_write` may already be true: a
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
    instead: see ``comports.serial_by_id_links``."""
    stable = matched_port.get("stable_device")
    if isinstance(stable, str) and stable:
        return stable
    return str(matched_port["device"])


def profile_baudrate(profile_port: JsonObject, port_name: str) -> int:
    """The baudrate a workspace profile asks for, or a refusal naming the key.

    `agentic-hil.config.example.yaml` is hand-written and reaches this with no
    schema in front of it, unlike the file it helps generate. A bare `int()` over
    it turned `baudrate: fast` into a `ValueError` out of a generation that had
    already read the board: a traceback where the project's own rule is that a
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
    # The backend that actually answered, never a fixed one. Discovery has two
    # (STM32CubeProgrammer's CLI, and the USB inventory with OpenOCD as the
    # toolchain), and writing `stlink` for both put an
    # STM32CubeProgrammer-shaped entry on a host that has no
    # STM32CubeProgrammer: a file that loads and refuses at the first call. It is
    # also what `plan_adoption` compares an existing entry's `type` against, so a
    # generated bench and an adopted one have to agree on the word.
    backend = str(discovery.get("backend") or BOOTSTRAP_BACKEND)
    skeleton_debugger = next((value for value in template.get("debuggers", {}).values() if isinstance(value, dict)), {})
    if backend == BOOTSTRAP_FALLBACK_BACKEND:
        # OpenOCD reaches a target through these two and through nothing else,
        # so they are the keys that have to survive the entry being rewritten.
        # The profile's pair when it names one, the skeleton's otherwise.
        interface_cfg, target_cfg = profile_openocd_scripts(profile)
        transport = {"interface_cfg": interface_cfg, "target_cfg": target_cfg}
    else:
        # `interface` is the stlink backend's transport and is ignored by the
        # others; it stays exactly where it was.
        transport = {"interface": str(skeleton_debugger.get("interface") or "SWD")}
    # The caveat a probe bound off the USB serial inventory was bound under, in
    # the file itself. It is the half a disclosure on the discovery answer alone
    # could never do: the answer is read once by whoever ran the command, and the
    # file is what every later flash, reset and probe read resolves the board
    # through, so `discovered_by`, `probe_inventory: incomplete` and the sentence
    # naming what that inventory cannot see belong beside the `probe_id` they
    # qualify. Absent on an explicit selection and on STM32CubeProgrammer's own
    # authoritative count, where there is no caveat to make (#423).
    carried = ("discovered_by", "probe_inventory", "probe_inventory_note")
    inventory_caveat = {field: discovery[field] for field in carried if field in discovery} if discovery.get("probe_inventory") == PROBE_INVENTORY_INCOMPLETE else {}
    template["debuggers"] = {
        "dut": {
            "type": backend,
            "executable": discovery["executable"],
            "probe_id": discovery["probe_id"],
            **inventory_caveat,
            **transport,
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
            # this path cannot disagree with the skeleton again. It did, and the
            # discovery path is the one a bench with a board attached takes, so
            # it was the copy that mattered.
            #
            # A profile that names one is still honoured: a person who wrote
            # `allow_mass_erase: false` into their project's example
            # configuration meant it, and a default that overrode it would be the
            # same silent widening the carried-over permissions on the
            # regeneration path exist to prevent. For a flag the skeleton grants
            # this only narrows; the two interlocks default false, so an example
            # that writes one true opens it and the generated file carries that
            # grant, which is why the missing-configuration remediation tells a
            # reader to read the reported permissions rather than assume both are
            # false. Over MCP this profile decides nothing at all
            # (`project_config_create` rewrites every permission afterwards)
            # because there it is repository-controlled data rather than a file a
            # person ran `init` against.
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
                # A serial is what this path normally writes (the port was found
                # by it), so this is the adapter that published none, and the
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


# The two regions of the shipped skeleton a profile can fill in when no bench
# was found, quoted exactly. They are anchors rather than a re-parse of the
# document because the skeleton's value to an operator is its comments, and a
# YAML round trip drops every one of them: what is written when discovery finds
# nothing has to stay the annotated file it has always been.
#
# `apply_profile_to_placeholder` refuses rather than silently skipping when
# either is missing, and a test holds the skeleton to both, so the two cannot
# come apart without something saying so.
PLACEHOLDER_TARGET_ANCHOR = 'target:\n  name: "example-target"\n  controller: "unknown-controller"\n'
PLACEHOLDER_COM_PORTS_ANCHOR = "com_ports: {}\n"


def apply_profile_to_placeholder(template_text: str, profile: JsonObject) -> str:
    """The skeleton, carrying the names this project's profile already states.

    What `init` wrote when nothing was attached said `example-target`,
    `unknown-controller` and `com_ports: {}` for a workspace whose own
    `agentic-hil.config.example.yaml` names the board and names the ports. The
    ports were the expensive half: a plan that opens `dut_uart` was refused
    "references a COM port that is not in the authoritative config" for a port
    the project declares, and the operator went looking for a mistake in the
    plan.

    So the profile's names go in, and only the names: the board's name and
    controller, and each port's name, baudrate and permissions. Not a device,
    because which of this host's serial devices a port is, is exactly what
    discovery did not find out; the entry is written unbound (see
    ``types.com_port_is_unbound``) and every call that names it is refused as
    `com_port_not_bound`, which says the bench is not filled in rather than that
    the plan is wrong.

    Text rather than a document, so the comments survive. Both regions are
    quoted from the skeleton and a missing one raises: the skeleton is shipped
    with this module, and a silent no-op would leave an operator with the
    placeholder this exists to replace and nothing saying why.
    """
    target_profile = profile.get("target") if isinstance(profile.get("target"), dict) else {}
    name = str(target_profile.get("name") or "").strip()
    controller = str(target_profile.get("controller") or "").strip()
    text = template_text
    if name or controller:
        if PLACEHOLDER_TARGET_ANCHOR not in text:
            raise ConfigError(
                "config_invalid",
                "The bundled configuration skeleton no longer carries the placeholder target block, so this project's "
                f"{PROJECT_PROFILE} could not be applied to it.",
                {"field": "target", "profile": PROJECT_PROFILE},
            )
        # `json.dumps` rather than a one-value `yaml.safe_dump`, which appends
        # YAML's own document-end marker and would turn the rest of the skeleton
        # into a second document. JSON is a subset of YAML, so this writes the
        # same quoted scalar the anchor it replaces already was, and it is how
        # `init` spells both roots a few lines further on.
        text = text.replace(
            PLACEHOLDER_TARGET_ANCHOR,
            "target:\n"
            f"  name: {json.dumps(name or 'example-target')}\n"
            f"  controller: {json.dumps(controller or 'unknown-controller')}\n",
        )
    ports = _placeholder_com_ports(profile)
    if ports:
        if PLACEHOLDER_COM_PORTS_ANCHOR not in text:
            raise ConfigError(
                "config_invalid",
                "The bundled configuration skeleton no longer carries the empty com_ports block, so this project's "
                f"{PROJECT_PROFILE} could not be applied to it.",
                {"field": "com_ports", "profile": PROJECT_PROFILE},
            )
        rendered = yaml.safe_dump({"com_ports": ports}, sort_keys=False, allow_unicode=False)
        text = text.replace(
            PLACEHOLDER_COM_PORTS_ANCHOR,
            f"# Named by this project's {PROJECT_PROFILE}. No bench was attached when this file was written, so each\n"
            "# entry carries the port's name, baudrate and permissions and no `device`: which of this host's serial\n"
            "# devices it is, is what discovery did not find out. Nothing opens an entry in this state, and a call or\n"
            f"# a plan step that names one is refused `com_port_not_bound`. `{ADOPT_HARDWARE_COMMAND}` fills the\n"
            "# device in from the attached board.\n"
            f"{rendered}",
        )
    return text


def _placeholder_com_ports(profile: JsonObject) -> JsonObject:
    """The profile's ports as unbound entries, permissions exactly as asked.

    ``device`` is deliberately absent rather than null: both read as unbound, and
    leaving the key out is what a file that has never been bound looks like.
    ``allow_write`` follows the profile the way the discovered path follows it,
    so a project that closed a port keeps it closed and one that said nothing
    gets the generated default."""
    profile_ports = profile.get("com_ports") if isinstance(profile.get("com_ports"), dict) else {}
    entries: JsonObject = {}
    for name, value in profile_ports.items():
        if not isinstance(value, dict):
            continue
        requested = value.get("permissions") if isinstance(value.get("permissions"), dict) else {}
        entries[str(name)] = {
            "baudrate": profile_baudrate(value, str(name)),
            "permissions": {flag: bool(requested.get(flag, default)) for flag, default in generated_permissions("com_ports").items()},
        }
    return entries


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


def _incomplete_inventory_refusal(*, executable: str, com_ports: JsonObject, **found_by: object) -> JsonObject:
    """Discovery's answer when the USB serial inventory saw no probe at all.

    The one reading with nothing to bind and nothing to conclude. The inventory
    reaches an ST-Link only through the virtual COM port a V2-1 or a V3 publishes,
    so a standalone ST-LINK/V2 -- or any probe with no VCP -- can be attached and
    never appear: seeing none is a blind spot, not a proof that none is connected.
    Reporting `adapter_not_found` here would send an operator to attach a bench
    that may be plugged in already, so `init` writes an unbound placeholder from
    this failure and `project_config_create` refuses (round 2, finding 3).

    A sole visible probe is a different reading and takes a different answer: it
    is bound, carrying `probe_inventory: incomplete` into the result and into the
    generated configuration, because the ordinary OpenOCD-only bench has to reach
    a working configuration from `init` alone (#423). Two or more visible probes
    are `ambiguous_hardware`, as they are on either enumeration.

    The way forward rides the failure itself: the shared `probe_inventory_incomplete`
    catalogue entry's `remediation` and `do_not` are merged in beside this call's own
    `next_step`, so the public `project_config_create` refusal and the placeholder
    `init` writes carry the same inline fix every other catalogued failure does,
    rather than a next step with no catalogue content behind it (round 3, finding 1)."""
    summary = (
        "This host's USB serial inventory saw no ST-Link at all, and it is not an authoritative count: it reaches an "
        "ST-Link only through the virtual COM port a V2-1 or a V3 publishes, so a standalone ST-LINK/V2 -- or any probe "
        "with no VCP -- could be attached right now and would not appear. Discovery will not report an absent bench off "
        "a reading that cannot see one."
    )
    # Nothing was visible to name, and a serial this inventory did not see is one
    # `select_probe_id` cannot match, so pointing at `--probe-id` here would be a
    # dead end. What reaches a bound bench is a probe this inventory can see, or
    # the vendor CLI whose count is authoritative.
    next_step = (
        "This host's USB serial inventory saw no ST-Link at all and cannot reach one that publishes no virtual COM "
        "port, so there is no visible serial to name as probe_id. Attach a probe that publishes a virtual COM port and "
        "run this again, which binds the one it then shows, or install STM32CubeProgrammer for an authoritative probe "
        "count."
    )
    return _discovery_failure(
        "probe_inventory_incomplete",
        summary,
        executable=executable,
        probes=[],
        probe_inventory_complete=False,
        next_step=next_step,
        com_ports=com_ports,
        **remediation_fields("probe_inventory_incomplete"),
        **found_by,
    )
