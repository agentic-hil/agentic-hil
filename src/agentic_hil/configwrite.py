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
same motion and without being told, handed over the permissions block.

**The split is enforced twice.** Once by the key model, which never maps a
``permissions`` key onto the description grant. And once by comparing the
permissions actually present in the document before and against after — a check
that reads the document rather than the request, so it holds even where the
first one would be wrong. That second check is the one that closes a detour: not
because a detour is known to exist, but because a boundary defended only by a
path parser is a boundary defended only as well as the parser is right.

**And it only goes one way.** hardci-hq#96 turned the generated default over:
a configuration is now created with every permission true, because the closed
one cost this project's owner a working day of hand-edited YAML and bought
nothing an agent with a shell could not already reach. What replaces it is the
direction. ``permission_widening`` refuses any write that turns a permission on
— one this server never touched, and one it set to ``false`` itself a moment
earlier — so the invariant from #71 and #80 survives read from the other side:
**an agent can only ever reduce its own authority.** It is the only one left,
which is why it is enforced on the same before/after surfaces rather than on the
keys a request happened to name, and why no actor waives it.

Closing ``allow_config_permissions_write`` is therefore the terminal move, and
the result of that call says so: what stands frozen, that the caller cannot undo
it, and the command a person reopens the file with.

**And there is a gate on the one-way street.** hardci-hq#102: everything above
answers the reopen question for a file that does not exist yet — a generation
opens everything — and answered nothing for one that does. `init --force` does
reopen it, because it is the full regeneration and writes the open skeleton over
whatever was there; it costs the baudrate, the `resource_id`, the `state_root`
and every artifact root the operator set, which is the same bill deleting the
file comes with. It names the permissions it reopened that way in its result, so
the reset is loud rather than silent (hardci-hq#113). ``set_permission`` at the
bottom of this module is ``agentic-hil grant`` and ``agentic-hil revoke``: one
named permission, written through ``project_config_set`` like every other
change, from the command line and from nowhere else. It waives the permissions
grant, because a bench that closed that grant is exactly the bench whose only
way back was the full reset, and because the same shell already holds
``init --force``, which rewrites every permission in the file without consulting
anything. Nothing on the MCP
surface moves: the ratchet there is what it was.
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
    permission_summary,
    secure_atomic_write_text,
    secure_optional_read_bytes,
    secure_user_file_lock,
    write_generated_config,
)
from agentic_hil.configstate import config_stale, config_status, with_config_status
from agentic_hil.knowledge import (
    CONFIG_DESCRIPTION_RIGHT,
    CONFIG_KEY_RULES,
    CONFIG_NAMED_SECTIONS,
    CONFIG_PERMISSIONS_RIGHT,
    CONFIG_REOPEN_COMMAND,
    CONFIG_RIGHTS,
    CONFIG_SHAPE_URI,
    CONFIG_WIDENING_ERROR,
    CONFIG_WRITE_RIGHT,
    PERMISSION_CHANGE_IN_OPEN_RUN,
    ResolvedConfigKey,
    config_key_schema,
    config_rule_fields,
    permissions_frozen_notice,
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
# The *authorization* on the permissions half is waived for nobody: the
# before/after comparison further down reads the document rather than the request
# and refuses a permission that moved without `allow_config_permissions_write`,
# whoever asked. The *direction* rule from hardci-hq#96 is narrower on purpose —
# it nails the MCP surface shut, so only the agent actor is held to writing
# `false` and nothing else. What an operator does to their own configuration from
# their own shell is theirs.
ACTOR_AGENT = "agent"
ACTOR_HUMAN = "human"
ACTOR_PHRASES = {ACTOR_AGENT: "an agent", ACTOR_HUMAN: "a person"}

# One comment line, rewritten rather than appended, so a file changed a hundred
# times does not grow a hundred lines of banner. It sits in the header because
# the header is what a person reads first, and a generated header states which
# permissions below are true — which was so when it was written, and is exactly
# what a narrowing since then has stopped being. A reader must not go on
# believing the header without being told the file has moved.
CHANGE_MARKER_PREFIX = "# Changed by "
NOT_STARTED: JsonObject = {"side_effect_committed": False, "side_effect_status": "not_started", "hardware_state": "unchanged"}


# ---------------------------------------------------------------------------
# Reading what is actually in a document.


def permission_surface(node: object, prefix: str = "", *, entry_ids: bool = False) -> dict[str, Any]:
    """Every value in a document that decides what may be done, by dotted path.

    Almost entirely name-driven rather than key-model-driven. This walks whatever
    the document actually contains and collects two things at any depth: a key
    named ``allow_*``, and every leaf inside a mapping named ``permissions``. A
    guard that understood only the paths the key model produces would be blind
    exactly where something that got around the key model would land.

    The one place it has to know the schema is the level directly under
    ``debuggers``, ``com_ports`` and ``can_buses``, because that level is not
    field names at all — it is operator-chosen entry ids, and ``permissions``,
    ``provenance`` and ``allow_anything`` are all legal ones. Reading them as
    grant names made every field of such an entry a permission: repointing
    ``debuggers.permissions`` at another board produced a permission delta and
    demanded ``allow_config_permissions_write``, while `project_config_describe`
    went on offering the same key as description-writable. The entry is still
    walked, so its real ``permissions`` block is still found, one level further
    down where the schema actually puts it.

    A *scalar* under an id level is still read as a grant. An entry is a mapping
    in every section here, so a scalar there is not an entry that happens to be
    called ``allow_flash`` — it is the shape this walk exists to catch, and it
    keeps catching it.
    """
    found: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            named_entry = entry_ids and isinstance(value, dict)
            if str(key).startswith("allow_") and not named_entry:
                found[path] = value
            elif str(key) == "permissions" and isinstance(value, dict) and not named_entry:
                for flag, granted in value.items():
                    found[f"{path}.{flag}"] = granted
            found.update(permission_surface(value, path, entry_ids=not prefix and str(key) in CONFIG_NAMED_SECTIONS))
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


def permission_widening(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Which permission paths a change opens rather than closes.

    The whole of hardci-hq#96 rests on this list being empty for every write an
    agent makes over MCP. A generated configuration now grants everything, so the
    property left to defend is the direction: an agent writes ``false`` into a
    permission and never ``true``, which means it can only ever reduce its own
    authority. It says nothing about creation — a regeneration writes the open
    skeleton and is the operator's call, not a permissions write — and nothing
    about what a person does at the command line.

    Read off ``permission_delta``, so the two answers cannot disagree about what
    moved, and truthiness rather than ``is True`` decides which way. That is
    deliberately fail-closed: a value the schema would refuse anyway — ``1``,
    ``"false"``, a non-empty string — counts as opening, so a widening cannot
    arrive dressed as a type error and be sorted out by a later layer that is not
    looking for it. A path missing before counts as closed, which is what makes a
    permission appearing on a newly named entry a widening rather than a
    creation."""
    return [path for path in permission_delta(before, after) if bool(after.get(path, False))]


# Where the schema puts a grant, and nowhere else. `permission_surface` above is
# name-driven at every depth but one, because its job is to *find* a grant
# wherever one got in. This is the opposite job — deciding what to *drop* — and a
# name-driven rule there removes real description. Both stop at the same place
# and for the same reason: `debuggers`, `com_ports` and `can_buses`
# (`CONFIG_NAMED_SECTIONS`) are keyed by operator-chosen entry ids, and
# `permissions`, `provenance` and `allow_anything` are all legal ids. One tuple
# for both so the finder and the dropper cannot disagree about where an id level
# is.
# Grants that sit directly on a fixed section rather than inside a permissions
# block: `debug.allow_all_symbols`, `artifacts.allow_upload`.
_SECTION_GRANT_KEYS = {"debug": ("allow_all_symbols",), "artifacts": ("allow_upload",)}
# Top-level keys that are not part of what the bench *is*: the project grants,
# and the audit trail the writer maintains itself.
_TOP_LEVEL_NON_DESCRIPTION = ("permissions", "provenance")


def description_view(node: object) -> object:
    """A document with everything that grants anything removed.

    The mirror of ``permission_surface``, and — unlike it — schema-aware. Two
    callers depend on what survives: the compare-and-swap against
    ``expect_document``, and the check for whether a change needs
    `allow_config_description_write`. Both read a *removal* as "nothing there
    changed", so removing more than the grants blinds them.

    That is what a name-driven rule did. It dropped every mapping key called
    `permissions` or `provenance`, and every key starting with `allow_`, at every
    depth — and those are legal entry ids. A debugger an operator named
    `permissions` had its whole entry deleted from this view, so its `probe_id`
    and `type` were outside the CAS *and* outside the description-right check: a
    concurrent repoint of that entry from board A to board B was invisible, and
    discovery from A could still be committed onto it.

    So the drops here are the schema's grant locations, spelled out. A grant that
    turns up anywhere else is still caught — by ``permission_surface``, which
    finds it, and now by this view too, which keeps it and therefore reports it
    as a description change. Both rights, rather than neither."""
    if not isinstance(node, dict):
        return node
    view: dict[str, Any] = {}
    for key, value in node.items():
        if key in _TOP_LEVEL_NON_DESCRIPTION:
            continue
        if key in CONFIG_NAMED_SECTIONS and isinstance(value, dict):
            view[key] = {
                entry_id: {field: item for field, item in entry.items() if field != "permissions"} if isinstance(entry, dict) else entry
                for entry_id, entry in value.items()
            }
            continue
        if key in _SECTION_GRANT_KEYS and isinstance(value, dict):
            view[key] = {field: item for field, item in value.items() if field not in _SECTION_GRANT_KEYS[key]}
            continue
        view[key] = value
    return view


def deny_all_permissions(section: str) -> JsonObject:
    """The permissions block a newly named entry is created with.

    Written by the server, never taken from the request. It stayed `false` when
    the generated default turned over (hardci-hq#96), and for the reason that
    default rests on rather than in spite of it: a permission that appeared
    through a write would be a grant an agent had produced, which is the one
    thing this surface never does. A generation grants; a write only ever takes
    away, and creating an entry is a write.

    So a device named through `project_config_set` arrives closed, and opening it
    is ``agentic-hil init --force`` — the same command that reopens anything else
    in this file, and a person's."""
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


def config_document_snapshot(target_path: Path) -> tuple[bytes | None, Exception | None]:
    """One trusted read of the authoritative file: its bytes, or what stopped it.

    ``None, None`` means the file is not there, which is a different answer from
    every failure and is the one case a caller may have a policy for. Everything
    else arrives as the original exception so the caller decides what to say
    about it — and, for the tool documented to report the configuration's state
    even while refusing, so that one read serves both the state and the document.
    """
    try:
        return secure_optional_read_bytes(target_path), None
    except (ConfigError, OSError, ValueError) as error:
        return None, error


def config_read_error(error: Exception, target_path: Path) -> ConfigError:
    """A read failure as a refusal this server knows how to talk about.

    ``secure_optional_read_bytes`` raises what the platform raises. Left alone
    they leave the MCP layer with an internal error where a structured answer was
    promised, on exactly the paths an operator hits with a half-written file: a
    configuration saved as UTF-16 by a Windows editor, a directory that is no
    longer traversable, a device that went away.
    """
    if isinstance(error, ConfigError):
        return error
    if isinstance(error, UnicodeDecodeError):
        return ConfigError(
            "config_unreadable",
            "The authoritative configuration is not UTF-8 text, so it cannot be read as a configuration.",
            {"path": str(target_path), "backend_error": str(error)},
        )
    return ConfigError(
        "config_unreadable",
        "The authoritative configuration could not be read.",
        {"path": str(target_path), "backend_error": str(error)},
    )


def parse_config_document(raw: bytes | None, target_path: Path) -> tuple[str, JsonObject]:
    """The document in a snapshot, or the refusal that says why there is none."""
    if raw is None:
        raise ConfigError("config_file_not_found", "This workspace has no Agentic HIL configuration to change.", {"path": str(target_path)})
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise config_read_error(error, target_path) from error
    try:
        document = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        details: JsonObject = {"path": str(target_path), "backend_error": str(error)}
        mark = getattr(error, "problem_mark", None)
        if mark is not None:
            details.update({"line": mark.line + 1, "column": mark.column + 1})
        raise ConfigError("config_invalid", "The authoritative configuration is not valid YAML.", details) from error
    if not isinstance(document, dict):
        raise ConfigError("config_invalid", "The authoritative configuration is not a mapping.", {"path": str(target_path)})
    return text, document


def load_config_document(target_path: Path) -> tuple[str, JsonObject]:
    raw, failure = config_document_snapshot(target_path)
    if failure is not None:
        raise config_read_error(failure, target_path)
    return parse_config_document(raw, target_path)


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
    generated header states which permissions below are true, which was so when
    it was written and is the kind of thing a later reader keeps believing unless
    something in front of them says the file has moved — and a narrowing is
    precisely the change that makes it wrong. It names the actor because "an agent
    changed this" and "you changed this" are different things to read at the top
    of a policy file."""
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


# The dotted path `permission_surface` gives the grant that opens the permissions
# half. Derived from the same `_right_key` the refusals name, so the check for
# "this call was the terminal move" and the key a caller is told about cannot
# come apart.
GRANTING_PATH = _right_key(CONFIG_PERMISSIONS_RIGHT)


def _only_false_allowed(requested: list[str], path: Path) -> JsonObject:
    """A permission change that carried any value other than literal ``false``.

    Refused on the request rather than on the document, because this is the case
    the document cannot show: sending ``true`` for a permission that is already
    ``true`` moves nothing, and a rule that only watched for movement would let
    that through, rewrite the file and stamp its provenance for a write this
    surface is not allowed to make. The same error type as a widening, because
    for a caller it is the same fact — nothing here turns a permission on."""
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_SET,
        "error_type": CONFIG_WIDENING_ERROR,
        "summary": (
            f"{len(requested)} permission change(s) in this call carried a value other than false, and false is the only "
            "value this surface writes into a permission. A configuration is generated with every permission granted, so "
            "an agent can narrow one and never open one — including one that is already open, which this call would not "
            "have changed. Nothing was written."
        ),
        "widened_keys": sorted(requested),
        "path": str(path),
        "reopened_by": CONFIG_REOPEN_COMMAND,
        "reference": CONFIG_SHAPE_URI,
        "next_step": (
            "Send `false` for a permission you mean to take away, and send no change at all for one that should stay as "
            "it is. If the operator asked for a permission to be turned on, that is theirs to do with "
            f"`{CONFIG_REOPEN_COMMAND}` in the project root; report it and stop."
        ),
        **remediation_fields(CONFIG_WIDENING_ERROR),
        **NOT_STARTED,
        "retry_safe": False,
    }


def _widening_denied(widened: list[str], path: Path) -> JsonObject:
    """A write that would have turned a permission on.

    Not a `permission_denied`: the grant is usually there and this is refused
    anyway, so naming a permission would send a caller looking for one that opens
    it. There is none, and the answer is the direction rather than the grant."""
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_SET,
        "error_type": CONFIG_WIDENING_ERROR,
        "summary": (
            f"{len(widened)} permission(s) in this configuration would have been turned on by this change, and nothing "
            "on this surface turns a permission on. A configuration is generated with every permission granted and can "
            "only be narrowed from here, so an agent writes false into a permission and never true. Nothing was "
            "written."
        ),
        "widened_keys": sorted(widened),
        "path": str(path),
        "reopened_by": CONFIG_REOPEN_COMMAND,
        "reference": CONFIG_SHAPE_URI,
        "next_step": (
            "This refusal is the answer to the request. Report which permissions were asked for and that only a person "
            f"can grant one, with `{CONFIG_REOPEN_COMMAND}` in the project root, then stop. If the call also carried "
            "narrowings, re-send those on their own: a change is applied whole or not at all, so they were refused with "
            "the widening."
        ),
        **remediation_fields(CONFIG_WIDENING_ERROR),
        **NOT_STARTED,
        "retry_safe": False,
    }


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
    expect: dict[str, Any] | None = None,
    expect_document: JsonObject | None = None,
    waive_permissions_grant: bool = False,
) -> JsonObject:
    """Set named configuration keys, or refuse and change nothing.

    ``actor`` and ``via`` say who is asking and through what, and both end up in
    ``provenance``. ``ACTOR_HUMAN`` is a person at the CLI, which is the
    authority ``agentic-hil init`` already runs under; it waives the description
    grant and nothing else.

    ``waive_permissions_grant`` waives the other one, and exactly one caller sets
    it: ``set_permission`` below, which is ``agentic-hil grant`` and
    ``agentic-hil revoke``. It is not part of any tool schema and no MCP path
    reaches it. The grant is otherwise consulted for everybody including
    ``ACTOR_HUMAN`` — a bench that closed the permissions half is closed to
    `project_config_set` from every direction — and that is what made the closing
    a move with no way back, which is hardci-hq#102. What reopens it is not a
    wider `project_config_set`; it is a command that writes one named permission,
    under the authority the same shell already has in `agentic-hil init --force`,
    and refuses to touch anything else in the file. Refused for every actor but
    ``ACTOR_HUMAN`` here as well, so the waiver cannot be reached with an agent's
    provenance on it even by a caller inside this process.

    ``expect`` maps a key to the value the caller decided against. It is not
    part of any tool schema and no caller supplies it: it exists for a caller
    that read the document, spent time deciding, and must not then overwrite what
    somebody else wrote in between. Checked inside the write lock against the
    document as it is now, so a mismatch refuses the whole call.

    ``expect_document`` is the whole document the caller planned against. Not
    every input to a plan is a key this tool can set — a debugger's ``type``, the
    set of entries a document has — so a per-key expectation cannot cover them,
    and a plan invalidated by one of those is exactly as wrong as one invalidated
    by a carried key. Compared on its description, so a permissions change or a
    provenance bump by another writer does not refuse a change it cannot affect."""
    try:
        return _project_config_set(
            workspace,
            existing,
            changes,
            open_holds=open_holds,
            actor=actor,
            via=via,
            expect=expect,
            expect_document=expect_document,
            waive_permissions_grant=waive_permissions_grant and actor == ACTOR_HUMAN,
        )
    except ConfigError as error:
        # The state of the configuration belongs on a refusal about the
        # configuration too: a caller told "this did not load" and not told
        # whether the file has moved under the server has half the picture.
        return with_config_status({"tool": PROJECT_CONFIG_SET, **error.to_dict(), **NOT_STARTED}, config_status(existing))


def _invalid(field: str, summary: str, **extra: object) -> JsonObject:
    return {"ok": False, "tool": PROJECT_CONFIG_SET, "error_type": "invalid_argument", "field": field, "summary": summary, "reference": CONFIG_SHAPE_URI, **extra, **NOT_STARTED}


def _project_config_set(
    workspace: Path,
    existing: AgenticHILConfig | None,
    changes: object,
    *,
    open_holds: JsonObject | None,
    actor: str,
    via: str,
    expect: dict[str, Any] | None = None,
    expect_document: JsonObject | None = None,
    waive_permissions_grant: bool = False,
) -> JsonObject:
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
        # Before the grants and before anything is applied: a plan made against a
        # document that has since moved is not a plan for this document, and the
        # only safe thing to do with it is to say so.
        stale = _stale_expectations(document, requested, expect)
        if stale:
            return _changed_underneath(target_path, stale)
        if expect_document is not None and description_view(document) != description_view(expect_document):
            return _document_changed_underneath(target_path)
        rights = granted_rights(existing, document)
        # An operator at the CLI is the person the description grant belongs to,
        # so it is not consulted for them; the permissions grant is consulted for
        # everybody, and layer 3 below enforces that from the document itself.
        # The one exception is the pair of commands that exist to move a single
        # named permission — see `waive_permissions_grant` on the caller above.
        rights = {
            **rights,
            CONFIG_DESCRIPTION_RIGHT: rights[CONFIG_DESCRIPTION_RIGHT] or actor == ACTOR_HUMAN,
            CONFIG_PERMISSIONS_RIGHT: rights[CONFIG_PERMISSIONS_RIGHT] or waive_permissions_grant,
        }

        # 1. The key model. A permissions key never resolves to the description
        #    grant, so this alone already separates the two halves.
        needed: dict[str, list[str]] = {}
        for resolved, _ in requested:
            needed.setdefault(resolved.right, []).append(resolved.key)
        for right in (CONFIG_DESCRIPTION_RIGHT, CONFIG_PERMISSIONS_RIGHT):
            if needed.get(right) and not rights[right]:
                return _permission_denied(PROJECT_CONFIG_SET, right, needed[right], target_path)

        # 1b. The direction, stated where the rule is actually about: the value a
        #     request carries. hardci-hq#96 nails the MCP surface shut and nothing
        #     else, and on that surface an agent writes `false` into a permission
        #     and never anything else. The document comparison in 2b below is the
        #     backstop and cannot be this check: a request that sends `true` for a
        #     permission that is already `true` moves nothing, so it produces no
        #     delta to catch, and the file would be rewritten and its provenance
        #     stamped for a write the rule does not allow to be made at all. A
        #     value the schema would coerce — `1`, `"false"` — is refused here for
        #     the same reason, before anything is applied.
        #
        #     A person at the command line is not held to it. That is the owner's
        #     clarification on this issue: the ratchet binds the MCP surface, and
        #     what an operator does to their own configuration from their own
        #     shell is theirs.
        if actor != ACTOR_HUMAN:
            not_false = [resolved.key for resolved, value in requested if resolved.right == CONFIG_PERMISSIONS_RIGHT and value is not False]
            if not_false:
                return _only_false_allowed(not_false, target_path)

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
        after_permissions = permission_surface(updated)
        moved = permission_delta(before_permissions, after_permissions)
        if moved and not rights[CONFIG_PERMISSIONS_RIGHT]:
            return _permission_denied(PROJECT_CONFIG_SET, CONFIG_PERMISSIONS_RIGHT, moved, target_path)
        # 2b. And the direction again, read from the document rather than from
        #     the request. 1b above refuses the value an agent sent; this refuses
        #     a permission that came *on* however it got there — under a key the
        #     key model resolved somewhere else, on an entry a change created, in
        #     a shape a path parser would not have recognised. Extending the
        #     comparison that was already here rather than replacing it is the
        #     point: it is the layer that does not depend on having anticipated
        #     the key.
        #
        #     Scoped to the same surface as 1b, and for the same reason: a person
        #     at the CLI reaches this function through `agentic-hil
        #     adopt-hardware`, which names no permission, and their own shell is
        #     not what this ratchet was decided against.
        widened = permission_widening(before_permissions, after_permissions) if actor != ACTOR_HUMAN else []
        if widened:
            return _widening_denied(widened, target_path)
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

    # The same rule the regeneration path follows: this server keeps serving the
    # configuration it loaded, because swapping a policy under a live session is
    # not something a tool call may do behind its back. `reload_required` is read
    # off the staleness check rather than asserted here, so that this result and
    # the block every later answer carries are one statement made once — an
    # operator must never get "restart needed" from the write and "no change"
    # from the next tool call.
    status = config_status(existing)
    # The terminal move, announced in the result of the call that made it. Read
    # off the same before/after surfaces the direction check used, so what is
    # reported as frozen is what the file actually says rather than what the
    # request asked for.
    froze_permissions = bool(before_permissions.get(GRANTING_PATH)) and not bool(after_permissions.get(GRANTING_PATH))
    frozen = permissions_frozen_notice(GRANTING_PATH, after_permissions, written.config_path) if froze_permissions else None
    result = {
        "ok": True,
        "tool": PROJECT_CONFIG_SET,
        "summary": (
            f"{len(applied)} configuration key(s) were changed; the file was validated before it replaced the previous one."
            + (f" {frozen['summary']}" if frozen is not None else "")
        ),
        "path": written.config_path,
        "workspace_root": written.workspace_root,
        "changes": applied,
        "created_entries": sorted(created_entries),
        "permissions_changed": moved,
        **({"permissions_frozen": frozen} if frozen is not None else {}),
        "provenance": dict(updated.get("provenance") or {}),
        "reload_required": bool(status["reload_required"]),
        "next_steps": [
            *(frozen["next_steps"] if frozen is not None else []),
            "Report which keys changed and where the file is.",
            "This server is still serving the configuration it loaded at startup. Ask the operator to restart the MCP "
            "server before relying on anything this change decides.",
            f"`project_config_describe` says what else this configuration leaves open; {CONFIG_SHAPE_URI} explains the shape.",
        ],
        **NOT_STARTED,
        "cleanup_required": False,
    }
    return with_config_status(result, status)


def _stale_expectations(document: JsonObject, requested: list[tuple[ResolvedConfigKey, Any]], expect: dict[str, Any] | None) -> list[JsonObject]:
    """Keys whose current value is no longer the one the caller planned against.

    A caller that classifies a document, then does something slow — reading a
    probe takes seconds — and then writes has a window in which another CLI or
    MCP process can fill the very placeholder it decided was free. The document
    is re-read inside this lock anyway; comparing it against what the caller saw
    is what turns "fill only what is unset" into a promise rather than a hope."""
    if not expect:
        return []
    stale: list[JsonObject] = []
    for resolved, _ in requested:
        if resolved.key not in expect:
            continue
        current = _current_value(document, resolved.section, resolved.entry, resolved.under_permissions, resolved.field)
        if current != expect[resolved.key]:
            stale.append({"key": resolved.key, "expected_value": expect[resolved.key], "current_value": current})
    return stale


def _changed_underneath(target_path: Path, stale: list[JsonObject]) -> JsonObject:
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_SET,
        "error_type": "config_changed_underneath",
        "summary": (
            f"{len(stale)} key(s) this change was planned against no longer hold the value they were planned against, so "
            "somebody else wrote this file in the meantime and nothing was written here."
        ),
        "stale_keys": stale,
        "path": str(target_path),
        "next_step": "Read the configuration again and decide against what it says now; the plan this call carried is out of date.",
        "reference": CONFIG_SHAPE_URI,
        **NOT_STARTED,
        "retry_safe": True,
    }


def _document_changed_underneath(target_path: Path) -> JsonObject:
    """The plan was made against a description of the bench that has since moved.

    Broader than the per-key check on purpose, and not reducible to it: a plan
    can be decided by values this tool cannot set — a debugger's ``type``, which
    entries exist at all — and one of those moving invalidates the plan just as
    completely as a carried key moving. Compared on the description, so a
    concurrent permissions change, which no plan here reads, does not refuse a
    write it has nothing to do with.
    """
    return {
        "ok": False,
        "tool": PROJECT_CONFIG_SET,
        "error_type": "config_changed_underneath",
        "summary": (
            "The description of the bench in this configuration is no longer the one this change was planned against, "
            "so somebody else wrote this file in the meantime and nothing was written here. The plan was decided by "
            "more than the keys it would have set — which entries exist, which backend an entry names — and any of "
            "those moving makes it a plan for a document that is gone."
        ),
        "document_changed": True,
        "path": str(target_path),
        "next_step": "Read the configuration again and decide against what it says now; the plan this call carried is out of date.",
        "reference": CONFIG_SHAPE_URI,
        **NOT_STARTED,
        "retry_safe": True,
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
    it would make the result say something the file does not. Setting one to
    `true` there is refused a few lines later as the widening it is, so what
    "left alone" can mean is a `false` written twice over."""
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
# agentic-hil grant / agentic-hil revoke.
#
# hardci-hq#96 inverted the generated default and answered the reopen question
# for the first run only: a generation opens everything. For a file that already
# exists there was no answer that did not cost the rest of the file.
# `project_config_set` writes `false` into a permission and nothing else; and
# `init --force` and deleting the file both do come back open, at the price of the
# baudrate, the `resource_id`, the `state_root` and every artifact root the
# operator ever set. That is the bill hardci-hq#102 was filed over, and it was
# paid.
#
# `carry_over_permissions` is the regeneration that keeps a narrowing, and it is
# the MCP path's — `project_config_create`, where the caller is an agent and the
# grants come from the configuration that server loaded rather than from the
# request. `init --force` is the operator's reset and deliberately carries
# nothing (hardci-hq#113); `agentic-hil adopt-hardware` is the command that
# refreshes the hardware and leaves everything else standing.
#
# So this is the gate on the one-way street, and it is deliberately the narrowest
# thing that opens it: the same closed key set `project_config_set` names, one
# permission per name, written through that same function. Not a second write
# path — everything below ends in `project_config_set` — and not an MCP tool: it
# is reachable from `agentic-hil` and from nowhere else, so the ratchet on the
# MCP surface is exactly as it was.


PERMISSION_GRANT = "grant"
PERMISSION_REVOKE = "revoke"
# What each command writes. Both ship together: a CLI that could only open would
# be the next one-way street with the arrow reversed, and an operator who opened
# something to try it would be back to editing YAML to put it back.
PERMISSION_COMMAND_VALUES = {PERMISSION_GRANT: True, PERMISSION_REVOKE: False}


def resolve_permission_key(key: str) -> tuple[ResolvedConfigKey | None, str | None]:
    """One permission key, resolved, or why it is not one.

    Two spellings reach the same key, and both are the key model's rather than a
    parallel one. The canonical `can_buses.dut.permissions.allow_write` is what
    `project_config_set` and `project_config_describe` name. The short
    `can_buses.dut.allow_write` is what the issue that asked for this command
    wrote, and it is what an operator reads off `permission_summary` and off the
    file itself, where the flag sits under a `permissions:` block whose name
    nobody repeats out loud.

    The short form is only ever tried after the long one has failed to resolve,
    so it can never shadow a real key: a description key resolves and is refused
    below for being a description key, and only a spelling that names nothing at
    all is rewritten. The rewrite is the exact inverse of the key model's own
    right-to-left reading — field names contain no dot, so the last component is
    the field and everything before it is the entry, whatever dots the operator
    put in the entry name.

    Returns ``(resolved, None)`` or ``(None, reason)``."""
    resolved = resolve_config_key(key)
    if resolved is None:
        entry, dot, field = key.rpartition(".")
        candidate = resolve_config_key(f"{entry}.permissions.{field}") if dot and entry else None
        if candidate is None or candidate.right != CONFIG_PERMISSIONS_RIGHT:
            return None, f"`{key}` is not a permission in an Agentic HIL configuration."
        return candidate, None
    if resolved.right != CONFIG_PERMISSIONS_RIGHT:
        return None, (
            f"`{key}` is a key of this bench's description rather than a permission, and these commands move "
            "permissions only. What hardware is there and how it is reached is changed with "
            f"`{PROJECT_CONFIG_SET}` over MCP, or with `agentic-hil adopt-hardware` for what an attached board "
            "already knows about itself."
        )
    return resolved, None


def _permission_key_refusal(command: str, rejected: list[JsonObject], path: Path, document: JsonObject) -> JsonObject:
    """A name that is not a permission, answered with the ones that are.

    The list comes out of *this* document rather than out of the key patterns,
    because `can_buses.<name>.permissions.allow_write` is not something anybody
    can type: an operator needs the entry names their own bench has. This is the
    whole of the discoverability answer, and it is why there is no wildcard and
    no section form — a name has to be read before it can be typed, and the
    refusal is where it is read."""
    return {
        "ok": False,
        "command": f"agentic-hil {command}",
        "error_type": "invalid_argument",
        "summary": (
            f"{len(rejected)} of the names given to `agentic-hil {command}` do not name a permission in this "
            "configuration, so nothing was written. Every key this command accepts is listed in "
            "`permission_keys_here`, read out of this file as it stands."
        ),
        "rejected_keys": rejected,
        "permission_keys_here": sorted(str(entry["key"]) for entry in _concrete_keys(document) if entry["right"] == CONFIG_PERMISSIONS_RIGHT),
        "path": str(path),
        "reference": CONFIG_SHAPE_URI,
        "next_step": (
            "Name one of `permission_keys_here`. A permission is named one at a time and several may be given in one "
            "command; there is no wildcard and no whole-entry form, so what is opened is what was typed."
        ),
        **NOT_STARTED,
        "retry_safe": False,
    }


def _permission_open_run_refusal(existing: AgenticHILConfig, command: str, open_holds: JsonObject) -> JsonObject:
    """Somebody holds this bench, so its permissions do not move.

    The same rule as `config_write_in_open_run` and hardci-hq#80's: what is held
    was taken under the permissions this file states. Its own error type because
    the holder here is another process — an MCP server, another terminal — and
    "close your run" is not advice this caller can act on."""
    return {
        "ok": False,
        "command": f"agentic-hil {command}",
        "error_type": PERMISSION_CHANGE_IN_OPEN_RUN,
        "summary": (
            "Something on this machine is holding this bench's hardware right now, and it took those holds under the "
            "permissions this configuration states. Opening or closing one underneath them would move the rules during "
            "the run they govern, so nothing was written."
        ),
        "open_holds": open_holds,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        **remediation_fields(PERMISSION_CHANGE_IN_OPEN_RUN),
        **NOT_STARTED,
        "retry_safe": True,
    }


def set_permission(
    workspace: Path,
    existing: AgenticHILConfig | None,
    keys: list[str],
    *,
    command: str,
    open_holds: JsonObject | None = None,
) -> JsonObject:
    """Move the named permissions to what ``command`` writes, or change nothing.

    ``command`` is ``grant`` or ``revoke``; the value follows from it and is
    never taken from a caller, so there is no spelling of these commands that
    writes something other than a boolean into a permission.

    **Single keys, several per call, no section form and no wildcard.** That is
    the open question hardci-hq#102 left, and this is the answer with its
    reasons:

    * A section form would be defined by a moving target. `can_buses.dut` means
      the fields `config_rule_fields` reads out of the shipped schema *at the
      moment it runs* — so a permission added to the schema in a later release
      silently joins every section form an operator ever learned to type. A named
      key means the same thing forever, and a group named in this module would
      drift the same way for the same reason.
    * The two directions are not symmetric. A slip on `revoke` costs a refused
      call and is visible the next time the bench is used; a slip on `grant`
      hands out authority over hardware and is visible to nobody. Convenience is
      worth much less on the side that opens.
    * The convenience is available without the blast radius: several keys in one
      command are applied together or not at all, so opening a whole entry is
      still one line — one that names what it opened, and whose result lists
      exactly those keys.
    * The whole-file form already exists. `agentic-hil init --force` reopens
      everything, and the gap hardci-hq#102 describes is the surgical one. A
      section form sits between the two and is the shape that reads narrow and
      behaves broad.
    * What a wildcard is really for is not typing but finding out. That is
      answered where it is asked: an unrecognised name is refused with every
      permission key of *this* configuration, entry names and all.
    """
    try:
        return _set_permission(workspace, existing, keys, command=command, open_holds=open_holds)
    except ConfigError as error:
        return with_config_status({"command": f"agentic-hil {command}", **error.to_dict(), **NOT_STARTED}, config_status(existing))


def _set_permission(workspace: Path, existing: AgenticHILConfig | None, keys: list[str], *, command: str, open_holds: JsonObject | None) -> JsonObject:
    if existing is None:  # pragma: no cover - the CLI fails to load before it gets here
        raise ConfigError("config_file_not_found", "This workspace has no Agentic HIL configuration to change.", {"workspace_root": str(workspace)})
    value = PERMISSION_COMMAND_VALUES[command]
    target_path = authoritative_write_target(workspace, existing)
    _, document = load_config_document(target_path)

    resolved: list[ResolvedConfigKey] = []
    rejected: list[JsonObject] = []
    seen: set[str] = set()
    for key in keys:
        found, reason = resolve_permission_key(key)
        if found is None:
            rejected.append({"key": key, "reason": reason})
            continue
        if found.key in seen:
            continue
        seen.add(found.key)
        resolved.append(found)
    if rejected:
        return _permission_key_refusal(command, rejected, target_path, document)

    # After the names are known to be good and before anything is written, so an
    # operator whose command was a typo is told about the typo rather than about
    # somebody else's run.
    if open_holds:
        return _permission_open_run_refusal(existing, command, open_holds)

    changing: dict[str, Any] = {}
    unchanged: list[JsonObject] = []
    for item in resolved:
        current = _current_value(document, item.section, item.entry, item.under_permissions, item.field)
        # `is value` on a bool and nothing else: a permission holding `1` or
        # `"true"` is not a permission holding `true`, and rewriting it as one is
        # a change rather than the no-op a truthiness test would call it.
        if isinstance(current, bool) and current is value:
            unchanged.append({"key": item.key, "value": current})
        else:
            changing[item.key] = current

    if not changing:
        return _nothing_to_change(command, value, unchanged, existing)

    # The document was read outside the write lock, so every value this decision
    # rests on travels with the call and is compared again inside it. A
    # concurrent write between the two refuses rather than overwrites — and it
    # is what makes "already open, so nothing was written" a statement about the
    # file rather than about a document that has since moved.
    written = project_config_set(
        workspace,
        existing,
        [{"key": key, "value": value} for key in changing],
        open_holds=None,
        actor=ACTOR_HUMAN,
        via=f"cli:{command}",
        expect=dict(changing),
        waive_permissions_grant=True,
    )
    if written.get("ok") is not True:
        # Carried through as the write path stated it, with `command` added so a
        # reader knows which of the two asked. The refusals worth reaching this
        # way are its own — a permission on an entry that does not exist, a
        # document somebody else moved in between, a file that stopped loading.
        return {**written, "command": f"agentic-hil {command}"}
    return _permission_change_result(command, value, written, unchanged, existing)


def _nothing_to_change(command: str, value: bool, unchanged: list[JsonObject], existing: AgenticHILConfig) -> JsonObject:
    """Every named permission already stood where it was asked to stand.

    Not an error — asking for what is already so is how an operator makes sure —
    and not a change either. Nothing is written, which means no provenance entry
    and no change marker in the header for a call that moved nothing, and the
    result says so in those words rather than reporting a write."""
    return {
        "ok": True,
        "command": f"agentic-hil {command}",
        "summary": (
            f"Nothing was written: all {len(unchanged)} permission(s) named are already {str(value).lower()} in this "
            "configuration. This was a no-op, not a change — the file, its `provenance` and its header are exactly as "
            "they were."
        ),
        "changed": [],
        "unchanged": unchanged,
        "path": existing.config_path,
        "workspace_root": existing.workspace_root,
        "restart_required": False,
        "next_steps": [
            f"`agentic-hil doctor` reports what this configuration grants, if the question was what the bench allows "
            f"rather than what `agentic-hil {command}` did.",
        ],
        **NOT_STARTED,
        "cleanup_required": False,
    }


def _permission_change_result(command: str, value: bool, written: JsonObject, unchanged: list[JsonObject], existing: AgenticHILConfig) -> JsonObject:
    """What moved, from what, and what it takes for it to bind.

    The last part is the one an operator cannot infer. A running MCP server
    parses permissions once, at startup: `project_config_reload_description`
    picks up a changed *description* without a restart and deliberately re-reads
    no permission in either direction. So a permission opened here is in the file
    immediately and in force nowhere until that server is restarted, and a result
    that did not say so would leave the operator watching a granted permission
    keep being refused."""
    changed = [{"key": item["key"], "previous_value": item["previous_value"], "value": item["value"]} for item in written.get("changes") or []]
    verb = "granted" if value else "revoked"
    return {
        "ok": True,
        "command": f"agentic-hil {command}",
        "summary": (
            f"{len(changed)} permission(s) {verb} in {written.get('path')}"
            + (f"; {len(unchanged)} named permission(s) were already {str(value).lower()} and were not written" if unchanged else "")
            + ". Nothing else in the file was touched. A running MCP server does not re-read permissions, so restart it "
            "before relying on this."
        ),
        "changed": changed,
        "unchanged": unchanged,
        "path": written.get("path"),
        "workspace_root": written.get("workspace_root"),
        "provenance": written.get("provenance"),
        **({"permissions_frozen": written["permissions_frozen"]} if "permissions_frozen" in written else {}),
        # Not `reload_required`: a description reload is precisely the call that
        # will not take this. The word an operator needs is the stronger one.
        "restart_required": True,
        "next_steps": [
            "Restart the MCP server for this workspace — the agent host's, not this command line. A server parses "
            "permissions once at startup and `project_config_reload_description` re-reads none of them, so until then "
            "it keeps enforcing the value that was there before.",
            f"`agentic-hil doctor` reads this file fresh and reports what it now grants; `agentic-hil "
            f"{PERMISSION_REVOKE if value else PERMISSION_GRANT}` is the way back for any of it.",
        ],
        **NOT_STARTED,
        "cleanup_required": False,
        "config_status": config_status(existing),
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
    # One read, and both halves of the answer come out of it. This tool needs no
    # permission, so it is also the answer to "is what this server enforces still
    # what the file says" — and a status taken from one read beside a document
    # taken from another can describe two versions of the file in one response:
    # `state: unchanged` next to values from a document that is not the unchanged
    # one, or a status saying the file is there next to `loaded_policy`.
    #
    # The divergence that remains is the deliberate one: the keys and values
    # below come from disk while the grants that gate them are the narrower of
    # disk and what this server loaded.
    raw, failure = config_document_snapshot(target_path)
    status = config_status(existing, snapshot=(raw, failure))
    document: JsonObject | None = None
    if failure is not None:
        return _describe_unreadable(existing, config_read_error(failure, target_path), status)
    if raw is not None:
        # The file this server loaded is gone, and refusing with
        # `config_file_not_found` would say the wrong thing twice: this workspace
        # is not unconfigured — this server holds a policy and is enforcing it —
        # and that error sends a caller to `project_config_create`, which is
        # refused here and would replace the operator's settings with a
        # generated skeleton if it were not. Nothing else exposes the
        # policy still in force, so an absent file answers with it, and says
        # plainly that it came from memory and cannot be compared against
        # anything. A file that is there and will not parse is a different
        # condition with a different repair, and is refused rather than dressed
        # up as an answer.
        try:
            _, document = parse_config_document(raw, target_path)
        except ConfigError as error:
            return _describe_unreadable(existing, error, status)
    # No file is no grant: `granted_rights` takes the narrower of the loaded
    # policy and the document, and an absent document grants nothing.
    rights = granted_rights(existing, document if document is not None else {})

    writable: list[JsonObject] = []
    locked: list[JsonObject] = []
    for entry in _concrete_keys(document) if document is not None else []:
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
            if document is not None
            else "The authoritative configuration is no longer at its path, so no key can be changed. What this server "
            "loaded is still the policy in force and is reported below as `permissions_in_force`."
        ),
        "config_status": status,
        **({"config_stale": True} if config_stale(status) else {}),
        # Which document the keys and values below were read from. `disk` is the
        # file as it is now; `loaded_policy` means there is no file and every
        # value here comes from what this process parsed at startup, so nothing
        # in this answer can be compared against anything on disk.
        "document_source": "disk" if document is not None else "loaded_policy",
        # The grants this server is actually enforcing, whatever the file says.
        # On a stale or absent file this is the half a caller cannot get any
        # other way: `writable_keys` and `locked_keys` are read from disk, and
        # these are what a call is really gated on.
        "permissions_in_force": permission_summary(existing),
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
        "next_steps": _describe_next_steps(rights, writable, open_holds, status, on_disk=document is not None),
        **NOT_STARTED,
        "cleanup_required": False,
    }


def _describe_unreadable(existing: AgenticHILConfig, error: ConfigError, status: JsonObject) -> JsonObject:
    """A configuration that is there and will not parse, said in this tool's terms.

    `project_config_describe` carries `config_status` whatever the state is —
    that is what it is for — so a refusal from it must too. And it still reports
    `permissions_in_force`: the policy this server is enforcing is not affected
    by the file having been made unreadable, and it is the half a caller cannot
    get any other way once the document is unusable.
    """
    return with_config_status(
        {
            "tool": PROJECT_CONFIG_DESCRIBE,
            **error.to_dict(),
            "summary": (
                f"{error.summary} This server is still enforcing the configuration it loaded at startup, reported "
                "below as `permissions_in_force`; no key can be listed or changed while the file cannot be read."
            ),
            "document_source": "loaded_policy",
            "permissions_in_force": permission_summary(existing),
            "path": existing.config_path,
            "workspace_root": existing.workspace_root,
            "config_version": existing.config_version,
            "writable_keys": [],
            "locked_keys": [],
            "next_steps": [
                "Report `backend_error`: it is the parser's own message and names what has to be repaired.",
                "Ask the operator to repair the file at `config_status.path` and confirm it with `agentic-hil doctor`, "
                "which reads it fresh, before restarting the MCP server.",
                "Do not call `project_config_create` to replace it: it is refused here, and it would write a "
                "generated skeleton over whatever the operator was in the middle of writing.",
                f"{CONFIG_SHAPE_URI} describes the shape a configuration has to have.",
            ],
            "reference": CONFIG_SHAPE_URI,
            **NOT_STARTED,
            "cleanup_required": False,
        },
        status,
        prominent=True,
    )


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
                    "permission false and written by this server. A generation grants; a write only ever takes away, "
                    f"and this is a write. A person opens the new entry with `{CONFIG_REOPEN_COMMAND}`."
                ),
            }
        )
    return patterns


def _describe_next_steps(rights: dict[str, bool], writable: list[JsonObject], open_holds: JsonObject | None, status: JsonObject, *, on_disk: bool) -> list[str]:
    steps: list[str] = []
    if not on_disk:
        steps.append(
            "There is no file at the path this server loaded its configuration from, so `writable_keys` and "
            "`locked_keys` are empty and no value here can be compared against a document: everything below comes "
            "from what this process parsed at startup. `permissions_in_force` is the policy still being enforced. "
            "Ask the operator to put the file back at `config_status.path`, or to confirm the removal was intended, "
            "and then to restart the MCP server. Do not call `project_config_create` to replace it — it is refused, "
            "and it would write the generated skeleton over the operator's settings if it were not."
        )
    elif config_stale(status):
        steps.append(
            "The file has moved since this server loaded it, so the two halves of this answer come from two "
            "documents: the keys and values below are read from disk, and the three configuration-write grants "
            "under `rights` are the narrower of what this server loaded and what the file now says. One of those "
            "revoked since startup is already in force here; one added since startup is not, and will not be until "
            "the MCP server is restarted — a server bound to a policy does not widen itself from a file that "
            "changed underneath it. `permissions_in_force` is the whole loaded policy and is not intersected with "
            "the file: every hardware and per-device grant, and the section-level `debug.allow_all_symbols` and "
            "`artifacts.allow_upload`, keeps enforcing what startup parsed in either direction until a restart."
        )
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
            f"The permissions in this file are fixed for this call: {_right_key(CONFIG_PERMISSIONS_RIGHT)} is false, so "
            f"none of them can be changed through `{PROJECT_CONFIG_SET}` in either direction. A person reopens the file "
            f"with `{CONFIG_REOPEN_COMMAND}`."
        )
    else:
        steps.append(
            f"Permissions here can be narrowed and never widened through `{PROJECT_CONFIG_SET}`: a change may write "
            "`false` into one and never `true`, whichever key it names. Closing "
            f"{_right_key(CONFIG_PERMISSIONS_RIGHT)} is the last permission change this call can make, after which "
            f"`{CONFIG_REOPEN_COMMAND}` — or a regeneration through `project_config_create`, while "
            "`permissions.allow_config_write` is still true — is what writes permissions into this file again."
        )
    steps.append(f"{CONFIG_SHAPE_URI} describes what a configuration looks like and what deliberately cannot be changed over MCP.")
    return steps


__all__ = [
    "ACTOR_AGENT",
    "ACTOR_HUMAN",
    "GRANTING_PATH",
    "PERMISSION_COMMAND_VALUES",
    "PERMISSION_GRANT",
    "PERMISSION_REVOKE",
    "PROJECT_CONFIG_DESCRIBE",
    "PROJECT_CONFIG_SET",
    "authoritative_write_target",
    "deny_all_permissions",
    "description_view",
    "granted_rights",
    "load_config_document",
    "permission_delta",
    "permission_surface",
    "permission_widening",
    "project_config_describe",
    "project_config_set",
    "resolve_permission_key",
    "set_permission",
]
