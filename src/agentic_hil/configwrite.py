"""Field-wise changes to the authoritative configuration, gated by grants in it.

The one-time provisioning path (#71) let an agent generate a configuration it
could not find, and the file it generated refused every further write. That kept
the invariant — an agent enables itself up to observation, never up to
modification — and it left every later correction as hand work on YAML. In the
session that produced this module, that ended with an agent printing a
24-character probe serial for a human to retype.

So there is a door now, and three things make it one that can be left open:

**It is field-wise.** The agent does not author this file. It names keys from a
closed set and supplies scalars, each checked against the shipped JSON schema.
There is no way to send a document, and — because ``value`` is a scalar — no way
to send a subtree that could carry a ``permissions:`` block inside it.

**It is two grants, not one.** ``allow_config_description_write`` reaches what
the bench *is*; ``allow_config_permissions_write`` reaches what it *may do*.
Somebody who opens the first so a probe serial can be entered has not, in the
same motion and without being told, handed over ``allow_flash``.

**The split is enforced twice.** Once by the key model, which never maps a
``permissions`` key onto the description grant. And once by comparing the
permissions actually present in the document before and against after — a check
that reads the document rather than the request, so it holds even where the
first one would be wrong. That second check is the one that closes a detour: not
because a detour is known to exist, but because a boundary defended only by a
path parser is a boundary defended only as well as the parser is right.
"""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from agentic_hil.config import (
    CONFIG_ENV,
    GENERATED_WRITE_PERMISSIONS,
    ConfigError,
    UniqueKeyLoader,
    authoritative_config_target,
    config_schema,
    load_authoritative_config,
    secure_atomic_write_text,
    secure_optional_read_text,
    secure_user_file_lock,
    write_generated_config,
)
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_KEY_RULES,
    CONFIG_NAMED_SECTIONS,
    CONFIG_PERMISSIONS_RIGHT,
    CONFIG_RIGHTS,
    CONFIG_SHAPE_URI,
    CONFIG_WRITE_RIGHT,
    ResolvedConfigKey,
    config_key_schema,
    config_rule_fields,
    remediation_fields,
    resolve_config_key,
)
from agentic_hil.types import AgenticHILConfig, JsonObject

PROJECT_CONFIG_SET = "project_config_set"
PROJECT_CONFIG_DESCRIBE = "project_config_describe"

# Who is changing the file, in `provenance`'s own two words. Not a decoration:
# the description grant exists to gate *the agent*, and a person at the CLI is
# the one that grant belongs to — `agentic-hil init` consults no permission
# either, and `init --force` rewrites the whole file including every grant in it.
# So the actor decides whether the description grant is consulted, and it is
# recorded so the file says which of the two moved it.
#
# The permissions half is waived for nobody. The before/after comparison further
# down reads the document rather than the request and refuses a permission that
# moved without `allow_config_permissions_write`, whoever asked.
ACTOR_AGENT = "agent"
ACTOR_HUMAN = "human"
ACTOR_PHRASES = {ACTOR_AGENT: "an agent", ACTOR_HUMAN: "a person"}

# One comment line, rewritten rather than appended, so a file changed a hundred
# times does not grow a hundred lines of banner. It sits in the header because
# the header is what a person reads first, and a generated header states that
# every permission below is false — true when it was written, and something a
# later reader must not go on believing without being told the file has moved.
CHANGE_MARKER_PREFIX = "# Changed by "
NOT_STARTED: JsonObject = {"side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged"}


# ---------------------------------------------------------------------------
# Reading what is actually in a document.


def permission_surface(node: object, prefix: str = "") -> dict[str, Any]:
    """Every value in a document that decides what may be done, by dotted path.

    Deliberately not driven by the key model. This walks whatever the document
    actually contains and collects two things at any depth: a key named
    ``allow_*``, and every leaf inside a mapping named ``permissions``. A guard
    that understood only the paths the key model produces would be blind exactly
    where something that got around the key model would land.
    """
    found: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).startswith("allow_"):
                found[path] = value
            elif str(key) == "permissions" and isinstance(value, dict):
                for flag, granted in value.items():
                    found[f"{path}.{flag}"] = granted
            found.update(permission_surface(value, path))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.update(permission_surface(value, f"{prefix}[{index}]"))
    return found


def permission_delta(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which permission paths a change moved.

    A path missing on one side counts as ``False``, so an entry created with an
    explicit deny-all block is not read as a change, while a permission that
    appeared as ``true`` is."""
    return sorted(path for path in set(before) | set(after) if before.get(path, False) != after.get(path, False))


def description_view(node: object) -> object:
    """A document with everything that grants anything removed.

    The mirror of ``permission_surface``: what is left is the description, and
    comparing two of these says whether a change touched the bench's description
    regardless of which key was named to do it."""
    if isinstance(node, dict):
        return {
            key: description_view(value)
            for key, value in node.items()
            if key != "permissions" and not str(key).startswith("allow_") and key != "provenance"
        }
    if isinstance(node, list):
        return [description_view(item) for item in node]
    return node


def deny_all_permissions(section: str) -> JsonObject:
    """The permissions block a newly named entry is created with.

    Written by the server, never taken from the request: a device an agent adds
    must not be able to arrive already allowed to be flashed."""
    return dict.fromkeys(GENERATED_WRITE_PERMISSIONS.get(section, ()), False)


# ---------------------------------------------------------------------------
# The document on disk.


def authoritative_write_target(workspace: Path, existing: AgenticHILConfig) -> Path:
    """The file this call may touch, or a refusal.

    Two rules have to agree: this workspace's authoritative location, and the
    file this server actually loaded its policy from. If they differ, the server
    is serving a configuration that is not the one in force here — and writing
    either of them would be wrong. Changing the authoritative file would move a
    policy this server is not running under; changing the loaded one would move a
    file nothing reads. "Validate before replacing" is worth nothing if the thing
    replaced is the wrong file, so this refuses instead of choosing."""
    target = authoritative_config_target(workspace)
    if os.path.normcase(str(target)) != os.path.normcase(str(Path(existing.config_path))):
        raise ConfigError(
            "config_invalid",
            "This server loaded its configuration from a file that is not this workspace's authoritative one, so a "
            "change here would move a file that is not in force. Nothing was written.",
            {"path": existing.config_path, "authoritative_path": str(target), "workspace_root": str(workspace), "environment_variable": CONFIG_ENV},
        )
    return target


def load_config_document(target_path: Path) -> tuple[str, JsonObject]:
    text = secure_optional_read_text(target_path)
    if text is None:
        raise ConfigError("config_file_not_found", "This workspace has no Agentic HIL configuration to change.", {"path": str(target_path)})
    document = yaml.load(text, Loader=UniqueKeyLoader)
    if not isinstance(document, dict):
        raise ConfigError("config_invalid", "The authoritative configuration is not a mapping.", {"path": str(target_path)})
    return text, document


def _header(text: str) -> str:
    """The leading comment block of a file, kept across a rewrite.

    Comments below the first setting are not preserved: this path re-serializes
    the document, and the YAML parser this project uses does not round-trip
    comments. The header is what carries the statement about the file as a
    whole, so it is the part that must survive."""
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if not stripped.startswith(CHANGE_MARKER_PREFIX):
                kept.append(line)
            continue
        break
    return "".join(kept)


def _marked_header(text: str, timestamp: str, actor: str, via: str) -> str:
    """The header, with exactly one line saying the file has been changed since.

    One line, and it replaces the previous one rather than following it — a file
    changed a hundred times must not grow a hundred banners. It matters because a
    generated header states that every permission below is false, which was true
    when it was written and is the kind of thing a later reader keeps believing
    unless something in front of them says the file has moved. It names the actor
    because "an agent changed this" and "you changed this" are different things to
    read at the top of a policy file."""
    header = _header(text)
    if header and not header.endswith("\n"):  # pragma: no cover - splitlines(keepends) preserves the newline
        header += "\n"
    phrase = ACTOR_PHRASES.get(actor, actor)
    return f"{header}{CHANGE_MARKER_PREFIX}{phrase} through {via} at {timestamp}; `provenance` below records what moved.\n"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Grants.


def granted_rights(existing: AgenticHILConfig, document: JsonObject) -> dict[str, bool]:
    """Which configuration grants hold right now.

    Both sides have to agree. ``existing`` is the policy this server loaded and
    is bound by — the same rule the provisioning path follows, so a file removed
    or widened underneath a running server cannot turn its own refusal into a
    fresh start. The document on disk is checked too, so a grant an operator
    revoked since startup takes effect on the next call rather than at the next
    restart. The narrower of the two wins.
    """
    on_disk = document.get("permissions")
    on_disk = on_disk if isinstance(on_disk, dict) else {}
    loaded = existing.permissions
    return {
        name: bool(getattr(loaded, name, False)) and on_disk.get(name, False) is True
        for name in (CONFIG_WRITE_RIGHT, CONFIG_DESCRIPTION_RIGHT, CONFIG_PERMISSIONS_RIGHT)
    }


def _right_key(right: str) -> str:
    return f"permissions.{right}"


def _permission_denied(tool: str, right: str, keys: list[str], path: Path) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": "permission_denied",
        "summary": (
            f"Changing {'permissions in' if right == CONFIG_PERMISSIONS_RIGHT else 'the description of'} this project's "
            f"configuration is denied by permissions.{right} in the configuration itself. This server may read it and "
            "not change it there, and only a person editing that file can change that."
        ),
        "permission": right,
        "permission_key": _right_key(right),
        "reason": "config_write_denied",
        "denied_keys": sorted(keys),
        "path": str(path),
        "reference": CONFIG_SHAPE_URI,
        "next_step": (
            "This refusal is the answer to the request. Report it, name the permission that is denied and the file "
            f"that carries it, then stop. You must not enable it: {_right_key(right)} belongs to the operator and "
            "only the operator may edit it. You must not reach the same change another way either."
        ),
        **remediation_fields("permission_denied", right),
        **NOT_STARTED,
        "retry_safe": False,
    }


# ---------------------------------------------------------------------------
# project_config_set.


def project_config_set(
    workspace: Path,
    existing: AgenticHILConfig | None,
    changes: object,
    *,
    open_holds: JsonObject | None = None,
    actor: str = ACTOR_AGENT,
    via: str = f"mcp:{PROJECT_CONFIG_SET}",
) -> JsonObject:
    """Set named configuration keys, or refuse and change nothing.

    ``actor`` and ``via`` say who is asking and through what, and both end up in
    ``provenance``. ``ACTOR_HUMAN`` is a person at the CLI, which is the
    authority ``agentic-hil init`` already runs under; it waives the description
    grant and nothing else."""
    try:
        return _project_config_set(workspace, existing, changes, open_holds=open_holds, actor=actor, via=via)
    except ConfigError as error:
        return {"tool": PROJECT_CONFIG_SET, **error.to_dict(), **NOT_STARTED}


def _invalid(field: str, summary: str, **extra: object) -> JsonObject:
    return {"ok": False, "tool": PROJECT_CONFIG_SET, "error_type": "invalid_argument", "field": field, "summary": summary, "reference": CONFIG_SHAPE_URI, **extra, **NOT_STARTED}


def _project_config_set(workspace: Path, existing: AgenticHILConfig | None, changes: object, *, open_holds: JsonObject | None, actor: str, via: str) -> JsonObject:
    if existing is None:  # pragma: no cover - the unprovisioned service answers first
        raise ConfigError("config_file_not_found", "This workspace has no Agentic HIL configuration to change.", {"workspace_root": str(workspace)})
    if open_holds:
        return _open_run_refusal(existing, open_holds)
    requested = _parse_changes(changes)
    if isinstance(requested, dict):
        return requested

    target_path = authoritative_write_target(workspace, existing)
    with secure_user_file_lock(target_path):
        previous_text, document = load_config_document(target_path)
        rights = granted_rights(existing, document)
        # An operator at the CLI is the person the description grant belongs to,
        # so it is not consulted for them; the permissions grant is consulted for
        # everybody, and layer 3 below enforces that from the document itself.
        rights = {**rights, CONFIG_DESCRIPTION_RIGHT: rights[CONFIG_DESCRIPTION_RIGHT] or actor == ACTOR_HUMAN}

        # 1. The key model. A permissions key never resolves to the description
        #    grant, so this alone already separates the two halves.
        needed: dict[str, list[str]] = {}
        for resolved, _ in requested:
            needed.setdefault(resolved.right, []).append(resolved.key)
        for right in (CONFIG_DESCRIPTION_RIGHT, CONFIG_PERMISSIONS_RIGHT):
            if needed.get(right) and not rights[right]:
                return _permission_denied(PROJECT_CONFIG_SET, right, needed[right], target_path)

        before_permissions = permission_surface(document)
        before_description = description_view(document)
        updated = deepcopy(document)
        applied: list[JsonObject] = []
        created_entries: list[str] = []
        for resolved, value in requested:
            refusal, previous = _apply_change(updated, resolved, value, created_entries)
            if refusal is not None:
                return refusal
            applied.append({"key": resolved.key, "previous_value": previous, "value": value, "right": resolved.right})

        _deny_new_entries(updated, created_entries)
        missing = _missing_required_fields(updated, created_entries)
        if missing:
            return _invalid(
                missing[0],
                "A newly named entry is missing a field the schema requires. Send it in the same call; nothing was written.",
                missing_keys=missing,
                created_entries=sorted(created_entries),
            )

        # 2. The document, not the request. Whatever the keys were called, this
        #    compares what the file grants before against what it grants after,
        #    and what it describes before against after. A change that moved a
        #    permission without the permissions grant fails here even if it
        #    reached the document some way the key model did not anticipate.
        moved = permission_delta(before_permissions, permission_surface(updated))
        if moved and not rights[CONFIG_PERMISSIONS_RIGHT]:
            return _permission_denied(PROJECT_CONFIG_SET, CONFIG_PERMISSIONS_RIGHT, moved, target_path)
        if description_view(updated) != before_description and not rights[CONFIG_DESCRIPTION_RIGHT]:
            return _permission_denied(PROJECT_CONFIG_SET, CONFIG_DESCRIPTION_RIGHT, [item["key"] for item in applied], target_path)

        timestamp = _utc_now()
        _record_provenance(updated, [str(item["key"]) for item in applied], timestamp, actor, via)
        text = _marked_header(previous_text, timestamp, actor, via) + yaml.safe_dump(updated, sort_keys=False, allow_unicode=False)

        # 3. Validate, then replace. write_generated_config loads the new text
        #    from a temporary file first, so a change that would not load never
        #    reaches the authoritative path at all.
        write_generated_config(target_path, workspace, text)
        try:
            written = load_authoritative_config(workspace)
        except ConfigError as error:
            return _rolled_back(target_path, previous_text, error)

    return {
        "ok": True,
        "tool": PROJECT_CONFIG_SET,
        "summary": f"{len(applied)} configuration key(s) were changed; the file was validated before it replaced the previous one.",
        "path": written.config_path,
        "workspace_root": written.workspace_root,
        "changes": applied,
        "created_entries": sorted(created_entries),
        "permissions_changed": moved,
        "provenance": dict(updated.get("provenance") or {}),
        # The same rule the regeneration path follows: this server keeps serving
        # the configuration it loaded, because swapping a policy under a live
        # session is not something a tool call may do behind its back.
        "reload_required": True,
        "next_steps": [
            "Report which keys changed and where the file is.",
            "This server is still serving the configuration it loaded at startup. Ask the operator to restart the MCP "
            "server before relying on anything this change decides.",
            f"`project_config_describe` says what else this configuration leaves open; {CONFIG_SHAPE_URI} explains the shape.",
        ],
        **NOT_STARTED,
        "cleanup_required": False,
    }


def _parse_changes(changes: object) -> list[tuple[ResolvedConfigKey, Any]] | JsonObject:
    if not isinstance(changes, list) or not changes:  # pragma: no cover - the tool schema requires a non-empty array
        return _invalid("changes", "changes must be a non-empty array of {key, value} objects.")
    parsed: list[tuple[ResolvedConfigKey, Any]] = []
    seen: set[str] = set()
    for index, change in enumerate(changes):
        if not isinstance(change, dict):  # pragma: no cover - the tool schema requires objects
            return _invalid(f"changes.{index}", "Each change must be an object with a key and a value.")
        key = change.get("key")
        if not isinstance(key, str):  # pragma: no cover - the tool schema requires a string key
            return _invalid(f"changes.{index}.key", "key must be a dotted configuration key.")
        if key in seen:
            return _invalid(f"changes.{index}.key", f"`{key}` is set twice in one call. Send each key once; the last write would silently win.")
        seen.add(key)
        resolved = resolve_config_key(key)
        if resolved is None:
            return _invalid(
                f"changes.{index}.key",
                f"`{key}` is not a configuration key this tool sets. It names keys from a closed set; it does not "
                "author the file.",
                rejected_key=key,
                next_step=f"Call `project_config_describe` for the keys this configuration leaves open, or read {CONFIG_SHAPE_URI}.",
            )
        value_schema = config_key_schema(_rule_for(resolved), resolved.field)
        if value_schema is None:  # pragma: no cover - config_rule_fields reads the same schema
            return _invalid(f"changes.{index}.key", f"`{key}` has no value shape in the shipped schema.")
        errors = sorted(Draft202012Validator(value_schema).iter_errors(change.get("value")), key=lambda item: str(item.validator))
        if errors:
            return _invalid(
                f"changes.{index}.value",
                f"The value for `{key}` does not match the shape the shipped schema declares for it: {errors[0].message}",
                rejected_key=key,
                value_schema=value_schema,
            )
        parsed.append((resolved, change.get("value")))
    return parsed


def _rule_for(resolved: ResolvedConfigKey) -> Any:
    for rule in CONFIG_KEY_RULES:
        if rule.section == resolved.section and rule.under_permissions == resolved.under_permissions:
            return rule
    raise ConfigError("config_invalid", "No key rule covers a key that resolved.", {"field": resolved.key})  # pragma: no cover - resolution comes from the rules


def _apply_change(document: JsonObject, resolved: ResolvedConfigKey, value: Any, created_entries: list[str]) -> tuple[JsonObject | None, Any]:
    """Set one key, or say why not. Returns (refusal or None, previous value)."""
    section = document.get(resolved.section)
    if section is None:
        section = {}
        document[resolved.section] = section
    if not isinstance(section, dict):
        return _invalid(resolved.section, f"`{resolved.section}` in this configuration is not a mapping, so a key under it cannot be set."), None
    container: JsonObject = section
    if resolved.entry is not None:
        entry = section.get(resolved.entry)
        if entry is None:
            # Only a description key brings a device into existence. Granting a
            # permission on an entry that does not exist is not a thing anyone
            # means, and letting it create one would make the refusal that
            # follows name the wrong grant.
            if resolved.under_permissions:
                return _invalid(
                    f"{resolved.section}.{resolved.entry}",
                    f"`{resolved.section}.{resolved.entry}` is not in this configuration, and a permission does not "
                    "create a device. Describe the device first; a new entry is created with every permission false.",
                    unknown_entry=f"{resolved.section}.{resolved.entry}",
                ), None
            entry = {}
            section[resolved.entry] = entry
            created_entries.append(f"{resolved.section}.{resolved.entry}")
        if not isinstance(entry, dict):
            return _invalid(f"{resolved.section}.{resolved.entry}", f"`{resolved.section}.{resolved.entry}` is not a mapping, so a key under it cannot be set."), None
        container = entry
        if resolved.under_permissions:
            permissions = entry.get("permissions")
            if permissions is None:
                permissions = {}
                entry["permissions"] = permissions
            if not isinstance(permissions, dict):
                return _invalid(f"{resolved.section}.{resolved.entry}.permissions", "That entry's permissions block is not a mapping."), None
            container = permissions
    previous = container.get(resolved.field)
    container[resolved.field] = value
    return None, previous


def _deny_new_entries(document: JsonObject, created_entries: list[str]) -> None:
    """Complete every entry this call created with a deny-all permissions block.

    Written by the server, never inferred from the request: a device an agent
    adds must not be able to arrive already allowed to be flashed. A flag the
    call did set explicitly is left alone — it went through the permissions grant
    and through the before/after comparison like any other, and quietly undoing
    it would make the result say something the file does not."""
    for created in created_entries:
        section, _, name = created.partition(".")
        entry = (document.get(section) or {}).get(name)
        if not isinstance(entry, dict):  # pragma: no cover - _apply_change refuses a non-mapping entry
            continue
        explicit = entry.pop("permissions", None)
        entry["permissions"] = {**deny_all_permissions(section), **(explicit if isinstance(explicit, dict) else {})}


def _missing_required_fields(document: JsonObject, created_entries: list[str]) -> list[str]:
    """Keys a newly named entry still needs, read from the shipped schema.

    An entry created halfway would be caught by validation anyway, and the file
    would survive it — but "com_ports.probe_uart requires device" is an answer,
    and a schema violation on a document the caller never saw is a puzzle."""
    schema = config_schema()
    missing: list[str] = []
    for created in created_entries:
        section, _, name = created.partition(".")
        node = ((schema.get("properties") or {}).get(section) or {}).get("additionalProperties") or {}
        entry = ((document.get(section) or {}).get(name)) or {}
        missing += [f"{created}.{field}" for field in node.get("required", []) if field not in entry]
    return sorted(missing)


def _record_provenance(document: JsonObject, keys: list[str], timestamp: str, actor: str, via: str) -> None:
    """Record what moved, when, and through which call.

    ``created_by`` and ``created_at`` are left exactly as they were: they answer
    who wrote the file, which a change does not alter. ``modification_count`` is
    a count rather than a log, because an unbounded history inside the policy
    file is a policy file that grows without bound."""
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        document["provenance"] = provenance
    previous = provenance.get("modification_count")
    provenance["last_modified_by"] = actor
    provenance["last_modified_via"] = via
    provenance["last_modified_at"] = timestamp
    provenance["last_modified_keys"] = sorted(keys)
    provenance["modification_count"] = (previous if isinstance(previous, int) and not isinstance(previous, bool) and previous >= 0 else 0) + 1


def _rolled_back(target_path: Path, previous_text: str, error: ConfigError) -> JsonObject:
    result: JsonObject = {
        "tool": PROJECT_CONFIG_SET,
        **error.to_dict(),
        "summary": "The changed configuration did not load as authoritative and the previous file was restored. Nothing changed.",
        "path": str(target_path),
        **NOT_STARTED,
    }
    try:
        secure_atomic_write_text(target_path, previous_text)
    except (ConfigError, OSError) as failure:  # pragma: no cover - the restore writes what was just read
        result["rollback_error"] = str(failure)
    return result


def _open_run_refusal(existing: AgenticHILConfig, open_holds: JsonObject) -> JsonObject:
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_SET,
        "error_type": "config_write_in_open_run",
        "summary": (
            "This server is holding hardware right now, and those holds were taken under the policy this configuration "
            "states. Changing it underneath them would move the rules during the run they govern, so nothing was written."
        ),
        "open_holds": open_holds,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        **remediation_fields("config_write_in_open_run"),
        **NOT_STARTED,
        "retry_safe": True,
    }


# ---------------------------------------------------------------------------
# project_config_describe.


def project_config_describe(workspace: Path, existing: AgenticHILConfig | None, *, open_holds: JsonObject | None = None) -> JsonObject:
    """What this caller may change in this configuration, right now.

    Not a general list. After the split into two grants the answer differs with
    what an operator has opened, and a general list would tell most callers
    something untrue about their own bench. Reading needs no permission, so this
    answers whatever the grants are."""
    try:
        return _project_config_describe(workspace, existing, open_holds=open_holds)
    except ConfigError as error:
        return {"tool": PROJECT_CONFIG_DESCRIBE, **error.to_dict(), **NOT_STARTED}


def _project_config_describe(workspace: Path, existing: AgenticHILConfig | None, *, open_holds: JsonObject | None) -> JsonObject:
    if existing is None:  # pragma: no cover - the unprovisioned service answers first
        raise ConfigError("config_file_not_found", "This workspace has no Agentic HIL configuration to describe.", {"workspace_root": str(workspace)})
    target_path = authoritative_write_target(workspace, existing)
    _, document = load_config_document(target_path)
    rights = granted_rights(existing, document)

    writable: list[JsonObject] = []
    locked: list[JsonObject] = []
    for entry in _concrete_keys(document):
        if rights[str(entry["right"])]:
            writable.append(entry)
        else:
            # A locked key carries the way out, not only the fact that it is
            # shut: which grant opens it, and where that grant lives.
            locked.append({**entry, "unlocked_by": _right_key(str(entry["right"]))})

    return {
        "ok": True,
        "tool": PROJECT_CONFIG_DESCRIBE,
        "summary": (
            f"{len(writable)} configuration key(s) are open to this caller and {len(locked)} are not; "
            f"every locked one names the grant that would open it."
        ),
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        "config_version": existing.config_version,
        "rights": [
            {
                "permission": name,
                "key": _right_key(name),
                "granted": rights[name],
                "covers": CONFIG_RIGHTS[name],
            }
            for name in (CONFIG_DESCRIPTION_RIGHT, CONFIG_PERMISSIONS_RIGHT)
        ],
        "regeneration": {
            "permission": CONFIG_WRITE_RIGHT,
            "key": _right_key(CONFIG_WRITE_RIGHT),
            "granted": rights[CONFIG_WRITE_RIGHT],
            "covers": "Regenerate the whole file from hardware discovery with `project_config_create`. Carries every existing grant over unchanged.",
        },
        "writable_keys": writable,
        "locked_keys": locked,
        "new_entry_patterns": _entry_patterns(rights),
        "open_holds": open_holds or None,
        "writes_blocked_by_open_run": bool(open_holds),
        "reference": CONFIG_SHAPE_URI,
        "next_steps": _describe_next_steps(rights, writable, open_holds),
        **NOT_STARTED,
        "cleanup_required": False,
    }


def _concrete_keys(document: JsonObject) -> list[JsonObject]:
    """Every settable key of this document, resolved against what is in it."""
    keys: list[JsonObject] = []
    for rule in CONFIG_KEY_RULES:
        entries: list[str | None]
        if rule.named:
            section = document.get(rule.section)
            entries = sorted(str(name) for name in section) if isinstance(section, dict) else []
        else:
            entries = [None]
        for entry in entries:
            for field in config_rule_fields(rule):
                resolved = f"{rule.section}.{entry}" if entry is not None else rule.section
                key = f"{resolved}.permissions.{field}" if rule.under_permissions else f"{resolved}.{field}"
                keys.append(
                    {
                        "key": key,
                        "right": rule.right,
                        "current_value": _current_value(document, rule.section, entry, rule.under_permissions, field),
                        "value_schema": config_key_schema(rule, field) or {},
                    }
                )
    return keys


def _current_value(document: JsonObject, section: str, entry: str | None, under_permissions: bool, field: str) -> Any:
    node = document.get(section)
    if not isinstance(node, dict):
        return None
    if entry is not None:
        node = node.get(entry)
        if not isinstance(node, dict):
            return None
    if under_permissions:
        node = node.get("permissions")
        if not isinstance(node, dict):
            return None
    return node.get(field)


def _entry_patterns(rights: dict[str, bool]) -> list[JsonObject]:
    """How to address a device this configuration does not have yet."""
    patterns: list[JsonObject] = []
    for rule in CONFIG_KEY_RULES:
        if rule.section not in CONFIG_NAMED_SECTIONS or rule.under_permissions:
            continue
        patterns.append(
            {
                "key": rule.pattern,
                "right": rule.right,
                "granted": rights[rule.right],
                "fields": list(config_rule_fields(rule)),
                "note": (
                    f"Setting a key under a `{rule.section}` name that does not exist creates that entry, with every "
                    "permission false and written by this server, so a new device can never arrive already granted."
                ),
            }
        )
    return patterns


def _describe_next_steps(rights: dict[str, bool], writable: list[JsonObject], open_holds: JsonObject | None) -> list[str]:
    steps: list[str] = []
    if open_holds:
        steps.append("A run or session is holding hardware, so no configuration write is accepted until it is closed with `bench_run_stop`.")
    if writable:
        steps.append("Send the keys you need as `project_config_set` changes; every value is checked against the shipped schema before anything is written.")
    if not rights[CONFIG_DESCRIPTION_RIGHT]:
        steps.append(
            f"Describing this bench over MCP is closed: {_right_key(CONFIG_DESCRIPTION_RIGHT)} is false. Report that and "
            "let the operator decide; do not edit the file another way."
        )
    if not rights[CONFIG_PERMISSIONS_RIGHT]:
        steps.append(
            f"Granting is closed: {_right_key(CONFIG_PERMISSIONS_RIGHT)} is false, so no permission in this file can be "
            "changed from here. That is the split working, not an obstacle."
        )
    steps.append(f"{CONFIG_SHAPE_URI} describes what a configuration looks like and what deliberately cannot be changed over MCP.")
    return steps


__all__ = [
    "ACTOR_AGENT",
    "ACTOR_HUMAN",
    "PROJECT_CONFIG_DESCRIBE",
    "PROJECT_CONFIG_SET",
    "authoritative_write_target",
    "deny_all_permissions",
    "description_view",
    "granted_rights",
    "load_config_document",
    "permission_delta",
    "permission_surface",
    "project_config_describe",
    "project_config_set",
]
