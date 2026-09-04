from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from agentic_hil.config import (
    ConfigError,
    atomic_write_text,
    display_path,
    project_state_directory,
    resolve_work_path,
    safe_append_text,
    safe_configured_directory,
    safe_directory,
    safe_file_lock,
    safe_file_path,
    safe_read_bytes,
    safe_read_text,
    safe_write_text,
)
from agentic_hil.configstate import config_stale, config_status
from agentic_hil.redact import filesystem_error_detail
from agentic_hil.types import AgenticHILConfig, JsonObject

_GENESIS_DIGEST = "0" * 64


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp_for_filename() -> str:
    return utc_now_iso().replace("-", "").replace(":", "").replace(".", "")


def reports_directory(config: AgenticHILConfig) -> str:
    return safe_configured_directory(config, config.reports.directory, "reports.directory")


def logs_directory(config: AgenticHILConfig) -> str:
    return safe_configured_directory(config, config.logs.directory, "logs.directory")


def append_jsonl(log_path: str, event: JsonObject, config: AgenticHILConfig | None = None) -> Exception | None:
    entry = dict(event)
    entry.setdefault("time", utc_now_iso())
    line = json.dumps(entry) + "\n"
    # The canonical trusted copy is written FIRST: if the untrusted workspace
    # mirror is the only thing that succeeds we must still fail the audit, and
    # if canonical write fails we never claim the effect was recorded.
    if config is not None:
        canonical_error = append_canonical_audit_log(config, log_path, line)
        if canonical_error is not None:
            return canonical_error
    try:
        safe_append_text(log_path, line)
    except (ConfigError, OSError, ValueError) as error:
        return error
    return None


def append_jsonl_audited(config: AgenticHILConfig, log_path: str, event: JsonObject) -> Exception | None:
    """append_jsonl for agent-initiated hardware-effect events: mirrors the line
    into the trusted canonical ledger under state_root as well as the untrusted
    workspace log."""
    return append_jsonl(log_path, event, config)


def audit_logs_directory(config: AgenticHILConfig) -> Path:
    return safe_directory(project_state_directory(config) / "audit-logs")


def canonical_audit_log_path(config: AgenticHILConfig, log_path: str | Path) -> Path:
    return audit_logs_directory(config) / safe_filename(Path(log_path).name)


def _canonical_sidecar_path(canonical_path: Path) -> Path:
    return canonical_path.with_name(canonical_path.name + ".digest")


def _canonical_lock_path(config: AgenticHILConfig, canonical_path: Path) -> str:
    return str(audit_logs_directory(config) / (canonical_path.name + ".lock"))


def _read_canonical_sidecar(canonical_path: Path) -> JsonObject:
    try:
        text = safe_read_text(_canonical_sidecar_path(canonical_path))
    except FileNotFoundError:
        return {"sequence": 0, "chain_sha256": _GENESIS_DIGEST, "bytes": 0}
    try:
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("coordination_state_invalid", "Canonical audit ledger is corrupted.", {"path": str(_canonical_sidecar_path(canonical_path))}) from error
    if not isinstance(data, dict) or not isinstance(data.get("sequence"), int) or not isinstance(data.get("chain_sha256"), str) or not isinstance(data.get("bytes"), int):
        raise ConfigError("coordination_state_invalid", "Canonical audit ledger has an invalid format.", {"path": str(_canonical_sidecar_path(canonical_path))})
    return data


def append_canonical_audit_log(config: AgenticHILConfig, log_path: str | Path, text: str) -> Exception | None:
    """Append a line to the trusted canonical audit copy under state_root and
    advance its tamper-evident hash chain and sequence. Returns an error instead
    of raising so callers fold it into their existing audit-failure handling."""
    canonical = canonical_audit_log_path(config, log_path)
    try:
        with safe_file_lock(_canonical_lock_path(config, canonical)):
            sidecar = _read_canonical_sidecar(canonical)
            try:
                actual_size = os.path.getsize(canonical)
            except FileNotFoundError:
                actual_size = 0
            if actual_size != int(sidecar["bytes"]):
                # A prior append advanced the file but not the sidecar (or vice
                # versa). Fail closed rather than extend a chain that no longer
                # matches its own log.
                raise ConfigError("coordination_state_invalid", "Canonical audit ledger size disagrees with its digest sidecar.", {"path": str(canonical), "recorded_bytes": int(sidecar["bytes"]), "actual_bytes": actual_size})
            safe_append_text(str(canonical), text)
            chain = hashlib.sha256((str(sidecar["chain_sha256"]) + text).encode("utf-8")).hexdigest()
            atomic_write_text(
                _canonical_sidecar_path(canonical),
                json.dumps({"sequence": int(sidecar["sequence"]) + 1, "chain_sha256": chain, "bytes": int(sidecar["bytes"]) + len(text.encode("utf-8"))}, indent=2) + "\n",
            )
    except (ConfigError, OSError, ValueError) as error:
        return error
    return None


def write_audit_log(config: AgenticHILConfig, log_path: str, content: str) -> Exception | None:
    """Full-file audit log write (e.g. the GDB session snapshot) that also
    refreshes the canonical trusted copy and its digest."""
    canonical = canonical_audit_log_path(config, log_path)
    try:
        with safe_file_lock(_canonical_lock_path(config, canonical)):
            sidecar = _read_canonical_sidecar(canonical)
            # Canonical copy lives under the trusted state_root, so it is written
            # with the state-root primitive, not the workspace-scoped one.
            atomic_write_text(canonical, content)
            chain = hashlib.sha256(content.encode("utf-8")).hexdigest()
            atomic_write_text(
                _canonical_sidecar_path(canonical),
                json.dumps({"sequence": int(sidecar["sequence"]) + 1, "chain_sha256": chain, "bytes": len(content.encode("utf-8"))}, indent=2) + "\n",
            )
        safe_write_text(config, log_path, content)
    except (ConfigError, OSError, ValueError) as error:
        return error
    return None


def _is_ordered_subsequence(needles: list[str], haystack: list[str]) -> bool:
    """True when every element of `needles` appears in `haystack` in order."""
    iterator = iter(haystack)
    return all(any(candidate == needle for candidate in iterator) for needle in needles)


def canonical_audit_evidence(config: AgenticHILConfig, log_path: str | Path) -> JsonObject:
    """Trusted evidence pointer for a workspace log: canonical path, sequence,
    chain digest, and whether the untrusted workspace mirror still matches.

    The canonical copy is the trusted ledger of agent-initiated hardware EFFECTS.
    The workspace mirror may legitimately hold extra passive lines (e.g. COM RX
    feedback) that are not mirrored, so verification checks that every canonical
    line is still present in the workspace log in order: deletion or alteration
    of an effect record fails; extra passive lines are tolerated."""
    canonical = canonical_audit_log_path(config, log_path)
    canonical_exists = Path(canonical).exists()
    try:
        sidecar = _read_canonical_sidecar(canonical)
        canonical_bytes = safe_read_bytes(canonical)
    except FileNotFoundError:
        return {"canonical_log_present": False, "workspace_log_verified": False}
    except (ConfigError, OSError):
        # The trusted anchor itself is unreadable/corrupt: report it distinctly
        # from "no canonical log" so it is never mistaken for benign absence.
        return {"canonical_log_present": canonical_exists, "canonical_log_corrupted": True, "workspace_log_verified": False}
    evidence: JsonObject = {
        "canonical_log_present": True,
        # Emit only the log's basename: the absolute canonical path lives under
        # the (environment-derived) state_root and must not reach an operator or
        # MCP sink. Presence, sequence, and chain digest are the evidence.
        "canonical_log_name": Path(canonical).name,
        "log_sequence": int(sidecar["sequence"]),
        "log_chain_sha256": str(sidecar["chain_sha256"]),
    }
    try:
        workspace_bytes = safe_read_bytes(Path(log_path))
    except (FileNotFoundError, ConfigError, OSError):
        evidence["workspace_log_verified"] = False
        return evidence
    canonical_lines = canonical_bytes.decode("utf-8", errors="replace").splitlines()
    workspace_lines = workspace_bytes.decode("utf-8", errors="replace").splitlines()
    evidence["workspace_log_verified"] = _is_ordered_subsequence(canonical_lines, workspace_lines)
    return evidence


def attach_canonical_audit_evidence(config: AgenticHILConfig, result: JsonObject) -> JsonObject:
    """Attach trusted canonical-log evidence to a report/classification result so
    callers can detect a missing or tampered workspace log and fall back to the
    canonical copy under state_root."""
    display_log = result.get("log_path")
    if not isinstance(display_log, str) or not display_log:
        return result
    try:
        workspace_log = resolve_work_path(config, display_log)
    except (ConfigError, OSError, ValueError):
        workspace_log = display_log
    evidence = canonical_audit_evidence(config, workspace_log)
    if not evidence.get("canonical_log_present"):
        return result
    return {**result, "canonical_audit": evidence}


def safe_filename(value: str, fallback: str = "item") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def last_report_path(config: AgenticHILConfig) -> str:
    return str(Path(reports_directory(config)) / "last-report.json")


def last_failure_path(config: AgenticHILConfig) -> str:
    return str(Path(reports_directory(config)) / "last-failure.json")


def report_lock_path(config: AgenticHILConfig) -> str:
    return str(report_state_directory(config) / ".report-state.lock")


def report_state_path(config: AgenticHILConfig) -> str:
    return str(report_state_directory(config) / "report-state.json")


def report_state_directory(config: AgenticHILConfig) -> Path:
    return safe_directory(project_state_directory(config) / "reports")


def run_reports_directory(config: AgenticHILConfig) -> Path:
    return safe_directory(report_state_directory(config) / "runs")


def canonical_run_report_path(config: AgenticHILConfig, handle: str) -> Path:
    """Where this run's own report is kept, under the trusted state root.

    One file per run handle, and nothing ever writes over it: that is the whole
    point of it existing beside ``last-report.json``, which the next run
    replaces. A bench collecting the evidence of three runs used to keep one of
    them and find out only when the third had already overwritten the first."""
    return run_reports_directory(config) / f"{safe_filename(handle, 'run')}.json"


# The field naming that file, and the sentence the run's summary carries it in.
CANONICAL_REPORT_KEY = "canonical_report_path"

# The marker on the non-success placeholder the per-run copy is staged as before
# the transaction commits. Its presence, and the audit_ok=False that rides with
# it, are what let an auditor tell a staged-but-uncommitted copy from the green
# success it is only promoted to once every other required write has landed.
CANONICAL_PENDING_KEY = "canonical_write_pending"


def staged_report_snapshot(snapshot: JsonObject) -> JsonObject:
    """The non-green pending form of a run snapshot.

    Byte-distinct from the green document on exactly the fields that decide
    success: ``audit_ok`` is turned off and a ``canonical_write_pending`` marker
    is added, so a trusted record holding this *before* the transaction commits
    reads as unfinished rather than as the success it will only become once every
    other required write has landed and it is promoted in place. Both trusted
    records use it -- the per-run copy on disk and the ``report-state.json``
    readback -- so neither can read green until the green document is promoted as
    the very last write, and no failure before that, nor any failure of a
    best-effort cleanup, can leave a success the returned result rejected."""
    marker = {
        "error_type": "canonical_write_pending",
        "summary": "This per-run report copy was staged but not yet committed as a success.",
    }
    return {**snapshot, "audit_ok": False, CANONICAL_PENDING_KEY: True, "audit_error": marker, "audit_errors": [marker]}


def staged_canonical_document(snapshot: JsonObject) -> str:
    """The staged per-run copy as bytes on disk. See ``staged_report_snapshot``."""
    return json.dumps(staged_report_snapshot(snapshot), indent=2) + "\n"


def canonical_run_report(config: AgenticHILConfig, report: JsonObject) -> Path | None:
    """This report's per-run copy, or None for a report that is not a run's.

    Keyed on ``run``, which the reactor puts on every report it commits and
    nothing else sets: a per-run copy of a report belonging to no run would be a
    file with no reader and a state root that grows with every hardware call."""
    handle = report.get("run")
    if not isinstance(handle, str) or not handle:
        return None
    return canonical_run_report_path(config, handle)


def naming_canonical_report(report: JsonObject, canonical: Path) -> JsonObject:
    """``report`` with the per-run path in a field and in its summary.

    Both, because the two are read by different people. A caller parsing the
    document wants the field; an operator reading the one line a run prints at
    the end wants it said there, which is the moment they still know which run
    it was. Idempotent, so a report re-committed after a terminal lease
    transition keeps one sentence rather than gaining another.

    The absolute path is stated rather than withheld, which is the opposite of
    what ``canonical_audit_evidence`` does with the ledger's path beside it.
    They answer different questions: the ledger's evidence is its presence, its
    sequence and its chain digest, and naming the file adds nothing a reader
    needs; here the path *is* the answer, because the question is where the runs
    before this one went. It is the same environment-derived kind of path as
    ``config_in_force.path``, which a report has always carried."""
    if CANONICAL_REPORT_KEY in report:
        return report
    summary = str(report.get("summary") or "").rstrip()
    sentence = f"This run's own report is kept at {canonical}; later runs do not overwrite it."
    return {**report, CANONICAL_REPORT_KEY: str(canonical), "summary": f"{summary} {sentence}" if summary else sentence}


CONFIG_IN_FORCE_KEY = "config_in_force"


def config_in_force(config: AgenticHILConfig) -> JsonObject:
    """Which configuration this action was decided by, and whether disk still agreed.

    A report that names only the path of the authoritative configuration says
    nothing about the policy that was in force: the path is constant over a
    project's lifetime and the content is not. "Under which permission did this
    happen" is the question an audit trail exists to answer, and for a flash or a
    mass erase it is the only one that matters.

    The digest is ``AgenticHILConfig.config_digest`` (the exact bytes this
    server parsed and is enforcing), published in the spelling ``config_status``
    already uses, so the record and a live answer can be compared directly and
    the fact has one name. It is deliberately *not* the coordinator's
    ``config_sha256``: that one answers a different question ("is the file the
    same now as when this lease was taken", which is what ``recover`` compares),
    it is published in a different spelling (the bare hex the lease records on
    disk carry, not the algorithm-prefixed one ``config_status`` uses), and a
    report is written on paths that never took a lease at all. It is derived from
    these same parsed bytes; it used to come from a second
    read at coordinator construction and could, under the very race this record
    exists for, name a document nobody parsed.

    ``file_state`` is the comparison made when the report was written, which is
    inside the action rather than after it. `changed`, `missing` and `unreadable`
    each say the loaded version had already come apart from the file at that
    moment, so whatever is at the path now is not evidence of the policy this
    action ran under.
    """
    status = config_status(config)
    evidence: JsonObject = {
        "path": status.get("path"),
        "digest_algorithm": status.get("digest_algorithm"),
        "digest": status.get("loaded_digest"),
        "description_source": status.get("description_source"),
        "file_state": status.get("state"),
        "file_digest": status.get("current_digest"),
        # `configstate`'s own predicate, so "diverged" means here exactly what it
        # means in a live result. In particular `unknown` is not divergence (it
        # is a comparison that was never made), and `file_state` says which.
        "diverged_from_file": config_stale(status),
        "checked_at": status.get("checked_at"),
    }
    permissions_source = status.get("permissions_source")
    if isinstance(permissions_source, dict):
        # After a description reload the grants in force are still the startup
        # document's while `digest` names the reloaded one. The question this
        # record exists for is about permissions, so that divergence travels with
        # it rather than being left to a reader to infer.
        evidence["permissions_source"] = permissions_source
    return evidence


def write_report(config: AgenticHILConfig, report: JsonObject) -> JsonObject:
    enriched = dict(report)
    enriched.setdefault("audit_ok", True)
    # setdefault, not assignment: `recommit_report_with_status` and the adoption
    # path commit the same report again after a terminal lease transition, and
    # the version an action ran under must not be restamped by a later check.
    # That would replace the one fact the record is for with a fact about the
    # re-commit.
    enriched.setdefault(CONFIG_IN_FORCE_KEY, config_in_force(config))
    # The per-run copy, staged non-green on disk and tracked here so it can be
    # promoted once every other required write has committed. Because the green
    # document is written last of all, no failure after it, and no failure of a
    # best-effort cleanup, can leave a green trusted record the live call
    # rejected: an auditor enumerating the canonical archive can never find a
    # success the returned result turned around and rejected as unaudited.
    committed_canonical: Path | None = None
    try:
        report_path = last_report_path(config)
        display_report_path = display_path(config, report_path)
        canonical = canonical_run_report(config, enriched)
        with safe_file_lock(report_lock_path(config)):
            state = load_or_initialize_report_state(config)
            snapshot_allowed = True
            if is_failure_report(enriched):
                try:
                    safe_write_text(config, last_failure_path(config), json.dumps(enriched, indent=2) + "\n")
                except (ConfigError, OSError, ValueError) as error:
                    enriched = mark_audit_failure(enriched, error)
                    snapshot_allowed = False
            if snapshot_allowed:
                snapshot = {**enriched, "report_path": display_report_path}
                if canonical is not None:
                    snapshot = naming_canonical_report(snapshot, canonical)
                document = json.dumps(snapshot, indent=2) + "\n"
                try:
                    # The per-run copy first, so the untrusted workspace mirror
                    # never stands where the trusted copy does not. It is staged
                    # as a non-green placeholder, though, and only promoted to the
                    # identical green document once the mirror and the report
                    # state have both landed: the two can still be compared by
                    # digest after everything commits, while any failure in
                    # between leaves at worst a non-green placeholder rather than
                    # a false success.
                    if canonical is not None:
                        atomic_write_text(canonical, staged_canonical_document(snapshot))
                        committed_canonical = canonical
                    safe_write_text(config, report_path, document)
                except (ConfigError, OSError, ValueError) as error:
                    enriched = mark_audit_failure(enriched, error)
                else:
                    enriched = snapshot
            # The trusted readback (`report-state.json`) and the per-run copy move
            # in lockstep. Whenever a per-run copy has been staged for promotion,
            # the readback is written non-green too, so a promotion that fails
            # together with its corrective rewrite cannot leave `read_last_report`
            # green for a call that returned `audit_ok: false` (review round 0,
            # finding 1). The readback is turned green only once the green document
            # is promoted, and that promotion is the last write.
            promoting = committed_canonical is not None and enriched.get("audit_ok") is not False
            readback = staged_report_snapshot(enriched) if promoting else enriched
            state["last_report"] = readback
            # The failure record advances on the run's own verdict, never on the
            # readback. The staging placeholder is non-green by construction, so
            # reading the record off it filed every promoted success as the last
            # failure and discarded the real one an operator looks up after a red
            # run (#411). `enriched` is that verdict: a run that genuinely failed
            # is recorded here with its own error type, and a success records
            # nothing, leaving the previous failure standing.
            if is_failure_report(enriched):
                state["last_failure"] = enriched
            try:
                write_report_state(config, state)
            except (ConfigError, OSError, ValueError) as error:
                # `write_report_state` renames the new document into place and only
                # then fsyncs the parent directory, so a failure raised here may
                # already have committed the requested state: the same post-replace
                # ambiguity the promotion below resolves by reading disk. Resolve it
                # the same way here. When the state we asked for is on disk only the
                # durability fsync failed, so the transaction continues unchanged.
                # When it is not, this run's state never landed, and a run whose
                # state write fails is a genuinely failed run: mark it audit-failed,
                # drop the paths it can no longer prove, and record it as both the
                # last report and the last failure, so an operator's failure lookup
                # is never left stale for a call that returned `audit_ok: false`
                # (review round 1, finding 1). The promotion below must not run on an
                # audit-failed run, so it is disabled here.
                if _report_state_on_disk(config) != json.dumps(state, indent=2) + "\n":
                    enriched = mark_audit_failure(enriched, error)
                    enriched.pop("report_path", None)
                    enriched.pop(CANONICAL_REPORT_KEY, None)
                    promoting = False
                    state["last_report"] = enriched
                    state["last_failure"] = enriched
                    write_report_state(config, state)
            if promoting:
                # `atomic_write_bytes` renames the finished document into place and
                # only then fsyncs the parent directory, so a failure raised here
                # may already have committed the green document: the rename landed
                # and the later durability fsync did not. It is not safe to assume
                # the failed write left the placeholder in place, so the trusted
                # copy is read back and what is on disk decides.
                canonical_committed = True
                try:
                    atomic_write_text(committed_canonical, document)
                except (ConfigError, OSError, ValueError) as error:
                    if _canonical_on_disk(committed_canonical) != document:
                        # The placeholder never became green. Mark the result
                        # audit-failed as for any required write; the readback was
                        # staged non-green above, so even a failed corrective
                        # rewrite below leaves it non-green, never a false green.
                        canonical_committed = False
                        enriched = mark_audit_failure(enriched, error)
                        enriched.pop("report_path", None)
                        enriched.pop(CANONICAL_REPORT_KEY, None)
                if canonical_committed:
                    enriched = snapshot
                # Reconcile the readback to the resolved verdict. It can only ever
                # *become* green when the green per-run copy already exists on disk,
                # so this write can never expose a green readback the result would
                # reject. When the copy committed green the run is the success that
                # trusted per-run copy records, so a failure to refresh the readback
                # here only leaves it showing the staged placeholder -- a safe
                # under-report -- and must not turn the returned result
                # audit-failed. When the copy did not commit green the readback is
                # non-green either way, so a failure is raised to the outer handler
                # like any other required write.
                state["last_report"] = enriched
                if is_failure_report(enriched):
                    state["last_failure"] = enriched
                try:
                    write_report_state(config, state)
                except (ConfigError, OSError, ValueError):
                    if not canonical_committed:
                        raise
    except (ConfigError, OSError, ValueError) as error:
        failed = mark_audit_failure(enriched, error)
        # report_path can't be trusted after a failed commit: strip it so the
        # result never advertises a path whose persisted content it cannot prove.
        # The per-run copy is stripped for the same reason and by the same rule.
        failed.pop("report_path", None)
        failed.pop(CANONICAL_REPORT_KEY, None)
        # A staged placeholder can still be on disk; reconcile it to the
        # audit-failed result so the leftover record names the real error. It is
        # already non-green, so this reconciliation is for accuracy only, not for
        # the fail-safe guarantee, and a failure of it cannot leave a false green.
        _revoke_stale_canonical(committed_canonical, failed)
        return failed
    if committed_canonical is not None and enriched.get("audit_ok") is False:
        # A required write after the placeholder failed and turned the returned
        # result audit-failed while the staged placeholder is still on disk.
        # Reconcile it to what is returned. The placeholder is non-green already,
        # so even a failed reconciliation cannot leave a false success.
        _revoke_stale_canonical(committed_canonical, {key: value for key, value in enriched.items() if key != CANONICAL_REPORT_KEY})
    return enriched


def _canonical_on_disk(canonical: Path) -> str | None:
    """The per-run copy's current text, or ``None`` when it cannot be read.

    Used only on the promotion failure path. ``atomic_write_bytes`` renames the
    finished document into place before its final directory fsync, so a promotion
    that raises may already have committed the green document. Reading the file
    back is how ``write_report`` tells that committed green document from a
    placeholder that was never promoted, so the returned result is reconciled to
    what is on disk rather than to an assumption that the failed write left the
    previous contents in place. Any read error answers ``None``, which the caller
    reads as "not the green document", so it marks the audit failure and
    reconciles the leftover rather than trusting an unverifiable file."""
    try:
        return safe_read_text(canonical)
    except (ConfigError, OSError, ValueError):
        return None


def _report_state_on_disk(config: AgenticHILConfig) -> str | None:
    """The report-state document's current text, or ``None`` when it cannot be read.

    The ``report-state.json`` mirror of ``_canonical_on_disk``.
    ``write_report_state`` renames the finished document into place and only then
    fsyncs the parent directory, so a failure it raises may already have committed
    the requested state: the rename landed and the later durability fsync did not.
    Reading the file back is how ``write_report`` tells a state whose replace
    landed from one that never did, so the returned result is reconciled to what
    is on disk rather than to an assumption that the failed write left the previous
    contents in place. Any read error answers ``None``, which the caller reads as
    "not the requested state", so it records the audit failure rather than trusting
    an unverifiable file."""
    try:
        return safe_read_text(report_state_path(config))
    except (ConfigError, OSError, ValueError):
        return None


def _revoke_stale_canonical(canonical: Path | None, result: JsonObject) -> None:
    """Reconcile a staged per-run placeholder to the audit-failed result, or remove it.

    Called only on the failure paths of ``write_report``, where the trusted
    per-run file has already been staged as a non-green placeholder but a later
    required write failed and the returned result is now ``audit_ok: false``.
    The placeholder is already non-green, so this no longer carries the fail-safe
    guarantee: that comes from staging the copy non-green and promoting it to the
    green document only as the very last write. What it adds is accuracy: the
    leftover record names the real error rather than the generic pending marker.

    Best effort by construction, and safe in every outcome: the corrective
    rewrite lands the audit-failed document; if even that cannot be written the
    file is removed, and a run that has to be read back from ``last-report.json``
    is strictly better than a stale placeholder. A removal that also fails leaves
    the placeholder, which is non-green, so no failed cleanup can leave a false
    green and the returned result already carries the audit failure regardless."""
    if canonical is None:
        return
    try:
        atomic_write_text(canonical, json.dumps(result, indent=2) + "\n")
    except (ConfigError, OSError, ValueError):
        with suppress(ConfigError, OSError, ValueError):
            Path(canonical).unlink()


def recommit_report_with_status(config: AgenticHILConfig, written: JsonObject, status: JsonObject) -> JsonObject:
    """After a terminal lease transition, re-commit the report so the persisted
    evidence carries the same lease_state as the returned result. A no-op when
    the state is already consistent."""
    if written.get("lease_state") == status.get("lease_state"):
        return {**written, **status}
    final = write_report(config, {**written, **status})
    merged = {**final, **status}
    if final.get("audit_ok") is False:
        # The re-commit write failed its audit; the lease.status() overlay
        # (audit_ok=True on a cleanly-released lease) must not mask that. Surface
        # the audit failure fail-closed instead of returning apparent success.
        merged.update({"audit_ok": False, "ok": False, "cleanup_required": True})
        merged["audit_error"] = final.get("audit_error")
        merged["audit_errors"] = final.get("audit_errors")
    return merged


def read_last_report(config: AgenticHILConfig) -> JsonObject:
    return read_report_state_entry(config, "last_report", last_report_path, "get_last_report", "No Agentic HIL report has been written yet.")


def read_last_failure(config: AgenticHILConfig) -> JsonObject:
    return read_report_state_entry(config, "last_failure", last_failure_path, "classify_last_error", "No Agentic HIL failure has been recorded yet.")


def read_report_state_entry(
    config: AgenticHILConfig,
    key: str,
    legacy_path_factory: Callable[[AgenticHILConfig], str],
    tool: str,
    missing_summary: str,
) -> JsonObject:
    try:
        with safe_file_lock(report_lock_path(config)):
            state = read_report_state(config)
            if state is None:
                return {"ok": False, "tool": tool, "error_type": "report_not_found", "summary": missing_summary}
            report = state.get(key)
            if isinstance(report, dict):
                return report
            return {"ok": False, "tool": tool, "error_type": "report_not_found", "summary": missing_summary}
    except ConfigError as error:
        # error.to_dict() carries details["path"] = the absolute state-root path;
        # drop it so no environment-derived path reaches the sink.
        return {"tool": tool, **{key: value for key, value in error.to_dict().items() if key != "path"}}
    except (OSError, ValueError) as error:
        # str(OSError) embeds the absolute state-root filename; emit only the
        # error class/errno so no environment-derived path reaches the sink.
        return {
            "ok": False,
            "tool": tool,
            "error_type": "report_unreadable",
            "summary": "Agentic HIL report state could not be read.",
            **filesystem_error_detail(error),
        }


def read_report_state(config: AgenticHILConfig) -> JsonObject | None:
    path = report_state_path(config)
    try:
        text = safe_read_text(path)
    except FileNotFoundError:
        return None
    try:
        state = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("config_invalid", "Agentic HIL report state is not valid JSON.", {"path": path}) from error
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ConfigError("config_invalid", "Agentic HIL report state has an unsupported format.", {"path": path})
    for key in ("last_report", "last_failure"):
        if state.get(key) is not None and not isinstance(state.get(key), dict):
            raise ConfigError("config_invalid", "Agentic HIL report state contains an invalid entry.", {"path": path, "field": key})
    return state


def load_or_initialize_report_state(config: AgenticHILConfig) -> JsonObject:
    state = read_report_state(config)
    if state is not None:
        return state
    state = {
        "version": 1,
        "last_report": None,
        "last_failure": None,
    }
    write_report_state(config, state)
    return state


def claim_auto_recover_default_warning(config: AgenticHILConfig) -> bool:
    """Claim the one-time warning that machine recovery drove hardware under a
    defaulted ``recovery.auto_recover``.

    True exactly once per project state. A bench that never named a policy still
    gets the default, but the first physical act taken under it is stated in the
    report instead of only being implied by a missing config key."""
    with safe_file_lock(report_lock_path(config)):
        state = load_or_initialize_report_state(config)
        if state.get("auto_recover_default_warned") is True:
            return False
        state["auto_recover_default_warned"] = True
        write_report_state(config, state)
        return True


def read_legacy_report(config: AgenticHILConfig, path: str) -> JsonObject | None:
    try:
        text = safe_read_text(path, workspace=config.work_dir)
    except FileNotFoundError:
        return None
    try:
        report = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("config_invalid", "Legacy Agentic HIL report is not valid JSON.", {"path": display_path(config, path)}) from error
    if not isinstance(report, dict):
        raise ConfigError("config_invalid", "Legacy Agentic HIL report root must be an object.", {"path": display_path(config, path)})
    return report


def write_report_state(config: AgenticHILConfig, state: JsonObject) -> None:
    atomic_write_text(report_state_path(config), json.dumps(state, indent=2) + "\n")


def read_report_file(config: AgenticHILConfig, path_factory: Callable[[AgenticHILConfig], str], tool: str, missing_summary: str) -> JsonObject:
    try:
        report_path = path_factory(config)
        text = safe_read_text(report_path, workspace=config.work_dir)
    except FileNotFoundError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "report_not_found",
            "summary": missing_summary,
        }
    except ConfigError as error:
        return {"tool": tool, **error.to_dict()}
    except UnicodeDecodeError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "config_invalid",
            "summary": "Agentic HIL report is not valid UTF-8 text.",
            "report_path": display_path(config, report_path),
        }
    except OSError as error:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "report_unreadable",
            "summary": "Agentic HIL report could not be read.",
            "backend_error": str(error),
        }
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "config_invalid",
            "summary": "Agentic HIL report is not valid JSON.",
            "report_path": display_path(config, report_path),
        }


def is_failure_report(report: JsonObject) -> bool:
    return not overall_success(report)


# The checks `overall_success` folds into one boolean, in the order it applies
# them and named by the field each one reads. One table rather than a chain of
# `and`s because a caller that has to say *why* a result is not a success must
# read the same predicates the boolean was computed from: a second copy of them
# somewhere else is a second copy that drifts, and the verdict it produces then
# names a check the boolean did not fail on.
SUCCESS_CHECKS: tuple[tuple[str, Callable[[JsonObject], bool]], ...] = (
    ("ok", lambda result: result.get("ok") is True),
    ("target_ok", lambda result: result.get("target_ok") is not False),
    ("audit_ok", lambda result: result.get("audit_ok") is not False),
    ("cleanup_ok", lambda result: result.get("cleanup_ok") is not False),
    ("cleanup_required", lambda result: result.get("cleanup_required") is not True),
    ("quarantined", lambda result: result.get("quarantined") is not True),
    ("lease_state", lambda result: result.get("lease_state") in {None, "active", "released"}),
    ("side_effect_status", lambda result: result.get("side_effect_status") not in {"unknown", "partial"}),
    ("hardware_state", lambda result: result.get("hardware_state") != "unknown"),
)


def failed_success_check(result: JsonObject) -> str | None:
    """Which check this result failed first, or None when it is a success.

    `overall_success` answers "may this go on", which is what almost every
    caller wants and exactly what none of them may report as the reason it
    stopped: nine different facts arrive as one `False`, and a caller that
    describes that `False` in its own words describes whichever of the nine it
    happened to be written about. The run teardown's recovery did that with a
    reset into halt: a reset the target confirmed, whose report could not be
    written, failed the `audit_ok` check and was reported as a target that never
    confirmed the reset, contradicting the recovery's own log and denying the
    halted state the recovery had just left the board in (#389).
    """
    return next((name for name, check in SUCCESS_CHECKS if not check(result)), None)


def overall_success(result: JsonObject) -> bool:
    return failed_success_check(result) is None


def classify_failure_report(config: AgenticHILConfig, likely_causes: Callable[[str], list[str]]) -> JsonObject:
    report = read_last_failure(config)
    if report.get("ok") is not True and report.get("tool") == "classify_last_error" and report.get("error_type") not in {None, "report_not_found"}:
        return report
    if not report.get("ok") and report.get("error_type") == "report_not_found":
        return {"ok": False, "tool": "classify_last_error", "error_type": "report_not_found", "summary": "No Agentic HIL failure has been recorded yet."}
    if overall_success(report):
        return {"ok": True, "tool": "classify_last_error", "error_type": None, "summary": "Last Agentic HIL failure record did not contain an error."}
    error_type = str(report.get("target_error_type") or report.get("error_type") or ("audit_failed" if report.get("audit_ok") is False else "unknown_debugger_error"))
    result = {
        "ok": True,
        "tool": "classify_last_error",
        "error_type": error_type,
        "summary": report.get("summary", "Last Agentic HIL failure contained an error."),
        "likely_causes": report.get("likely_causes", likely_causes(error_type)),
        "report_path": report.get("report_path"),
        "log_path": report.get("log_path"),
        "source_tool": report.get("tool"),
    }
    if "backend_error_type" in report:
        result["backend_error_type"] = report["backend_error_type"]
    if "failed_step" in report:
        result["failed_step"] = report["failed_step"]
    if "step_error_type" in report:
        result["step_error_type"] = report["step_error_type"]
    return attach_canonical_audit_evidence(config, result)


def ensure_audit_ready(config: AgenticHILConfig) -> None:
    for directory in (reports_directory(config), logs_directory(config)):
        probe = Path(directory) / f".audit-probe-{os.getpid()}"
        try:
            safe_write_text(config, probe, "")
        finally:
            with suppress(FileNotFoundError):
                probe.unlink()
    safe_file_path(last_report_path(config), config.work_dir)
    safe_file_path(last_failure_path(config), config.work_dir)
    safe_file_path(report_state_path(config))
    with safe_file_lock(report_lock_path(config)):
        state = load_or_initialize_report_state(config)
        write_report_state(config, state)


def audit_unavailable(tool: str, error: Exception) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": "audit_unavailable",
        "summary": "Hardware action was not started because audit output is unavailable.",
        "side_effect_committed": False,
        "audit_ok": False,
        "audit_error": error.to_dict()
        if isinstance(error, ConfigError)
        else {"error_type": type(error).__name__, "backend_error": str(error)},
    }


# What a session publishes about its own earliest hardware touch, and the field a
# later reader (including the dead-owner release in the coordination layer)
# consults instead of inferring one.
CONTACT_MARKER_KEY = "first_contact_at"
CONTACT_MARKER_SOURCE_KEY = "first_contact_by"


class ContactMarker:
    """Whether this session has provably touched its hardware yet.

    Quarantine answers "I do not know what state the board is in", so the
    question every failure handler actually has is whether there was anything to
    not know about. That question used to be answered by classifying the
    exception: a per-backend set of classes that prove no contact, and everything
    outside the set treated as unknown. Three benches were held in one week by
    failures nobody had enumerated (a socket bind, a missing interface, a port
    another program was already holding), and every miss failed the same way,
    because a set of known-innocent classes is only ever as complete as the last
    incident.

    The marker inverts the question. Contact is recorded at the one point in each
    backend where it is *provable*, before anything downstream of it can fail, and
    every handler asks the marker before it asks the exception. A failure with no
    marker refuses by construction rather than by enumeration, so an exception
    class nobody has ever seen produces the safe answer instead of the loud one.
    The classifier sets stay exactly where they are: they can still turn an
    after-contact failure into a refusal where the class genuinely proves
    innocence, which is a narrowing the marker cannot make. What they are no
    longer is the only line of defence.

    Three states, and the third is the reason this is not a boolean:

    ``made``: contact is proven. Everything downstream keeps precisely the
    behaviour it has today, quarantines included.

    ``proves_no_contact``: nothing was recorded, and the failure happened at or
    before the point where contact would have been recorded had it occurred. This
    is the state that licenses the refusal.

    Neither: ``record_unproven`` was called. Some backends reach a place where
    contact can be neither shown nor ruled out: a bridge process that was handed
    an open request and never answered leaves its own side to itself. Reading that
    as innocence is exactly the failure quarantine exists to prevent, so it
    withholds both answers and keeps the containment it earns today.

    Monotonic in that order. A recorded contact is never taken back, and
    ``record_unproven`` never overwrites one.
    """

    __slots__ = ("_at", "_by", "_unproven_by")

    def __init__(self) -> None:
        self._at: str | None = None
        self._by: str | None = None
        self._unproven_by: str | None = None

    def record(self, by: str) -> None:
        """Note that the hardware was reached, naming the proof. First call wins."""
        if self._at is None:
            self._at = utc_now_iso()
            self._by = by

    def record_unproven(self, by: str) -> None:
        """Note that contact can be neither shown nor ruled out from here on."""
        if self._at is None and self._unproven_by is None:
            self._unproven_by = by

    @property
    def made(self) -> bool:
        return self._at is not None

    @property
    def proves_no_contact(self) -> bool:
        return self._at is None and self._unproven_by is None

    @property
    def at(self) -> str | None:
        return self._at

    @property
    def by(self) -> str | None:
        return self._by

    def report_fields(self) -> JsonObject:
        """The marker as report fields, or nothing at all when no contact was made.

        Absent rather than null, because a reader has to be able to tell "this
        session never reached its hardware" from "this report predates the
        marker", and only presence carries that difference forward.
        """
        if self._at is None:
            return {}
        return {CONTACT_MARKER_KEY: self._at, CONTACT_MARKER_SOURCE_KEY: self._by}


def no_contact_refusal(result: JsonObject, contact: ContactMarker) -> JsonObject:
    """``result`` restated as a no-contact refusal, where the marker allows it.

    Returned unchanged whenever the marker does not positively say the hardware
    was never reached, and unchanged for the two failures the marker has no
    standing over:

    An audit that could not be written keeps quarantining regardless. A bench
    whose ledger cannot be written proves nothing about anything, including about
    itself, and that rule predates the marker and outlives it.

    A result already carrying ``cleanup_required`` keeps it. The marker speaks
    about the hardware; ``cleanup_required`` is usually about something else still
    live on this host (an unreaped bridge child, a handle that would not close),
    and a process that is still running can still act, whatever the bus state was
    when it started.

    ``retry_safe`` is defaulted rather than overwritten: a backend that explicitly
    said a retry cannot settle its own ambiguity is making a claim this layer has
    no evidence against.
    """
    if not contact.proves_no_contact:
        return result
    if result.get("audit_ok") is False or result.get("cleanup_required") is True:
        return result
    refusal = {**result, "target_contacted": False, "side_effect_committed": False, "side_effect_status": "not_started"}
    refusal.setdefault("retry_safe", True)
    return refusal


# Tools whose device lease is taken inside the call they report and given back
# inside it, unless the call reached the hardware, in which case a session start
# keeps it for the session.
#
# Read by the coordination layer to decide whether a report can speak for a lease
# that is still open on disk. For a tool in this set, a report
# saying `side_effect_status: not_started` beside a still-active lease says the
# owner died between committing that report and releasing the lease, and in that
# window nothing can have touched the bench, because the next call would have
# written the next report. Every other tool runs under a lease an earlier call
# opened, and that earlier call (the session start that did reach the hardware)
# is no longer in the report state at all; a `can_read` failure on an open bus
# reports `not_started` truthfully about itself and says nothing about the open.
# That is the distinction this set exists to make, and widening it past the tools
# that own their own lease would erase it.
CALL_SCOPED_LEASE_TOOLS = frozenset(
    {
        "debugger_probes_list",
        "probe_target",
        "flash_firmware",
        "reset_target",
        "debug_start_session",
        "com_session_start",
        "can_session_start",
    }
)


def mark_side_effect(result: JsonObject) -> JsonObject:
    if result.get("tool") not in {
        "flash_firmware", "reset_target", "debug_start_session", "debug_set_breakpoint", "debug_continue",
        "com_session_start", "com_write", "can_session_start", "can_send",
    }:
        return result
    enriched = dict(result)
    if result.get("side_effect_status") in {"not_started", "committed", "partial", "unknown"}:
        return enriched
    if result.get("ok") is True or result.get("target_ok") is False:
        enriched.update({"side_effect_committed": True, "side_effect_status": "committed", "retry_safe": False})
    elif result.get("error_type") in {
        "permission_denied",
        "invalid_argument",
        "session_already_active",
        "session_not_active",
        "not_supported",
        "artifact_not_found",
        "artifact_validation_failed",
        "output_validation_failed",
        "resource_busy",
    }:
        enriched.update({"side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True})
    elif result.get("error_type") == "resource_quarantined":
        enriched.update({"side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": False})
    elif result.get("side_effect_committed") is False:
        enriched.update({"side_effect_status": "not_started", "retry_safe": True})
    elif result.get("side_effect_committed") is True:
        enriched.update({"side_effect_status": "partial", "retry_safe": False})
    else:
        enriched.update({"side_effect_status": "unknown", "retry_safe": False})
    return enriched


def merge_audit_status(result: JsonObject, *sources: JsonObject) -> JsonObject:
    failed = [source for source in sources if source.get("audit_ok") is False]
    if not failed:
        return result
    errors: list[JsonObject] = []
    for source in failed:
        for error in audit_errors(source):
            if error not in errors:
                errors.append(error)
    enriched = dict(result)
    enriched.update({"audit_ok": False, "retry_safe": False})
    if errors:
        enriched.update({"audit_error": errors[0], "audit_errors": errors})
    return enriched


def mark_audit_failure(result: JsonObject, error: Exception) -> JsonObject:
    enriched = dict(result)
    new_error = {
        "error_type": getattr(error, "error_type", type(error).__name__),
        "summary": getattr(error, "summary", "Audit output could not be written."),
        "backend_error": str(error),
    }
    errors = audit_errors(result)
    if new_error not in errors:
        errors.append(new_error)
    enriched["audit_ok"] = False
    enriched["audit_error"] = errors[0]
    enriched["audit_errors"] = errors
    if enriched.get("side_effect_committed") is True:
        enriched["retry_safe"] = False
    return enriched


def audit_errors(result: JsonObject) -> list[JsonObject]:
    nested = result.get("audit_errors")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    single = result.get("audit_error")
    return [single] if isinstance(single, dict) else []
