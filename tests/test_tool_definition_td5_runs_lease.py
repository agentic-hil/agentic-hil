"""What a host reads about bench runs, test plan runs and the lease status (#642).

`bench_run_start`, `bench_run_status`, `bench_run_stop`, `test_reactor_run`,
`test_reactor_status`, `test_reactor_stop` and `hardware_lease_status` are read
here the way a host reads them: through a real `tools/list` request answered by
the server. What they must carry is what the code does today. Which devices a
run declares and how each field of a selector names one, how long a run is held
and what ends it, what a refused start leaves held, what a repeated call
answers, what a stop releases and what it leaves to a session, what recovery a
failure gets and who decides it, where a plan is read from and which refusals
come before any step, what a detached run waits for, what a run handle looks
like and which states a run passes through, which field is the verdict and
which report stays, what a stop asks of a run, which incident needs
hardware_recover, and where each tool's view of the bench ends.

The checks are about meaning, not wording. A fact that could be stated the
wrong way round (held or taken, and or or, required or never required, `false`
or `true`, always or as the policy allows, at once or after a wait, finishes
the step or kills it) is checked as a relation inside one sentence or clause,
and every such check is run against its own inverted statement as well, which
it must refuse. A test never pins a sentence.

The second half holds the behaviour those definitions describe where no
existing test already holds it. The rest is held elsewhere and not repeated: a
run that holds its devices across calls, refuses an undeclared device, refuses
a second declaration, answers a stop with no run open, is released when the
service closes, refuses a bad `wait_s` or an unknown device before it locks
anything, gives back what it took when a later device is held, and names a
session's lease that outlives it (tests/test_devices.py); a shared bus that
needs a participant in a selector (tests/test_can_participant_sessions.py); a
stop that recovers a run left with an incident, and the recovery a policy
withholds or narrows (tests/test_run_abort_recovery.py); a lease read that
leaves its incident and agrees with `agentic-hil lease-status`
(tests/test_lease_status_tool.py); a run's devices in `bench_held` and
`held_devices` (tests/test_coordination.py); a plan run, a detached run, status,
stop, a plan outside the workspace, a permission refusal and an unprovisioned
workspace over MCP (tests/test_reactor_mcp_tools.py); the default plan, a plan
naming a device the bench lacks, a plan inside a declared run and a stop on an
ended run (tests/test_tool_descriptions.py); and a malformed or unknown handle,
a delay a stop ends early and a detached worker that never publishes, read
through the run lifecycle module (tests/test_run_lifecycle.py).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import FAKE_OPENOCD
from support import scaled_time_bound
from test_devices import config_for, mcp_call
from test_reactor_mcp_tools import RESET_PLAN, bound_service
from test_run_lifecycle import bench_workspace
from test_tool_definition_can import (
    FALSE,
    TRUE,
    clauses,
    definition,
    listed_tools,
    names,
    participant_required_on_a_shared_bus,
    property_text,
    stated,
)
from test_tool_descriptions import DESCRIPTION_LIMIT, PROPERTY_DESCRIPTION_LIMIT

import agentic_hil
import agentic_hil.runlifecycle as runlifecycle
from agentic_hil.bench import MAX_WAIT_S, BenchMutex
from agentic_hil.config import bind_debugger, load_authoritative_config
from agentic_hil.devices import debugger_device
from agentic_hil.knowledge import DEFAULT_TEST_CONFIG_PATH
from agentic_hil.runlifecycle import (
    RUN_FINISHED,
    RUN_HANDLE_PATTERN,
    RUN_RUNNING,
    RUN_STARTING,
    RUN_STOPPED,
    RUN_WORKER_GONE,
    WORKER_PUBLISH_TIMEOUT_S,
    worker_publish_window_s,
)
from agentic_hil.test_reactor import declared_devices, load_test_config
from agentic_hil.tools import AgenticHILToolService

RUN_START = "bench_run_start"
RUN_STATUS = "bench_run_status"
RUN_STOP = "bench_run_stop"
PLAN_RUN = "test_reactor_run"
PLAN_STATUS = "test_reactor_status"
PLAN_STOP = "test_reactor_stop"
LEASE = "hardware_lease_status"
TOOLS = (RUN_START, RUN_STATUS, RUN_STOP, PLAN_RUN, PLAN_STATUS, PLAN_STOP, LEASE)
READ_ONLY = (RUN_STATUS, PLAN_STATUS, LEASE)

# The annotations as they stand. The definitions are rewritten around them, so
# what they say must stay true of the text and of the code.
ANNOTATIONS = {
    RUN_START: {"title": "Declare a bench run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    RUN_STOP: {"title": "End the bench run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    RUN_STATUS: {"title": "Bench run status", "readOnlyHint": True, "openWorldHint": False},
    PLAN_RUN: {"title": "Run a test plan", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    PLAN_STATUS: {"title": "Test run status", "readOnlyHint": True, "openWorldHint": False},
    PLAN_STOP: {"title": "Stop a test run", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    LEASE: {"title": "Bench lease status", "readOnlyHint": True, "openWorldHint": False},
}

# The input schemas as they stand, every annotation keyword left out. Describing
# an input must not change what a call may pass: the schema is the gate in front
# of the code, and a narrower or wider one is a runtime change. A `description`
# or a `default` changes nothing a call may pass (contracts.py:640 validates
# with `iter_errors`, which applies no default), so they are compared apart:
# descriptions by the checks below, defaults against the code that applies them.
NONEMPTY = {"type": "string", "minLength": 1}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
SCHEMAS = {
    RUN_START: {
        "type": "object",
        "properties": {
            "devices": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind"],
                    "properties": {"kind": {"type": "string", "enum": ["debugger", "uart", "can"]}, "id": NONEMPTY, "participant": NONEMPTY},
                },
            },
            "label": NONEMPTY,
            "wait_s": {"type": "number", "minimum": 0, "maximum": 900},
        },
        "additionalProperties": False,
        "required": ["devices"],
    },
    RUN_STOP: EMPTY,
    RUN_STATUS: EMPTY,
    PLAN_RUN: {"type": "object", "properties": {"test_config_path": NONEMPTY, "detach": {"type": "boolean"}}, "additionalProperties": False},
    PLAN_STATUS: {"type": "object", "properties": {"run": NONEMPTY}, "additionalProperties": False},
    PLAN_STOP: {"type": "object", "properties": {"run": NONEMPTY}, "additionalProperties": False, "required": ["run"]},
    LEASE: EMPTY,
}

# Keywords that say something about a value without deciding whether a call
# may pass it (JSON Schema's annotation vocabulary).
ANNOTATION_KEYWORDS = frozenset({"description", "default", "examples", "title", "$comment"})

# The default each input is documented with, as the code applies it when the
# input is absent: only `detach: true` detaches (tools.py:1577), and an absent
# `wait_s` is no wait (tools.py:1436). Both are held in behaviour: a plan run
# without detach answers with its steps (tests/test_reactor_mcp_tools.py:77-97)
# and a start without wait_s fails at once (below).
IMPLEMENTED_DEFAULTS: dict[tuple[str, str], object] = {
    (PLAN_RUN, "/detach"): False,
    (RUN_START, "/wait_s"): 0,
}


@pytest.fixture(scope="module")
def listed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return listed_tools(tmp_path_factory.mktemp("td5-tools-list"))


def what_a_call_may_pass(node: object, *, naming_properties: bool = False) -> object:
    """The schema with every annotation keyword left out, at every level.

    Only where a keyword stands as one: the keys under `properties` are property
    names, and a property called `title` or `default` is kept."""
    if isinstance(node, dict):
        if naming_properties:
            return {name: what_a_call_may_pass(child) for name, child in node.items()}
        return {key: what_a_call_may_pass(value, naming_properties=key == "properties") for key, value in node.items() if key not in ANNOTATION_KEYWORDS}
    if isinstance(node, list):
        return [what_a_call_may_pass(item) for item in node]
    return node


def every_property(node: object, where: str = "") -> Iterator[tuple[str, dict]]:
    """Every property an input schema declares, at any depth, by its path."""
    if isinstance(node, dict):
        for key, child in node.items():
            if key == "properties" and isinstance(child, dict):
                for name, schema in child.items():
                    yield f"{where}/{name}", schema
                    yield from every_property(schema, f"{where}/{name}")
            else:
                yield from every_property(child, f"{where}/{key}")
    elif isinstance(node, list):
        for child in node:
            yield from every_property(child, where)


def selector_text(tool: dict, name: str) -> str:
    """One field of the device selector `bench_run_start` takes in `devices`."""
    return str(tool["inputSchema"]["properties"]["devices"]["items"]["properties"][name].get("description", ""))


# ---------------------------------------------------------------------------
# The relations a definition could state the wrong way round. Each is a named
# check, so the controls below can show it refuses the inverted claim.

AT_ONCE = r"\b(at once|immediately|no wait|without waiting|does not wait)\b"
POLICY = r"auto_recover|\bpolic(y|ies)\b"
REPORTED = r"\b(reported|result|answers?|returns?)\b"
UNCONDITIONAL = r"\balways\b|\bguarantee|\bnever\b|\bno recovery\b|\bwhatever\b|\bregardless\b|\bleft alone\b"
MISLEADING_VERDICT = r"`?\bok`? (is|means|gives|carries) the (test |run |plan )?(verdict|result)\b|\bfinished\b[^.;]*\b(means|=)\b[^.;]*\bpass"


def run_held_until_stop_or_exit(text: str) -> bool:
    """coordination.py:875-901 (`run_status`) and tests/test_devices.py:930: a run
    holds its devices until bench_run_stop or until the server closes, whichever
    comes first (coordination.py:894)."""
    return stated(
        text,
        r"\buntil\b",
        r"bench_run_stop`?,? or\b[^.;]*\b(server|process)\b|\b(server|process)\b[^.;]*\bor\b[^.;]*bench_run_stop",
        unless=r"\bsurviv|\boutlives?\b|\bpersists?\b",
    )


def run_has_no_timeout(text: str) -> bool:
    """coordination.py:879-880: nothing times a run out."""
    return stated(text, r"\bno (timeout|time ?limit|expiry)\b|\bnever (times out|expires)\b|\bdoes not (time out|expire)\b|\bnothing times it out\b")


def acquisition_is_all_or_nothing(text: str) -> bool:
    """bench.py:369-402 and tools.py:1419-1420: a refused start holds none of what it declared."""
    return stated(
        text,
        r"\ball[ -]or[ -]nothing\b|\b(holds|takes|keeps) none\b|\bnothing is (held|taken|kept)\b|\bnone (is|are) (held|taken|kept)\b",
        unless=r"\bkeeps? (what|those|the ones|the devices) it (took|got|has)\b|\bpartial(ly)? (held|acquired)\b",
    )


def undeclared_device_refused(text: str) -> bool:
    """coordination.py:940-951: a call in a run reaching a device the run did not declare."""
    return stated(text, r"undeclared_device", r"\b(only|other|others|undeclared|not declared)\b", unless=r"\bnever\b|\bany device\b")


def second_start_refused(text: str) -> bool:
    """coordination.py:739-740: a start while a run is open answers `run_already_active`."""
    return stated(text, r"\b(second|another|repeat(ed)?|again)\b", r"run_already_active", unless=r"\bnever\b|\breplac|\babsorb|\bjoins?\b")


def resolved_before_any_is_locked(text: str) -> bool:
    """devices.py:826-833 (`resolve_devices`): every selector is resolved before anything is locked."""
    return stated(text, r"\bresolv|\bchecked\b|\bvalidated\b", r"\bbefore\b", r"\block|\btaken\b|\bheld\b", unless=r"\bafter\b|\bone at a time\b|\bas (it goes|each is)\b")


def wait_defaults_to_failing_at_once(text: str) -> bool:
    """tools.py:1436 and bench.py:646-676: `wait_s` defaults to 0, and with no wait a held device is `device_busy` at once."""
    return stated(text, r"\bdefault", r"(?<![\d.])0(?![\d.])", r"device_busy", AT_ONCE, unless=r"\b(forever|indefinitely|unbounded|until (it is )?free)\b")


def wait_is_bounded_in_seconds(text: str) -> bool:
    """bench.py:63 and 666-670: seconds, finite, from 0 up to 900."""
    return stated(
        text,
        r"(?<![\d.])0(?![\d.])\s*(to|-|and|through)\s*900\b|\bbetween 0 and 900\b|\bat most 900\b|\bup to 900\b",
        r"\b(seconds?|s)\b",
        unless=r"\bat least 900\b|\bunbounded|\bno (limit|maximum)\b",
    )


def label_is_optional_with_no_default(text: str) -> bool:
    """tools.py:1442: an absent label is None; nothing stands in for it."""
    return stated(text, r"\boptional\b", r"\bno default\b", unless=r"\brequired\b|\bdefaults? to (the|a)\b")


def the_board_is_not_a_device(text: str) -> bool:
    """devices.py:766-770 and 821: the board under test is what the devices drive, not a kind."""
    return stated(text, r"\b(board|target|dut)\b", r"\bnot\b|\bno\b", r"\bkind\b|\bdevice\b")


def id_required_for_uart_and_can(text: str) -> bool:
    """devices.py:797-807: a uart or can selector without an id is `invalid_argument`."""
    return stated(text, r"\b(required|must)\b", r"\buart\b", r"\bcan\b", unless=r"\b(not|never)\s+(be\s+)?required\b|\boptional\b|\bmay (be )?(omit|left out|leave)")


def debugger_id_may_be_left_out_for_the_bound_or_only_debugger(text: str) -> bool:
    """devices.py:710-731 and 792-795: only a debugger selector may omit `id`, and
    it then means the debugger this server is bound to or the only one configured
    (devices.py:715); configreload.py:257 keeps the binding when another appears."""
    return stated(
        text,
        r"\bomit|\bleft out\b|\bleave (it )?out\b|\bwithout (an )?id\b",
        r"\bdebugger\b",
        r"\bbound\b",
        r"\b(only|sole|single|exactly one)\b",
        unless=r"\b(uart|ports?|any kind|every kind|all kinds)\b",
    )


def participant_only_for_can(text: str) -> bool:
    """devices.py:792-793: a participant on any other kind is `invalid_argument`."""
    return stated(text, r"\bonly\b", r"\bcan\b", r"invalid_argument", unless=r"\b(any|every|all) kinds?\b|\buart\b|\bdebugger\b")


def no_run_answers_run_was_active_false(text: str) -> bool:
    """coordination.py:833-847: with no run open the stop answers ok with `run_was_active: false`."""
    return stated(text, r"\b(no|not|without|nothing)\b", r"\brun\b", r"run_was_active" + FALSE)


def a_session_keeps_its_own_device(text: str) -> bool:
    """coordination.py:849-873 and tools.py:1486-1514: a live COM, CAN or debug
    session keeps its lease when the run ends, named in `open_leases` and `still_held_devices`."""
    return stated(
        text,
        r"\bsessions?\b",
        r"\b(keeps?|kept|still|stays?|remains?)\b",
        r"open_leases|still_held_devices",
        unless=r"\bsessions? (is|are) (also )?(closed|ended|stopped)\b|\b(closes|ends|stops) (every|all|each|any|its|the|open) (open )?sessions?\b",
    )


def an_open_incident_runs_the_recovery_its_policy_allows(text: str) -> bool:
    """tools.py:1466-1467 and 1469-1484: an incident still open once the devices are
    back runs the recovery, which `recovery.auto_recover` may withhold
    (tools.py:1912), narrow to a probe re-read (tools.py:1953) or leave unconfirmed
    (tools.py:1958); the `recovery` block says which."""
    return stated(text, r"\bincident\b", r"\brecovery\b", POLICY, REPORTED, unless=UNCONDITIONAL)


def a_failing_step_runs_the_recovery_its_policy_allows(text: str) -> bool:
    """test_reactor.py:3284-3298 and tools.py:1846-1928: a failed step or cleanup
    runs the recovery the policy and the probe's grants allow, in `recovery`."""
    return stated(text, r"\bfail", r"\brecovery\b", POLICY, REPORTED, unless=UNCONDITIONAL)


def lease_status_sees_other_holders(text: str) -> bool:
    """coordination.py:875-901 reads this server's own coordinator only; the
    lease status reads the machine-wide device holds (coordination.py:1687-1692)."""
    return stated(
        text,
        r"hardware_lease_status",
        r"\b(other|another|any) (process|processes|server|servers|holder|holders|program)\b|\bmachine\b|\belsewhere\b",
        unless=r"\bsame\b",
    )


def default_plan_named(text: str) -> bool:
    """knowledge.py:56 and test_reactor.py:628-630: without a path the plan is `.agentic-hil/testconfig.yaml`."""
    return stated(text, re.escape(DEFAULT_TEST_CONFIG_PATH), r"\bdefault|\bwithout\b|\bomitted\b|\bunless\b")


def plan_held_to_the_workspace(text: str) -> bool:
    """test_reactor.py:629-651: a path is read relative to workspace_root, and one
    that resolves outside it is refused (test_reactor.py:633)."""
    return stated(
        text,
        r"workspace_root",
        r"\b(inside|within|under)\b|\boutside\b[^.;]*\b(fails?|refused|test_config_invalid)\b",
        unless=r"\banywhere\b|\bany path\b|\b(allowed|accepted|permitted)\b",
    )


def missing_plan_not_found(text: str) -> bool:
    """test_reactor.py:652-655."""
    return stated(text, r"test_config_not_found", r"\b(missing|absent|not found|does not exist|no file)\b", unless=r"\bnever\b")


def bad_plan_invalid(text: str) -> bool:
    """test_reactor.py:642-651, 676-695, 747, 812 and 857: outside the workspace, not UTF-8, not YAML, or failing validation."""
    return stated(text, r"test_config_invalid", r"\b(outside|malformed|invalid|bad|schema|absent|unknown|YAML|lacks)\b", unless=r"\bnever\b")


def inside_a_declared_run_refused(text: str) -> bool:
    """tools.py:1547-1566: a plan asked for while bench_run_start's run is open is `run_already_active`."""
    return stated(
        text,
        r"run_already_active",
        r"bench_run_start|bench_run_stop|\bdeclared run\b|\bbench run\b",
        unless=r"\b(uses|shares|joins) the (open )?run'?s devices\b|\bnever\b",
    )


def held_device_fails_at_once(text: str) -> bool:
    """tools.py:1577-1579 passes no wait, so reactorrun.py:118-145 answers `device_busy` with no step run."""
    return stated(text, r"device_busy", AT_ONCE, unless=r"\bwaits? (for|until)\b")


def permission_refused_before_any_step(text: str) -> bool:
    """tests/test_reactor_mcp_tools.py:206-249: preflight refuses the whole plan before its first hardware action."""
    return stated(text, r"permission_denied", r"\bbefore\b|\bno step\b")


ONE_BY_ONE = r"\bone by one\b|\bone at a time\b|\bsingle (tool )?calls?\b"
NOT_USED = r"\b(never|not|don't)\s+use\b|\b(cannot|can't)\b"


def calls_one_by_one_belong_to_a_bench_run(text: str) -> bool:
    """A plan file is test_reactor_run's job; the calls an agent makes one by one
    are held together by a bench_run_start run, inside which a plan is refused
    (tools.py:1547-1566)."""
    return stated(text, r"\buse\b", ONE_BY_ONE, r"\bbench_run_start\b", unless=NOT_USED + r"|\binstead of bench_run_start\b")


def a_bench_run_is_read_by_its_own_status(text: str) -> bool:
    """test_reactor_status reads a `run-` handle's record (runlifecycle.py:59,
    1001); a bench_run_start run has none and bench_run_status reads it from this
    server's memory (tools.py:1523, coordination.py:875-901)."""
    return stated(text, r"\bbench_run_start\b", r"\buse bench_run_status\b", unless=NOT_USED + r"|\binstead of bench_run_status\b")


def plan_stop_ends_the_run(text: str) -> bool:
    """runlifecycle.py:1115-1177: test_reactor_stop asks a detached plan run to end; bench_run_stop does not reach it."""
    return stated(text, r"\btest_reactor_stop\b", r"\b(ends?|stops?)\b", unless=NOT_USED + r"|\bbench_run_stop\b")


def detach_defaults_to_false(text: str) -> bool:
    """contracts.py `detach` default and tools.py:1577-1579."""
    return stated(text, r"\bdefault", r"\bfalse\b", unless=r"\bdefault(s)?\b[^.;]*\btrue\b")


def a_plain_run_answers_at_the_plans_end(text: str) -> bool:
    """reactorrun.py `run_plan`: without detach the call ends with the plan and answers with its steps and verdict."""
    return stated(
        text,
        r"\bfalse\b|\bwithout detach\b|\bsynchronous",
        r"\b(when|until|once) the plan (ends|finishes|has ended)\b|\bplan'?s end\b|\bend of the plan\b",
        r"\bsteps\b|\breport\b|\bverdict\b",
        unless=r"\b(at once|immediately)\b",
    )


def detach_waits_for_the_worker_not_the_plan(text: str) -> bool:
    """runlifecycle.py:703-717 and 720-822: detached, the call waits for the worker
    to publish, at most 30 s with no device wait (runlifecycle.py:63, 759), never
    for the plan's end, and answers with `run` and `state` (or, for a run that
    already ended, its verdict, runlifecycle.py:805)."""
    return stated(
        text,
        r"\btrue\b|\bdetach",
        r"\b(not|without|never)\b[^.;]*\bplan\b|\bbefore the plan\b",
        r"\bworker\b",
        r"\b30 ?s\b|\b30 seconds\b",
        r"\bstate\b|\bhandle\b",
        unless=r"\bwaits? (for|until) the plan\b|\b(at once|immediately)\b",
    )


def handle_shape_named(text: str) -> bool:
    """runlifecycle.py:58 and 132-139: `run-` and sixteen hex digits, else `invalid_argument`."""
    return stated(text, r"`?run-`?", r"\b(16|sixteen)\b", r"\bhex")


def without_a_handle_lists_runs(text: str) -> bool:
    """runlifecycle.py:986-987 and 1073-1113: no handle lists the bench's runs."""
    return stated(
        text,
        r"\bwithout\b|\bomit|\bno `?run`?\b|\bleave (it )?out\b",
        r"\blists?\b|`runs`|active_runs",
        unless=r"invalid_argument|\brequired\b",
    )


def worker_gone_means_the_process_died(text: str) -> bool:
    """runlifecycle.py:1001-1022 and 1145-1159: the process running the plan is gone."""
    return stated(text, r"worker_gone", r"\b(died|dead|gone|exited|crashed)\b|hardware_lease_status")


def run_ok_is_the_verdict(text: str) -> bool:
    """runlifecycle.py:1026 and 647: status `ok` says the read worked; `run_ok` is
    the run's verdict once it ended, and `finished` is not a pass."""
    return stated(text, r"run_ok", r"\bverdict\b|\bpass(ed)?\b") and not any(names(MISLEADING_VERDICT, clause) for clause in clauses(text))


def canonical_report_outlives_the_mirror(text: str) -> bool:
    """runlifecycle.py:1034-1047: `canonical_report_path` is the run's own report;
    `report_path` is a mirror the next run overwrites."""
    return stated(text, r"canonical_report_path", r"\b(own|stays|kept|keeps|stable)\b", unless=r"\bnext run\b|\bmirror\b|\boverwrit") and stated(
        text, r"(?<!\w)report_path\b", r"\bmirror\b|\boverwrit|\bnext run\b", unless=r"canonical_report_path"
    )


def stop_writes_a_request_the_run_reads(text: str) -> bool:
    """runlifecycle.py:1115-1121 and 1160-1161: the call writes a request file the
    run reads; nothing reaches the process."""
    return stated(
        text,
        r"\bwrit(es?|ing)\b",
        r"\brequest\b",
        r"\breads?\b",
        unless=r"\b(signals?|signalling|kills?|killing|terminates?|terminating)\b",
    )


def stop_finishes_the_step_first(text: str) -> bool:
    """runlifecycle.py:1117-1121 and 1166-1167: cooperative, the run finishes its current step."""
    return stated(text, r"\b(finish|finishes|completes|after)\b", r"\bstep\b", unless=r"\b(kill|kills|killed|abort|aborts|interrupt|interrupts|mid-step)\b")


def a_waiting_step_ends_early(text: str) -> bool:
    """test_reactor.py:1417-1450 and runlifecycle.py:1118-1120: a delay, and a wait
    for a device another run holds, read the request and end early."""
    return stated(text, r"\b(delay|wait)", r"\bends? early\b|\bcut short\b", unless=r"\b(not|never)\b[^.;]*\b(early|short)\b|\bruns? (out|to (its|the) end)\b")


def stop_closes_devices_and_writes_the_report(text: str) -> bool:
    """runlifecycle.py:1167: it closes its devices in the usual order and writes its report."""
    return stated(text, r"\b(closes|releases)\b", r"\bdevices?\b", r"\breport\b", unless=r"\bno report\b|\bheld\b")


def a_starting_run_ends_before_any_step(text: str) -> bool:
    """runlifecycle.py:1162-1165: a run still taking its devices ends before any step runs."""
    return stated(text, r"\b(starting|taking its devices|waiting for (a|its) devices?)\b", r"\bbefore (any|its first|the first) step\b")


def ended_run_answers_stop_requested_false(text: str) -> bool:
    """runlifecycle.py:1136-1144."""
    return stated(text, r"\b(ended|finished|already|over|done)\b", r"stop_requested" + FALSE)


def a_repeat_on_a_live_run_answers_true(text: str) -> bool:
    """runlifecycle.py:1160-1177: a run not yet ended that is asked again has its
    request written anew and answers `stop_requested: true` again."""
    return stated(text, r"\b(repeat(ed)?|again|second)\b", r"stop_requested" + TRUE, unless=r"stop_requested" + FALSE + r"|\brefused\b|\bfails?\b")


def stop_answers_before_the_run_ends(text: str) -> bool:
    """runlifecycle.py:1160-1177: the call writes the request and returns; the run ends later."""
    return stated(
        text,
        r"\b(at once|immediately|does not wait|only asks|returns before)\b",
        r"stop_requested" + TRUE + r"|\bstopped\b|test_reactor_status",
        unless=r"\bwaits? (for|until)\b",
    )


def worker_gone_refusal(text: str) -> bool:
    """runlifecycle.py:1145-1159."""
    return stated(text, r"run_worker_gone", r"\b(died|dead|gone|exited|crashed)\b|hardware_lease_status")


def only_a_standing_incident_needs_recover(text: str) -> bool:
    """coordination.py:706-720 and 1708: only an incident whose evidence chain broke stands."""
    return stated(
        text,
        r"\bonly\b",
        r"\bstand|incident_stands",
        r"\b(needs?|requires?|takes?|calls? for)\b",
        r"hardware_recover",
        unless=r"\bwithout hardware_recover\b|\b(every|any|all) (open )?incidents?\b|incident_stands" + FALSE + r"|\b(does not|doesn't|never) stand",
    )


def other_incidents_settle_at_the_next_call(text: str) -> bool:
    """coordination.py:2450 and 2493-2505: a non-standing incident is settled or stood down by the next hardware call."""
    return stated(text, r"\bnext\b", r"\bcall\b", r"\b(settles?|clears?|stands? (it )?down|resolves?)\b", unless=r"\brefused until\b|\bnever\b")


def held_devices_cover_every_process(text: str) -> bool:
    """coordination.py:1687-1692 and 1928: `bench_held` and `held_devices` read the machine-wide device holds."""
    return stated(
        text,
        r"bench_held|held_devices",
        r"\b(any|every|other|another) (process|processes|server|servers|holder|holders|program)\b|\bmachine\b|\bruns? and sessions?\b",
        unless=r"\bonly (this|the) (server|process)\b",
    )


def standing_incidents_belong_to_others(text: str) -> bool:
    """coordination.py:1693-1701: a neighbour's unresolved record on shared devices."""
    return stated(text, r"standing_incidents", r"\b(other|another|neighbou?r'?s?)\b", unless=r"\bthis project'?s\b|\byours\b(?! to)")


def reads_without_driving(text: str) -> bool:
    """tools.py:607-613 and coordination.py:1626-1735: no backend call, no stand-down."""
    return stated(
        text,
        r"\b(drives|touches|reaches|contacts|resets|moves) no (device|board|target|probe)\b|\bnever (drives|touches|reaches|contacts|resets)\b|\bno (device|board|target) is (driven|touched|reset)\b",
    )


RELATIONS: list[tuple[Callable[[str], bool], str, str]] = [
    (run_held_until_stop_or_exit, "It holds them until bench_run_stop or server exit.", "It holds them until bench_run_stop and survives a server exit."),
    (run_held_until_stop_or_exit, "It holds them until bench_run_stop or server exit.", "It holds them until bench_run_stop and server exit."),
    (run_has_no_timeout, "Held with no timeout.", "The run times out after 15 minutes."),
    (acquisition_is_all_or_nothing, "All or nothing: a refused start holds none.", "A refused start keeps the devices it got and waits for the rest."),
    (acquisition_is_all_or_nothing, "Nothing is held when one device is busy.", "Not all or nothing: it keeps what it took."),
    (undeclared_device_refused, "Other devices fail `undeclared_device`.", "It may touch any device; `undeclared_device` is never returned."),
    (second_start_refused, "A second start fails `run_already_active`.", "A second start replaces the open run instead of `run_already_active`."),
    (resolved_before_any_is_locked, "All resolved before any is locked.", "Each is locked as it is resolved, one at a time."),
    (wait_defaults_to_failing_at_once, "Default 0: a held device fails `device_busy` at once.", "Default 0 waits until it is free, then `device_busy`."),
    (wait_defaults_to_failing_at_once, "Default 0, so `device_busy` comes immediately.", "Default 900: `device_busy` at once only after the wait."),
    (wait_defaults_to_failing_at_once, "Default 0: `device_busy` at once.", "Default 10: `device_busy` at once."),
    (wait_is_bounded_in_seconds, "Seconds to wait, 0 to 900.", "Seconds to wait; no maximum, 900 is only typical."),
    (wait_is_bounded_in_seconds, "Seconds to wait, at most 900.", "Seconds to wait, at least 900."),
    (wait_is_bounded_in_seconds, "Seconds to wait, 0 to 900.", "Seconds to wait, 900 to 0."),
    (label_is_optional_with_no_default, "Optional, no default: free text.", "Optional; defaults to the project name."),
    (label_is_optional_with_no_default, "Optional, no default.", "Required, no default."),
    (the_board_is_not_a_device, "The board under test is not a kind.", "The board under test is a kind too."),
    (id_required_for_uart_and_can, "Required for uart and can.", "Optional for uart and can."),
    (
        debugger_id_may_be_left_out_for_the_bound_or_only_debugger,
        "A debugger may leave it out, meaning the one this server is bound to or the only one configured.",
        "A debugger may leave it out only when exactly one is configured.",
    ),
    (
        debugger_id_may_be_left_out_for_the_bound_or_only_debugger,
        "id may be omitted for a debugger: the bound one, or the only one.",
        "id may be omitted for a uart or a debugger: the bound one, or the only one.",
    ),
    (participant_only_for_can, "Only for can, else `invalid_argument`.", "Any kind may carry one; `invalid_argument` only for a uart."),
    (no_run_answers_run_was_active_false, "With no run open it answers `run_was_active: false`.", "With no run open it answers `run_was_active: true`."),
    (no_run_answers_run_was_active_false, "Safe with no run open: `run_was_active: false`.", "With no run open it fails `run_not_active`."),
    (a_session_keeps_its_own_device, "A session still open keeps its own device: `open_leases`, `still_held_devices`.", "It also closes every open session; `still_held_devices` is then empty."),
    (
        an_open_incident_runs_the_recovery_its_policy_allows,
        "With an incident open it runs the recovery `recovery.auto_recover` allows, reported in `recovery`.",
        "With an incident open it always resets the target into halt, reported in `recovery`.",
    ),
    (
        an_open_incident_runs_the_recovery_its_policy_allows,
        "An open incident gets the recovery the policy allows, reported in `recovery`.",
        "An open incident gets no recovery, whatever the policy, reported in `recovery`.",
    ),
    (
        a_failing_step_runs_the_recovery_its_policy_allows,
        "A failing step runs the recovery `recovery.auto_recover` allows, result `recovery`.",
        "A failing step always resets the target into halt.",
    ),
    (
        a_failing_step_runs_the_recovery_its_policy_allows,
        "A failing step runs the recovery the policy allows, reported in `recovery`.",
        "A failing step halts the target whatever the policy, reported in `recovery`.",
    ),
    (lease_status_sees_other_holders, "What another process holds shows in hardware_lease_status.", "hardware_lease_status shows the same as this."),
    (default_plan_named, "Default `.agentic-hil/testconfig.yaml`.", "Required: there is no `.agentic-hil/testconfig.yaml` fallback."),
    (plan_held_to_the_workspace, "A path inside workspace_root.", "Any path works, inside workspace_root or anywhere."),
    (plan_held_to_the_workspace, "Outside workspace_root fails `test_config_invalid`.", "Paths outside workspace_root are allowed."),
    (missing_plan_not_found, "Missing: `test_config_not_found`.", "A missing plan is `test_config_invalid`; `test_config_not_found` is never returned."),
    (bad_plan_invalid, "Outside it or malformed: `test_config_invalid`.", "`test_config_invalid` is never returned for a malformed plan."),
    (inside_a_declared_run_refused, "Inside a bench_run_start run it fails `run_already_active`.", "Inside a bench_run_start run it uses the run's devices, never `run_already_active`."),
    (held_device_fails_at_once, "A device held elsewhere fails `device_busy` at once.", "It waits for a held device, up to 900 s, then `device_busy`."),
    (permission_refused_before_any_step, "A step whose permission is off fails `permission_denied` before any step runs.", "A step whose permission is off fails `permission_denied` when reached, after the earlier steps ran."),
    (calls_one_by_one_belong_to_a_bench_run, "For calls made one by one, use bench_run_start.", "For calls made one by one, use this tool instead of bench_run_start."),
    (calls_one_by_one_belong_to_a_bench_run, "For calls made one by one, use bench_run_start.", "For a plan file, use bench_run_start."),
    (a_bench_run_is_read_by_its_own_status, "For a bench_run_start run, use bench_run_status.", "For a bench_run_start run, use this tool instead of bench_run_status."),
    (a_bench_run_is_read_by_its_own_status, "For a bench_run_start run, use bench_run_status.", "For a test_reactor_run run, use bench_run_status."),
    (plan_stop_ends_the_run, "With detach, test_reactor_stop ends it.", "With detach, test_reactor_stop cannot end it; bench_run_stop does."),
    (detach_defaults_to_false, "Default false: returns when the plan ends.", "Default true: answers at once."),
    (detach_defaults_to_false, "Default false.", "Default true, false waits."),
    (a_plain_run_answers_at_the_plans_end, "Default false: returns when the plan ends, with `steps` and `report_path`.", "Default false: answers at once with a handle; `steps` come from test_reactor_status."),
    (a_plain_run_answers_at_the_plans_end, "Default false: answers at the plan's end with `ok`, `steps`.", "Default false: answers with `ok`, `steps` from the first step."),
    (
        detach_waits_for_the_worker_not_the_plan,
        "True: once a worker holds the devices (30 s max), not at the plan's end: `run`, `state`.",
        "True: waits until the plan ends, then gives `run` and `state`.",
    ),
    (
        detach_waits_for_the_worker_not_the_plan,
        "True: once its worker publishes, within 30 s, not when the plan ends: `run`, `state`.",
        "True: answers at once with `run` and `state`, not at the plan's end.",
    ),
    (handle_shape_named, "`run-` and 16 hex digits.", "Any text naming the run."),
    (without_a_handle_lists_runs, "Without run, lists this bench's runs: `runs`, `active_runs`.", "Without run it fails `invalid_argument`."),
    (worker_gone_means_the_process_died, "worker_gone: its process died.", "worker_gone means the run is still starting."),
    (run_ok_is_the_verdict, "`ok` means the read worked; `run_ok` is the verdict once ended.", "`ok` is the test verdict; `run_ok` is the verdict too."),
    (run_ok_is_the_verdict, "`run_ok` is the verdict once ended.", "`run_ok` is the verdict; finished means it passed."),
    (
        canonical_report_outlives_the_mirror,
        "`canonical_report_path` is its own report; `report_path` is a mirror the next run overwrites.",
        "`canonical_report_path` is a mirror the next run overwrites; `report_path` is its own report.",
    ),
    (stop_writes_a_request_the_run_reads, "Ask a run to stop by writing a request it reads between steps.", "Stop a run by signalling its process, which reads no request it writes."),
    (stop_writes_a_request_the_run_reads, "It writes a request the run reads.", "It kills the run's process; the run reads nothing."),
    (stop_finishes_the_step_first, "It finishes the step it is in.", "It kills the run at once, mid-step."),
    (a_waiting_step_ends_early, "A delay or device wait ends early.", "A delay runs to its end before the run stops."),
    (stop_closes_devices_and_writes_the_report, "It closes its devices and writes its report.", "It stops with its devices held and no report."),
    (a_starting_run_ends_before_any_step, "A run still taking its devices ends before any step.", "A starting run runs its first step, then stops."),
    (ended_run_answers_stop_requested_false, "An ended run answers `stop_requested: false`.", "An ended run answers `stop_requested: true`."),
    (a_repeat_on_a_live_run_answers_true, "Answers at once `stop_requested: true`, also on a repeat.", "A repeat answers `stop_requested: false`."),
    (
        a_repeat_on_a_live_run_answers_true,
        "Asked again, a live run answers `stop_requested: true`.",
        "Asked again, a live run fails `stop_already_requested`; the first answered `stop_requested: true`.",
    ),
    (stop_answers_before_the_run_ends, "Answers at once with `stop_requested: true`.", "It waits until the run has stopped."),
    (worker_gone_refusal, "`run_worker_gone` when its process died.", "`run_worker_gone` means the stop was delivered."),
    (only_a_standing_incident_needs_recover, "Only an incident that stands needs hardware_recover.", "Every open incident needs hardware_recover, standing or not."),
    (only_a_standing_incident_needs_recover, "Only `incident_stands: true` needs hardware_recover.", "Only an incident that stands is cleared without hardware_recover."),
    (only_a_standing_incident_needs_recover, "Only `incident_stands: true` needs hardware_recover.", "Only `incident_stands: false` needs hardware_recover."),
    (other_incidents_settle_at_the_next_call, "Otherwise the next hardware call settles it or stands it down.", "Until hardware_recover runs, the next call is refused."),
    (held_devices_cover_every_process, "`bench_held` and `held_devices` cover any process.", "`held_devices` lists only this server's devices."),
    (standing_incidents_belong_to_others, "`standing_incidents` are other projects' incidents.", "`standing_incidents` lists this project's incidents."),
    (reads_without_driving, "It drives no device.", "It resets the target to read its state."),
]


@pytest.mark.parametrize(("check", "true_statement", "inverted"), RELATIONS, ids=[f"{check.__name__}-{index}" for index, (check, _, _) in enumerate(RELATIONS)])
def test_each_relation_check_refuses_its_inverted_statement(check: Callable[[str], bool], true_statement: str, inverted: str) -> None:
    assert check(true_statement), true_statement
    assert not check(inverted), inverted


# ---------------------------------------------------------------------------
# What every definition owes, whatever tool it is.


@pytest.mark.parametrize("tool_name", TOOLS)
def test_every_input_at_any_depth_carries_its_own_description_within_the_limits(listed: dict[str, dict], tool_name: str) -> None:
    """A property inside an array's items is an input a host shows like any
    other, so the selector's `kind`, `id` and `participant` owe a description as
    much as `devices` does."""
    tool = listed[tool_name]
    found = dict(every_property(tool["inputSchema"]))

    undescribed = sorted(path for path, schema in found.items() if not str(schema.get("description", "")).strip())
    assert undescribed == [], f"{tool_name}: {undescribed}"
    over = {path: len(schema["description"]) for path, schema in found.items() if len(schema["description"]) > PROPERTY_DESCRIPTION_LIMIT}
    assert over == {}, over
    assert len(tool["description"]) <= DESCRIPTION_LIMIT, len(tool["description"])


def test_the_walk_reaches_the_selector_fields(listed: dict[str, dict]) -> None:
    found = {path for path, _ in every_property(listed[RUN_START]["inputSchema"])}

    assert found == {"/devices", "/devices/items/kind", "/devices/items/id", "/devices/items/participant", "/label", "/wait_s"}, found


@pytest.mark.parametrize("tool_name", TOOLS)
def test_describing_an_input_leaves_what_a_call_may_pass_unchanged(listed: dict[str, dict], tool_name: str) -> None:
    assert what_a_call_may_pass(listed[tool_name]["inputSchema"]) == SCHEMAS[tool_name]
    assert "outputSchema" not in listed[tool_name]


def test_annotation_keywords_are_left_out_only_where_they_stand_as_keywords() -> None:
    schema = {"type": "object", "default": {}, "properties": {"title": {"type": "string", "title": "x", "default": "a"}}}

    assert what_a_call_may_pass(schema) == {"type": "object", "properties": {"title": {"type": "string"}}}


def test_every_documented_default_is_the_one_the_code_applies(listed: dict[str, dict]) -> None:
    """A `default` a host reads is a value it may leave out on that promise, so
    it has to be the value the code uses when the input is absent, of the same
    kind: `0` is not `false`."""
    documented = {(tool_name, path): schema["default"] for tool_name in TOOLS for path, schema in every_property(listed[tool_name]["inputSchema"]) if "default" in schema}

    assert (PLAN_RUN, "/detach") in documented, documented
    for key, value in documented.items():
        assert key in IMPLEMENTED_DEFAULTS, key
        expected = IMPLEMENTED_DEFAULTS[key]
        assert (isinstance(value, bool), value) == (isinstance(expected, bool), expected), (key, value)


@pytest.mark.parametrize("tool_name", TOOLS)
def test_the_annotations_stay_and_the_text_does_not_contradict_them(listed: dict[str, dict], tool_name: str) -> None:
    tool = listed[tool_name]

    assert tool["annotations"] == ANNOTATIONS[tool_name], tool["annotations"]
    if tool_name not in READ_ONLY:
        assert not names(r"\bread-only\b|\b(changes|writes|touches) nothing\b", tool["description"]), tool["description"]


def test_every_identifier_a_definition_names_is_a_listed_tool_or_a_value_the_code_spells(listed: dict[str, dict]) -> None:
    """A field, code or tool written in a definition has to be one the code
    spells. A name no code returns is an outcome a model would wait for in vain,
    and a tool that is not listed is a call that answers `unknown_tool`."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path(agentic_hil.__file__).parent.rglob("*.py"))
    for tool_name in TOOLS:
        text = definition(listed[tool_name])
        written = set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text)) - set(listed)
        unknown = sorted(token for token in written if f'"{token}"' not in source)
        assert unknown == [], (tool_name, unknown)


@pytest.mark.parametrize("tool_name", TOOLS)
def test_no_definition_speaks_in_command_line_flags(listed: dict[str, dict], tool_name: str) -> None:
    """An MCP caller passes arguments, not flags: a `--run` or `--detach` names
    something no tool takes (runlifecycle.py:1112 says it in a summary, which is
    reported, not repeated)."""
    text = definition(listed[tool_name])

    assert not re.search(r"(?<![\w-])--[a-z]", text), text


# Every refusal a tool answers that a caller can act on, by the branch that
# answers it. A definition that leaves one out leaves the caller to meet it
# unexplained.
REFUSALS: dict[str, tuple[str, ...]] = {
    # devices.py:716-731 and 734-758 (unknown_device), bench.py:487-531
    # (device_busy), coordination.py:739-740 (run_already_active), 940-951
    # (undeclared_device) and 747 (resource_quarantined); devices.py:792-793,
    # 797-807 and 715-728 (invalid_argument), 809-810 (can_participant_required)
    # and 746 (can_participant_not_configured).
    RUN_START: (
        "unknown_device",
        "device_busy",
        "run_already_active",
        "undeclared_device",
        "resource_quarantined",
        "invalid_argument",
        "can_participant_required",
        "can_participant_not_configured",
    ),
    # test_reactor.py:652-655 (not_found), 642-651 and 676-695 (invalid), 658
    # (unreadable); tests/test_reactor_mcp_tools.py:206-249 (permission_denied);
    # tools.py:1547-1566 (run_already_active); reactorrun.py:118-145
    # (device_busy); runlifecycle.py:784-797 (run_worker_unresponsive).
    PLAN_RUN: (
        "test_config_not_found",
        "test_config_invalid",
        "test_config_unreadable",
        "permission_denied",
        "run_already_active",
        "device_busy",
        "run_worker_unresponsive",
    ),
    # runlifecycle.py:989-998 (run_not_found), 195-215 (run_state_invalid),
    # 132-139 (invalid_argument).
    PLAN_STATUS: ("run_not_found", "run_state_invalid", "invalid_argument"),
    # runlifecycle.py:1124-1133, 1145-1159, 195-215 and 132-139.
    PLAN_STOP: ("run_not_found", "run_worker_gone", "run_state_invalid", "invalid_argument"),
}


@pytest.mark.parametrize(("tool_name", "error_type"), [(tool_name, code) for tool_name, codes in REFUSALS.items() for code in codes])
def test_every_refusal_a_tool_answers_is_named_in_its_definition(listed: dict[str, dict], tool_name: str, error_type: str) -> None:
    text = definition(listed[tool_name])

    assert names(rf"\b{error_type}\b", text), (error_type, text)


# ---------------------------------------------------------------------------
# bench_run_start: what a run declares, holds and refuses.


def test_bench_run_start_says_how_long_a_run_holds_and_what_it_may_touch(listed: dict[str, dict]) -> None:
    """Held until bench_run_stop or the server's exit, with no timeout
    (coordination.py:875-901); taken all or nothing (bench.py:369-402); a call
    reaching an undeclared device is refused (coordination.py:940-951); a second
    start is refused (coordination.py:739-740)."""
    text = definition(listed[RUN_START])

    assert run_held_until_stop_or_exit(text), text
    assert run_has_no_timeout(text), text
    assert acquisition_is_all_or_nothing(text), text
    assert undeclared_device_refused(text), text
    assert second_start_refused(text), text


def test_bench_run_start_names_its_result_fields(listed: dict[str, dict]) -> None:
    """Success carries `declared_devices` and `run_label` (coordination.py:815-831)."""
    text = definition(listed[RUN_START])

    for token in ("declared_devices", "run_label"):
        assert names(rf"\b{token}\b", text), (token, text)


def test_devices_says_what_the_run_holds_and_when_it_is_checked(listed: dict[str, dict]) -> None:
    text = property_text(listed[RUN_START], "devices")

    assert names(r"\bdeclared_devices\b", text), text
    assert resolved_before_any_is_locked(text), text


def test_kind_says_which_config_section_each_kind_names(listed: dict[str, dict]) -> None:
    """contracts.py DEVICE_SELECTOR and devices.py:761-770 (`config_devices`):
    a debugger is a `debuggers` entry, a uart a `com_ports` one, a can a
    `can_buses` one; the board under test is none of them (devices.py:821)."""
    text = selector_text(listed[RUN_START], "kind")

    assert names(r"\bdebugger\b[^,;.]*\bdebuggers\b", text), text
    assert names(r"\buart\b[^,;.]*\bcom_ports\b", text), text
    assert names(r"\bcan\b[^,;.]*\bcan_buses\b", text), text
    assert the_board_is_not_a_device(text), text


def test_id_says_what_it_names_and_when_it_may_be_left_out(listed: dict[str, dict]) -> None:
    """A name the configuration lacks is `unknown_device` (devices.py:716-758); a
    uart or can needs one (devices.py:797-807); a debugger may leave it out for
    the bound debugger or the only one, else `invalid_argument` (devices.py:715-728)."""
    text = selector_text(listed[RUN_START], "id")

    assert stated(text, r"\b(config|configuration)\b", r"\b(entry|name)\b"), text
    assert names(r"\bunknown_device\b", text), text
    assert id_required_for_uart_and_can(text), text
    assert debugger_id_may_be_left_out_for_the_bound_or_only_debugger(text), text
    assert names(r"\binvalid_argument\b", text), text


def test_participant_says_where_it_belongs_and_what_a_shared_bus_needs(listed: dict[str, dict]) -> None:
    """devices.py:792-793 (only for can), 809-810 (a bus with `shares` needs one)
    and 746 (a name the bus lacks)."""
    text = selector_text(listed[RUN_START], "participant")

    assert participant_only_for_can(text), text
    assert participant_required_on_a_shared_bus(text), text
    for code in ("can_participant_required", "can_participant_not_configured"):
        assert names(rf"\b{code}\b", text), (code, text)


def test_label_says_it_is_optional_and_where_it_appears(listed: dict[str, dict]) -> None:
    """No label is None (tools.py:1442); a label is the run's `run_label` and the
    holder label another owner is refused with (coordination.py:812-813,
    bench.py:496-503), both held by tests/test_devices.py:841-880."""
    text = property_text(listed[RUN_START], "label")

    assert label_is_optional_with_no_default(text), text
    assert names(r"\brun_label\b", text), text
    assert names(r"\bholder\b|\bdevice_busy\b", text), text


def test_wait_s_names_its_unit_range_and_default(listed: dict[str, dict]) -> None:
    text = property_text(listed[RUN_START], "wait_s")

    assert MAX_WAIT_S == 900.0
    assert wait_is_bounded_in_seconds(text), text
    assert wait_defaults_to_failing_at_once(text), text


# ---------------------------------------------------------------------------
# bench_run_stop and bench_run_status: what ends, what stays, what is seen.


def test_bench_run_stop_says_what_it_releases_and_what_it_leaves(listed: dict[str, dict]) -> None:
    """`released_devices` (coordination.py:846); `run_was_active: false` with no
    run open (coordination.py:846, tests/test_devices.py:917-928); a session keeps
    its own device (coordination.py:849-873); an incident still open runs the
    recovery its policy allows and reports it in `recovery` (tools.py:1466-1467,
    1905-1928, tests/test_run_abort_recovery.py)."""
    text = listed[RUN_STOP]["description"]

    assert RUN_START in text, text
    assert names(r"\breleased_devices\b", text), text
    assert no_run_answers_run_was_active_false(text), text
    assert a_session_keeps_its_own_device(text), text
    assert an_open_incident_runs_the_recovery_its_policy_allows(text), text


def test_bench_run_status_names_its_fields_and_where_its_view_ends(listed: dict[str, dict]) -> None:
    """In memory, this server's own run (contracts.py annotation comment,
    coordination.py:875-901); another process's holds are read by the lease
    status (coordination.py:1687-1692)."""
    text = listed[RUN_STATUS]["description"]

    for token in ("run_active", "declared_devices", "run_label", "held_devices"):
        assert names(rf"\b{token}\b", text), (token, text)
    assert names(r"\bthis (server|process)\b|\bin[- ]memory\b", text), text
    assert lease_status_sees_other_holders(text), text


# ---------------------------------------------------------------------------
# test_reactor_run: where the plan comes from, what refuses it, what it answers.


def test_test_config_path_names_the_default_plan_and_the_workspace_rule(listed: dict[str, dict]) -> None:
    text = property_text(listed[PLAN_RUN], "test_config_path")

    assert DEFAULT_TEST_CONFIG_PATH == ".agentic-hil/testconfig.yaml"
    assert default_plan_named(text), text
    assert plan_held_to_the_workspace(text), text


def test_test_reactor_run_names_the_refusals_that_come_before_any_step(listed: dict[str, dict]) -> None:
    """A missing plan, a malformed one or one outside the workspace
    (test_reactor.py:628-690); a disabled permission (tests/test_reactor_mcp_tools.py:206-249);
    a declared run already open (tools.py:1547-1566); a device another holder has,
    with no wait over MCP (tools.py:1577-1579, reactorrun.py:118-145)."""
    text = definition(listed[PLAN_RUN])

    assert missing_plan_not_found(text), text
    assert bad_plan_invalid(text), text
    assert permission_refused_before_any_step(text), text
    assert inside_a_declared_run_refused(text), text
    assert held_device_fails_at_once(text), text


def test_test_reactor_run_says_what_recovery_a_failing_plan_gets(listed: dict[str, dict]) -> None:
    """A failed step aborts into the recovery the policy and the probe's grants
    allow: withheld (tools.py:1912, 1923), a probe re-read only (tools.py:1953)
    or a reset into halt that may go unconfirmed (tools.py:1958), named in the
    result's `recovery` block (test_reactor.py:3298)."""
    text = definition(listed[PLAN_RUN])

    assert a_failing_step_runs_the_recovery_its_policy_allows(text), text


def test_detach_names_its_default_and_what_each_value_answers(listed: dict[str, dict]) -> None:
    """Default false: the call returns at the plan's end with `ok` and `steps`
    (tests/test_reactor_mcp_tools.py:77-97). True: it waits for the worker to
    publish, at most 30 s over MCP, which passes no device wait (tools.py:1577,
    reactorrun.py:37-45), and answers with `run` and `state`, or with the verdict
    of a run that already ended (runlifecycle.py:805), or `run_worker_unresponsive`
    (runlifecycle.py:784-797)."""
    text = property_text(listed[PLAN_RUN], "detach")

    assert worker_publish_window_s(0.0) == WORKER_PUBLISH_TIMEOUT_S == 30.0
    assert detach_defaults_to_false(text), text
    assert a_plain_run_answers_at_the_plans_end(text), text
    assert detach_waits_for_the_worker_not_the_plan(text), text
    assert names(r"\bverdict\b|\balready (ended|finished)\b", text), text
    assert names(r"\brun_worker_unresponsive\b", text), text


# ---------------------------------------------------------------------------
# test_reactor_status and test_reactor_stop: the handle, the states, the stop.


@pytest.mark.parametrize("tool_name", [PLAN_STATUS, PLAN_STOP])
def test_run_says_what_a_handle_is_and_where_it_comes_from(listed: dict[str, dict], tool_name: str) -> None:
    text = property_text(listed[tool_name], "run")

    assert RUN_HANDLE_PATTERN.pattern == r"^run-[0-9a-f]{16}$"
    assert handle_shape_named(text), text
    assert PLAN_RUN in text, text


def test_test_reactor_status_names_the_states_a_run_passes_through(listed: dict[str, dict]) -> None:
    text = definition(listed[PLAN_STATUS])

    for state in (RUN_STARTING, RUN_RUNNING, RUN_FINISHED, RUN_STOPPED, RUN_WORKER_GONE):
        assert names(rf"\b{state}\b", text), (state, text)
    assert worker_gone_means_the_process_died(text), text


def test_test_reactor_status_names_its_fields_and_the_listing(listed: dict[str, dict]) -> None:
    """A named run answers `state`, `stop_requested_at` and, once ended, `run_ok`,
    `canonical_report_path` and `report_path` (runlifecycle.py:1023-1047, the
    record written at 646-647); without a handle the bench's runs are listed
    (runlifecycle.py:1073-1113)."""
    text = definition(listed[PLAN_STATUS])

    for token in ("state", "stop_requested_at", "run_ok", "canonical_report_path", "report_path", "active_runs"):
        assert names(rf"\b{token}\b", text), (token, text)
    assert without_a_handle_lists_runs(text), text


def test_test_reactor_status_tells_the_read_from_the_verdict_and_the_report_from_its_mirror(listed: dict[str, dict]) -> None:
    """The status call's `ok` says the record was read (runlifecycle.py:1026);
    `run_ok` is the verdict (runlifecycle.py:647); the run's own report is
    `canonical_report_path`, and `report_path` is the mirror the next run
    overwrites (runlifecycle.py:1034-1047)."""
    text = definition(listed[PLAN_STATUS])

    assert run_ok_is_the_verdict(text), text
    assert canonical_report_outlives_the_mirror(text), text


def test_the_plan_tools_say_when_a_bench_run_is_the_tool_instead(listed: dict[str, dict]) -> None:
    """test_reactor_run runs a plan file; calls made one by one belong to a
    bench_run_start run, which bench_run_status reads and test_reactor_status does
    not. A detached plan run is ended by test_reactor_stop, whose request
    `stop_requested_at` dates (runlifecycle.py:1023-1047, 1160-1177)."""
    run = listed[PLAN_RUN]["description"]
    status = listed[PLAN_STATUS]["description"]

    assert stated(clauses(run)[0], r"\bruns?\b", r"\bplan\b"), run
    assert calls_one_by_one_belong_to_a_bench_run(run), run
    assert plan_stop_ends_the_run(run), run
    assert a_bench_run_is_read_by_its_own_status(status), status
    assert stated(status, r"\bstop_requested_at\b", rf"\b{PLAN_STOP}\b"), status


def test_test_reactor_stop_says_what_it_asks_of_the_run(listed: dict[str, dict]) -> None:
    """Cooperative (runlifecycle.py:1115-1121): the call writes a request the run
    reads; the run finishes its step, a waiting step ends early
    (test_reactor.py:1417-1450), it closes its devices and writes its report, and
    one still taking its devices ends before any step (runlifecycle.py:1162-1167).
    The call answers once the request is written (runlifecycle.py:1160-1177)."""
    text = definition(listed[PLAN_STOP])

    assert stop_writes_a_request_the_run_reads(text), text
    assert stop_finishes_the_step_first(text), text
    assert a_waiting_step_ends_early(text), text
    assert stop_closes_devices_and_writes_the_report(text), text
    assert a_starting_run_ends_before_any_step(text), text
    assert stop_answers_before_the_run_ends(text), text


def test_test_reactor_stop_names_what_a_repeat_an_ended_run_and_a_lost_run_answer(listed: dict[str, dict]) -> None:
    text = definition(listed[PLAN_STOP])

    assert a_repeat_on_a_live_run_answers_true(text), text
    assert ended_run_answers_stop_requested_false(text), text
    assert worker_gone_refusal(text), text


# ---------------------------------------------------------------------------
# hardware_lease_status: who holds the bench and what an incident needs.


def test_hardware_lease_status_names_the_fields_an_agent_acts_on(listed: dict[str, dict]) -> None:
    """coordination.py:1683-1735 and 2453-2512."""
    text = listed[LEASE]["description"]

    for token in ("bench_held", "held_devices", "blocked", "incident_stands", "cleanup_reasons", "auto_recoverable", "quarantine_guidance", "standing_incidents", "next_step"):
        assert names(rf"\b{token}\b", text), (token, text)
    assert held_devices_cover_every_process(text), text
    assert standing_incidents_belong_to_others(text), text


def test_hardware_lease_status_says_which_incident_needs_hardware_recover(listed: dict[str, dict]) -> None:
    text = listed[LEASE]["description"]

    assert only_a_standing_incident_needs_recover(text), text
    assert other_incidents_settle_at_the_next_call(text), text


def test_hardware_lease_status_says_it_drives_nothing_and_never_that_it_writes_nothing(listed: dict[str, dict]) -> None:
    """It calls no backend and skips the end-of-call stand-down (tools.py:607-613).
    It is not free of writes: with no live owner holding the project lock, a record
    left `active` is quarantined or released by the read itself
    (coordination.py:1645-1666), so the text must not promise that it writes nothing."""
    text = listed[LEASE]["description"]

    assert reads_without_driving(text), text
    assert not names(r"\b(writes|changes|modifies) nothing\b", text), text


# ---------------------------------------------------------------------------
# The behaviour those definitions describe, where nothing else holds it yet.

# Device locks are machine-wide, so the ports and probes here carry resource
# ids no other test module names.
TWO_PORTS = """com_ports:
  td5_a:
    device: "COM_TD5_A"
    resource_id: "td5-board-a"
  td5_b:
    device: "COM_TD5_B"
    resource_id: "td5-board-b"
"""
LOCK_A = "physical:td5-board-a"
LOCK_B = "physical:td5-board-b"
SECOND_DEBUGGER = f"""debuggers:
  td5_second:
    type: openocd
    probe_id: "TD5-PROBE-SECOND"
    resource_id: "td5-probe-second"
    executable: "{FAKE_OPENOCD.as_posix()}"
"""


def test_bench_run_status_reports_the_open_runs_label_and_what_this_server_holds(tmp_path: Path) -> None:
    service = AgenticHILToolService(config_for(tmp_path, com_ports_yaml=TWO_PORTS), frontend="mcp")
    try:
        before = mcp_call(service, RUN_STATUS)
        started = mcp_call(service, RUN_START, {"devices": [{"kind": "uart", "id": "td5_a"}], "label": "td5-run"})
        during = mcp_call(service, RUN_STATUS)
        stopped = mcp_call(service, RUN_STOP)
        after = mcp_call(service, RUN_STATUS)
    finally:
        service.close()

    assert before["run_active"] is False and before["declared_devices"] == [] and before["held_devices"] == [], before
    assert started["ok"] is True and started["run_label"] == "td5-run", started
    assert during["run_active"] is True, during
    assert during["run_label"] == "td5-run", during
    assert during["declared_devices"] == [LOCK_A], during
    assert during["held_devices"] == [LOCK_A], during
    assert stopped["released_devices"] == [LOCK_A] and stopped["run_was_active"] is True, stopped
    assert after["run_active"] is False and after["run_label"] is None and after["held_devices"] == [], after


def test_bench_run_start_without_wait_s_fails_at_once_on_a_held_device_and_takes_nothing(tmp_path: Path) -> None:
    """The default `wait_s` over MCP is no wait (tools.py:1436): the start answers
    `device_busy` naming the holder at once, and the free device it declared
    beside the held one is not taken."""
    service = AgenticHILToolService(config_for(tmp_path, com_ports_yaml=TWO_PORTS), frontend="mcp")
    stranger = BenchMutex(frontend="stranger", label="td5-holder")
    stranger.acquire([LOCK_B])
    try:
        began = time.monotonic()
        refused = mcp_call(service, RUN_START, {"devices": [{"kind": "uart", "id": "td5_a"}, {"kind": "uart", "id": "td5_b"}]})
        waited = time.monotonic() - began
        held = service.coordinator.bench.held_resources()
        status = mcp_call(service, RUN_STATUS)
    finally:
        stranger.release_all()
        service.close()

    assert refused["ok"] is False and refused["error_type"] == "device_busy", refused
    assert refused["resource"] == LOCK_B and refused["holder"]["label"] == "td5-holder", refused
    assert waited < scaled_time_bound(2.0), waited
    assert held == frozenset(), held
    assert status["run_active"] is False, status


def test_a_debugger_selector_without_an_id_means_the_bound_debugger_among_several(tmp_path: Path) -> None:
    """With two debuggers configured, a selector naming none means the one this
    server is bound to (devices.py:715); unbound, it is `invalid_argument` naming
    the configured debuggers (devices.py:716-728)."""
    config = config_for(tmp_path, probe_id="TD5-PROBE-DUT", debuggers_yaml=SECOND_DEBUGGER)
    assert sorted(config.debuggers) == ["dut", "td5_second"] and config.debugger_id is None, (sorted(config.debuggers), config.debugger_id)
    bound = bind_debugger(config, "td5_second")

    unbound_service = AgenticHILToolService(config, frontend="mcp")
    try:
        refused = mcp_call(unbound_service, RUN_START, {"devices": [{"kind": "debugger"}]})
    finally:
        unbound_service.close()
    service = AgenticHILToolService(bound, frontend="mcp")
    try:
        started = mcp_call(service, RUN_START, {"devices": [{"kind": "debugger"}]})
        stopped = mcp_call(service, RUN_STOP)
    finally:
        service.close()

    assert refused["ok"] is False and refused["error_type"] == "invalid_argument", refused
    assert refused["configured_debuggers"] == ["dut", "td5_second"], refused
    assert started["ok"] is True, started
    assert started["declared_devices"] == sorted(debugger_device(bound, "td5_second").lock_keys), started
    assert stopped["run_was_active"] is True, stopped


def test_a_plan_run_over_mcp_does_not_wait_for_a_device_another_holder_has(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP tool passes no wait to the run (tools.py:1577-1579), so a plan
    whose device is held answers `device_busy` with no step run, at once."""
    workspace, plan = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)
    stranger = BenchMutex(frontend="stranger", label="td5-plan-holder")
    stranger.acquire(declared_devices(config, load_test_config(str(plan), config.work_dir)))
    service = bound_service(workspace)
    try:
        began = time.monotonic()
        refused = mcp_call(service, PLAN_RUN, {})
        waited = time.monotonic() - began
    finally:
        service.close()
        stranger.release_all()

    assert refused["ok"] is False and refused["error_type"] == "device_busy", refused
    assert refused["steps"] == [], refused["steps"]
    assert refused["holder"]["label"] == "td5-plan-holder", refused
    assert waited < scaled_time_bound(10.0), waited


@pytest.mark.parametrize("tool_name", [PLAN_STATUS, PLAN_STOP])
def test_a_handle_the_tools_cannot_read_is_refused_by_field_over_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    """Through the tool, not the module: a handle that is not `run-` and sixteen
    hex digits is `invalid_argument` on the field `run`, and one this bench never
    issued is `run_not_found` (tools.py:1585-1598, runlifecycle.py:132-139)."""
    workspace, _ = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    service = bound_service(workspace)
    try:
        malformed = mcp_call(service, tool_name, {"run": "run-NOTAHANDLE"})
        unknown = mcp_call(service, tool_name, {"run": "run-00000000000000ff"})
    finally:
        service.close()

    assert malformed["ok"] is False and malformed["error_type"] == "invalid_argument", malformed
    assert malformed["field"] == "run", malformed
    assert malformed["tool"] == tool_name, malformed
    assert unknown["ok"] is False and unknown["error_type"] == "run_not_found", unknown


def test_a_repeated_stop_on_a_live_run_answers_true_again_and_writes_the_request_anew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """runlifecycle.py:1160-1177: a run that has not ended, asked again, has its
    request file written again and answers `stop_requested: true` both times. The
    worker is reported alive, as the lock a running worker holds would report it."""
    workspace, _ = bench_workspace(tmp_path, monkeypatch, RESET_PLAN)
    config = load_authoritative_config(workspace)
    handle = "run-00000000000000d5"
    runlifecycle.write_run_record(config, handle, {"version": runlifecycle.RUN_RECORD_VERSION, "state": RUN_RUNNING, "name": "testconfig"})
    monkeypatch.setattr(runlifecycle, "worker_is_gone", lambda *_: False)
    service = bound_service(workspace)
    try:
        first = mcp_call(service, PLAN_STOP, {"run": handle})
        second = mcp_call(service, PLAN_STOP, {"run": handle})
    finally:
        service.close()

    assert first["ok"] is True and first["stop_requested"] is True, first
    assert second["ok"] is True and second["stop_requested"] is True, second
    assert runlifecycle.stop_requested_at(config, handle) == second["stop_requested_at"], second


def test_clauses_are_the_unit_every_relation_is_read_in() -> None:
    """The relations above read one sentence or semicolon clause at a time, so a
    fact split across two clauses is not one the check accepts."""
    assert clauses("Default false; returns `steps`.") == ["Default false;", "returns `steps`."]
    assert not a_plain_run_answers_at_the_plans_end("Default false. It answers at once; `steps` come later.")
