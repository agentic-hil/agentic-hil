"""Carrying attached hardware into a configuration that was written without it.

Discovery used to happen exactly once, at ``init`` time. The most common way to
set a bench up — install the tool, then plug the board in — therefore produced a
placeholder file (``probe_id: null``, ``executable: null``, a placeholder target)
and no way back into it. In the session that produced this, that ended with an
agent printing a 24-character probe serial for a person to retype, which is the
one value nobody can guess and nobody transcribes correctly twice.

This is the way back, and it is narrow on purpose:

**It carries identity, and only identity.** ``probe_id``, ``executable``,
``target.controller`` and a COM device — precisely what an attached probe hands
you and what the file cannot know without it. Every one of them falls in the
description half of the configuration write, so this path cannot reach a
``permissions:`` block at all.

**It takes no values from its caller.** Arguments *select* — which probe of
several, which entry receives them — and never supply. The values come from
what is attached to this machine, exactly as ``project_config_create``'s do.

**It never overwrites a value somebody chose.** A field is carried only when the
document has nothing there: absent, ``null``, empty, or the placeholder the
shipped skeleton writes. A value that is none of those and disagrees with the
hardware is reported as a disagreement and left alone. Silently replacing a
deliberate choice is the failure mode that would make this path worse than the
retyping it removes.

**It reads the board the way every other read of a board happens.** Enumerating
probes and connecting in HOTPLUG mode is a hardware read, and a read still
perturbs — an SWD attach halts the core. What answers that in this repository is
not a grant but exclusivity, so this takes the same machine-wide locks
``debugger_probes_list`` takes, writes the same audit record, and quarantines the
same way if the read raises. Both the MCP tool and ``agentic-hil adopt-hardware``
go through that one path, so a second process on this machine gets ``device_busy``
instead of reaching into a board somebody is driving.

**It writes through one door.** The changes go into ``project_config_set``, so
the grants, the scalar-only shape, the closed key model, the before/after
comparison of every permission in the document, the refusal while hardware is
held, the validate-before-replace and the ``provenance`` record are all the ones
that were already there. There is no second way to write this file. The plan is
carried into that write as an expectation per key *and* as the description of the
document it was decided against, so a placeholder somebody else filled while the
probe was being read refuses the write instead of losing to it — and so does a
change to something the plan depended on but cannot set, such as the entry's
``type`` or the ``probe_id`` that decided which physical board was read.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from agentic_hil.bootstrap import discover_attached_hardware, port_device_name
from agentic_hil.config import DEFAULT_CONFIG_TEMPLATE, ConfigError
from agentic_hil.configstate import config_status, with_config_status
from agentic_hil.configwrite import (
    ACTOR_AGENT,
    NOT_STARTED,
    PROJECT_CONFIG_SET,
    authoritative_write_target,
    load_config_document,
    project_config_set,
)
from agentic_hil.coordination import (
    DEBUGGER_DISCOVERY_RESOURCE,
    CoordinationError,
    HardwareCoordinator,
    HardwareLease,
)
from agentic_hil.devices import DebuggerDevice
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_NAMED_SECTIONS,
    CONFIG_SHAPE_URI,
    remediation_fields,
)
from agentic_hil.report import (
    audit_unavailable,
    ensure_audit_ready,
    overall_success,
    recommit_report_with_status,
    write_report,
)
from agentic_hil.types import (
    AgenticHILConfig,
    ComPortConfig,
    JsonObject,
    com_port_carries_hardware_identity,
    com_port_identity_source,
    fold_device_path,
    fold_hardware_id,
    is_stable_device_name,
)

PROJECT_CONFIG_ADOPT = "project_config_adopt_hardware"

# The name a COM port entry gets when the configuration has none yet. The same
# name the generated configuration uses, so a bench set up the two ways reads
# the same.
DEFAULT_COM_PORT_ID = "dut_uart"


# ---------------------------------------------------------------------------
# What counts as unset.


@cache
def _skeleton() -> JsonObject:
    """The shipped generation skeleton, parsed once.

    The placeholders are read out of it rather than listed here. A second list of
    "values that mean nothing was found" would be a list that drifts from the
    template that actually writes them, and the whole point of this check is that
    it recognises what `init` really put in the file."""
    loaded = yaml.safe_load(DEFAULT_CONFIG_TEMPLATE)
    return loaded if isinstance(loaded, dict) else {}


def skeleton_placeholder(section: str, field: str) -> Any:
    """What the shipped skeleton writes for a field when discovery found nothing."""
    node = _skeleton().get(section)
    if not isinstance(node, dict):
        return None
    if section in CONFIG_NAMED_SECTIONS:
        entry = next((value for value in node.values() if isinstance(value, dict)), {})
        return entry.get(field)
    return node.get(field)


def is_unset(value: Any, section: str, field: str) -> bool:
    """Whether a field holds nothing a person put there.

    Three spellings of nothing, and no fourth: the key is absent or ``null``, it
    is an empty string, or it is exactly the placeholder the shipped skeleton
    writes for that key — ``target.controller: "unknown-controller"`` is the one
    that matters in practice. Anything else is a value somebody chose, including
    a wrong one, and this path does not get to decide it was a mistake."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    placeholder = skeleton_placeholder(section, field)
    return isinstance(placeholder, str) and value == placeholder


# ---------------------------------------------------------------------------
# Choosing which entry receives the values.


def _mapping(document: JsonObject, section: str) -> JsonObject:
    node = document.get(section)
    return node if isinstance(node, dict) else {}


def _entries(document: JsonObject, section: str) -> dict[str, JsonObject]:
    return {str(name): entry for name, entry in _mapping(document, section).items() if isinstance(entry, dict)}


def _invalid(field: str, summary: str, **extra: object) -> JsonObject:
    return {"ok": False, "tool": PROJECT_CONFIG_ADOPT, "error_type": "invalid_argument", "field": field, "summary": summary, "reference": CONFIG_SHAPE_URI, **extra, **NOT_STARTED, "retry_safe": False}


def _choose_debugger(document: JsonObject, requested: str | None) -> tuple[str, JsonObject] | JsonObject:
    """Which debugger entry this is about, or a refusal that names the choice.

    With several probes configured and none named there is no entry this could
    mean, and choosing one is how a serial lands on the wrong board — the same
    rule `devices.debugger_device` follows for a run."""
    entries = _entries(document, "debuggers")
    if requested is not None:
        if requested not in entries:
            return {
                **_invalid(
                    "debugger_id",
                    f"This configuration declares no debugger named '{requested}'. Adoption fills in an entry that exists; it does not add a probe.",
                    configured_debuggers=sorted(entries),
                ),
                "error_type": "unknown_device",
            }
        return requested, entries[requested]
    if len(entries) == 1:
        name = next(iter(entries))
        return name, entries[name]
    if not entries:
        return _invalid("debugger_id", "This configuration declares no debugger at all, so there is no entry to carry a probe into.", configured_debuggers=[])
    return _invalid(
        "debugger_id",
        f"This configuration declares {len(entries)} debuggers and the call named none, so there is no entry this probe belongs to.",
        configured_debuggers=sorted(entries),
        next_step="Ask which configured probe the attached board is, then name it as debugger_id.",
    )


def discovered_port_names(matched_port: JsonObject | None) -> tuple[str, ...]:
    """Every name the discovered port answers to, stable spelling first.

    A port has two on Linux — ``/dev/serial/by-id/usb-..._0669FF...-if02`` and
    ``/dev/ttyACM0`` — and one on Windows. Both name one device, so an entry
    holding either of them is this port, and recognising that is what lets a
    configuration written before stable names existed be *re-spelled* rather than
    treated as somebody's deliberate choice of another device."""
    if not isinstance(matched_port, dict):
        return ()
    names = [str(matched_port.get("stable_device") or ""), str(matched_port.get("device") or "")]
    return tuple(dict.fromkeys(name for name in names if name))


def _names_discovered_port(value: object, port_names: tuple[str, ...]) -> bool:
    """Whether a configured `device` is one of the discovered port's spellings.

    A path question, so case folds the way the host's own filesystem folds it —
    `COM7` and `com7` are one port on Windows and `/dev/ttyACM0` and
    `/dev/ttyacm0` are two on Linux."""
    if not isinstance(value, str) or not value:
        return False
    folded = fold_device_path(value)
    return any(folded == fold_device_path(name) for name in port_names)


def _stabilises_device(current: object, device: str, port_names: tuple[str, ...]) -> bool:
    """Whether writing `device` over `current` only makes one port's name durable.

    True in exactly one situation: the entry already names this very port, by a
    spelling the host can reassign, and the discovered spelling is one it cannot.
    Deliberately that narrow. It is not a licence to normalise the case of a name
    somebody wrote, and it can never repoint an entry at a different device —
    both would be adoption overruling an operator, which is the thing this whole
    path does not do.

    It therefore never fires on Windows, where no device name is durable: there
    `device` stays exactly as written and `serial_number` carries the identity by
    itself."""
    if not isinstance(current, str) or current == device:
        return False
    return is_stable_device_name(device) and not is_stable_device_name(current) and _names_discovered_port(current, port_names)


def _choose_com_port(document: JsonObject, requested: str | None, port_names: tuple[str, ...]) -> tuple[str | None, JsonObject | None]:
    """Which COM port entry receives the discovered device, if any.

    An entry that already names this device wins, so running this twice on a
    bench with several ports is idempotent instead of ambiguous — and "names this
    device" means any of the port's spellings, so the entry is still recognised
    on the run that upgrades its kernel name to the stable one. Otherwise: the
    named one, the only one, or a new one when there is none. Several ports and
    no name is not resolved here — the caller is told to choose."""
    entries = _entries(document, "com_ports")
    if requested is not None:
        return requested, entries.get(requested)
    if port_names:
        matched = [name for name, entry in entries.items() if _names_discovered_port(entry.get("device"), port_names)]
        if len(matched) == 1:
            return matched[0], entries[matched[0]]
    if len(entries) == 1:
        name = next(iter(entries))
        return name, entries[name]
    if not entries:
        return DEFAULT_COM_PORT_ID, None
    return None, None


# ---------------------------------------------------------------------------
# The plan.


def _propose(carried: list[JsonObject], already: list[JsonObject], kept: list[JsonObject], *, key: str, section: str, field: str, current: Any, value: Any) -> None:
    """Sort one field into carried, already current, or left alone."""
    if value is None or (isinstance(value, str) and not value.strip()):  # pragma: no cover - callers filter empty discoveries
        return
    if current == value:
        already.append({"key": key, "value": value})
    elif is_unset(current, section, field):
        carried.append({"key": key, "value": value, "previous_value": current})
    else:
        kept.append(
            {
                "key": key,
                "configured_value": current,
                "discovered_value": value,
                "reason": "This key holds a value that is neither empty nor the placeholder the skeleton writes, so somebody set it. Adoption does not replace it.",
            }
        )


def _resulting(entry: JsonObject | None, field: str, discovered: Any) -> Any:
    """What a field holds once this adoption has been applied.

    Adoption fills what is unset and keeps what somebody set, so the value the
    entry ends up with is the configured one whenever there is one. Used to work
    out what will identify the entry *after* the write rather than before it."""
    current = (entry or {}).get(field)
    return discovered if is_unset(current, "com_ports", field) else current


def _adopted_identity_source(entry: JsonObject | None, device: str, serial: str, vid: int | None, pid: int | None) -> str | None:
    """What this entry's ``identity_source`` has to say once the values are in, if anything.

    Two jobs, and both exist because the file alone cannot answer the question
    version 3 asks. An adapter that publishes no serial number and one nobody has
    got round to reading are the same file; only a read of the hardware separates
    them, and this call has just made one.

    The first job is the declaration itself: an entry that will still carry no
    serial, no ``resource_id`` and no ``/dev/serial/by-id/...`` name gets the
    honest name of what does identify it — ``vid_pid``, ``vid``, ``pid``, or
    ``device`` when the adapter published nothing at all. That is the deliberate
    exception version 3 requires, written by the command that found out it
    applies rather than by hand.

    The second is keeping an existing declaration true. A port that once
    published no serial and was written down as ``identity_source: device`` may
    be replaced by one that does; adoption then fills the serial in, and a
    declaration left behind saying ``device`` would contradict the entry it sits
    in and refuse the file at load. So a declaration that has gone stale is
    corrected — narrowly, and only towards what the entry's own keys now say. It
    is not a value adoption is overruling: it never was a choice, only a record.

    ``None`` when the entry needs no declaration and carries none, which is the
    ordinary case on a bench whose adapters publish serials."""
    resulting = ComPortConfig(
        device=str(_resulting(entry, "device", device) or device),
        baudrate=0,
        timeout_s=0.0,
        write_timeout_s=0.0,
        encoding="",
        max_buffer_bytes=0,
        max_write_bytes=0,
        serial_number=_optional_string(_resulting(entry, "serial_number", serial)),
        vid=_resulting(entry, "vid", vid),
        pid=_resulting(entry, "pid", pid),
        resource_id=_optional_string((entry or {}).get("resource_id")),
    )
    declared = (entry or {}).get("identity_source")
    if com_port_carries_hardware_identity(resulting) and is_unset(declared, "com_ports", "identity_source"):
        return None
    return com_port_identity_source(resulting)


def _discovered_usb_id(matched_port: JsonObject | None, field: str) -> int | None:
    """One USB id out of a discovery record, in the spelling the file stores.

    pyserial reports integers and that is what is written down; a record that
    carries something else carries no answer, and adoption proposes nothing
    rather than a value the loader would have to guess at."""
    value = (matched_port or {}).get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def configured_probe_id(debugger: JsonObject) -> str | None:
    """The probe serial this entry already names, if somebody named one."""
    value = debugger.get("probe_id")
    if not isinstance(value, str) or is_unset(value, "debuggers", "probe_id"):
        return None
    return value


def _different_board(debugger_name: str, configured: str, discovered: str) -> JsonObject:
    """Refuse the whole plan, because it would describe two boards at once.

    The identity keys are not independent. ``probe_id`` names a board, and
    ``target.controller``, the COM device and the backend executable all describe
    *that* board. Carrying the ones that happen to be unset while keeping a
    ``probe_id`` that names a different probe produces a configuration whose
    debugger drives one Nucleo and whose UART talks to another — a file that
    loads, passes `doctor`, and is wrong in the one way nobody would look for.
    So the disagreement is not a per-key `kept`: nothing is planned and nothing
    is written."""
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_ADOPT,
        "error_type": "hardware_mismatch",
        "field": "probe_id",
        "summary": (
            f"`debuggers.{debugger_name}.probe_id` names probe '{configured}' and the attached probe is '{discovered}', so "
            "this is a different board. Nothing was planned and nothing was written: the controller, the COM device and "
            "the toolchain path all describe the attached board, and carrying them into an entry that names another probe "
            "would produce a configuration pointing at two boards at once."
        ),
        "debugger_id": debugger_name,
        "configured_probe_id": configured,
        "discovered_probe_id": discovered,
        "next_step": (
            f"Attach the board `debuggers.{debugger_name}` is configured for, or name the entry this attached board "
            "belongs to as debugger_id. Repointing a configured entry at another probe is an operator's edit, not this "
            "path's."
        ),
        "reference": CONFIG_SHAPE_URI,
        **NOT_STARTED,
        "retry_safe": False,
    }


def plan_adoption(document: JsonObject, discovery: JsonObject, *, debugger_id: str | None = None, com_port_id: str | None = None) -> JsonObject:
    """What carrying this hardware into this document would change, and what not.

    Pure: it reads a document and a discovery result and decides nothing about
    permissions, because nothing it produces can name one."""
    chosen = _choose_debugger(document, debugger_id)
    if isinstance(chosen, dict):
        return chosen
    debugger_name, debugger = chosen

    configured = configured_probe_id(debugger)
    discovered = discovery.get("probe_id")
    if configured is not None and isinstance(discovered, str) and discovered and fold_hardware_id(configured) != fold_hardware_id(discovered):
        return _different_board(debugger_name, configured, discovered)

    carried: list[JsonObject] = []
    already: list[JsonObject] = []
    kept: list[JsonObject] = []
    unavailable: list[JsonObject] = []

    _propose(
        carried,
        already,
        kept,
        key=f"debuggers.{debugger_name}.probe_id",
        section="debuggers",
        field="probe_id",
        current=debugger.get("probe_id"),
        value=discovery.get("probe_id"),
    )

    # The executable belongs to a backend, and `type` is deliberately not a key
    # this door opens: it decides which program drives the board rather than what
    # the board is. Writing an STM32CubeProgrammer path into an OpenOCD entry
    # would produce a configuration that loads and then fails at the first call,
    # so the mismatch is reported instead of carried.
    backend = str(discovery.get("backend") or "")
    entry_type = str(debugger.get("type") or skeleton_placeholder("debuggers", "type") or "")
    if entry_type == backend:
        _propose(
            carried,
            already,
            kept,
            key=f"debuggers.{debugger_name}.executable",
            section="debuggers",
            field="executable",
            current=debugger.get("executable"),
            value=discovery.get("executable"),
        )
    else:
        unavailable.append(
            {
                "key": f"debuggers.{debugger_name}.executable",
                "discovered_value": discovery.get("executable"),
                "reason": (
                    f"Discovery ran on the '{backend}' backend and this entry is type '{entry_type}', so the toolchain it "
                    "found is not the one this entry drives. `type` is not a key this path can change — it decides which "
                    "program reaches the board, not what the board is."
                ),
                "next_step": f"Set `debuggers.{debugger_name}.type` in the configuration file if this bench really is a '{backend}' bench, then run this again.",
            }
        )

    detected = discovery.get("target")
    controller = str(detected.get("controller") or "").lower() if isinstance(detected, dict) else ""
    override = debugger.get("target") if isinstance(debugger.get("target"), dict) else None
    if controller and override is not None:
        # This entry carries its own `target:` block, and `bind_debugger` gives
        # that block to everything that runs on this probe. Writing the detected
        # controller into the top-level `target:` would therefore not reach this
        # board at all — it would change the fallback the *other* entries use,
        # which is a different board, from a call that named this one. The closed
        # key model has no `debuggers.<name>.target.*` key, so there is nowhere
        # correct to put it.
        #
        # That is a fact about where a value could go, not about what the value
        # is — and the two were being conflated. Every override went to
        # `unavailable` with "set the discovered value yourself", including one
        # that already held exactly the discovered controller (nothing to do) and
        # one that deliberately held something else (an operator's choice this
        # path does not overrule, and does not quietly recommend overruling
        # either). Adoption's contract is report-and-preserve, so the *value* is
        # sorted first and only the genuinely unplaceable case is `unavailable`.
        configured = str(override.get("controller") or "").lower()
        if configured == controller:
            already.append({"key": f"debuggers.{debugger_name}.target.controller", "value": controller})
        elif is_unset(override.get("controller"), "target", "controller"):
            unavailable.append(
                {
                    "key": f"debuggers.{debugger_name}.target.controller",
                    "discovered_value": controller,
                    "reason": (
                        f"`debuggers.{debugger_name}` has its own `target:` block, which overrides the project target for "
                        "everything that runs on this probe, and that block does not name a controller. The detected "
                        "controller belongs in it, and this path cannot write it: `target.controller` at the top level is "
                        "the target other entries fall back to, so carrying it there would change a different board's "
                        "configuration from a call about this one."
                    ),
                    "next_step": (
                        f"Set `controller: {controller}` inside `debuggers.{debugger_name}.target` in the configuration "
                        f"file, or remove that block so `debuggers.{debugger_name}` uses the project target this path can fill in."
                    ),
                }
            )
        else:
            kept.append(
                {
                    "key": f"debuggers.{debugger_name}.target.controller",
                    "configured_value": override.get("controller"),
                    "discovered_value": controller,
                    "reason": (
                        f"`debuggers.{debugger_name}` has its own `target:` block naming a controller, and the attached "
                        "board reports a different one. Somebody set that value, so adoption does not replace it — and "
                        "does not tell you to replace it either: a deliberate override and a stale one look the same from "
                        "here, and only the operator knows which this is."
                    ),
                    "next_step": (
                        f"Decide which is right. If the board really is `{controller}`, change "
                        f"`debuggers.{debugger_name}.target.controller` in the configuration file yourself; if the "
                        "override is deliberate, nothing needs to happen."
                    ),
                }
            )
    elif controller:
        _propose(carried, already, kept, key="target.controller", section="target", field="controller", current=_mapping(document, "target").get("controller"), value=controller)

    matched_port = discovery.get("com_port") if isinstance(discovery.get("com_port"), dict) else None
    device = port_device_name(matched_port) if matched_port and matched_port.get("device") else None
    port_names = discovered_port_names(matched_port)
    # The port was correlated *by* this serial, so it is known whenever a port is.
    # Falling back to the probe's own spelling would be inventing an identity;
    # the enumerator's spelling is the one a later open compares against.
    port_serial = str((matched_port or {}).get("serial_number") or "") if device is not None else ""
    port_vid = _discovered_usb_id(matched_port, "vid") if device is not None else None
    port_pid = _discovered_usb_id(matched_port, "pid") if device is not None else None
    port_name, port_entry = _choose_com_port(document, com_port_id, port_names)
    if device is None:
        available = discovery.get("available_com_ports")
        ports = available.get("ports") if isinstance(available, dict) else None
        unavailable.append(
            {
                "key": "com_ports.<name>.device",
                "reason": (
                    "No host serial port carries this probe's serial number, so discovery cannot say which device belongs "
                    "to this board. A Nucleo exposes one over the probe; a board wired to a separate USB-serial adapter does not."
                ),
                "host_com_ports": ports if isinstance(ports, list) else [],
                "next_step": "Name the device yourself with `project_config_set` on `com_ports.<name>.device` if you know which of the host ports it is.",
            }
        )
    elif port_name is None:
        unavailable.append(
            {
                "key": "com_ports.<name>.device",
                "discovered_value": device,
                "reason": f"This configuration declares {len(_entries(document, 'com_ports'))} COM ports, none of them naming {device}, and the call named none. Adoption does not choose one.",
                "configured_com_ports": sorted(_entries(document, "com_ports")),
                "next_step": "Name the entry this device belongs to as com_port_id.",
            }
        )
    else:
        current_device = (port_entry or {}).get("device")
        if _stabilises_device(current_device, device, port_names):
            # The entry already names this very port, by its other spelling.
            # Rewriting it is not replacing a value somebody chose — it is the
            # same device, written down so it survives the next replug — so it
            # goes to `carried` rather than to `kept`, which is where the "keeps
            # working, and gets fixed on the next adopt-hardware" half of
            # the serial-port identity rule is actually implemented.
            carried.append(
                {
                    "key": f"com_ports.{port_name}.device",
                    "value": device,
                    "previous_value": current_device,
                    "reason": (
                        f"`{current_device}` and `{device}` are the same port. The kernel name depends on the order "
                        "devices were enumerated in, so it can come to mean another board; the stable name cannot."
                    ),
                }
            )
        else:
            _propose(
                carried,
                already,
                kept,
                key=f"com_ports.{port_name}.device",
                section="com_ports",
                field="device",
                current=current_device,
                value=device,
            )
        if port_serial:
            _propose(
                carried,
                already,
                kept,
                key=f"com_ports.{port_name}.serial_number",
                section="com_ports",
                field="serial_number",
                current=(port_entry or {}).get("serial_number"),
                value=port_serial,
            )
        # The same record already carries the vendor and product ids, so they
        # cost nothing to write and they are what makes the serial mean a unit:
        # a USB serial is unique within a vendor and nowhere else.
        # Reported exactly like the serial — carried, already current, or left
        # alone — because they are the same kind of fact about the same entry.
        for field, discovered in (("vid", port_vid), ("pid", port_pid)):
            if discovered is None:
                continue
            _propose(
                carried,
                already,
                kept,
                key=f"com_ports.{port_name}.{field}",
                section="com_ports",
                field=field,
                current=(port_entry or {}).get(field),
                value=discovered,
            )
        # And what identifies the entry, when the adapter published no serial to
        # identify it by. This is the half of the version 3 rule that only a read
        # of the hardware can settle: the file cannot tell "this adapter has no
        # serial number" from "nobody filled it in", and this call has just asked
        # the adapter. Written as a fact discovered here, exactly like the serial
        # and the ids above, so an entry adopted from a cheap USB-serial bridge
        # satisfies version 3 without anybody hand-editing YAML.
        #
        # Not written when the entry names hardware on its own — a serial, a
        # `resource_id`, or a `/dev/serial/by-id/...` name — because there the
        # declaration would be a key that repeats what is already unambiguous.
        declared = _adopted_identity_source(port_entry, device, port_serial, port_vid, port_pid)
        current_declaration = (port_entry or {}).get("identity_source")
        if declared is not None and current_declaration not in (None, declared) and isinstance(current_declaration, str):
            # A declaration that has stopped describing the entry. Corrected
            # rather than kept, for the reason `_stabilises_device` above rewrites
            # a device name: this is not a value being overruled but the same
            # entry's own answer, brought back into agreement with the keys this
            # call just filled in. Left alone it would refuse the file at load.
            carried.append(
                {
                    "key": f"com_ports.{port_name}.identity_source",
                    "value": declared,
                    "previous_value": current_declaration,
                    "reason": (
                        f"This entry says it is identified by `{current_declaration}` and its keys now say `{declared}`. "
                        "The declaration records which key carries the identity; a version 3 configuration is refused "
                        "while the two disagree."
                    ),
                }
            )
        elif declared is not None:
            _propose(
                carried,
                already,
                kept,
                key=f"com_ports.{port_name}.identity_source",
                section="com_ports",
                field="identity_source",
                current=current_declaration,
                value=declared,
            )

    return {
        "ok": True,
        "debugger_id": debugger_name,
        "com_port_id": port_name,
        "creates_com_port": bool(port_name is not None and port_entry is None and device is not None),
        "carried": carried,
        "already_current": already,
        "kept": kept,
        "unavailable": unavailable,
    }


# ---------------------------------------------------------------------------
# The tool.


def project_config_adopt_hardware(
    workspace: Path,
    existing: AgenticHILConfig | None,
    arguments: JsonObject | None = None,
    *,
    coordinator: HardwareCoordinator | None = None,
    frontend: str = "python",
    open_holds: JsonObject | None = None,
    actor: str = ACTOR_AGENT,
    via: str = f"mcp:{PROJECT_CONFIG_ADOPT}",
) -> JsonObject:
    """Read what is attached and carry its identity into the configuration.

    ``coordinator`` is the caller's own — the MCP service's, so a hold it already
    has is seen — and a caller that has none gets one for the length of this call.
    There is deliberately no way to run this without one: the read below talks to
    a board, and a board on this machine belongs to whoever holds it."""
    if coordinator is not None or existing is None:
        return _guarded(workspace, existing, arguments or {}, coordinator, open_holds=open_holds, actor=actor, via=via)
    owned = HardwareCoordinator(existing, frontend)
    try:
        return _guarded(workspace, existing, arguments or {}, owned, open_holds=open_holds, actor=actor, via=via)
    finally:
        owned.close()


def _guarded(workspace: Path, existing: AgenticHILConfig | None, arguments: JsonObject, coordinator: HardwareCoordinator | None, *, open_holds: JsonObject | None, actor: str, via: str) -> JsonObject:
    try:
        return _adopt(workspace, existing, arguments, coordinator, open_holds=open_holds, actor=actor, via=via)
    except ConfigError as error:
        # Reading the document is the first thing this does, so a file that will
        # not decode or will not parse arrives here — as a refusal that says so,
        # carrying the state of the configuration, rather than as an internal
        # error from underneath the MCP layer.
        return with_config_status({"tool": PROJECT_CONFIG_ADOPT, **error.to_dict(), **NOT_STARTED}, config_status(existing))


def _adopt(workspace: Path, existing: AgenticHILConfig | None, arguments: JsonObject, coordinator: HardwareCoordinator | None, *, open_holds: JsonObject | None, actor: str, via: str) -> JsonObject:
    if existing is None:  # pragma: no cover - the unprovisioned service answers first
        raise ConfigError("config_file_not_found", "This workspace has no Agentic HIL configuration to carry hardware into.", {"workspace_root": str(workspace)})
    # Before discovery, not only before the write. Enumerating probes and
    # connecting in HOTPLUG mode talks to the board, and a run holding it must not
    # have something reach past it — the same reason a configuration write is
    # refused here, one step earlier because this call touches hardware first.
    if open_holds:
        return _held_refusal(existing, open_holds)

    target_path = authoritative_write_target(workspace, existing)
    _, document = load_config_document(target_path)

    # Which entry this is about is decided before anything is said to a board.
    # A call that cannot name one is refused without a probe having been touched,
    # and the entry decides both the legacy read grant below and which physical
    # probe this call may talk to at all.
    chosen = _choose_debugger(document, _optional_string(arguments.get("debugger_id")))
    if isinstance(chosen, dict):
        return {**chosen, "path": str(target_path), "workspace_root": existing.workspace_root}
    debugger_name, debugger_entry = chosen

    denied = _read_denied(existing, debugger_name, target_path)
    if denied is not None:
        return denied

    apply = bool(arguments.get("apply", False))
    # The configured serial selects, when there is one. Otherwise a bench with
    # two boards attached would enumerate, pick the one probe that answers, and
    # produce a plan about a board this entry is not for.
    requested_probe = _optional_string(arguments.get("probe_id")) or configured_probe_id(debugger_entry)
    configured_resources = configured_probe_resource(existing, debugger_name)
    discovery, refusal = discover_under_hardware_lease(
        existing,
        coordinator,
        tool=PROJECT_CONFIG_ADOPT,
        reason_prefix="adopt",
        resources=configured_resources,
        probe_id=requested_probe,
    )
    if refusal is not None:
        return {**refusal, "path": str(target_path), "workspace_root": existing.workspace_root}
    if not overall_success(discovery):
        return {
            **discovery,
            "tool": PROJECT_CONFIG_ADOPT,
            "summary": f"{discovery.get('summary', 'Hardware discovery failed.')} Nothing was read out of the configuration and nothing was written.",
            "path": str(target_path),
            "workspace_root": existing.workspace_root,
            "next_step": str(discovery.get("next_step") or "Attach the board this project drives, then call this again. Nothing was written."),
            **NOT_STARTED,
        }

    plan = plan_adoption(document, discovery, debugger_id=debugger_name, com_port_id=_optional_string(arguments.get("com_port_id")))
    if plan.get("ok") is not True:
        return {**plan, "hardware_discovery": discovery, "path": str(target_path), "workspace_root": existing.workspace_root}

    payload: JsonObject = {
        "path": str(target_path),
        "workspace_root": existing.workspace_root,
        "debugger_id": plan["debugger_id"],
        "com_port_id": plan["com_port_id"],
        "carried": plan["carried"],
        "already_current": plan["already_current"],
        "kept": plan["kept"],
        "unavailable": plan["unavailable"],
        "hardware_discovery": discovery,
    }
    carried: list[JsonObject] = list(plan["carried"])

    if not apply:
        return {
            "ok": True,
            "tool": PROJECT_CONFIG_ADOPT,
            "summary": _plan_summary(plan),
            "applied": False,
            **payload,
            "next_steps": _next_steps(plan, applied=False),
            **NOT_STARTED,
            "cleanup_required": False,
        }
    if not carried:
        return {
            "ok": True,
            "tool": PROJECT_CONFIG_ADOPT,
            "summary": f"{_plan_summary(plan)} Nothing was written.",
            "applied": False,
            **payload,
            "next_steps": _next_steps(plan, applied=False),
            **NOT_STARTED,
            "cleanup_required": False,
        }

    write = project_config_set(
        workspace,
        existing,
        [{"key": str(item["key"]), "value": item["value"]} for item in carried],
        open_holds=open_holds,
        actor=actor,
        via=via,
        # What each key held when the plan decided it was free to fill. Reading a
        # probe takes seconds, and another CLI or MCP process can fill a
        # placeholder inside them; without this the "never overwrites a value
        # somebody chose" rule would hold only against values chosen before the
        # read started.
        expect={str(item["key"]): item["previous_value"] for item in carried},
        # And the document those decisions were taken against, whole. The keys
        # this write names are not the only inputs to the plan: `probe_id` when
        # it was already set decided which physical probe was read at all, `type`
        # decided whether the executable could be carried, and which entries
        # exist decided which entry receives any of it. None of those is a key
        # this tool can set, so no per-key expectation can cover them — and a
        # `probe_id` that moves from A to B inside the read window would
        # otherwise commit A's controller, executable and COM device into an
        # entry that now names B. That is precisely the mixed-board file this
        # module refuses to produce, arriving by the back door.
        expect_document=document,
    )
    if not overall_success(write):
        # The refusal is the answer, and the values stay in it: an agent that may
        # not write this file can still hand the operator the exact keys rather
        # than a probe listing they have to correlate themselves.
        return {
            **write,
            "tool": PROJECT_CONFIG_ADOPT,
            "applied": False,
            "summary": f"{write.get('summary', 'The change was refused.')} Nothing was written; the values this would have carried are under `carried`.",
            **payload,
            "write": write,
        }
    return {
        "ok": True,
        "tool": PROJECT_CONFIG_ADOPT,
        "summary": f"{len(carried)} configuration key(s) were filled in from the attached hardware.",
        "applied": True,
        **payload,
        "created_entries": write.get("created_entries", []),
        "permissions_changed": write.get("permissions_changed", []),
        "provenance": write.get("provenance", {}),
        "write": write,
        "reload_required": True,
        "next_steps": _next_steps(plan, applied=True),
        **NOT_STARTED,
        "cleanup_required": False,
    }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------
# Reading the board, under the locks and the audit a board read takes.


def _read_denied(existing: AgenticHILConfig, debugger_id: str, target_path: Path) -> JsonObject | None:
    """Refuse to read a probe a version 1 configuration keeps closed.

    From version 2 on there is no read permission — exclusivity replaced it, and
    the locks below are that exclusivity. A version 1 file still says whether its
    probe may be read at all, and `allow_probe: false` there means `probe_target`
    and `debugger_probes_list` are both refused. Adoption reads the same probe by
    the same means, so a file that closed it must not find this path open: the
    ability to fill in a serial is not the ability to widen a policy."""
    entry = existing.debuggers.get(debugger_id)
    if existing.probe_allowed(entry):
        return None
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_ADOPT,
        "error_type": "permission_denied",
        "summary": (
            f"Reading debugger '{debugger_id}' is disabled by allow_probe in this version {existing.config_version} "
            "configuration, and carrying hardware in means reading it. Nothing was read and nothing was written."
        ),
        "permission": "allow_probe",
        "permission_key": f"debuggers.{debugger_id}.permissions.allow_probe",
        "debugger_id": debugger_id,
        "config_version": existing.config_version,
        "path": str(target_path),
        "workspace_root": existing.workspace_root,
        "next_step": (
            "This refusal is the answer to the request. Report it and name the permission that is denied, then stop. "
            "You must not enable it, and you must not read the probe another way. Migrating this file to version 2 — "
            "where reading needs no grant — is the operator's edit."
        ),
        "reference": CONFIG_SHAPE_URI,
        **remediation_fields("permission_denied"),
        **NOT_STARTED,
        "retry_safe": False,
    }


def configured_probe_resource(existing: AgenticHILConfig, debugger_id: str) -> list[str]:
    """The machine-wide locks of a configured entry, when it names a board.

    ``resource_id`` and ``probe_id`` name a physical unit and produce the same
    keys a run holding that board holds. The remaining fallbacks name a toolchain
    — every unfilled skeleton on the host would derive one and the same
    `probe:openocd` — so locking those would serialize two unrelated benches
    against each other while protecting neither, and none is returned.

    Every key the device carries, not just its primary: an entry naming both a
    ``resource_id`` and a probe serial is leased under both, so the read holds the
    serial another workspace's bootstrap `init` derives from enumeration as well
    as the alias only this workspace knows."""
    entry = existing.debuggers.get(debugger_id)
    if entry is None:
        return []
    device = DebuggerDevice(config_id=debugger_id, debugger=entry)
    return list(device.lock_keys) if device.identity_source in {"resource_id", "probe_id"} else []


def discover_under_hardware_lease(
    existing: AgenticHILConfig,
    coordinator: HardwareCoordinator | None,
    *,
    tool: str,
    reason_prefix: str,
    resources: list[str],
    probe_id: str | None = None,
) -> tuple[JsonObject, JsonObject | None]:
    """Read the attached probe holding what a probe read holds.

    Four things, and none of them is this path's invention — they are what
    `debugger_probes_list` already does for the same two commands:

    * the audit trail is proven writable *before* the board is touched, so a
      bench that cannot record what happened does not have it happen;
    * the host-wide enumeration pseudo-resource, the entries this call is about
      (``resources``) and the physical probe the enumeration selects are all
      locked, so a second MCP server, a second `agentic-hil adopt-hardware`, or a
      run in another project is told `device_busy` instead of finding a HOTPLUG
      connect underneath it. A configured entry has to be in that list in its own
      right: it is the canonical alias a run holds the board through, and a read
      that locked only the enumerated serial would connect to a board another
      owner is holding under its `resource_id`;
    * the read is written to the audit trail before anything is done with it, and
      a failure to write it quarantines;
    * a raised exception or a release that does not come back clean leaves the
      locks taken and the leases quarantined, because what was said to that board
      is then unknown. A terminal record that cannot be committed comes after the
      locks are already back, so it raises the incident on the coordinator
      instead — same effect on the next caller, which is that there is no next
      hardware call until an operator resolves it.

    ``tool`` names the caller in every result and record, and ``reason_prefix``
    names it in a quarantine reason — the callers are `project_config_adopt_hardware`,
    `project_config_create` and `agentic-hil init`, and an incident
    has to say which one left it.

    Returns the discovery result and, when the read never got that far or did not
    end cleanly, the refusal that is the whole answer.
    """
    if coordinator is None:  # pragma: no cover - every caller supplies or is given one
        raise ConfigError("config_invalid", "Reading attached hardware requires a hardware coordinator.", {"tool": tool})
    try:
        ensure_audit_ready(existing)
    except (ConfigError, OSError) as error:
        return {}, {**audit_unavailable(tool, error), **NOT_STARTED, "retry_safe": False}
    if coordinator.blocked:
        return {}, _quarantined_refusal(coordinator, tool)

    held: list[HardwareLease] = []
    requested = [DEBUGGER_DISCOVERY_RESOURCE, *dict.fromkeys(resources)]
    try:
        held.append(coordinator.acquire(*requested))
    except CoordinationError as error:
        return {}, _coordination_refusal(error, requested, tool)

    def before_connect(selected: str) -> JsonObject | None:
        # Which probe the enumeration actually chose is known only here, and the
        # HOTPLUG connect that follows is the first thing said to it.
        resource = f"probe:{fold_hardware_id(selected)}"
        if any(resource in lease.resources for lease in held):
            return None
        try:
            held.append(coordinator.acquire(resource))
        except CoordinationError as error:
            return _coordination_refusal(error, [resource], tool)
        return None

    try:
        discovery = discover_attached_hardware(probe_id=probe_id, before_connect=before_connect)
    except BaseException as error:
        # Fail closed. What reached the board is unknown, so the locks stay taken
        # and an operator — or `agentic-hil recover` — owns the incident.
        for lease in held:
            lease.quarantine(f"{reason_prefix}_discovery_exception", error)
        raise

    record = write_report(existing, {**discovery, "tool": tool, **_combined_status(held)})
    if record.get("audit_ok") is False:
        for lease in held:
            lease.quarantine(f"{reason_prefix}_discovery_audit_broken", audit_broken=True)
        return {}, {
            **record,
            "ok": False,
            "tool": tool,
            "error_type": "audit_failed_after_action",
            "summary": "The attached probe was read and the audit record of that read could not be written, so the probe is quarantined and nothing was written to the configuration.",
            **_combined_status(held),
            "retry_safe": False,
        }
    released = True
    for lease in reversed(held):
        if lease.state == "active":
            # `release` returns False for a durable-record or lock-release
            # failure and leaves the lease registered, blocking and quarantined.
            # Discarding that answer reported a clean read of a board this
            # process is still holding, and let the configuration write go ahead
            # under it — the one place where "the hardware is fine now" has to be
            # a checked claim rather than an assumption.
            released = lease.release() and released
    status = _combined_status(held)
    if not released or status["cleanup_required"] or status["quarantined"]:
        return {}, _refuse_after_failed_release(existing, record, discovery, status, tool)
    # The clean path re-commits too, so the persisted record carries the state the
    # leases actually ended in (`released`) rather than the `active` they were
    # written under. A no-op when the two already agree.
    committed = recommit_report_with_status(existing, record, status)
    if committed.get("audit_ok") is False:
        return {}, _terminal_audit_refusal(coordinator, committed, status, tool=tool, reason=f"{reason_prefix}_terminal_audit_broken")
    return discovery, None


def _terminal_audit_refusal(
    coordinator: HardwareCoordinator,
    committed: JsonObject,
    status: JsonObject,
    *,
    tool: str,
    reason: str,
) -> JsonObject:
    """The locks came back clean and the record of that could not be written.

    The leases are gone by the time this runs — released, deregistered, their
    locks handed back — so there is nothing left to quarantine and the status they
    ended in says `released`, `cleanup_required: false`, `quarantined: false`.
    Returning that beside `audit_failed_after_action` was two answers to one
    question: the summary said the bench needed an operator and every field a
    caller actually branches on said it was fine. Worse, this service went on
    serving; the coordinator was never told, so the next hardware call acquired as
    usual over a bench whose last read has no audit record.

    So the incident is raised on the coordinator, over the resources that were
    read, and the returned status is that incident rather than the pre-failure
    one. `poison` with no registered leases takes the project lock and persists a
    `cleanup_required` project record, which is what makes this survive the
    process: the next owner adopts it instead of finding a clean slate, and
    `agentic-hil recover` is the way out for both. A coordinator that cannot be
    poisoned — already closed, project lock unavailable — is reported rather than
    swallowed, and the refusal stands either way."""
    resources = [str(resource) for resource in status.get("resources") or [] if isinstance(resource, str)]
    poison_error: str | None = None
    try:
        coordinator.poison(reason, audit_broken=True, resources=resources)
    except (CoordinationError, OSError) as error:
        poison_error = str(error)
    blocked = {
        **status,
        "lease_state": "quarantined",
        "cleanup_required": True,
        "quarantined": True,
        "cleanup_reasons": sorted({*(str(item) for item in status.get("cleanup_reasons") or []), reason}),
        "quarantine_id": coordinator.quarantine_id,
    }
    refusal: JsonObject = {
        **committed,
        "ok": False,
        "tool": tool,
        "error_type": "audit_failed_after_action",
        "summary": (
            "The attached probe was read, the lease on it was released, and the record could not be updated to say "
            "so. Nothing was written to the configuration, because a write made now would not be described by any "
            "audit record an operator can read, and this bench is quarantined until an operator resolves it."
        ),
        "next_step": "Resolve the incident with `agentic-hil recover` once the bench is known to be in a safe state, then call this again.",
        **remediation_fields("audit_failed_after_action"),
        **blocked,
        "retry_safe": False,
    }
    if poison_error is not None:
        # The incident could not even be raised. Say so — the refusal is still a
        # refusal, and an operator now has two things to look at rather than one.
        refusal["quarantine_error"] = poison_error
    return refusal


def _refuse_after_failed_release(existing: AgenticHILConfig, record: JsonObject, discovery: JsonObject, status: JsonObject, tool: str) -> JsonObject:
    """Refuse, and make the audit trail say so.

    The read is written as an `ok: true`, `lease_state: active` report before the
    release is attempted, which is right — the read happened. What was wrong was
    stopping there: the release then failed, this returned `resource_quarantined`,
    and nothing rewrote the record. `get_last_report` went on describing a clean
    active discovery and `classify_last_error` had no failure at all, so the two
    places an operator looks after a refusal both said the bench was fine.

    So the terminal outcome is committed before it is returned. Committed
    unconditionally, unlike ``recommit_report_with_status``: that helper skips the
    write when the lease states already agree, and here they can — a release can
    report failure while leaving the lease's own state alone — and skipping is
    exactly the hole being closed. `last_failure` is what this is for, and only a
    written failure report reaches it.

    If that write itself fails it is not swallowed: the result says
    `audit_failed_after_action` and every lease is quarantined, because a bench
    whose incident cannot be recorded is in a worse state than one whose incident
    can.
    """
    refusal = _release_refusal(discovery, status, tool)
    committed = write_report(existing, {**record, **refusal, **status})
    if committed.get("audit_ok") is False:
        return {
            **committed,
            "ok": False,
            "tool": tool,
            "error_type": "audit_failed_after_action",
            "summary": (
                "The attached probe was read, the lease on it could not be released cleanly, and the record of that "
                "outcome could not be written either. The board is quarantined, nothing was written to the "
                "configuration, and the audit trail does not describe what happened."
            ),
            **status,
            "cleanup_required": True,
            "quarantined": True,
            "retry_safe": False,
        }
    return {**committed, **refusal, "report_path": committed.get("report_path"), "audit_ok": committed.get("audit_ok")}


def _discovery_side_effect(discovery: JsonObject) -> JsonObject:
    """What the discovery said about its own side effect, carried without inventing one.

    ``bool(discovery.get("side_effect_committed", False))`` rendered a marker
    that was never set as the positive claim that nothing was committed, and the
    status beside it defaulted to `not_started` from the same absence. Absence
    means unknown here as everywhere else — ``mark_side_effect`` turns a missing
    marker into `side_effect_status: unknown` precisely because a backend that
    could not tell must not be read as one that said no.

    So an absent marker stays absent and the status says `unknown`. A discovery
    that stated its own status keeps it; one that stated only the marker gets the
    status ``mark_side_effect`` would give it. Unreachable from the discovery
    path as it stands — every result it returns carries the marker — which is
    what makes the coercion worth removing rather than relying on.
    """
    committed = discovery.get("side_effect_committed")
    status = discovery.get("side_effect_status")
    fields: JsonObject = {}
    if isinstance(committed, bool):
        fields["side_effect_committed"] = committed
    if isinstance(status, str) and status:
        fields["side_effect_status"] = status
    elif isinstance(committed, bool):
        fields["side_effect_status"] = "partial" if committed else "not_started"
    else:
        fields["side_effect_status"] = "unknown"
    return fields


def _release_refusal(discovery: JsonObject, status: JsonObject, tool: str) -> JsonObject:
    """The probe was read and this process could not give it back.

    Nothing is written after this. The read itself succeeded, so its result is
    carried for the operator to read, but a configuration written now would be
    written by a process holding quarantined hardware — and the answer would say
    `ok: true` about a bench that needs `agentic-hil recover` before anything
    else touches it.
    """
    return {
        "ok": False,
        "tool": tool,
        "error_type": "resource_quarantined",
        "summary": (
            "The attached probe was read and the lease on it could not be released cleanly, so the board is "
            "quarantined and nothing was written to the configuration. What was discovered is under "
            "`hardware_discovery`; the bench has to be resolved before it can be carried into the file."
        ),
        "hardware_discovery": discovery,
        "next_step": "Resolve the incident with `agentic-hil recover` once the bench is known to be in a safe state, then call this again.",
        # The read is what happened; the release is what did not. Both are said,
        # and neither is inferred from the other — including where the discovery
        # said neither.
        **_discovery_side_effect(discovery),
        "hardware_state": str(discovery.get("hardware_state") or "unknown"),
        **remediation_fields("resource_quarantined"),
        **status,
        "retry_safe": False,
    }


# Worst first. An aggregate over several leases has to answer for the one in the
# worst state, because that is the one an operator has to act on; "the other lock
# came back cleanly" is not a fact anybody can use. `released` is last on purpose:
# it is the only value in this list that means nothing is held, so it may only be
# said when every lease says it.
_LEASE_STATE_PRIORITY = ("quarantined", "cleanup_required", "stale", "releasing", "active", "released")


def _combined_status(held: list[HardwareLease]) -> JsonObject:
    """One lease status for however many locks this read had to take.

    The aggregate state is the worst of them, not the first non-`active` one. A
    read takes two or three locks — the enumeration pseudo-resource and the
    physical probe — and they are released independently, so "the first one that
    is not active" was whichever came first in the list: with the enumeration
    lease released and the probe lease in `cleanup_required`, the result said
    `lease_state: released` beside `cleanup_required: true`. Callers are told to
    treat any state other than null/active/released as blocking, and that
    combination told them the opposite of what was true about the board.

    ``quarantine_id`` travels with it for the same reason: it is what
    `agentic-hil recover --quarantine-id` needs, and dropping it left the refusal
    naming an incident the operator could not address. Per-lease statuses are
    carried too, so a partial release can be read as the partial thing it is."""
    statuses = [lease.status() for lease in held]
    if not statuses:  # pragma: no cover - a read always holds at least the enumeration lease
        return {"lease_ids": [], "resources": [], "lease_state": "active", "cleanup_required": False, "quarantined": False, "cleanup_reasons": [], "quarantine_id": None}
    states = {str(status["lease_state"]) for status in statuses}
    worst = next((state for state in _LEASE_STATE_PRIORITY if state in states), sorted(states)[0])
    return {
        "lease_ids": [str(status["lease_id"]) for status in statuses],
        "resources": sorted({resource for status in statuses for resource in status["resources"]}),
        "lease_state": worst,
        "cleanup_required": any(status["cleanup_required"] for status in statuses),
        "quarantined": any(status["quarantined"] for status in statuses),
        "cleanup_reasons": sorted({reason for status in statuses for reason in status["cleanup_reasons"]}),
        # The incident an operator has to name to `recover`. The first lease that
        # has one, in the order they were taken.
        "quarantine_id": next((status["quarantine_id"] for status in statuses if status.get("quarantine_id")), None),
        "leases": [
            {"lease_id": status["lease_id"], "lease_state": status["lease_state"], "resources": list(status["resources"]), "quarantine_id": status["quarantine_id"]}
            for status in statuses
        ],
    }


def _quarantined_refusal(coordinator: HardwareCoordinator, tool: str) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": "resource_quarantined",
        "summary": "Hardware on this bench has unresolved cleanup or audit state, and reading a probe is a hardware action, so nothing was read and nothing was written.",
        "cleanup_required": True,
        "quarantined": True,
        "cleanup_reasons": sorted({reason for lease in coordinator.leases.values() for reason in lease.cleanup_reasons()}),
        "quarantine_id": coordinator.quarantine_id,
        "next_step": "Resolve the incident with `agentic-hil recover` once the bench is known to be in a safe state, then call this again.",
        **NOT_STARTED,
        "retry_safe": False,
    }


def _coordination_refusal(error: CoordinationError, resources: list[str], tool: str) -> JsonObject:
    result = dict(error.result)
    return {
        "tool": tool,
        **result,
        "requested_resources": sorted(resources),
        "next_step": str(
            result.get("next_step")
            or "The board this would read belongs to the owner named above. Wait for it to be released, then call this again. Nothing was read and nothing was written."
        ),
        **remediation_fields(str(result.get("error_type") or "")),
        **NOT_STARTED,
    }


def _plan_summary(plan: JsonObject) -> str:
    parts = [f"{len(plan['carried'])} configuration key(s) would be filled in from the attached hardware"]
    if plan["already_current"]:
        parts.append(f"{len(plan['already_current'])} already match it")
    if plan["kept"]:
        parts.append(f"{len(plan['kept'])} hold a value somebody set and are left alone")
    if plan["unavailable"]:
        parts.append(f"{len(plan['unavailable'])} could not be answered from what is attached")
    return f"{'; '.join(parts)}."


def _next_steps(plan: JsonObject, *, applied: bool) -> list[str]:
    steps: list[str] = []
    if not applied and plan["carried"]:
        steps.append('Call this again with {"apply": true} to write these keys. Nothing has been written yet.')
    if plan["kept"]:
        steps.append(
            "Report the keys under `kept`: the configuration and the attached hardware disagree there, and this path "
            f"does not decide which is right. Changing one is `{PROJECT_CONFIG_SET}` with `permissions.{CONFIG_DESCRIPTION_RIGHT}`, or the operator editing the file."
        )
    for item in plan["unavailable"]:
        if item.get("next_step"):
            steps.append(str(item["next_step"]))
    if applied:
        steps.append(
            "This server is still serving the configuration it loaded at startup. Ask the operator to restart the MCP "
            "server before relying on anything this change decides."
        )
        if plan["creates_com_port"]:
            steps.append("The COM port entry this created carries every permission false, written by the server. Writing to that port stays an operator's decision.")
    steps.append(f"{CONFIG_SHAPE_URI} explains which keys this configuration has and which permission opens each of them.")
    return steps


def _held_refusal(existing: AgenticHILConfig, open_holds: JsonObject) -> JsonObject:
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_ADOPT,
        "error_type": "config_write_in_open_run",
        "summary": (
            "This server is holding hardware right now. Reading a probe's identity means talking to it, and the "
            "configuration behind those holds may not move underneath them, so nothing was read and nothing was written."
        ),
        "open_holds": open_holds,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        "next_step": "Close the run with `bench_run_stop` and stop any open COM or CAN session, then call this again.",
        **NOT_STARTED,
        "retry_safe": True,
    }


__all__ = [
    "DEFAULT_COM_PORT_ID",
    "PROJECT_CONFIG_ADOPT",
    "configured_probe_id",
    "configured_probe_resource",
    "discover_under_hardware_lease",
    "is_unset",
    "plan_adoption",
    "project_config_adopt_hardware",
    "skeleton_placeholder",
]
