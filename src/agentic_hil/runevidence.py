"""The CI evidence one run leaves behind, mapped from the run's own report.

`docs/github-action-design.md` settles what a hardware job hands a reviewer who
has no access to the bench: a JUnit file, a JSON run summary, a job summary
somebody reads in a browser, and a copy of the run's event logs. The JUnit file
is `agentic-hil test-reactor --junit-xml`; the other three were specified and
nowhere implemented, so a workflow that wanted them rebuilt them out of the run
report by hand, and that is exactly where the identities the design keeps out of
a public summary came back in.

So the mapping lives here and the callers are thin. `agentic-hil run-evidence`
is one of them; a GitLab job and an operator at a shell are the others, and all
three get the same evidence from the same report because it is the same code.

Two properties this file is built for, and both are about what it does *not*
publish.

**The exclusions are the command's, not the caller's.** A run summary and a job
summary are world-readable on a public repository, so no probe serial, no device
path, no executable path, no absolute path from outside the workspace and no
lock key reaches either of them. That is enforced twice over: every field is
copied by name from a short list rather than by passing a report through, and
every string that does reach an output is swept for the values the report itself
says are identities. A busy device is named by the logical name its plan gave
it, never by the `probe:<serial>` the mutex took.

**A credential is withheld the same way, and by the same code as everywhere
else.** `print_json` and `emit_result` are the two ways a result leaves this
process, and both pass it through `redact_sensitive` first. This is a third sink
for the same documents, and the one whose files a CI system publishes, so the
report takes that same function before anything is mapped off it, and the same
fail-closed rule with it: a redaction that hands back something other than a
document is the one case that step exists for, so the bundle becomes a refusal
built from this command's name and no field of the report rather than the report
nothing vouched for.

That pass masks by key name (`token`, `password`, `api_key`) and content-scans
the captured streams a report carries, and it deliberately does not scan prose,
because in an operator's terminal a false positive eats the diagnosis instead of
a credential. But prose is exactly what reaches these two documents: what a
reviewer reads of a failed step is the sentence the backend wrote, and a backend
writes the URL it fetched from and the header it sent. So every string on its way
into either document takes the content pass too, the same `redact_stream_text`
those streams take, and the trade-off is decided the other way here for a
reason: this sink is not a terminal, only the credential span is replaced so the
decisive sentence still reads, and the unmasked words are still in the collected
log for whoever is allowed to read those. The identity sweep then runs on top of
both, because it covers what neither of them can: a path, a serial and a lock key
are not secret-named and are not credential-shaped, and no key name or pattern
would find them.

The logs are the exception and the reason is the same rule read the other way.
They are copied, not derived, and copied byte for byte: a backend writes its own
command line into its own log, and a mirror this command had edited could never
be checked against the hash-chained copy under `state_root`, which is the only
thing that makes the mirror worth uploading. A secret a backend printed into its
own log therefore stays there, and that is the log's problem to fix where it is
written rather than this command's to paper over; it is also why the two
summaries are the half of the bundle written to be read by anyone. So the
summaries carry no identity and the collected evidence is what the run recorded.

**Nothing is invented.** A field the environment did not supply is absent rather
than empty or guessed: a run outside CI has no `firmware` block at all, and a
report that carries no debugger version line produces a `tools` block with the
versions this process can actually prove. The evidence is worth what it is
derived from, and a plausible value nobody measured would be worth less than
nothing in the one document a reviewer is meant to trust.

The command reads two things and no more: the report it is handed, and the
workspace it is running in (the plan, and the JSONL event logs the report
names). It loads no configuration, so a report from a refused run is served
exactly as a green one is, and it touches no hardware at all.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath

from agentic_hil import __version__
from agentic_hil.config import ConfigError, to_posix
from agentic_hil.junit import run_refusal
from agentic_hil.knowledge import REDACTION_UNAVAILABLE_ERROR
from agentic_hil.redact import redact_sensitive, redact_stream_text
from agentic_hil.report import overall_success
from agentic_hil.test_reactor import (
    TEST_CONFIG_SHA256_KEY,
    flatten_steps,
    load_test_config,
    result_error_type,
    result_failed,
    step_device_classes,
)
from agentic_hil.types import JsonObject

RUN_EVIDENCE_TOOL = "run_evidence"

# The command as an operator types it. The redaction refusal is built from this
# and from nothing else, so it is spelled here rather than taken off a caller:
# a GitLab job and an operator at a shell reach this code through the same
# command, and a refusal that named whoever called it would be naming a caller
# instead of the sink that withheld the bundle.
RUN_EVIDENCE_COMMAND = "run-evidence"

# The three files, named once. A workflow uploads the directory, so the names
# are part of the contract with whoever reads the bundle afterwards.
RUN_SUMMARY_FILENAME = "run-summary.json"
JOB_SUMMARY_FILENAME = "job-summary.md"
LOGS_DIRECTORY_NAME = "logs"

# Where a GitHub job's summary is appended. Set by the runner, so its presence
# is what says the job summary has a second destination beside the file.
STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

# What both documents say when the redaction did not hand back a document. One
# sentence, used verbatim in the JSON and in the Markdown, so a reviewer reading
# the job page and a consumer parsing the summary are told the same thing.
REDACTION_UNAVAILABLE_SUMMARY = (
    "This evidence was not written from the run report. Redacting the report did not produce a document, so nothing "
    "can say its secret-named values were replaced, and the unredacted report is not what gets published instead. "
    "No field of the report is in this bundle and no event log was collected."
)

# What replaces an identity that reached a string on its way out. Redacted, not
# removed: a reader has to be able to see that something was withheld exactly
# there, or a sentence with a hole in it reads as a sentence about nothing.
WITHHELD = "[withheld]"

# The report fields that are identities by definition. `executable` and
# `probe_id` are the design's own two names; `executable_path` is the same
# value under the spelling a backend's resolution answers with.
IDENTITY_FIELDS = frozenset({"executable", "executable_path", "probe_id"})

# The fields that carry a lock key, which is keyed on the physical device
# (`probe:<serial>`, `com:<device>`, `can:<adapter>:<channel>`) and therefore
# carries exactly the identities everything else here is careful not to publish.
LOCK_KEY_FIELDS = frozenset({"declared_devices", "lock_key", "lock_keys", "resource", "resources"})

# The design's device grouping, keyed by the reactor's own device kinds so the
# two cannot drift: a kind added to the reactor and not named here is left out
# of the summary rather than guessed into the wrong group.
DEVICE_GROUPS: dict[str, str] = {"debugger": "debuggers", "uart": "com_ports", "can": "can_buses"}

# The same three groups as a person reads them. Spelled out rather than derived
# from the key, because "Can buses" is what deriving it produces and this table
# is the one a reviewer meets first.
DEVICE_LABELS: dict[str, str] = {"debuggers": "Debuggers", "com_ports": "COM ports", "can_buses": "CAN buses"}

# An absolute path inside a sentence. Three shapes: a Windows drive path, a UNC
# path, and a POSIX path of at least two segments. The last one is deliberately
# not one segment: `/min` in a comparator's own text is not a path, and a sweep
# that decided it was would eat the sentence it was meant to keep readable. The
# lookbehind is what stops `and/or` matching as `/or`.
ABSOLUTE_PATH = re.compile(r"(?<![\w.\-/\\])(?:[A-Za-z]:[\\/][^\s\"'<>|]*|\\\\[^\s\"'<>|]+|/[^\s\"'<>|/]+(?:/[^\s\"'<>|]*)+)")

# A commit as `GITHUB_EVENT_PATH` spells one. The pull request head is the one
# value here that comes out of a JSON document a fork's event wrote, so it is
# published only when it is a commit id and nothing else.
COMMIT_ID = re.compile(r"^[0-9a-f]{7,64}$")


def write_run_evidence(report_path: str, out_dir: str, *, workspace: str | Path | None = None, environ: Mapping[str, str] | None = None) -> JsonObject:
    """Write the three evidence files for one run report, and say where they went.

    The verdict is about the evidence, not about the run: a job whose plan
    failed still wrote its evidence correctly, and a step that went red because
    the run did would make `if: always()` impossible to reason about. The run's
    own verdict is `outcome`, on this result and in both documents. A redaction
    that cannot vouch for the report is the one thing that does turn this
    verdict red, and it is not an exception to that rule but the same rule: the
    evidence is what failed, so the bundle says so and the step says so.
    """
    root = Path(workspace).resolve() if workspace is not None else Path.cwd().resolve()
    environment = dict(environ) if environ is not None else dict(_os_environ())
    # Redacted first, and everything below reads the redacted document: the
    # summaries are mapped from it, the identity sweep is read off it, and the
    # logs are collected by the `log_path` fields it carries. Redaction touches
    # none of those three (no path field is secret-named), so the mapping is
    # unchanged and only the secret-shaped values differ.
    redacted = redact_sensitive(_read_report(report_path))
    if not isinstance(redacted, dict):
        return _redaction_refusal(Path(out_dir).expanduser(), environment)
    report = redacted
    excluded = excluded_values(report, root)
    plan = _plan_of(report, root)
    summary = run_summary(report, root, environment, plan=plan, excluded=excluded)

    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / RUN_SUMMARY_FILENAME
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    document = job_summary(report, summary, root, excluded=excluded)
    job_summary_path = out / JOB_SUMMARY_FILENAME
    job_summary_path.write_text(document, encoding="utf-8")
    step_summary = _append_step_summary(document, environment)

    logs = collect_logs(report, root, out / LOGS_DIRECTORY_NAME)
    result: JsonObject = {
        "ok": True,
        "tool": RUN_EVIDENCE_TOOL,
        "outcome": summary["outcome"],
        "report": _display(report_path, root),
        "out": str(out),
        "run_summary_path": str(summary_path),
        "job_summary_path": str(job_summary_path),
        "logs_path": str(out / LOGS_DIRECTORY_NAME),
        **logs,
        "summary": _evidence_summary(summary["outcome"], logs, step_summary),
    }
    if step_summary is not None:
        result["step_summary_path"] = step_summary
    return result


def _evidence_summary(outcome: str, logs: JsonObject, step_summary: str | None) -> str:
    copied = len(logs.get("logs_copied") or ())
    sentence = f"Run evidence written for a run whose outcome was {outcome}: the run summary, the job summary and {copied} event log file(s)."
    if step_summary is not None:
        sentence += f" The job summary was also appended to {STEP_SUMMARY_ENV}."
    missing = logs.get("logs_missing") or ()
    if missing:
        # Named rather than counted: a log the report promised and the workspace
        # does not have is the single most likely sign that this report was
        # written somewhere else, and a reader has to see which file it was.
        sentence += f" {len(missing)} log file(s) the report names are not in this workspace: {', '.join(missing)}."
    if logs.get("logs_outside_workspace"):
        sentence += (
            f" {logs['logs_outside_workspace']} more are named by absolute paths outside this workspace, so this report was written by a run in "
            "another workspace; those paths are identities this command does not publish, and the files were not collected."
        )
    return sentence


def _redaction_refusal(out: Path, environment: Mapping[str, str]) -> JsonObject:
    """The bundle a report the redaction could not vouch for leaves behind.

    Written rather than raised, because these files are what a workflow's
    `if: always()` upload step collects and a bundle that is simply missing
    reads as a step that never ran; the reviewer gets the statement instead, in
    both documents and on the job page where the summary is appended.

    Every byte of it comes from this command's own name. Carrying over one field
    of the report, even one that reads as harmless, would be the fail-open
    branch again in a smaller shape: what is not known here is precisely which
    of that report's values the redaction would have replaced. `outcome` is
    deliberately absent from `run-summary.json` for the same reason, and it is
    also what stops a consumer reading a refusal as a run that ended somehow.

    No log is collected either, and that is not a second rule: the report is the
    index that names them, and the index is the document nothing vouched for.
    """
    out.mkdir(parents=True, exist_ok=True)
    refusal: JsonObject = {
        "ok": False,
        "tool": RUN_EVIDENCE_TOOL,
        "error_type": REDACTION_UNAVAILABLE_ERROR,
        "command": RUN_EVIDENCE_COMMAND,
        "summary": REDACTION_UNAVAILABLE_SUMMARY,
    }
    summary_path = out / RUN_SUMMARY_FILENAME
    summary_path.write_text(json.dumps(refusal, indent=2) + "\n", encoding="utf-8")
    document = (
        f"## Agentic HIL: {RUN_EVIDENCE_COMMAND}\n\n"
        f"**Outcome:** {REDACTION_UNAVAILABLE_ERROR}\n\n"
        f"{REDACTION_UNAVAILABLE_SUMMARY}\n"
    )
    job_summary_path = out / JOB_SUMMARY_FILENAME
    job_summary_path.write_text(document, encoding="utf-8")
    logs_path = out / LOGS_DIRECTORY_NAME
    logs_path.mkdir(parents=True, exist_ok=True)
    step_summary = _append_step_summary(document, environment)
    result: JsonObject = {
        **refusal,
        "out": str(out),
        "run_summary_path": str(summary_path),
        "job_summary_path": str(job_summary_path),
        "logs_path": str(logs_path),
        "logs_copied": [],
    }
    if step_summary is not None:
        result["step_summary_path"] = step_summary
    return result


def _os_environ() -> Mapping[str, str]:
    import os

    return os.environ


def _read_report(report_path: str) -> JsonObject:
    path = Path(report_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigError("run_report_not_found", "The run report could not be found.", {"path": str(path)}) from error
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError("run_report_unreadable", "The run report could not be read as UTF-8 text.", {"path": str(path), "backend_error": str(error)}) from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise ConfigError("run_report_invalid", "The run report is not a JSON document.", {"path": str(path), "backend_error": str(error)}) from error
    if not isinstance(document, dict):
        raise ConfigError("run_report_invalid", "The run report must be a JSON object.", {"path": str(path)})
    return document


# ---------------------------------------------------------------------------
# The JSON run summary.


def run_summary(report: JsonObject, workspace: Path, environment: Mapping[str, str], *, plan: JsonObject | None = None, excluded: frozenset[str] | None = None) -> JsonObject:
    """The design's `run-summary.json`, as a pure function of what it is given.

    Every block is optional and absent when nothing supplied it, which is the
    whole difference between a summary a downstream consumer can trust and a
    template with holes in it: `firmware` outside CI, `tools.debuggers` for a
    report that carries no debugger version line, `bench.target` for a report
    that names no target."""
    sweep = excluded if excluded is not None else excluded_values(report, workspace)
    plan_block = plan if plan is not None else _plan_of(report, workspace)
    summary: JsonObject = {"outcome": run_outcome(report)}
    for key, block in (
        ("plan", {name: value for name, value in plan_block.items() if name in {"path", "name", "sha256", "sha256_mismatch"}}),
        ("firmware", _firmware(environment)),
        ("tools", _tools(report)),
        ("bench", _bench(report, plan_block, environment)),
        ("run", _run(report)),
    ):
        if block:
            summary[key] = block
    # Swept on the way out as well as built by name, because a plan is free to
    # call a device after the probe it drives and a target after its serial.
    return {key: _scrubbed(value, sweep, workspace) for key, value in summary.items()}


def run_outcome(report: JsonObject) -> str:
    """`success`, `failure` or `refused`, and the third is decided once.

    `refused` is the JUnit mapping's own predicate, imported rather than
    restated: a run that produced this document's `refused` and that document's
    `preflight` error is one run, and two definitions of "nothing ran" would be
    two answers waiting to differ."""
    if overall_success(report):
        return "success"
    return "refused" if run_refusal(report) is not None else "failure"


def _firmware(environment: Mapping[str, str]) -> JsonObject:
    """What was checked out, as the CI environment stated it.

    GitHub first, GitLab second, and each field independently: a field the
    environment did not set is absent, and a shell with neither set produces no
    `firmware` block at all rather than one full of nulls."""
    firmware: JsonObject = {}
    for key, names in (
        ("repository", ("GITHUB_REPOSITORY", "CI_PROJECT_PATH")),
        ("commit", ("GITHUB_SHA", "CI_COMMIT_SHA")),
        ("ref", ("GITHUB_REF", "CI_COMMIT_REF_NAME")),
    ):
        value = next((environment[name].strip() for name in names if environment.get(name, "").strip()), None)
        if value is not None:
            firmware[key] = value
    head = _pull_request_head(environment)
    if head is not None:
        # Beside the commit, never instead of it. `GITHUB_SHA` on a
        # `pull_request` is the merge commit, which is what was built and is
        # therefore what the evidence is about; the head is what a reviewer
        # recognises.
        firmware["head_commit"] = head
    return firmware


def _pull_request_head(environment: Mapping[str, str]) -> str | None:
    """The pull request's head commit, out of the event payload, when it is one.

    The only value here that comes from a document a fork's event wrote, so it
    is published only when it is a commit id and nothing else. A payload that is
    missing, unreadable or shaped differently yields nothing, because a summary
    that invented a commit would be worse than one that omits it."""
    event_path = environment.get("GITHUB_EVENT_PATH", "").strip()
    if not event_path:
        return None
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    pull_request = payload.get("pull_request")
    head = pull_request.get("head") if isinstance(pull_request, dict) else None
    commit = head.get("sha") if isinstance(head, dict) else None
    return commit if isinstance(commit, str) and COMMIT_ID.match(commit) else None


def _tools(report: JsonObject) -> JsonObject:
    """The versions this evidence was produced by.

    Two of them are facts about this process and are always there. The debugger
    version lines are the backend's own, and they exist only where the report
    carries a `debugger_info` result: `agentic-hil doctor` produces one per
    configured probe, and a plain run report produces none, so the block is
    absent rather than filled with the version of whatever happens to be
    installed here now."""
    tools: JsonObject = {"agentic_hil": __version__, "python": platform.python_version()}
    debuggers = _debugger_versions(report)
    if debuggers:
        tools["debuggers"] = debuggers
    return tools


def _debugger_versions(report: JsonObject) -> JsonObject:
    """Each debugger's backend and version line, and nothing else off that result.

    `executable` and `probe_id` are on the same result and are two of the
    design's three named exclusions, which is why this copies two fields by name
    instead of dropping two from a result it passed through."""
    entries = report.get("debuggers")
    if not isinstance(entries, dict):
        return {}
    versions: JsonObject = {}
    for name, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            continue
        check = entry.get("check") if isinstance(entry.get("check"), dict) else entry
        if check.get("tool") != "debugger_info":
            continue
        block = {key: check[key] for key in ("backend", "version") if isinstance(check.get(key), str)}
        if block:
            versions[str(name)] = block
    return versions


def _bench(report: JsonObject, plan: JsonObject, environment: Mapping[str, str]) -> JsonObject:
    """Which bench this was, by digest and by logical name.

    Never by hardware identity, for the reason the design gives: the digest says
    which policy decided the run and the logical names say what the plan
    addressed, and between them they identify a bench to everyone who is allowed
    to know which bench it is."""
    bench: JsonObject = {}
    in_force = report.get("config_in_force")
    if isinstance(in_force, dict):
        for key in ("digest", "digest_algorithm", "diverged_from_file"):
            if in_force.get(key) is not None:
                bench["config_digest" if key == "digest" else key] = in_force[key]
    runner = _runner(environment)
    if runner:
        bench["runner"] = runner
    target = report.get("target")
    if isinstance(target, dict):
        block = {key: target[key] for key in ("name", "controller") if isinstance(target.get(key), str)}
        if block:
            bench["target"] = block
    devices = plan.get("devices")
    if devices:
        bench["devices"] = devices
    return bench


def _runner(environment: Mapping[str, str]) -> JsonObject:
    runner: JsonObject = {}
    name = environment.get("RUNNER_NAME", "").strip()
    if name:
        runner["name"] = name
    labels = [label.strip() for label in environment.get("RUNNER_LABELS", "").split(",") if label.strip()]
    if labels:
        runner["labels"] = labels
    return runner


def _run(report: JsonObject) -> JsonObject:
    run: JsonObject = {}
    failed_step = report.get("failed_step")
    if isinstance(failed_step, int) and not isinstance(failed_step, bool):
        run["failed_step"] = failed_step
    if report.get("error_type"):
        run["error_type"] = str(report["error_type"])
    for key in ("cleanup_ok", "audit_ok"):
        if isinstance(report.get(key), bool):
            run[key] = report[key]
    recovery = report.get("recovery")
    if isinstance(recovery, dict):
        block = {key: recovery[key] for key in ("attempted", "outcome", "auto_recover_policy") if recovery.get(key) is not None}
        if block:
            run["recovery"] = block
    return run


# ---------------------------------------------------------------------------
# The plan, and the logical device names it addressed.


def _plan_of(report: JsonObject, workspace: Path) -> JsonObject:
    """The plan block, plus the logical device names grouped by kind.

    The plan's provenance is the digest the run recorded when it read the file,
    carried in the report as ``test_config_sha256`` and published here verbatim.
    The file on disk is not re-hashed into the summary: it can be edited between
    the run loading it and this collection, and hashing whatever is there now
    would pair the old run's name and step results with a new file's digest and
    device list, a plan that was never executed (review round 0, finding 4). If
    the file is still present and its bytes have diverged from the recorded
    digest, that divergence is reported (`sha256_mismatch`) rather than papered
    over by substituting the current digest.

    The plan file is still read where it is in this workspace, but only to fill in
    what a refused run left out: a run refused before its first step recorded no
    steps, so its device names exist only in the plan that never ran. That
    re-read is trusted for those names only when the file still matches the
    recorded digest, i.e. is the same bytes that ran; a diverged or
    digest-less-legacy file falls back to the executed records alone. A report
    from somewhere else names a plan that is not here, and then the executed
    records are all there is, which for a refusal is nothing."""
    plan: JsonObject = {}
    name = report.get("name")
    if isinstance(name, str) and name:
        plan["name"] = name
    routes: list[tuple[str, str]] = [
        (str(record["route"]), str(record.get("action") or ""))
        for record in _step_records(report)
        if isinstance(record.get("route"), str) and record["route"] not in {"", "-"}
    ]
    plan_path = report.get("test_config_path")
    resolved = _within_workspace(plan_path, workspace) if isinstance(plan_path, str) else None
    if resolved is not None:
        plan["path"] = _relative(resolved, workspace)
        recorded = report.get(TEST_CONFIG_SHA256_KEY)
        recorded = recorded if isinstance(recorded, str) and recorded else None
        current: str | None = None
        with suppress(OSError):
            current = hashlib.sha256(resolved.read_bytes()).hexdigest()
        # The recorded digest is authoritative; the file's own is only ever used
        # for a legacy report that carried none. A file that has diverged from the
        # recorded digest is flagged, never substituted for it.
        digest = recorded or current
        if digest is not None:
            plan["sha256"] = digest
        if recorded is not None and current is not None and current != recorded:
            plan["sha256_mismatch"] = True
        # Augment from the file only when it is provably the plan that ran: the
        # digest it carries matches what the run recorded (or the report predates
        # the field and there is nothing better to gate on). A diverged file is
        # not this run's plan, so its names are left out rather than mixed in.
        if recorded is None or (current is not None and current == recorded):
            with suppress(ConfigError, OSError, ValueError):
                loaded = load_test_config(str(resolved), str(workspace))
                plan.setdefault("name", loaded.name)
                routes = [(step.route, step.action) for step in flatten_steps(loaded.steps) if step.route != "-"] + routes
    devices = _grouped_devices(routes)
    if devices:
        plan["devices"] = devices
    return plan


def _grouped_devices(routes: Sequence[tuple[str, str]]) -> JsonObject:
    """Logical device names by kind, from the actions that addressed them.

    The action says which kind serves it, so no configuration is needed to group
    a name. An action several kinds serve (`delay` is declared on the base and
    inherited by all of them) says nothing about its device and contributes
    nothing rather than a guess."""
    grouped: dict[str, set[str]] = {}
    for route, action in routes:
        classes = step_device_classes(action)
        if len(classes) != 1:
            continue
        group = DEVICE_GROUPS.get(classes[0].kind)
        if group is not None:
            grouped.setdefault(group, set()).add(route)
    return {group: sorted(grouped[group]) for group in DEVICE_GROUPS.values() if grouped.get(group)}


# ---------------------------------------------------------------------------
# The event logs.


def collect_logs(report: JsonObject, workspace: Path, destination: Path) -> JsonObject:
    """Copy the workspace event logs the report names, and account for the rest.

    The report is the index: every serial session, every bus session and every
    debugger invocation records the file it wrote under `log_path`, so the
    collection is exactly what this run produced and never the directory's whole
    history. These are the workspace mirrors. The hash-chained copies under
    `state_root` stay on the runner, because shipping them to an artifact store
    would be publishing the thing they exist to be checked against.
    """
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []
    outside = 0
    taken: dict[str, Path] = {}
    for source in _log_paths(report):
        resolved = _within_workspace(source, workspace)
        if resolved is None:
            outside += 1
            continue
        if str(resolved) in {str(path) for path in taken.values()}:
            continue
        if not resolved.is_file():
            missing.append(_relative(resolved, workspace))
            continue
        target = _free_name(destination, resolved.name, taken)
        try:
            target.write_bytes(resolved.read_bytes())
        except OSError:
            missing.append(_relative(resolved, workspace))
            continue
        taken[target.name] = resolved
        copied.append(target.name)
    logs: JsonObject = {"logs_copied": sorted(copied)}
    if missing:
        logs["logs_missing"] = sorted(dict.fromkeys(missing))
    if outside:
        # A count, never the paths. They are absolute paths from outside this
        # workspace, which is one of the four things this command does not
        # publish, and a report that names them is a report from another bench.
        logs["logs_outside_workspace"] = outside
    return logs


def _log_paths(report: JsonObject) -> list[str]:
    seen: list[str] = []
    for key, value in _walk(report):
        if key == "log_path" and isinstance(value, str) and value and value not in seen:
            seen.append(value)
    return seen


def _free_name(destination: Path, name: str, taken: Mapping[str, Path]) -> Path:
    """A name in the collection directory that no other source already has.

    Two sessions in one logs directory cannot collide (the file name carries the
    timestamp and the device), but two directories can hold the same name, and a
    collection that quietly overwrote one with the other would hand a reviewer a
    file whose contents belong to a different device."""
    if name not in taken:
        return destination / name
    stem, _, suffix = name.partition(".")
    index = 2
    while f"{stem}-{index}.{suffix}" in taken:
        index += 1
    return destination / f"{stem}-{index}.{suffix}"


# ---------------------------------------------------------------------------
# The job summary.


def job_summary(report: JsonObject, summary: JsonObject, workspace: Path, *, excluded: frozenset[str] | None = None) -> str:
    """The Markdown a reviewer reads, and the same facts the JSON summary carries.

    Deliberately the same source, so the two documents cannot say different
    things about one run. Everything free-text in it is the run's own words, and
    every one of them is swept: a backend writes its own summary and a plan names
    its own devices, and neither of them knows what this file may not publish."""
    sweep = excluded if excluded is not None else excluded_values(report, workspace)

    def text(value: object) -> str:
        return _scrub(" ".join(str(value).split()), sweep, workspace)

    plan = summary.get("plan") if isinstance(summary.get("plan"), dict) else {}
    lines = [f"## Agentic HIL: {plan.get('name') or 'test-reactor'}", "", f"**Outcome:** {summary['outcome']}", ""]
    if plan.get("path"):
        lines += [f"Plan: `{plan['path']}`" + (f" (sha256 `{plan['sha256']}`)" if plan.get("sha256") else ""), ""]
    lines += _steps_section(report, text)
    lines += _failure_section(report, text)
    lines += _bench_section(summary, text)
    lines += _recovery_section(report, text)
    return "\n".join(lines).rstrip() + "\n"


def _steps_section(report: JsonObject, text: Callable[[object], str]) -> list[str]:
    records = _step_records(report)
    if not records:
        return ["No step ran.", ""]
    lines = ["| # | Route | Action | Result | Elapsed (ms) |", "| --- | --- | --- | --- | --- |"]
    for record in records:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        lines.append(
            f"| {record.get('index', '')} | {text(record.get('route') or '-')} | {text(record.get('action') or '-')} | "
            f"{'fail' if result_failed(result) else 'pass'} | {_step_elapsed_ms(record, result)} |"
        )
    return [*lines, ""]


def _step_elapsed_ms(record: JsonObject, result: JsonObject) -> str:
    """How long this step took, as the report recorded it.

    The step's own measurement first. The reactor times every step it runs, so
    that is the one number every row can have: reading the *tool's* duration
    instead gave a figure for `flash` and `reset`, which the debugger backends
    report, and an empty cell for a serial line, a bus and a repeat block, which
    do not.

    The tool's own is still read, for a report written before the reactor timed
    its steps. This command exists to be pointed at a report file, and a file
    written by an earlier version is exactly what it will be pointed at.
    """
    for elapsed in (record.get("elapsed_ms"), result.get("elapsed_ms")):
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            return str(elapsed)
    return ""


def _failure_section(report: JsonObject, text: Callable[[object], str]) -> list[str]:
    """What went wrong, in the run's own words and in full.

    A refusal and a failed step are one section because a reader wants one
    thing: the error type by name and the sentence that came with it. The
    sentence is never dropped, only swept, because a summary that hid the
    decisive line to stay safe would have made the artifact pointless."""
    refusal = run_refusal(report)
    if refusal is not None:
        lines = [f"### Refused: `{text(refusal.get('error_type') or report.get('error_type') or 'refused')}`", ""]
        located = [f"- {key}: `{text(refusal[key])}`" for key in ("field", "route", "action") if refusal.get(key)]
        if located:
            lines += [*located, ""]
        summary = refusal.get("summary") or report.get("summary")
        return [*lines, text(summary), ""] if summary else lines
    failed = next((record for record in _step_records(report) if result_failed(record.get("result") if isinstance(record.get("result"), dict) else {})), None)
    if failed is None:
        return []
    result = failed["result"]
    return [
        f"### Step {failed.get('index', '?')} failed: `{text(result_error_type(result))}`",
        "",
        text(result.get("summary") or "The step failed."),
        "",
    ]


def _config_digest(bench: Mapping[str, object]) -> str:
    """The configuration digest as one algorithm-prefixed string, spelled once.

    `config_in_force.digest` is the spelling `config_status` publishes, which
    already carries its own algorithm (`sha256:4cdb...`), and this row joined
    `digest_algorithm` onto it as though it were bare hex. Both fields are right
    on their own; the join was not, and it published `sha256sha256:4cdb...` in
    the one artifact a reviewer compares against the configuration (#466). A
    digest that is bare hex, which is what a report carries when its producer
    keeps the algorithm in the neighbouring field, still gets the prefix it is
    missing, so one row serves both spellings and doubles neither.
    """
    digest = str(bench.get("config_digest") or "").strip()
    algorithm = str(bench.get("digest_algorithm") or "").strip()
    if not algorithm or digest.startswith(f"{algorithm}:"):
        return digest
    return f"{algorithm}:{digest}"


def _bench_section(summary: JsonObject, text: Callable[[object], str]) -> list[str]:
    bench = summary.get("bench") if isinstance(summary.get("bench"), dict) else {}
    tools = summary.get("tools") if isinstance(summary.get("tools"), dict) else {}
    firmware = summary.get("firmware") if isinstance(summary.get("firmware"), dict) else {}
    rows: list[tuple[str, str]] = []
    if bench.get("config_digest"):
        rows.append(("Configuration digest", f"`{text(_config_digest(bench))}`"))
        rows.append(("Diverged from the file on disk", "yes" if bench.get("diverged_from_file") else "no"))
    target = bench.get("target") if isinstance(bench.get("target"), dict) else {}
    if target.get("name"):
        rows.append(("Target", text(f"{target['name']} ({target.get('controller')})" if target.get("controller") else target["name"])))
    devices = bench.get("devices") if isinstance(bench.get("devices"), dict) else {}
    for group, names in devices.items():
        rows.append((DEVICE_LABELS.get(group, group), ", ".join(f"`{text(name)}`" for name in names)))
    runner = bench.get("runner") if isinstance(bench.get("runner"), dict) else {}
    if runner.get("name"):
        rows.append(("Runner", text(runner["name"])))
    if runner.get("labels"):
        rows.append(("Runner labels", ", ".join(f"`{text(label)}`" for label in runner["labels"])))
    for key, label in (("repository", "Repository"), ("ref", "Ref"), ("commit", "Commit"), ("head_commit", "Pull request head")):
        if firmware.get(key):
            rows.append((label, f"`{text(firmware[key])}`"))
    rows.append(("Agentic HIL", f"`{text(tools.get('agentic_hil'))}`"))
    rows.append(("Python", f"`{text(tools.get('python'))}`"))
    for name, block in (tools.get("debuggers") or {}).items():
        rows.append((f"Debugger {text(name)}", text(f"{block.get('backend')}: {block.get('version')}")))
    return ["### Bench", "", "| Field | Value |", "| --- | --- |", *[f"| {label} | {value} |" for label, value in rows], ""]


def _recovery_section(report: JsonObject, text: Callable[[object], str]) -> list[str]:
    recovery = report.get("recovery")
    if not isinstance(recovery, dict):
        return []
    lines = [
        "### Recovery",
        "",
        f"- attempted: {'yes' if recovery.get('attempted') else 'no'}",
        f"- outcome: `{text(recovery.get('outcome') or 'unknown')}`",
    ]
    for key in ("auto_recover_policy", "reason_not_attempted"):
        if recovery.get(key):
            lines.append(f"- {key}: `{text(recovery[key])}`")
    if recovery.get("summary"):
        lines += ["", text(recovery["summary"])]
    return [*lines, ""]


def _append_step_summary(document: str, environment: Mapping[str, str]) -> str | None:
    """Append the job summary where `GITHUB_STEP_SUMMARY` points, when it is set.

    Appended, not written: the variable names one file per job step and a runner
    may already have put something in it, and a summary that truncated the step
    before it would delete evidence to publish evidence. The file is always
    written under `--out` as well, so a job on a runner that sets nothing, a
    GitLab job and an operator at a shell all still have the document."""
    target = environment.get(STEP_SUMMARY_ENV, "").strip()
    if not target:
        return None
    path = Path(target).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(document if document.startswith("\n") else "\n" + document)
    except OSError:
        # Best effort, and only here. The document under `--out` is the copy the
        # artifact carries, and losing a browser rendering must not cost a job
        # the evidence bundle it exists to produce.
        return None
    return str(path)


# ---------------------------------------------------------------------------
# The exclusions.


def excluded_values(report: JsonObject, workspace: Path) -> frozenset[str]:
    """Every string in this report that must not reach an output.

    Read off the report rather than guessed at, which is what makes the rule
    hold for a value this file has never seen: a probe serial is whatever the
    report called `probe_id`, a lock key is whatever the mutex took, and a path
    from another machine is any absolute path that is not under this workspace.
    """
    excluded: set[str] = set()
    for key, value in _walk(report):
        if not isinstance(value, str) or len(value) < 2:
            continue
        identity = key in IDENTITY_FIELDS or key in LOCK_KEY_FIELDS
        foreign = _is_absolute(value) and _within_workspace(value, workspace) is None
        if identity or foreign:
            excluded.add(value)
    in_force = report.get("config_in_force")
    if isinstance(in_force, dict) and isinstance(in_force.get("path"), str) and in_force["path"]:
        # Named as well as swept. A configuration stored inside the workspace is
        # refused at load, so this is normally caught as an outside path anyway;
        # it is listed by name because the design lists it by name, and a rule
        # that held only by side effect would not survive the next refactor.
        excluded.add(in_force["path"])
    return frozenset(excluded)


def _scrubbed(value: object, excluded: frozenset[str], workspace: Path) -> object:
    if isinstance(value, str):
        return _scrub(value, excluded, workspace)
    if isinstance(value, dict):
        return {key: _scrubbed(item, excluded, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrubbed(item, excluded, workspace) for item in value]
    return value


def _scrub(text: str, excluded: frozenset[str], workspace: Path) -> str:
    """One string with every credential and every identity taken out of it.

    The credential pass first, over the string as the backend wrote it, because
    `redact_stream_text` matches shapes rather than names -- a URL with userinfo,
    an `Authorization: Bearer` line, a `token=` assignment -- and it has to see
    the original bytes to find them. `redact_sensitive` on the report cannot
    reach these: the strings that arrive in either document arrive under
    `summary`, `route` and `action`, and it never content-scans a prose key.
    This is the one place in the project that decides that trade-off the other
    way, and it is decided by the sink: the file goes to an artifact store, only
    the credential span is replaced so the failing step's own sentence still
    reads, and the unmasked words remain in the collected log.

    Then the identities, longest first, so a value that contains another does not
    leave the shorter one's tail behind. The path sweep runs last and catches the
    shape rather than the value: an absolute path a backend printed inside a
    sentence was never a field, so nothing could have collected it by name."""
    text = redact_stream_text(text)
    for value in sorted(excluded, key=len, reverse=True):
        if value and value in text:
            text = text.replace(value, WITHHELD)
    return ABSOLUTE_PATH.sub(lambda match: _path_replacement(match.group(0), workspace), text)


def _path_replacement(value: str, workspace: Path) -> str:
    inside = _within_workspace(value, workspace)
    return _relative(inside, workspace) if inside is not None else WITHHELD


def _is_absolute(value: str) -> bool:
    """Absolute under either flavour, because a report travels between them.

    A POSIX report read on Windows is exactly the case this exists for: a
    `WindowsPath` says `/home/ci/logs` is relative, and a sweep that believed it
    would publish another machine's paths from a Linux runner's report."""
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _within_workspace(value: str | Path, workspace: Path) -> Path | None:
    """The path this value names inside the workspace, or None.

    None covers all three ways a path is not this workspace's: absolute and
    somewhere else, absolute in the other platform's flavour, and relative but
    climbing out through `..`."""
    text = str(value)
    if not text:
        return None
    if _is_absolute(text) and not Path(text).is_absolute():
        return None
    try:
        resolved = (workspace / text).resolve() if not Path(text).is_absolute() else Path(text).resolve()
        return resolved if resolved == workspace or resolved.is_relative_to(workspace) else None
    except (OSError, ValueError):
        return None


def _relative(path: Path, workspace: Path) -> str:
    with suppress(ValueError):
        return to_posix(str(path.relative_to(workspace)))
    return to_posix(path.name)


def _display(value: str, workspace: Path) -> str:
    inside = _within_workspace(value, workspace)
    return _relative(inside, workspace) if inside is not None else str(value)


def _step_records(report: JsonObject) -> list[JsonObject]:
    steps = report.get("steps")
    return [record for record in steps if isinstance(record, dict)] if isinstance(steps, list) else []


def _walk(document: object, key: str = "") -> Iterator[tuple[str, object]]:
    """Every scalar in a document, with the key it sat under.

    A list item inherits its list's key, which is what makes `declared_devices`
    a list of lock keys rather than a list of anonymous strings."""
    if isinstance(document, dict):
        for name, value in document.items():
            yield from _walk(value, str(name))
    elif isinstance(document, list):
        for value in document:
            yield from _walk(value, key)
    else:
        yield key, document
