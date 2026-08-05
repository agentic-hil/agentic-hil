"""Taking a changed device description without restarting the server.

A server parses its configuration once and enforces that one document until it
exits. That rule was written for the **permission** half, and it is right there:
grants were taken as a whole, and a document that appeared underneath a running
server may not widen them. It also hit the **description** half — probe serials,
COM devices, baudrates, ``target.*`` — for which it was never meant, and that is
what an operator hit every time a board was plugged in after the file was
written: the file said the right thing, the server said the old thing, and the
only way across was a restart of the agent's MCP server (hardci-hq#98).

This module is the description-only reload, and every restriction the original
decision put on a reload still holds:

* It happens only when it is asked for by name — ``project_config_reload_description``
  over MCP, ``agentic-hil config-reload`` at a shell. Never automatically, never
  as a side effect of noticing that the file moved.
* It is refused while this server holds hardware, and while an incident on this
  bench is unresolved. Both for the same reason a configuration write is: those
  holds were taken against the description this server was answering out of, and
  moving it underneath them repoints the names an operator is about to be asked
  to physically check.
* **The permissions are not re-read at all.** Not narrowed, not compared,
  not adopted — the permission state after a reload is the byte-for-byte one
  parsed at startup. A device the loaded policy has never heard of arrives with
  the dataclass defaults, which are all ``False``, so a board that appears on
  disk can be probed and read (exclusivity, not a grant, is what protects a read
  from version 2 on) and cannot be flashed, reset, mass-erased or written to
  until an operator restarts the server onto the file that grants it.
* It refuses when the file is missing, unreadable, or will not load, in the
  vocabulary ``config_status`` already uses for those three states.
* It builds a new configuration object and hands that to the service's
  collaborators at one point. Nothing field-pokes a live one.
* The object it hands over is validated *as composed*, not only as loaded. A
  check that a load skipped because the file's own grants reached no hardware
  applies again once this server's wider grants sit behind that entry, and a
  composition that cannot pass it is refused rather than enforced.

## Which half is which

The reload re-reads four sections and nothing else:

``target``, ``debuggers``, ``com_ports``, ``can_buses`` — what hardware this
bench *is*. Within an entry the ``permissions:`` block is skipped; everything
else in it comes from disk.

Everything outside those four stays exactly as it was loaded, and the reason is
the rule about a field that is both: ``debug``, ``artifacts``, ``validation`` and
``recovery`` each mix description with authority in one section —
``debug.allow_all_symbols`` and ``artifacts.allow_upload`` are grants sitting
directly on a section, ``artifacts.allowed_roots`` and ``debug.allowed_symbols``
are allowlists that decide what may be flashed and what may be read out of a
target, ``validation.*`` decides which files are accepted at all, and
``recovery.auto_recover`` decides whether this machine may drive a physical reset
on its own. None of those is a device description, and guessing any of them into
the description half would make this a general reload. They are named here rather
than silently omitted; a change to one of them still needs a restart.

Two more that are refused by name, for the same reason:

``version``
    the marker that decides which permission model the whole file is read under.
    Turning it from 1 to 2 makes every read on this bench free. That is a
    permission change wearing a description key's clothes, and it is not adopted.
``workspace_root`` / ``state_root``
    what this server is bound to and where its trusted state lives. A reload that
    could move either would be able to rebind a running server to another project.

## What a reload leaves visible

Afterwards the description in force *is* the file's, so ``config_status`` reports
that file as unchanged — a server that had just taken the disk's description and
still called it stale would be lying about its own state. What does not
disappear is the other half: if the file's permission blocks differ from the ones
this server is enforcing, ``config_status`` carries ``permissions_source``, which
says that the permissions in force came from the document parsed at startup and
that a restart is what adopts the file's. Not hidden, and not adopted.
"""

from __future__ import annotations

import os
from dataclasses import asdict, replace
from typing import Any

from agentic_hil.config import (
    ConfigError,
    load_authoritative_config,
    permission_summary,
    revalidate_permission_dependent_pinning,
    utc_now,
)
from agentic_hil.configstate import config_status
from agentic_hil.knowledge import CONFIG_SHAPE_URI, remediation_fields
from agentic_hil.types import (
    AgenticHILConfig,
    DebuggerPermissions,
    IoPermissions,
    JsonObject,
)

PROJECT_CONFIG_RELOAD = "project_config_reload_description"

# The refusal this tool adds. Its own type rather than `config_write_in_open_run`
# because nothing was written and saying so matters, and the same rule, the same
# remedy and the same family: what is held was taken under the description this
# server is answering out of, and that description may not move underneath it.
RELOAD_IN_OPEN_RUN_ERROR = "config_reload_in_open_run"

# The sections a reload takes from disk, minus each entry's `permissions:` block.
RELOADED_SECTIONS = ("target", "debuggers", "com_ports", "can_buses")
# The sections it does not, with why. Reported in the result so a caller that
# changed one of them is told which restart it is waiting for instead of being
# left to conclude the reload silently ignored it.
NOT_RELOADED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("permissions", "the project grants. Never re-read; a restart adopts them."),
    ("version", "which permission model this file is read under. A permission change, not a description one."),
    ("workspace_root", "what this server is bound to. A reload may not rebind a running server to another project."),
    ("state_root", "where this server's trusted state lives. Moving it under a running server would orphan its leases."),
    ("debug", "mixes description with authority: `allow_all_symbols` is a grant and `allowed_symbols` is an allowlist."),
    ("artifacts", "mixes description with authority: `allow_upload` is a grant and `allowed_roots` decides what may be flashed."),
    ("validation", "decides which artifacts are accepted at all."),
    ("recovery", "decides whether this machine may drive a physical reset on its own."),
    ("reports", "an operator-owned path; a running server's audit trail may not move underneath it."),
    ("logs", "an operator-owned path; a running server's audit trail may not move underneath it."),
    ("<entry>.permissions", "every device entry's own grants. Never re-read; a restart adopts them."),
)

NOT_STARTED: JsonObject = {"side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged"}


# ---------------------------------------------------------------------------
# Reading the two halves of a configuration apart.


def description_view(config: AgenticHILConfig) -> JsonObject:
    """The four device sections of a configuration, with every grant removed.

    Built off the parsed dataclasses rather than off a YAML document, so it says
    what the configuration actually *is* after defaults, pinning and validation
    rather than what somebody happened to write down. The comparison this feeds
    is therefore between two loaded policies, which is the comparison that
    decides what the reload changes.
    """
    return {
        "target": asdict(config.target),
        **{
            section: {
                name: {field: value for field, value in asdict(entry).items() if field != "permissions"}
                for name, entry in getattr(config, section).items()
            }
            for section in ("debuggers", "com_ports", "can_buses")
        },
    }


def permission_view(config: AgenticHILConfig) -> JsonObject:
    """Every grant a configuration holds, plus the model it is read under.

    ``permission_summary`` is already the complete list of grants; ``version``
    is added because it decides whether reading needs one at all, and a file that
    moved from 1 to 2 has widened this bench whatever its ``allow_*`` keys say.
    """
    return {"version": config.config_version, **permission_summary(config)}


def _flatten(node: object, prefix: str = "") -> dict[str, Any]:
    if isinstance(node, dict):
        flat: dict[str, Any] = {}
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(value, path))
        return flat
    return {prefix: node}


def moved_paths(before: JsonObject, after: JsonObject) -> list[str]:
    """Which dotted paths two views disagree about, in one sorted list.

    A path present on one side only counts as a disagreement, which is what makes
    an entry that appeared on disk — or one that was deleted from it — show up as
    the change it is rather than as nothing.
    """
    left, right = _flatten(before), _flatten(after)
    missing = object()
    return sorted(path for path in set(left) | set(right) if left.get(path, missing) != right.get(path, missing))


def description_changes(loaded: AgenticHILConfig, disk: AgenticHILConfig) -> list[str]:
    return moved_paths(description_view(loaded), description_view(disk))


def permission_differences(loaded: AgenticHILConfig, disk: AgenticHILConfig) -> list[str]:
    """Which grants the file holds that this server is not enforcing, either way.

    Reported and never acted on. A device that is only on disk shows up here with
    every grant the file gives it, which is exactly the thing an operator has to
    be told: the board is usable for reads now, and the flash permission the file
    grants it is waiting for a restart.
    """
    return moved_paths(permission_view(loaded), permission_view(disk))


# ---------------------------------------------------------------------------
# Building the reloaded configuration.


def _carried_permissions(loaded_entries: dict[str, Any], name: str, empty: Any) -> Any:
    """The grants this server holds for an entry, or none at all.

    ``empty`` is the all-``False`` dataclass, and it is what an entry the loaded
    policy has never seen gets. Absent is denied: there is no grant here to carry
    over, so none is invented, and every permission check downstream reads these
    same objects — so a board that appeared on disk is denied by the same gate
    that denies a board an operator switched off.
    """
    entry = loaded_entries.get(name)
    return empty if entry is None else entry.permissions


def merged_description(loaded: AgenticHILConfig, disk: AgenticHILConfig) -> AgenticHILConfig:
    """A new configuration: the file's description, this server's permissions.

    Built with ``dataclasses.replace`` off the loaded policy, so every section
    this reload does not touch is carried over by construction rather than by
    being copied field by field — a section added to the configuration later is
    then *not* reloaded by default, which is the safe direction for a decision
    that has to be made deliberately per section.

    Raises ``ConfigError`` when the composition does not pass the validations
    that depend on which permissions apply. The disk document was validated
    under the grants *it* carries, and this object pairs its entries with the
    grants parsed at startup — a pairing no load has seen. Under version 1 the
    difference is not cosmetic: an entry whose grants on disk are all false
    reaches no hardware, so pinning skipped its OpenOCD script check, and the
    startup grants put a real probe behind exactly those unchecked scripts. The
    check is re-asked here, on the object that would be enforced.
    """
    debuggers = {
        name: replace(entry, permissions=_carried_permissions(loaded.debuggers, name, DebuggerPermissions()))
        for name, entry in disk.debuggers.items()
    }
    com_ports = {
        name: replace(entry, permissions=_carried_permissions(loaded.com_ports, name, IoPermissions()))
        for name, entry in disk.com_ports.items()
    }
    can_buses = {
        name: replace(entry, permissions=_carried_permissions(loaded.can_buses, name, IoPermissions()))
        for name, entry in disk.can_buses.items()
    }
    # The binding follows the name this server is already driving, not the file's
    # ordering: a second board appearing in the file must not repoint a server
    # that is bound to the first. It moves only when the name it held is no
    # longer there, and then only where there is exactly one board it could mean
    # — which is also how a bench that configured no debugger at all picks up the
    # first one somebody plugs in.
    debugger_id = loaded.debugger_id if loaded.debugger_id in debuggers else (next(iter(debuggers)) if len(debuggers) == 1 else None)
    debugger = debuggers.get(debugger_id) if debugger_id is not None else None
    target = debugger.target if debugger is not None and debugger.target is not None else disk.target
    merged = replace(
        loaded,
        target=target,
        debuggers=debuggers,
        com_ports=com_ports,
        can_buses=can_buses,
        debugger_id=debugger_id,
        debugger=debugger,
        # The description in force now came from these bytes, so this is what
        # `config_status` compares the file against from here on.
        config_digest=disk.config_digest,
        # The permissions did not move. Whether that is visible depends on
        # whether the file's grants differ from the ones being enforced: when
        # they are the same document's, there is nothing to report and both
        # digests name the file; when they differ, this stays on the document
        # that was parsed at startup and `config_status` says so.
        permissions_digest=disk.config_digest if not permission_differences(loaded, disk) else (loaded.permissions_digest or loaded.config_digest),
        description_reloaded_at=utc_now(),
    )
    return revalidate_permission_dependent_pinning(merged)


# ---------------------------------------------------------------------------
# The tool.


def reload_description(
    existing: AgenticHILConfig | None,
    *,
    open_holds: JsonObject | None = None,
    quarantine: JsonObject | None = None,
) -> tuple[AgenticHILConfig | None, JsonObject]:
    """Re-read this bench's description, or say why nothing was re-read.

    Returns the configuration to swap in and the answer, or ``None`` and a
    refusal. Splitting it that way keeps the decision — what may be adopted —
    out of the service, which owns the other half: when the swap happens and what
    has to be told about it.
    """
    if existing is None:  # pragma: no cover - the unprovisioned service answers first
        return None, {
            "ok": False,
            "tool": PROJECT_CONFIG_RELOAD,
            "error_type": "config_file_not_found",
            "summary": "This server has no configuration loaded, so there is no description to re-read.",
            **remediation_fields("config_file_not_found"),
            **NOT_STARTED,
        }
    if open_holds:
        return None, _open_run_refusal(existing, open_holds)
    if quarantine:
        return None, _quarantine_refusal(existing, quarantine)
    try:
        # The same load startup performs, deliberately: the authoritative-path
        # rules, the single-link check, the executable and script pinning and the
        # probe-ownership validation all belong to it, and a reload that took a
        # cheaper path would be a second, weaker door onto the same file.
        disk = load_authoritative_config(existing.workspace_root)
    except (ConfigError, OSError) as error:
        return None, _unloadable_refusal(existing, error)
    if os.path.normcase(disk.config_path) != os.path.normcase(existing.config_path):
        # `load_authoritative_config` resolves the location afresh, including
        # `AGENTIC_HIL_CONFIG`. If that now names another file, the description on
        # disk is a different project's and adopting it would rebind this server.
        # Compared the way `configwrite.authoritative_write_target` compares the
        # same two paths, so one surface cannot call two files the same and the
        # other call them different.
        return None, _relocated_refusal(existing, disk)

    try:
        # The composition, not the file, is what gets enforced, and the checks
        # that depend on which permissions apply have to be met by it. The disk
        # document passing on its own is not the same statement.
        reloaded = merged_description(existing, disk)
    except ConfigError as error:
        return None, _uncomposable_refusal(existing, error)
    changes = description_changes(existing, disk)
    differences = permission_differences(existing, disk)
    # Taken against the reloaded configuration, so it reports the description
    # that is now in force. Its own read, deliberately: if the file moved again
    # between the load above and this line, that is a fact about the file and the
    # caller is told it rather than being handed a status built from bytes that
    # are already history.
    status = config_status(reloaded)
    return reloaded, {
        "ok": True,
        "tool": PROJECT_CONFIG_RELOAD,
        "reloaded": True,
        "description_changed": bool(changes),
        "summary": _summary(changes, differences),
        "config_status": status,
        "path": reloaded.config_path,
        "workspace_root": reloaded.workspace_root,
        "description_changes": changes,
        "reloaded_sections": list(RELOADED_SECTIONS),
        "not_reloaded_sections": [{"section": section, "why": why} for section, why in NOT_RELOADED_SECTIONS],
        # Always true, and stated rather than implied. This is the one property
        # the whole tool rests on, and a caller reading the answer should not have
        # to infer it from the absence of a permission field.
        "permissions_reloaded": False,
        "permissions_in_force": permission_summary(reloaded),
        "permission_differences": differences,
        "restart_required_for": differences,
        "reference": CONFIG_SHAPE_URI,
        "next_steps": _next_steps(changes, differences),
        **NOT_STARTED,
        "cleanup_required": False,
    }


def _summary(changes: list[str], differences: list[str]) -> str:
    head = (
        f"The description on disk was re-read and is now the one in force; {len(changes)} description value(s) moved."
        if changes
        else "The description on disk was re-read and matched the one already in force; nothing moved."
    )
    if not differences:
        return f"{head} The permissions in force are unchanged, as they always are here, and are the ones this file states."
    return (
        f"{head} The permissions in force are unchanged, as they always are here: they are the ones this server parsed "
        f"at startup, and {len(differences)} permission value(s) in this file differ from them. Restarting the MCP "
        "server is what adopts those; nothing here does."
    )


def _next_steps(changes: list[str], differences: list[str]) -> list[str]:
    steps = [
        "The devices in this answer are the ones on disk. A device this server had never seen carries no grant at all: "
        "it can be probed and read, and flashing, reset, mass erase and COM/CAN writes are denied on it until the "
        "server is restarted onto the file that grants them.",
    ]
    if differences:
        steps.append(
            "`permission_differences` lists every grant this file states that this server is not enforcing. Report "
            "them and ask the operator to restart the MCP server if any of them is one this bench needs; do not call "
            "this tool again for them, it does not adopt permissions in either direction."
        )
    if not changes:
        steps.append("Nothing in the description moved, so no answer from this server changes. The file and the loaded description already agreed.")
    steps.append(f"{CONFIG_SHAPE_URI} says which sections this reload covers and which ones still need a restart.")
    return steps


# ---------------------------------------------------------------------------
# Refusals.


def _open_run_refusal(existing: AgenticHILConfig, open_holds: JsonObject) -> JsonObject:
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_RELOAD,
        "error_type": RELOAD_IN_OPEN_RUN_ERROR,
        "summary": (
            "This server is holding hardware right now, and those holds name the devices this configuration describes. "
            "Re-reading the description underneath them could repoint a name at another board mid-run, so nothing was "
            "re-read and the run is untouched."
        ),
        "open_holds": open_holds,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        **remediation_fields(RELOAD_IN_OPEN_RUN_ERROR),
        **NOT_STARTED,
        "retry_safe": True,
    }


def _quarantine_refusal(existing: AgenticHILConfig, quarantine: JsonObject) -> JsonObject:
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_RELOAD,
        "error_type": "resource_quarantined",
        "summary": (
            "This bench has an unresolved incident, and the operator is going to be asked to physically check the "
            "devices it names. Re-reading the description could change which board a name means before that check "
            "happens, so nothing was re-read."
        ),
        **quarantine,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        "next_steps": [
            "Resolve the incident first: `hardware_lease_status` reports it, and it carries `quarantine_guidance` — "
            "what was attempted, what is confirmed, what is unknown, and the physical check the operator performs "
            "before `agentic-hil recover --confirm-safe-state --quarantine-id <id>`.",
            "Repeat this call after the recovery. Nothing here was done, so there is nothing to undo and nothing that "
            "makes the second attempt different from this one.",
        ],
        **NOT_STARTED,
        # This call provably reached no hardware — it reads a file — so repeating
        # it once the incident is cleared is safe. `quarantined` above is a fact
        # about the bench, not a claim that this call put it there.
        "retry_safe": True,
    }


def _unloadable_refusal(existing: AgenticHILConfig, error: Exception) -> JsonObject:
    """The file is gone, will not open, or will not load — said in its own terms.

    The vocabulary is the one ``config_status`` already uses for those three
    states, and the block is carried so the state and the refusal cannot
    disagree. The policy in force is reported too: a file that has just become
    unreadable does not change what this server is enforcing, and after a refusal
    that is the half a caller cannot get any other way.
    """
    failure = error if isinstance(error, ConfigError) else ConfigError("config_unreadable", "The authoritative configuration could not be read.", {"path": existing.config_path, "backend_error": str(error)})
    # `to_dict` already attaches the catalogue's fix, scoped by the config field
    # the error names. Adding an unscoped lookup on top of it here would replace
    # the specific advice with the general one, on exactly the errors that have
    # specific advice.
    return {
        "tool": PROJECT_CONFIG_RELOAD,
        **failure.to_dict(),
        "summary": (
            f"{failure.summary} Nothing was re-read: this server is still enforcing the configuration it loaded at "
            "startup, reported below as `permissions_in_force`."
        ),
        "config_status": config_status(existing),
        "permissions_in_force": permission_summary(existing),
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        "next_steps": [
            "Report `backend_error` and `summary`: they are the loader's own message and name what has to be repaired.",
            "`agentic-hil doctor` reads the file fresh and produces the same refusal without stopping anything, so an "
            "operator can repair it and confirm the repair before this call is made again.",
            "Nothing changed here. This server is enforcing exactly what it was a moment ago, and repeating the call "
            "after the file is repaired is the whole of the recovery.",
        ],
        **NOT_STARTED,
        "retry_safe": True,
        "cleanup_required": False,
    }


def _uncomposable_refusal(existing: AgenticHILConfig, error: ConfigError) -> JsonObject:
    """The file loads, and does not pass under the permissions in force.

    Its own refusal rather than the unloadable one, because the file is not the
    problem and telling an operator to repair it would send them at a document
    that is already valid on its own terms. What failed is the pairing: a check
    the file was exempt from under its own grants applies again once this
    server's wider grants are put behind its entries — an OpenOCD entry whose
    scripts were never validated because nothing in that file could drive it.
    Adopting it would be the split this reload exists to avoid, so nothing was
    re-read and the policy in force is untouched.
    """
    return {
        "tool": PROJECT_CONFIG_RELOAD,
        **error.to_dict(),
        "summary": (
            f"{error.summary} The file on disk loads on its own, but the description in it does not pass this "
            "validation under the permissions this server enforces, which are wider than the ones the file states. "
            "Nothing was re-read: this server is still enforcing the configuration it loaded at startup."
        ),
        "config_status": config_status(existing),
        "permissions_in_force": permission_summary(existing),
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        "next_steps": [
            "Report `field` and `summary`: they name the exact key in the file that has to be repaired, and the "
            "repair is the same one a restart onto this file would demand.",
            "The alternative is a restart of the MCP server. That adopts the file's permissions along with its "
            "description, and the pair is then the one document's again.",
            "Nothing changed here. This server is enforcing exactly what it was a moment ago, and repeating the call "
            "after the file is repaired is the whole of the recovery.",
        ],
        **NOT_STARTED,
        "retry_safe": True,
        "cleanup_required": False,
    }


def _relocated_refusal(existing: AgenticHILConfig, disk: AgenticHILConfig) -> JsonObject:
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_RELOAD,
        "error_type": "config_invalid",
        "summary": (
            "This workspace's authoritative configuration is no longer the file this server loaded, so the description "
            "on disk belongs to a different document than the policy in force. Nothing was re-read: a reload binds a "
            "running server to devices, and it may not bind it to another file's."
        ),
        "path": existing.config_path,
        "authoritative_path": disk.config_path,
        "workspace_root": existing.workspace_root,
        "next_steps": [
            "Report both paths. `path` is what this server is enforcing; `authoritative_path` is what this workspace "
            "now resolves to, and only an operator can say which of the two is meant.",
            f"If the new location is the right one, the way onto it is a restart of the MCP server, not this call. "
            f"{CONFIG_SHAPE_URI} has the location rules.",
        ],
        **NOT_STARTED,
        "retry_safe": False,
    }


__all__ = [
    "NOT_RELOADED_SECTIONS",
    "PROJECT_CONFIG_RELOAD",
    "RELOADED_SECTIONS",
    "RELOAD_IN_OPEN_RUN_ERROR",
    "description_changes",
    "description_view",
    "merged_description",
    "permission_differences",
    "permission_view",
    "reload_description",
]
