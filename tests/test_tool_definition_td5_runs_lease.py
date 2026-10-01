"""What a host reads about bench runs, test plan runs and the lease status (#642).

`bench_run_start`, `bench_run_status`, `bench_run_stop`, `test_reactor_run`,
`test_reactor_status`, `test_reactor_stop` and `hardware_lease_status` are read
here the way a host reads them: through a real `tools/list` request answered by
the server. What they must carry is what the code does today. Which devices a
run declares and how a selector names one, how long a run is held and what ends
it, what a refused start leaves held, what a repeated call answers, what a stop
releases and what it leaves to a session, where a plan is read from and which
refusals come before any step, what a detached run answers, what a run handle
looks like and which states a run passes through, what a stop asks of a run,
which incident needs hardware_recover, and where each tool's view of the bench
ends.

The checks are about meaning, not wording. A fact that could be stated the
wrong way round (held or taken, required or never required, `false` or
`true`, at once or after a wait, finishes the step or kills it) is checked as a
relation inside one sentence or clause, and every such check is run against its
own inverted statement as well, which it must refuse. A test never pins a
sentence.

The second half holds the behaviour those definitions describe where no
existing test already holds it. The rest is held elsewhere and not repeated: a
run that holds its devices across calls, refuses an undeclared device, refuses
a second declaration, answers a stop with no run open, is released when the
service closes, refuses a bad `wait_s` or an unknown device before it locks
anything, gives back what it took when a later device is held, and names a
session's lease that outlives it (tests/test_devices.py); a shared bus that
needs a participant in a selector (tests/test_can_participant_sessions.py); a
stop that recovers a run left with an incident (tests/test_run_abort_recovery.py);
a lease read that leaves its incident and agrees with `agentic-hil lease-status`
(tests/test_lease_status_tool.py); a run's devices in `bench_held` and
`held_devices` (tests/test_coordination.py); a plan run, a detached run, status,
stop, a plan outside the workspace, a permission refusal and an unprovisioned
workspace over MCP (tests/test_reactor_mcp_tools.py); the default plan, a plan
naming a device the bench lacks, a plan inside a declared run and a stop on an
ended run (tests/test_tool_descriptions.py); and a malformed or unknown handle
read through the run lifecycle module (tests/test_run_lifecycle.py).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

import pytest
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
from agentic_hil.bench import MAX_WAIT_S, BenchMutex
from agentic_hil.config import load_authoritative_config
from agentic_hil.knowledge import DEFAULT_TEST_CONFIG_PATH
from agentic_hil.runlifecycle import (
    RUN_FINISHED,
    RUN_HANDLE_PATTERN,
    RUN_RUNNING,
    RUN_STARTING,
    RUN_STOPPED,
    RUN_WORKER_GONE,
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

# The input schemas as they stand, every `description` left out. Describing an
# input must not change what a call may pass: the schema is the gate in front of
# the code, and a narrower or wider one is a runtime change.
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
    PLAN_RUN: {"type": "object", "properties": {"test_config_path": NONEMPTY, "detach": {"type": "boolean", "default": False}}, "additionalProperties": False},
    PLAN_STATUS: {"type": "object", "properties": {"run": NONEMPTY}, "additionalProperties": False},
    PLAN_STOP: {"type": "object", "properties": {"run": NONEMPTY}, "additionalProperties": False, "required": ["run"]},
    LEASE: EMPTY,
}


@pytest.fixture(scope="module")
def listed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return listed_tools(tmp_path_factory.mktemp("td5-tools-list"))


def without_descriptions(node: object) -> object:
    if isinstance(node, dict):
        return {key: without_descriptions(value) for key, value in node.items() if key != "description"}
    if isinstance(node, list):
        return [without_descriptions(item) for item in node]
    return node


# ---------------------------------------------------------------------------
# The relations a definition could state the wrong way round. Each is a named
# check, so the controls below can show it refuses the inverted claim.

AT_ONCE = r"\b(at once|immediately|no wait|without waiting|does not wait)\b"


def run_held_until_stop_or_exit(text: str) -> bool:
    """coordination.py:875-901 (`run_status`) and tests/test_devices.py:930: a run
    holds its devices until bench_run_stop, or until the server closes."""
    return stated(text, r"\buntil\b", r"bench_run_stop", r"\b(server|process)\b", unless=r"\bsurviv|\boutlives?\b|\bpersists?\b")


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


def wait_defaults_to_failing_at_once(text: str) -> bool:
    """tools.py:1436 and bench.py:646-676: `wait_s` defaults to 0, and with no wait a held device is `device_busy` at once."""
    return stated(text, r"\bdefault", r"(?<![\d.])0(?![\d.])", r"device_busy", AT_ONCE, unless=r"\b(forever|indefinitely|unbounded|until (it is )?free)\b")


def wait_is_bounded_in_seconds(text: str) -> bool:
    """bench.py:63 and 646-676: seconds, at most 900."""
    return stated(text, r"\b900\b", r"\b(seconds?|s)\b", unless=r"\bunbounded|\bno (limit|maximum)\b")


def debugger_id_may_be_left_out_with_one_configured(text: str) -> bool:
    """devices.py:792-808 and 710-731: only a debugger selector may omit `id`, and only with one debugger configured."""
    return stated(
        text,
        r"\bomit|\boptional\b|\bleft out\b|\bleave (it )?out\b|\bwithout (an )?id\b",
        r"\bdebugger\b",
        r"\b(exactly one|only one|one configured|the one|single|sole)\b",
        unless=r"\b(uart|ports?|any kind|every kind|all kinds)\b",
    )


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


def an_open_incident_triggers_recovery(text: str) -> bool:
    """tools.py:1466-1467, 1482-1484 and 1846-1890: an incident still open once the devices
    are back runs the recovery (reset into halt, probe re-read), reported in `recovery`."""
    return stated(text, r"\bincident\b", r"`?recovery`?", r"\b(halt|reset)", unless=r"\bnever\b|\bnot (touched|driven|reset|halted)\b|\bno recovery\b")


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
    """test_reactor.py:629-651: a path is read relative to workspace_root and must resolve inside it."""
    return stated(text, r"workspace_root", r"\b(inside|within|under|relative|outside)\b", unless=r"\banywhere\b|\bany path\b")


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


def a_failing_step_halts_the_target(text: str) -> bool:
    """tests/test_run_abort_recovery.py:232-256: a failed step aborts into a reset into halt where the policy allows."""
    return stated(text, r"\bfail", r"\bhalt", unless=r"\b(never|not|no)\b[^.;]*\bhalt|\bleaves? the target running\b")


def detach_defaults_to_false(text: str) -> bool:
    """contracts.py `detach` default and tools.py:1577-1579."""
    return stated(text, r"\bdefault", r"\bfalse\b", unless=r"\bdefault(s)?\b[^.;]*\btrue\b")


def a_plain_run_returns_the_report(text: str) -> bool:
    """reactorrun.py `run_plan`: without detach the call ends with the plan and answers with its steps and report."""
    return stated(
        text,
        r"\bfalse\b|\bwithout detach\b|\bsynchronous|\b(when|until) the plan (ends|finishes)\b",
        r"`?steps`?|`?report_path`?",
        unless=r"\b(at once|immediately)\b",
    )


def a_detached_run_answers_at_once_with_a_handle(text: str) -> bool:
    """runlifecycle.py:703-717 and 720-822: detached, the call answers with `run` once the worker holds its devices."""
    return stated(text, r"\btrue\b|\bdetach", r"\b(at once|immediately)\b", r"`run`|\bhandle\b", unless=r"\bwaits? (for|until) the plan\b")


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


def stop_finishes_the_step_first(text: str) -> bool:
    """runlifecycle.py:1117-1121 and 1166-1167: cooperative, the run finishes its current step."""
    return stated(text, r"\b(finish|finishes|completes|after)\b", r"\bstep\b", unless=r"\b(kill|kills|killed|abort|aborts|interrupt|interrupts|mid-step)\b")


def stop_closes_devices_and_writes_the_report(text: str) -> bool:
    """runlifecycle.py:1167: it closes its devices in the usual order and writes its report."""
    return stated(text, r"\b(closes|releases)\b", r"\bdevices?\b", r"\breport\b", unless=r"\bno report\b|\bheld\b")


def a_starting_run_ends_before_any_step(text: str) -> bool:
    """runlifecycle.py:1162-1165: a run still taking its devices ends before any step runs."""
    return stated(text, r"\b(starting|taking its devices|waiting for (a|its) devices?)\b", r"\bbefore (any|its first|the first) step\b")


def ended_run_answers_stop_requested_false(text: str) -> bool:
    """runlifecycle.py:1136-1144."""
    return stated(text, r"\b(ended|finished|already|over|done)\b", r"stop_requested" + FALSE)


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
        unless=r"\bwithout hardware_recover\b|\b(every|any|all) (open )?incidents?\b",
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
    (run_has_no_timeout, "Held with no timeout.", "The run times out after 15 minutes."),
    (acquisition_is_all_or_nothing, "All or nothing: a refused start holds none.", "A refused start keeps the devices it got and waits for the rest."),
    (acquisition_is_all_or_nothing, "Nothing is held when one device is busy.", "Not all or nothing: it keeps what it took."),
    (undeclared_device_refused, "Other devices fail `undeclared_device`.", "It may touch any device; `undeclared_device` is never returned."),
    (second_start_refused, "A second start fails `run_already_active`.", "A second start replaces the open run instead of `run_already_active`."),
    (wait_defaults_to_failing_at_once, "Default 0: a held device fails `device_busy` at once.", "Default 0 waits until it is free, then `device_busy`."),
    (wait_defaults_to_failing_at_once, "Default 0, so `device_busy` comes immediately.", "Default 900: `device_busy` at once only after the wait."),
    (wait_is_bounded_in_seconds, "Seconds to wait, 0 to 900.", "Seconds to wait; no maximum, 900 is only typical."),
    (debugger_id_may_be_left_out_with_one_configured, "id may be omitted only for a debugger when exactly one is configured.", "id is optional for a uart when one port is configured, or a debugger."),
    (debugger_id_may_be_left_out_with_one_configured, "Only the one configured debugger may be left out.", "Any kind may omit id when exactly one debugger is configured."),
    (no_run_answers_run_was_active_false, "With no run open it answers `run_was_active: false`.", "With no run open it answers `run_was_active: true`."),
    (no_run_answers_run_was_active_false, "Safe with no run open: `run_was_active: false`.", "With no run open it fails `run_not_active`."),
    (a_session_keeps_its_own_device, "A session still open keeps its own device: `open_leases`, `still_held_devices`.", "It also closes every open session; `still_held_devices` is then empty."),
    (an_open_incident_triggers_recovery, "With an incident still open it resets the target into halt, reported in `recovery`.", "An open incident is left alone; `recovery` is never attempted and the target is not reset."),
    (lease_status_sees_other_holders, "What another process holds shows in hardware_lease_status.", "hardware_lease_status shows the same as this."),
    (default_plan_named, "Default `.agentic-hil/testconfig.yaml`.", "Required: there is no `.agentic-hil/testconfig.yaml` fallback."),
    (plan_held_to_the_workspace, "A path inside workspace_root.", "Any path works, inside workspace_root or anywhere."),
    (missing_plan_not_found, "Missing: `test_config_not_found`.", "A missing plan is `test_config_invalid`; `test_config_not_found` is never returned."),
    (bad_plan_invalid, "Outside it or malformed: `test_config_invalid`.", "`test_config_invalid` is never returned for a malformed plan."),
    (inside_a_declared_run_refused, "Inside a bench_run_start run it fails `run_already_active`.", "Inside a bench_run_start run it uses the run's devices, never `run_already_active`."),
    (held_device_fails_at_once, "A device held elsewhere fails `device_busy` at once.", "It waits for a held device, up to 900 s, then `device_busy`."),
    (permission_refused_before_any_step, "A step whose permission is off fails `permission_denied` before any step runs.", "A step whose permission is off fails `permission_denied` when reached, after the earlier steps ran."),
    (a_failing_step_halts_the_target, "A failing step resets the target into halt.", "A failing step does not halt the target."),
    (detach_defaults_to_false, "Default false: returns when the plan ends.", "Default true: answers at once."),
    (detach_defaults_to_false, "Default false.", "Default true, false waits."),
    (a_plain_run_returns_the_report, "Default false: returns when the plan ends, with `steps` and `report_path`.", "Default false: answers at once with a handle; `steps` come from test_reactor_status."),
    (a_detached_run_answers_at_once_with_a_handle, "True: answers at once with `run` and `state`.", "True: waits until the plan ends, then gives a handle."),
    (handle_shape_named, "`run-` and 16 hex digits.", "Any text naming the run."),
    (without_a_handle_lists_runs, "Without run, lists this bench's runs: `runs`, `active_runs`.", "Without run it fails `invalid_argument`."),
    (worker_gone_means_the_process_died, "worker_gone: its process died.", "worker_gone means the run is still starting."),
    (stop_finishes_the_step_first, "It finishes the step it is in.", "It kills the run at once, mid-step."),
    (stop_closes_devices_and_writes_the_report, "It closes its devices and writes its report.", "It stops with its devices held and no report."),
    (a_starting_run_ends_before_any_step, "A run still taking its devices ends before any step.", "A starting run runs its first step, then stops."),
    (ended_run_answers_stop_requested_false, "An ended run answers `stop_requested: false`.", "An ended run answers `stop_requested: true`."),
    (stop_answers_before_the_run_ends, "Answers at once with `stop_requested: true`.", "It waits until the run has stopped."),
    (worker_gone_refusal, "`run_worker_gone` when its process died.", "`run_worker_gone` means the stop was delivered."),
    (only_a_standing_incident_needs_recover, "Only an incident that stands needs hardware_recover.", "Every open incident needs hardware_recover, standing or not."),
    (only_a_standing_incident_needs_recover, "Only `incident_stands: true` needs hardware_recover.", "Only an incident that stands is cleared without hardware_recover."),
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
def test_every_input_carries_its_own_description_within_the_limits(listed: dict[str, dict], tool_name: str) -> None:
    tool = listed[tool_name]
    properties = tool["inputSchema"]["properties"]

    undescribed = sorted(name for name, schema in properties.items() if not str(schema.get("description", "")).strip())
    assert undescribed == [], f"{tool_name}: {undescribed}"
    over = {name: len(schema["description"]) for name, schema in properties.items() if len(schema["description"]) > PROPERTY_DESCRIPTION_LIMIT}
    assert over == {}, over
    assert len(tool["description"]) <= DESCRIPTION_LIMIT, len(tool["description"])


@pytest.mark.parametrize("tool_name", TOOLS)
def test_describing_an_input_leaves_what_a_call_may_pass_unchanged(listed: dict[str, dict], tool_name: str) -> None:
    assert without_descriptions(listed[tool_name]["inputSchema"]) == SCHEMAS[tool_name]
    assert "outputSchema" not in listed[tool_name]


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


def test_bench_run_start_names_its_result_fields_and_refusals(listed: dict[str, dict]) -> None:
    """Success carries `declared_devices` and `run_label` (coordination.py:815-831);
    a name the configuration lacks is `unknown_device` (devices.py:716-731 and
    734-758); a device another holder has is `device_busy` (bench.py:487-531)."""
    text = definition(listed[RUN_START])

    for token in ("declared_devices", "run_label", "unknown_device", "device_busy"):
        assert f"`{token}`" in text, (token, text)


def test_devices_says_how_a_selector_names_a_device(listed: dict[str, dict]) -> None:
    """kind is debugger, uart or can (contracts.py DEVICE_SELECTOR); id names
    the config entry (devices.py:796-811); only a debugger may omit it, and only
    with one configured (devices.py:710-731, 792-795); a bus with `shares`
    requires a participant (devices.py:809-810)."""
    text = property_text(listed[RUN_START], "devices")

    for kind in ("debugger", "uart", "can"):
        assert names(rf"\b{kind}\b", text), (kind, text)
    assert stated(text, r"\bid\b", r"\b(config|configuration)\b|\bentry\b"), text
    assert debugger_id_may_be_left_out_with_one_configured(text), text
    assert participant_required_on_a_shared_bus(text), text


def test_label_says_where_it_appears(listed: dict[str, dict]) -> None:
    """The label is the run's `run_label` and the holder label another owner is
    refused with (coordination.py:812-813, bench.py:496-503), both held by
    tests/test_devices.py:841-880."""
    text = property_text(listed[RUN_START], "label")

    assert "run_label" in text, text
    assert names(r"\bholder\b|\bdevice_busy\b", text), text


def test_wait_s_names_its_unit_bound_and_default(listed: dict[str, dict]) -> None:
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
    recovery and reports it in `recovery` (tools.py:1466-1467)."""
    text = listed[RUN_STOP]["description"]

    assert RUN_START in text, text
    assert "`released_devices`" in text, text
    assert no_run_answers_run_was_active_false(text), text
    assert a_session_keeps_its_own_device(text), text
    assert an_open_incident_triggers_recovery(text), text


def test_bench_run_status_names_its_fields_and_where_its_view_ends(listed: dict[str, dict]) -> None:
    """In memory, this server's own run (contracts.py annotation comment,
    coordination.py:875-901); another process's holds are read by the lease
    status (coordination.py:1687-1692)."""
    text = listed[RUN_STATUS]["description"]

    for token in ("run_active", "declared_devices", "run_label", "held_devices"):
        assert f"`{token}`" in text, (token, text)
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


def test_test_reactor_run_says_what_a_failing_plan_leaves_on_the_board(listed: dict[str, dict]) -> None:
    text = definition(listed[PLAN_RUN])

    assert a_failing_step_halts_the_target(text), text


def test_detach_names_its_default_and_what_each_value_answers(listed: dict[str, dict]) -> None:
    """Default false: the call returns with the plan's report, `ok`, `steps`,
    `run` and `report_path` (tests/test_reactor_mcp_tools.py:77-97). True: it
    answers with `run` and `state` once the worker holds its devices
    (runlifecycle.py:806-822)."""
    text = property_text(listed[PLAN_RUN], "detach")

    assert detach_defaults_to_false(text), text
    assert a_plain_run_returns_the_report(text), text
    assert a_detached_run_answers_at_once_with_a_handle(text), text
    for token in ("ok", "steps", "run", "report_path", "state"):
        assert f"`{token}`" in definition(listed[PLAN_RUN]), (token, definition(listed[PLAN_RUN]))


# ---------------------------------------------------------------------------
# test_reactor_status and test_reactor_stop: the handle, the states, the stop.


@pytest.mark.parametrize("tool_name", [PLAN_STATUS, PLAN_STOP])
def test_run_says_what_a_handle_is_and_where_it_comes_from(listed: dict[str, dict], tool_name: str) -> None:
    text = property_text(listed[tool_name], "run")

    assert RUN_HANDLE_PATTERN.pattern == r"^run-[0-9a-f]{16}$"
    assert handle_shape_named(text), text
    assert PLAN_RUN in text, text
    assert "invalid_argument" in definition(listed[tool_name]), definition(listed[tool_name])


def test_test_reactor_status_names_the_states_a_run_passes_through(listed: dict[str, dict]) -> None:
    text = definition(listed[PLAN_STATUS])

    for state in (RUN_STARTING, RUN_RUNNING, RUN_FINISHED, RUN_STOPPED, RUN_WORKER_GONE):
        assert names(rf"\b{state}\b", text), (state, text)
    assert worker_gone_means_the_process_died(text), text


def test_test_reactor_status_names_its_fields_and_the_listing(listed: dict[str, dict]) -> None:
    """A named run answers `state`, `stop_requested_at` and, once ended, `run_ok`
    and `report_path` (runlifecycle.py:1023-1031, the record written at 646-647);
    a handle the bench never issued is `run_not_found` (runlifecycle.py:989-998);
    without a handle the bench's runs are listed (runlifecycle.py:1073-1113)."""
    text = definition(listed[PLAN_STATUS])

    for token in ("state", "stop_requested_at", "run_ok", "report_path", "run_not_found", "active_runs"):
        assert f"`{token}`" in text, (token, text)
    assert without_a_handle_lists_runs(text), text


def test_test_reactor_stop_says_what_it_asks_of_the_run(listed: dict[str, dict]) -> None:
    """Cooperative (runlifecycle.py:1117-1121): the run finishes its step, closes
    its devices and writes its report, and one still taking its devices ends
    before any step (runlifecycle.py:1162-1167). The call answers once the request
    is written (runlifecycle.py:1160-1177)."""
    text = definition(listed[PLAN_STOP])

    assert stop_finishes_the_step_first(text), text
    assert stop_closes_devices_and_writes_the_report(text), text
    assert a_starting_run_ends_before_any_step(text), text
    assert stop_answers_before_the_run_ends(text), text


def test_test_reactor_stop_names_what_a_repeat_and_a_lost_run_answer(listed: dict[str, dict]) -> None:
    text = definition(listed[PLAN_STOP])

    assert ended_run_answers_stop_requested_false(text), text
    assert "run_not_found" in text, text
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

# Device locks are machine-wide, so the ports here carry resource ids no other
# test module names.
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


def test_clauses_are_the_unit_every_relation_is_read_in() -> None:
    """The relations above read one sentence or semicolon clause at a time, so a
    fact split across two clauses is not one the check accepts."""
    assert clauses("Default false; returns `steps`.") == ["Default false;", "returns `steps`."]
    assert not a_plain_run_returns_the_report("Default false. It answers at once; `steps` come later.")
