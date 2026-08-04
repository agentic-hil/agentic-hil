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

import os
from contextlib import suppress
from pathlib import Path

from agentic_hil.config import (
    CONFIG_DIGEST_ALGORITHM,
    ConfigError,
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
# This configuration did not come from a file this process read, so there is
# nothing to compare against and no claim to make either way.
STATE_UNKNOWN = "unknown"

# The states in which the loaded policy and the file have definitely come apart.
# STATE_UNKNOWN is not one of them: it says the comparison could not be made, and
# calling that "stale" would put a claim on a result that nothing established.
STALE_STATES = frozenset({STATE_CHANGED, STATE_MISSING, STATE_UNREADABLE})

RUNNING_SERVER_SCOPE = "running_server"


def config_status(config: AgenticHILConfig | None) -> JsonObject:
    """What this server is enforcing, and whether the file still says the same.

    Cheap enough to run on every call: one small read and one hash, against tools
    that spawn debugger processes. It is run per call rather than cached against
    a stat, because a cache keyed on modification time inherits exactly the
    weakness the hash exists to remove.
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

    try:
        current = config_digest(safe_read_bytes(path))
    except (ConfigError, OSError, ValueError) as error:
        return _unreadable(base, error, path)

    if current == config.config_digest:
        return {
            **base,
            "state": STATE_UNCHANGED,
            "current_digest": current,
            "reload_required": False,
            "summary": "The authoritative configuration on disk is byte-for-byte the one this server loaded.",
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


def _unreadable(base: JsonObject, error: Exception, path: Path) -> JsonObject:
    """The file is gone, or it is there and will not open.

    Kept apart from "changed" because the remedy is not the same: a changed file
    is restarted onto, and a file that has disappeared has to be put back before
    anything can be restarted at all.
    """
    present = True
    with suppress(OSError):  # pragma: no cover - lexists swallows almost everything already
        present = os.path.lexists(path)
    missing = not present or isinstance(error, FileNotFoundError)
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
    "STALE_STATES",
    "STATE_CHANGED",
    "STATE_MISSING",
    "STATE_UNCHANGED",
    "STATE_UNKNOWN",
    "STATE_UNREADABLE",
    "config_stale",
    "config_status",
    "with_config_status",
]
