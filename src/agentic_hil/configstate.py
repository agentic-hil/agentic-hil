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

One thing does change what this module compares against, and it is not automatic:
``agentic_hil.configreload`` re-reads the *description* — devices, not grants —
when it is asked for by name, and moves ``config_digest`` onto the document it
took. After that, "unchanged" is a claim about the description in force, and the
permission half of the same configuration may be older. That is not hidden here:
``_permissions_source`` reports which document the grants came from whenever the
two have come apart, in every state, so a `unchanged` answer never means more
than it should.

Nothing here says what a restart would do either. That claim used to be made —
`changed` meant "a restart loads what is on disk now" — and holding it needed a
candidate validation that matched startup exactly: two code paths that have to
stay congruent, where every difference between them is a defect, and four were
found in four review rounds. The claim was cut back to the observation that
carries no such obligation (hardci-hq#95). The operator restarts; if the document
does not load, startup says why, with the message it already produces.
"""

from __future__ import annotations

import errno
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
# It hashes to something else. That is the whole of the claim: the file differs
# from the one this server loaded. Whether it would load is not asked, because
# answering it needs a second validation kept congruent with startup by hand.
STATE_CHANGED = "changed"
# It is gone. Kept apart from `changed` because the file has to be put back
# before there is anything to read at all, and the policy in force now exists
# only in this process.
STATE_MISSING = "missing"
# It is there and could not be read — a permission, a device error, bytes that
# are no longer UTF-8. Unknown, and unknown is not the same as unchanged.
STATE_UNREADABLE = "unreadable"
# This configuration did not come from a file this process read, so there is
# nothing to compare against and no claim to make either way.
STATE_UNKNOWN = "unknown"

# The states in which the loaded policy and the file have definitely come apart.
# STATE_UNKNOWN is not one of them: it says the comparison could not be made, and
# calling that "stale" would put a claim on a result that nothing established.
STALE_STATES = frozenset({STATE_CHANGED, STATE_MISSING, STATE_UNREADABLE})

RUNNING_SERVER_SCOPE = "running_server"


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
        return _unreadable(base, config, failure or FileNotFoundError(path))
    # Decoded, deliberately not to produce the digest: the digest is over the
    # exact bytes, because that is what was hashed at load time. The decode is
    # the same one `load_config` performs, and bytes that are not text are a file
    # that cannot be compared at all rather than one that differs — the same
    # class of answer as a file that will not open.
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        return _unreadable(base, config, error)
    current = config_digest(raw)

    divergence = _permissions_source(config)
    if current == config.config_digest:
        return {
            **base,
            "state": STATE_UNCHANGED,
            "current_digest": current,
            "reload_required": False,
            "summary": (
                (
                    "The authoritative configuration on disk is byte-for-byte the one whose description this server "
                    f"is enforcing. {divergence['permissions_source']['summary']}"
                )
                if divergence
                else "The authoritative configuration on disk is byte-for-byte the one this server loaded."
            ),
            **divergence,
        }
    return {
        **base,
        "state": STATE_CHANGED,
        "current_digest": current,
        "reload_required": True,
        "error_type": CONFIG_STALE_ERROR,
        "summary": (
            "The authoritative configuration on disk is not the one this server loaded. Every answer from this server, "
            "including which debugger backend it names and which permissions it enforces, still comes from the version "
            "loaded at startup; it is not reloaded while the server runs. Restart the MCP server to bind it to the file "
            "that is there now; if that document does not load, the restart says so with the reason."
        ),
        **remediation_fields(CONFIG_STALE_ERROR),
        **divergence,
    }


def _permissions_source(config: AgenticHILConfig) -> JsonObject:
    """Whether the grants in force came from a different document than the description.

    Empty for every configuration that was parsed in one piece, which is every
    one this process loads from a file — both digests are then the same bytes and
    there is nothing to distinguish. They come apart only through a description
    reload (``agentic_hil.configreload``) whose file also carried different
    permissions, and then the divergence is exactly what must not disappear: the
    description in force is the file's, so the state above is `unchanged` and
    `config_stale` is false, and calling it a day there would hide the half that
    is still the startup document's.

    This says which document the permissions came from and nothing about how the
    two documents' permissions differ, for the same reason the digest comparison
    above claims nothing about content: the startup document is no longer on disk
    to be compared against. What the two files' grants actually differed by was
    reported by the reload that produced the divergence, at the moment both were
    in hand.
    """
    if not config.permissions_digest or not config.config_digest or config.permissions_digest == config.config_digest:
        return {}
    return {
        "permissions_source": {
            "digest": config.permissions_digest,
            "loaded_at": config.loaded_at or None,
            "description_reloaded_at": config.description_reloaded_at or None,
            "summary": (
                "The permissions it enforces are not this file's: they come from the document this server parsed at "
                "startup, which is what a description reload leaves alone. Restart the MCP server to adopt the "
                "permissions this file states; nothing short of that adopts them."
            ),
        }
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


def _unreadable(base: JsonObject, config: AgenticHILConfig, error: Exception) -> JsonObject:
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
        # Carried here too: which document the grants came from is a fact about
        # this process, not about the file, so a file that has gone away or
        # stopped opening does not make it unanswerable.
        **_permissions_source(config),
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
    "read_config_snapshot",
    "with_config_status",
]
