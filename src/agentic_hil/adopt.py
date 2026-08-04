"""Carrying attached hardware into a configuration that was written without it.

Discovery used to happen exactly once, at ``init`` time. The most common way to
set a bench up — install the tool, then plug the board in — therefore produced a
placeholder file (``probe_id: null``, ``executable: null``, a placeholder target)
and no way back into it. In the session behind hardci-hq#76 that ended with an
agent printing a 24-character probe serial for a person to retype, which is the
one value nobody can guess and nobody transcribes correctly twice.

This is the way back, and it is narrow on purpose:

**It carries identity, and only identity.** ``probe_id``, ``executable``,
``target.controller`` and a COM device — precisely what an attached probe hands
you and what the file cannot know without it. Every one of them falls in the
description half of hardci-hq#80, so this path cannot reach a ``permissions:``
block at all.

**It takes no values from its caller.** Arguments *select* — which probe of
several, which entry receives them — and never supply. The values come from
what is attached to this machine, exactly as ``project_config_create``'s do.

**It never overwrites a value somebody chose.** A field is carried only when the
document has nothing there: absent, ``null``, empty, or the placeholder the
shipped skeleton writes. A value that is none of those and disagrees with the
hardware is reported as a disagreement and left alone. Silently replacing a
deliberate choice is the failure mode that would make this path worse than the
retyping it removes.

**It writes through one door.** The changes go into ``project_config_set``, so
the grants, the scalar-only shape, the closed key model, the before/after
comparison of every permission in the document, the refusal while hardware is
held, the validate-before-replace and the ``provenance`` record are all the ones
that were already there. There is no second way to write this file.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

from agentic_hil.bootstrap import discover_attached_hardware
from agentic_hil.config import DEFAULT_CONFIG_TEMPLATE, ConfigError
from agentic_hil.configwrite import (
    ACTOR_AGENT,
    NOT_STARTED,
    PROJECT_CONFIG_SET,
    authoritative_write_target,
    load_config_document,
    project_config_set,
)
from agentic_hil.knowledge import CONFIG_DESCRIPTION_RIGHT, CONFIG_NAMED_SECTIONS, CONFIG_SHAPE_URI
from agentic_hil.report import overall_success
from agentic_hil.types import AgenticHILConfig, JsonObject

PROJECT_CONFIG_ADOPT = "project_config_adopt_hardware"

# The name a COM port entry gets when the configuration has none yet. The same
# name the generated configuration uses, so a bench set up the two ways reads
# the same.
DEFAULT_COM_PORT_ID = "dut_uart"


# ---------------------------------------------------------------------------
# What counts as unset.


@cache
def _skeleton() -> JsonObject:
    """The shipped deny-by-default skeleton, parsed once.

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


def _choose_com_port(document: JsonObject, requested: str | None, device: str | None) -> tuple[str | None, JsonObject | None]:
    """Which COM port entry receives the discovered device, if any.

    An entry that already names this device wins, so running this twice on a
    bench with several ports is idempotent instead of ambiguous. Otherwise: the
    named one, the only one, or a new one when there is none. Several ports and
    no name is not resolved here — the caller is told to choose."""
    entries = _entries(document, "com_ports")
    if requested is not None:
        return requested, entries.get(requested)
    if device is not None:
        matched = [name for name, entry in entries.items() if entry.get("device") == device]
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


def plan_adoption(document: JsonObject, discovery: JsonObject, *, debugger_id: str | None = None, com_port_id: str | None = None) -> JsonObject:
    """What carrying this hardware into this document would change, and what not.

    Pure: it reads a document and a discovery result and decides nothing about
    permissions, because nothing it produces can name one."""
    chosen = _choose_debugger(document, debugger_id)
    if isinstance(chosen, dict):
        return chosen
    debugger_name, debugger = chosen

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
    if controller:
        _propose(carried, already, kept, key="target.controller", section="target", field="controller", current=_mapping(document, "target").get("controller"), value=controller)

    matched_port = discovery.get("com_port")
    device = str(matched_port["device"]) if isinstance(matched_port, dict) and matched_port.get("device") else None
    port_name, port_entry = _choose_com_port(document, com_port_id, device)
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
        _propose(
            carried,
            already,
            kept,
            key=f"com_ports.{port_name}.device",
            section="com_ports",
            field="device",
            current=(port_entry or {}).get("device"),
            value=device,
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
    open_holds: JsonObject | None = None,
    actor: str = ACTOR_AGENT,
    via: str = f"mcp:{PROJECT_CONFIG_ADOPT}",
) -> JsonObject:
    """Read what is attached and carry its identity into the configuration."""
    try:
        return _adopt(workspace, existing, arguments or {}, open_holds=open_holds, actor=actor, via=via)
    except ConfigError as error:
        return {"tool": PROJECT_CONFIG_ADOPT, **error.to_dict(), **NOT_STARTED}


def _adopt(workspace: Path, existing: AgenticHILConfig | None, arguments: JsonObject, *, open_holds: JsonObject | None, actor: str, via: str) -> JsonObject:
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

    apply = bool(arguments.get("apply", False))
    discovery = discover_attached_hardware(probe_id=_optional_string(arguments.get("probe_id")))
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

    plan = plan_adoption(document, discovery, debugger_id=_optional_string(arguments.get("debugger_id")), com_port_id=_optional_string(arguments.get("com_port_id")))
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
    "is_unset",
    "plan_adoption",
    "project_config_adopt_hardware",
    "skeleton_placeholder",
]
