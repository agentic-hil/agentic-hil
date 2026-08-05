"""Whether the configuration this server enforces is still the one on disk.

A running server parses the authoritative configuration once and answers out of
it until it exits. That is deliberate — a policy must not change under a held
board — but it was silent, and silence is what made it a defect: an operator who
switched a debugger from `stlink` to `pyocd` got `pyocd` from the CLI and
`stlink` from `debugger_info` in the same minute, with nothing in either answer
to say which of them was reading the file that exists now.

The authoritative configuration is the permission boundary, so a server serving
an older one is enforcing a policy nobody has chosen any more. That it happened
to be the narrower one that time is luck and not a property.

This module answers only that question, and it answers it about one file: the
one this server loaded. Whether that file is the workspace's authoritative
location at all is a different condition with a different remedy, decided by
``configwrite._authoritative_path``, and the two are deliberately not folded into
one message — "your file changed" and "you are bound to the wrong file" send an
operator to different places.

Nothing here reloads anything. A changed file is reported and still not obeyed,
because the grants this server runs under were taken as a whole and a document
that appeared underneath it may not widen them; the one narrowing that does track
the file is ``configwrite.granted_rights``, which takes the narrower of the two
sides and can therefore only ever take a right away.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import yaml

from agentic_hil.config import (
    CONFIG_DIGEST_ALGORITHM,
    ConfigError,
    UniqueKeyLoader,
    candidate_startup_error,
    config_digest,
    safe_read_bytes,
    utc_now,
)
from agentic_hil.knowledge import CONFIG_STALE_ERROR, remediation_fields
from agentic_hil.types import AgenticHILConfig, JsonObject

# The file on disk hashes to what this server parsed. Nothing to report.
STATE_UNCHANGED = "unchanged"
# It hashes to something else. The server is still enforcing what it loaded.
STATE_CHANGED = "changed"
# It is gone. Neither "unchanged" nor an ordinary "changed": there is no
# document to restart onto, and the policy in force now exists only in memory.
STATE_MISSING = "missing"
# It is there and could not be read — an ACL, a device error, bytes that are no
# longer UTF-8. Unknown, and unknown is not the same as unchanged.
STATE_UNREADABLE = "unreadable"
# It is there, it is readable, it is not what was loaded — and it will not load.
# Kept apart from `changed` because `changed` carries "restart the server to pick
# this up", and that instruction is false here: the restart fails and the
# operator is left with a server that will not come back rather than one serving
# an older policy. The repair comes first, and the message has to say so.
STATE_INVALID = "invalid"
# This configuration did not come from a file this process read, so there is
# nothing to compare against and no claim to make either way.
STATE_UNKNOWN = "unknown"

# The states in which the loaded policy and the file have definitely come apart.
# STATE_UNKNOWN is not one of them: it says the comparison could not be made, and
# calling that "stale" would put a claim on a result that nothing established.
STALE_STATES = frozenset({STATE_CHANGED, STATE_INVALID, STATE_MISSING, STATE_UNREADABLE})

RUNNING_SERVER_SCOPE = "running_server"

CONFIG_INVALID_ERROR = "config_invalid"


def read_config_snapshot(path: str | Path) -> tuple[bytes | None, Exception | None]:
    """One securely opened read of a configuration, bytes or the failure.

    Exists so that a caller which needs both the status of a file and the
    document in it takes *one* snapshot and answers out of it. Two reads can
    straddle an edit, and then one answer describes two versions of the file —
    `state: unchanged` beside values that came from a document that is not the
    unchanged one.
    """
    try:
        return safe_read_bytes(Path(path)), None
    except (ConfigError, OSError, ValueError) as error:
        return None, error


def _load_blocking_error(raw: bytes, config: AgenticHILConfig) -> Exception | None:
    """Why a restart onto these bytes would not produce a running server.

    The YAML parse plus ``config.candidate_startup_error`` — the startup loader's
    own checks, called rather than approximated. It used to stop after the shipped
    schema, and the schema is not the loader; then it stopped after
    ``validate_config_document``, and the document rules are not the loader
    either. Both left a file that will not load classified `changed`, whose
    remediation is "restart the server to pick this up" — advice that shuts down a
    working server and cannot bring it back. ``reports.directory: ../outside`` is
    the plainest case: schema-valid, document-valid, and refused by
    ``pin_configured_paths`` at every startup there will ever be.

    So the whole of the loader runs here now, minus its one side effect: startup
    creates ``state_root`` before walking its ACLs, and a question about a document
    that is not in force may not put a directory on the machine. That part of the
    chain which already exists is walked; the part that does not is left unjudged
    rather than guessed at. What survives is the only remaining way a `changed`
    verdict can be optimistic, and it is bounded to one thing that startup
    creates for itself.

    Run only when the digest already differs, so an unchanged file still costs one
    read and one hash.
    """
    try:
        loaded: Any = yaml.load(raw.decode("utf-8"), Loader=UniqueKeyLoader)
    except (yaml.YAMLError, UnicodeDecodeError) as error:
        return error
    # `loaded or {}` exactly as load_config does it, so an empty file gets the
    # same refusal here as it would at startup rather than a different one.
    return candidate_startup_error(loaded or {}, str(config.config_path), config)


def config_status(config: AgenticHILConfig | None, *, snapshot: tuple[bytes | None, Exception | None] | None = None) -> JsonObject:
    """What this server is enforcing, and whether the file still says the same.

    Cheap enough to run on every call: one small read and one hash, against tools
    that spawn debugger processes. It is run per call rather than cached against
    a stat, because a cache keyed on modification time inherits exactly the
    weakness the hash exists to remove.

    ``snapshot`` is a read a caller has already taken, from
    ``read_config_snapshot``. Passing it is how a caller that also parses the
    document guarantees this status and that document are the same bytes.
    """
    if config is None:
        return {
            "state": STATE_UNKNOWN,
            "path": None,
            "digest_algorithm": CONFIG_DIGEST_ALGORITHM,
            "loaded_digest": None,
            "current_digest": None,
            "loaded_at": None,
            "checked_at": utc_now(),
            "reload_required": False,
            "summary": "This server has no configuration loaded, so there is nothing to compare against the file on disk.",
        }

    path = Path(config.config_path)
    base: JsonObject = {
        "path": str(path),
        "digest_algorithm": CONFIG_DIGEST_ALGORITHM,
        "loaded_digest": config.config_digest or None,
        "loaded_at": config.loaded_at or None,
        "checked_at": utc_now(),
    }
    if not config.config_digest:
        return {
            **base,
            "state": STATE_UNKNOWN,
            "current_digest": None,
            "reload_required": False,
            "summary": (
                "This configuration was not parsed from a file this process read, so whether the file on disk still "
                "says the same cannot be established."
            ),
        }

    raw, failure = snapshot if snapshot is not None else read_config_snapshot(path)
    if failure is not None or raw is None:
        return _unreadable(base, failure or FileNotFoundError(path))
    # Decoded, deliberately not to produce the digest: the digest is over the
    # exact bytes, because that is what was hashed at load time. The decode is
    # the same one `load_config` performs, so a file that is no longer UTF-8 is a
    # file no restart can load — which makes it unreadable rather than a
    # `changed` document with a restart remedy that cannot work.
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        return _unreadable(base, error)
    current = config_digest(raw)

    if current == config.config_digest:
        return {
            **base,
            "state": STATE_UNCHANGED,
            "current_digest": current,
            "reload_required": False,
            "summary": "The authoritative configuration on disk is byte-for-byte the one this server loaded.",
        }
    # Only now, on a file that has actually moved: whether a restart onto it
    # would produce a running server at all. Saying "restart to load this" about
    # a document that will not load is worse than saying nothing — it sends an
    # operator to shut down a server that is enforcing a policy and cannot come
    # back with one.
    blocking = _load_blocking_error(raw, config)
    if blocking is not None:
        return {
            **base,
            "state": STATE_INVALID,
            "current_digest": current,
            "reload_required": True,
            "error_type": CONFIG_INVALID_ERROR,
            "backend_error": str(blocking),
            "summary": (
                "The authoritative configuration has changed since this server loaded it and the file that is there "
                "now does not load: it is not valid YAML, it does not satisfy the configuration schema, or it fails one "
                "of the checks the startup loader runs — a document rule, the workspace it is bound to, an output path "
                "that leaves the workspace, a configured executable that is not installed, the state_root trust rules. "
                "`backend_error` says which. This server keeps "
                "enforcing the version it loaded at startup, and a restart would not replace it — it would fail. "
                "Repair the file first, then restart."
            ),
            **remediation_fields(CONFIG_INVALID_ERROR, RUNNING_SERVER_SCOPE),
        }
    return {
        **base,
        "state": STATE_CHANGED,
        "current_digest": current,
        "reload_required": True,
        "error_type": CONFIG_STALE_ERROR,
        "summary": (
            "The authoritative configuration has changed since this server loaded it. Every answer from this server, "
            "including which debugger backend it names and which permissions it enforces, still comes from the version "
            "loaded at startup; it is not reloaded while the server runs. Restart the MCP server to serve the file "
            "that exists now."
        ),
        **remediation_fields(CONFIG_STALE_ERROR),
    }


def _is_missing(error: Exception) -> bool:
    """Whether this failure means the file is not there, rather than not readable.

    Only the error that originated says so. An earlier version asked
    ``os.path.lexists`` as a second opinion, and that call answers ``False`` for
    every stat failure it meets — an ancestor that is not a directory, a
    directory that cannot be traversed — so a file that exists and needs its path
    repaired was reported as one that had been deleted, with restore-the-file
    remediation that fixes nothing.
    """
    if isinstance(error, FileNotFoundError):
        return True
    if isinstance(error, ConfigError):
        return error.error_type == "config_file_not_found"
    return getattr(error, "errno", None) == errno.ENOENT


def _unreadable(base: JsonObject, error: Exception) -> JsonObject:
    """The file is gone, or it is there and will not open.

    Kept apart from "changed" because the remedy is not the same: a changed file
    is restarted onto, and a file that has disappeared has to be put back before
    anything can be restarted at all.
    """
    missing = _is_missing(error)
    error_type = "config_file_not_found" if missing else "config_unreadable"
    return {
        **base,
        "state": STATE_MISSING if missing else STATE_UNREADABLE,
        "current_digest": None,
        "reload_required": True,
        "error_type": error_type,
        "backend_error": str(error),
        "summary": (
            "The authoritative configuration this server loaded is no longer at its path. The policy being enforced "
            "now exists only in this process, and no restart can restore it until the file is back."
            if missing
            else "The authoritative configuration this server loaded can no longer be read, so whether it still says "
            "the same cannot be established. The server keeps enforcing the version it loaded at startup."
        ),
        **remediation_fields(error_type, RUNNING_SERVER_SCOPE),
    }


def config_stale(status: JsonObject) -> bool:
    return status.get("state") in STALE_STATES


def with_config_status(result: JsonObject, status: JsonObject, *, prominent: bool = False) -> JsonObject:
    """Attach what this answer was decided by.

    ``prominent`` is for the two answers the issue names — `debugger_info` and
    `doctor`. They carry the block whatever the state is, so that "this is the
    configuration in force" is a positive statement in the place an operator
    compares two backends, and a divergence is said in the summary rather than
    only in a field further down. Every other answer carries it exactly when
    there is something to say.
    """
    if not isinstance(result, dict) or "config_status" in result:
        return result
    stale = config_stale(status)
    if not prominent and status.get("state") == STATE_UNCHANGED:
        return result
    amended: JsonObject = {**result, "config_status": status}
    if stale:
        amended["config_stale"] = True
        if prominent:
            summary = str(result.get("summary") or "").strip()
            amended["summary"] = f"{summary} {status['summary']}".strip()
    return amended


__all__ = [
    "CONFIG_INVALID_ERROR",
    "STALE_STATES",
    "STATE_CHANGED",
    "STATE_INVALID",
    "STATE_MISSING",
    "STATE_UNCHANGED",
    "STATE_UNKNOWN",
    "STATE_UNREADABLE",
    "config_stale",
    "config_status",
    "read_config_snapshot",
    "with_config_status",
]
