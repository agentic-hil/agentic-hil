from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml
from jsonschema import Draft202012Validator, SchemaError

from agentic_hil.backends.common import DEBUG_SESSION_WAY_OUT
from agentic_hil.backends.gdbdebug import INTEGER_VALUE_WIDTHS
from agentic_hil.can import parse_can_id, parse_hex_bytes
from agentic_hil.comports import data_result, decode_bytes, matched_span_bytes
from agentic_hil.config import (
    ConfigError,
    UniqueKeyLoader,
    absolute_without_symlinks,
    bind_debugger,
    is_path_within_frozen,
    reject_nonfinite_numbers,
    safe_read_bytes,
)
from agentic_hil.contracts import validate_tool_arguments
from agentic_hil.devices import (
    CanDevice,
    DebuggerDevice,
    Device,
    DeviceError,
    DeviceSet,
    UartDevice,
    can_device,
    debugger_device,
    uart_device,
)
from agentic_hil.knowledge import DEFAULT_TEST_CONFIG_PATH as DEFAULT_TEST_CONFIG_PATH
from agentic_hil.knowledge import LISTEN_ONLY_MODE_ERROR, plan_schema_document
from agentic_hil.knowledge import PLAN_FEATURE_VERSION_KEY as PLAN_FEATURE_VERSION_KEY
from agentic_hil.knowledge import TEST_CONFIG_SCHEMA_RESOURCE as TEST_CONFIG_SCHEMA_RESOURCE
from agentic_hil.report import audit_errors, overall_success
from agentic_hil.tools import (
    AgenticHILToolService,
    configured_sessionless_debug_reads,
    sessionless_capable_debug_tools,
)
from agentic_hil.types import AgenticHILConfig, DebuggerConfig, JsonObject

# The default plan path, the packaged schema and the marker its version gate
# reads are re-exported from `knowledge`, which is where the reference document
# an agent reads is generated from them. One statement of each, so the format a
# plan author is shown and the format a plan is refused by cannot come apart.
# The one action the reactor serves itself. It routes to no device and drives no
# hardware: what it does is run the subset of steps nested under it again, which
# is a property of the sequence rather than of anything the bench has. So it is
# not a `StepDevice` action, and the dispatch that resolves a step to a device
# never sees it.
REPEAT_ACTION = "repeat"
# What a `uart_expect` that timed out answers with: the last of what the line
# actually said. Capped, because a chatty port must not turn one red step into an
# unbounded result, but never zero, because a red step that cannot say what the
# port did say is the failure an operator has to reproduce by hand.
UART_EXPECT_TAIL_BYTES = 512
# How long a `uart_expect` pass that read nothing waits before the next one.
# Reached only when `com_read` returned early (a session that is no longer
# active ends its wait at once), so this paces a line that has stopped talking
# rather than spinning on it until the step's deadline.
UART_EXPECT_IDLE_POLL_S = 0.05
# What a `uart_expect` says about itself, per kind of expectation: (matched,
# timed out). A plan that waited for a literal and a plan that waited for a
# pattern fail for different reasons, and a result that called both "expected
# text" would send an operator looking for a substring nobody asked for.
UART_EXPECT_SUMMARIES: dict[str, tuple[str, str]] = {
    "text": (
        "Expected text appeared on the COM port.",
        "Expected text did not appear on the COM port before this step's timeout.",
    ),
    "pattern": (
        "Expected pattern matched the COM port output.",
        "Expected pattern did not match the COM port output before this step's timeout.",
    ),
}
# What a `comparator:` says about itself, per kind of claim: (met, unmet). Same
# two-sentence shape as the v2 table above and for the same reason: a plan that
# waited for an exact value and a plan that waited for a value inside a range
# fail for different reasons, and one sentence for both would send an operator
# looking for the wrong thing.
COMPARATOR_SUMMARIES: dict[str, tuple[str, str]] = {
    "equals": (
        "The COM port output equalled the expected value.",
        "The COM port output never equalled the expected value before this step's timeout.",
    ),
    "pattern": (
        "Expected pattern matched the COM port output.",
        "Expected pattern did not match the COM port output before this step's timeout.",
    ),
    "range": (
        "A value captured from the COM port output fell inside the expected range.",
        "No value captured from the COM port output fell inside the expected range before this step's timeout.",
    ),
}
# The same table for a bus. Separate sentences rather than one wording with the
# medium substituted in: what went unmet on a bus is a frame, and a result that
# said "output" would send an operator to a serial line that was never involved.
CAN_COMPARATOR_SUMMARIES: dict[str, tuple[str, str]] = {
    "equals": (
        "A frame on the CAN bus carried the expected payload.",
        "No frame on the CAN bus carried the expected payload before this step's timeout.",
    ),
    "pattern": (
        "Expected pattern matched the payload of a frame on the CAN bus.",
        "Expected pattern matched no frame's payload on the CAN bus before this step's timeout.",
    ),
    "range": (
        "A value captured from a frame's payload fell inside the expected range.",
        "No value captured from a frame's payload fell inside the expected range before this step's timeout.",
    ),
}
# What a `read_symbol` comparator says about itself, per kind of claim: (met,
# unmet). The same two-sentence shape the other two media use, with one word
# missing from every unmet sentence on purpose: there is no "before this step's
# timeout" here, because there is no timeout. A halted target's memory does not
# change under the step, so the value is read once and judged once, and a
# sentence promising a deadline would describe waiting that never happens.
SYMBOL_COMPARATOR_SUMMARIES: dict[str, tuple[str, str]] = {
    "equals": (
        "The symbol held the expected value.",
        "The symbol did not hold the expected value.",
    ),
    "range": (
        "The symbol's value fell inside the expected range.",
        "The symbol's value fell outside the expected range.",
    ),
    "mask": (
        "The symbol's masked bits held the expected value.",
        "The symbol's masked bits did not hold the expected value.",
    ),
}
# What a `read_symbol` verdict repeats from the read it judged. The value in
# every reading the tool returned, and where it came from: a red step that
# cannot say what it read is a failure an operator has to reproduce by hand,
# which is the same honesty `received_tail` is on a serial line, in the unit
# this medium has.
SYMBOL_VALUE_FIELDS: tuple[str, ...] = ("symbol", "address", "size_bytes", "resolved_from", "hex", "byte_order", "value_unsigned", "value_signed")
# How many of the frames a `can_read` comparator saw are reported when its claim
# went unmet. The `received_tail` honesty, in the unit this medium has: a red
# step that cannot say what the bus did carry is a failure an operator has to
# reproduce by hand, and a busy bus must not turn one red step into an
# unbounded result, which is why it is the last few rather than all of them.
CAN_COMPARATOR_TAIL_FRAMES = 16
# How long a `can_read` comparator pass that read no frames waits before the
# next one. Reached only when `can_read` returned early, so this paces a quiet
# bus rather than spinning on it until the step's deadline.
CAN_COMPARATOR_IDLE_POLL_S = 0.05
# The longest a `delay` step may wait, in milliseconds. Ten minutes: a plan that
# must wait longer than that is waiting for something it should be expecting
# instead, and an unbounded wait in a plan is how a bench ends up held by a run
# nobody is watching.
DELAY_MAX_MS = 600_000
# The longest a `delay` sleeps in one piece. A stop is answered between steps,
# and a step that waits ten minutes would make "between steps" mean ten minutes;
# slicing the wait is what makes the wait itself somewhere a stop can be
# answered. The total is unchanged, because the slices are counted against the
# step's own deadline rather than added up.
DELAY_STOP_POLL_MS = 250
# What a run that was asked to end reports as. Its own name, because a stop is
# neither a pass nor a failure of the firmware: borrowing an existing error type
# would send a reader looking for a board that misbehaved, and reporting success
# would let a partial run be read as a complete one.
RUN_STOPPED_ERROR = "run_stopped"


def never_stopped() -> bool:
    """The stop question, for a run nobody has addressed.

    The default everywhere, so a reactor built without a run to stop behaves
    exactly as one built before stopping existed: the question is asked between
    every step, and it always answers no."""
    return False


def decoded_equals(decoded: str, expected: str, final: bool) -> tuple[str, tuple[int, int]] | None:
    """What the line said that equals `expected` and where, or None if nothing did.

    The text rather than a boolean, because a met claim has to be able to say
    what met it: a green step that answers only "yes" leaves a reader who wants
    to see what the board actually replied with no place to look but the COM
    log. None rather than the empty string for "nothing did", so `equals: ""`
    stays a claim that can be met.

    The character span alongside the text, because the raw wire bytes of the
    match are sliced back out of the receive buffer by it: a decoded string is
    not enough to find its own bytes once `errors="replace"` has made the decode
    irreversible, but its position in the window is (see `matched_span_bytes`).

    A serial line has no end, so "the whole thing" is read two ways. Any one
    complete line of what was received counts at once: a terminator is the board
    saying that value is finished, which is what a board printing its banner in a
    loop looks like, where the whole buffer is never one value.

    The whole of what was received counts too (a board that answers once and
    stops sends no terminator), but only on the last pass, once the step's
    deadline has run out and nothing further is coming. Accepting it earlier
    would pass `equals: "Hello"` on a board halfway through printing "Hello
    World", and a framework answering green to a value that was still arriving is
    worse than one that waits out its own timeout to be sure."""
    if final and decoded.strip() == expected:
        return decoded, (0, len(decoded))
    position = 0
    complete: list[tuple[str, int, int]] = []
    for raw_line in decoded.splitlines(keepends=True):
        # `splitlines(keepends=True)` splits on the same boundaries as
        # `splitlines()`, so stripping the terminator off each element recovers
        # the line content while `position` tracks where it began in the window.
        content = (raw_line.splitlines() or [""])[0]
        complete.append((content, position, position + len(content)))
        position += len(raw_line)
    if complete and not decoded.endswith(("\n", "\r")):
        # The last fragment has no terminator yet, so it is not a line: it is
        # the beginning of one, and matching it would be matching a prefix.
        complete = complete[:-1]
    return next(((content, (begin, stop)) for content, begin, stop in complete if content.strip() == expected), None)


class StepComparator:
    """What a feedback step claims about what it received.

    An object in the plan (`comparator: {equals: ...}`) rather than a bare value,
    so the preprocessing a bench eventually needs (scaling a raw count into a
    unit, converting a base) can be added as further keys without invalidating
    plans written today. None of that is implemented here; the shape is what
    makes it addable.

    Three claims, all judged against everything read so far rather than against
    the chunk that happened to arrive last: `equals` is an exact value, `pattern`
    is a Python regular expression under `re.search`, and `pattern` with `range`
    is a numeric value read out of that pattern's one capture group and held to
    inclusive bounds."""

    def __init__(self, raw: JsonObject):
        self.raw = dict(raw)
        self.equals: str | None = None if raw.get("equals") is None else str(raw["equals"])
        self.pattern: str | None = None if raw.get("pattern") is None else str(raw["pattern"])
        bounds = raw.get("range")
        self.bounds: tuple[float, float] | None = None if bounds is None else (float(bounds["min"]), float(bounds["max"]))
        # Compiled once per step, not per read. The pattern already compiled
        # cleanly at preflight, so this cannot raise.
        self._finditer = re.compile(self.pattern).finditer if self.pattern is not None else None
        # The last value the pattern captured and could read as a number, kept so
        # a step that timed out can say what it did see against what it wanted.
        self.captured_text: str | None = None
        self.captured_value: float | None = None
        # What satisfied the claim on the pass that satisfied it: the line that
        # equalled the value, or the span the pattern matched. None whenever the
        # claim is unmet, which is what `matches` returning False means, so a
        # value left over from an earlier pass can never be reported as this
        # pass's evidence.
        self.matched_text: str | None = None
        # Where in the decoded window that text sat, so its raw wire bytes can be
        # sliced back out of the receive buffer rather than re-encoded from a
        # string a `replace` decode may have altered. Cleared with `matched_text`.
        self.matched_span: tuple[int, int] | None = None

    @property
    def claim(self) -> str:
        if self.bounds is not None:
            return "range"
        return "equals" if self.equals is not None else "pattern"

    @property
    def written(self) -> str:
        """The plan's own string, which sizes the read window."""
        return self.equals or self.pattern or ""

    def number(self, text: str) -> float:
        """The number a capture holds, in the base the medium writes it in.

        Decimal here, because a serial line carries text a human wrote a format
        string for. A medium whose output is bytes rendered as hexadecimal reads
        its captures in that base instead, and says so by overriding this: the
        one place the two differ, so a `range` means the same thing either
        way: bounds in decimal, held against the value that was really there."""
        return float(text)

    def matches(self, decoded: str, final: bool) -> bool:
        """Whether what has been received meets this claim. `final` is true on
        the pass the step's deadline has run out on: the last look at the line,
        where a value that never carried a terminator can still be judged.

        Records what met the claim in `matched_text` on the way, so a met claim
        answers with the evidence rather than only with the verdict."""
        self.matched_text = None
        self.matched_span = None
        if self.equals is not None:
            equal = decoded_equals(decoded, self.equals, final)
            if equal is None:
                return False
            self.matched_text, self.matched_span = equal
            return True
        if self._finditer is None:
            return False
        if self.bounds is None:
            match = next(self._finditer(decoded), None)
            if match is None:
                return False
            self.matched_text, self.matched_span = match.group(0), match.span()
            return True
        low, high = self.bounds
        matched = False
        # Every match, not the first: a line that reported an out-of-range value
        # and then an in-range one is a bench that settled, and searching only
        # the earliest occurrence would keep answering with the older reading.
        for match in self._finditer(decoded):
            try:
                value = self.number(match.group(1))
            except ValueError:
                continue
            self.captured_text, self.captured_value = match.group(1), value
            if low <= value <= high:
                # The latest reading inside the bounds, for the same reason the
                # loop does not stop at the first match: the one that settled is
                # the one the step passed on.
                matched, self.matched_text, self.matched_span = True, match.group(0), match.span()
        return matched

    def report(self) -> JsonObject:
        """What the result says about this comparator, met or unmet."""
        if self.bounds is None:
            return {"comparator": self.raw}
        return {"comparator": self.raw, "captured_text": self.captured_text, "captured_value": self.captured_value}


def pattern_refusal(location: StepLocation, step: TestStep, field_name: str, pattern: object, summary: str, needs_capture: bool) -> JsonObject | None:
    """Refuse a regular expression this plan cannot mean, before the run.

    A pattern that does not compile, or a `range` whose pattern captures nothing
    to put in it, is a fault in the plan and is answered here: preflight builds
    nothing, so a plan refused for either has opened no session and touched no
    board. `re.error` carries the position it gave up at, which is the whole
    reason this is not left to raise out of the run as a traceback."""
    if pattern is None:
        return None
    try:
        compiled = re.compile(str(pattern))
    except re.error as error:
        return preflight_error(location, step, field_name, summary, {"error_type": "invalid_argument", "value": str(pattern)[:128], "regex_error": str(error)})
    if needs_capture and compiled.groups != 1:
        return preflight_error(
            location,
            step,
            field_name,
            "A `range` comparator reads its value out of the pattern's one capture group, so the pattern must have exactly one.",
            {"error_type": "invalid_argument", "value": str(pattern)[:128], "capture_groups": compiled.groups},
        )
    return None


def comparator_pattern_refusal(location: StepLocation, step: TestStep) -> JsonObject | None:
    """A `comparator:`'s own pattern, refused before the run.

    Written once for every medium a comparator judges: what a pattern must
    satisfy is a property of the claim, not of the line or the bus it is read
    against, so a serial step and a frame step are refused in the same shape and
    under the same field name, which is what makes the two readable side by side
    in a report."""
    comparator = step.arguments.get("comparator") or {}
    return pattern_refusal(
        location,
        step,
        "comparator.pattern",
        comparator.get("pattern"),
        "Test step's comparator pattern is not a valid Python regular expression.",
        needs_capture=comparator.get("range") is not None,
    )


@dataclass(frozen=True)
class UartReadOutcome:
    """What one bounded read of a serial line came back with."""

    matched: bool
    reads: int
    bytes_received: int
    tail: bytes
    encoding: str
    # The read's own refusal, when `com_read` itself failed. A closed session, a
    # denied permission or a broken audit latch is not an expectation that went
    # unmet, and answering it as a timeout would send an operator to look at
    # firmware that never ran.
    read_failure: JsonObject | None = None
    # The raw wire bytes the comparator matched, sliced out of the receive buffer
    # rather than re-encoded from the decoded string: `errors="replace"` makes a
    # decode irreversible, so re-encoding could put bytes in `matched_text.hex`
    # the port never sent. None when there was no match, or when the encoding's
    # byte span could not be established (see `matched_span_bytes`).
    matched_raw: bytes | None = None

    @property
    def tail_result(self) -> JsonObject:
        return {"received_tail": data_result(self.tail, self.encoding), "received_tail_truncated": self.bytes_received > len(self.tail)}

    def matched_result(self, matched: str | None) -> JsonObject:
        """What a met claim matched, in the shape the unmet one answers in.

        The green half of `received_tail`. A red step already quotes what the
        port did say; a green one used to quote nothing at all, so a report could
        not show what the board answered and a reader had to open the COM log to
        see the line the step passed on. The same `{hex, text, encoding}` shape
        as the tail, so one reader reads both, and bounded by the same cap for
        the same reason: a claim met by a line inside a megabyte of banner must
        not put the megabyte in the report. Bounded from the front rather than
        the back, because a match begins where it begins.

        The `hex` is the raw wire bytes of the match, taken from the receive
        buffer: re-encoding the decoded string instead would fabricate bytes for
        anything a `replace` decode altered (invalid `ff` reads back as
        `efbfbd`), and the field sits in the same evidence shape as the raw tail,
        so a green report cannot be made to attest to bytes the port never sent.
        When the byte span cannot be established the text is still quoted and the
        byte field is dropped, rather than re-encoded into a false one.

        Nothing at all when there is no matched text, rather than a null: the
        v2 `uart_expect` reports no comparator and a report that predates this
        carries no field, and only absence keeps those readable as what they
        are."""
        if matched is None:
            return {}
        if self.matched_raw is not None:
            kept = self.matched_raw[:UART_EXPECT_TAIL_BYTES]
            return {"matched_text": data_result(kept, self.encoding), "matched_text_truncated": len(kept) < len(self.matched_raw)}
        kept_text = matched[:UART_EXPECT_TAIL_BYTES]
        return {
            "matched_text": {"text": kept_text, "encoding": self.encoding},
            "matched_text_bytes_unavailable": True,
            "matched_text_truncated": len(kept_text) < len(matched),
        }
# What a step means is answered by one class per device kind (see StepDevice
# below), and ACTION_SCHEMAS / ROUTE_FIELDS are merged from those classes rather
# than written out here. Routing used to be a two-way split held in two
# constants ("UART or debugger?" in one, "which actions need a probe?" in
# another), and the second was already documented as the thing that would go
# stale silently the first time a kind was added. The authority is now the class
# that serves the action, which cannot be added to without answering for itself.


def result_failed(result: JsonObject) -> bool:
    return not overall_success(result)


def result_error_type(result: JsonObject) -> str:
    if result.get("error_type"):
        return str(result["error_type"])
    if result.get("target_ok") is False:
        return str(result.get("target_error_type") or "target_failed")
    if result.get("audit_ok") is False:
        return "audit_failed"
    return "step_failed"


def propagate_result_status(aggregate: JsonObject, sources: list[JsonObject]) -> None:
    audit_failures = [source for source in sources if source.get("audit_ok") is False]
    if audit_failures:
        aggregate["audit_ok"] = False
        flattened: list[JsonObject] = []
        for source in audit_failures:
            for error in audit_errors(source):
                if error not in flattened:
                    flattened.append(error)
        if flattened:
            aggregate["audit_error"] = flattened[0]
        aggregate["audit_errors"] = flattened
        aggregate["retry_safe"] = audit_failures[0].get("retry_safe", False)
    target_failures = [source for source in sources if source.get("target_ok") is False]
    if target_failures:
        aggregate["target_ok"] = False
        if "target_error_type" in target_failures[0]:
            aggregate["target_error_type"] = target_failures[0]["target_error_type"]


def merge_result_status(result: JsonObject, *sources: JsonObject) -> JsonObject:
    merged = dict(result)
    propagate_result_status(merged, list(sources))
    retry_values = [source["retry_safe"] for source in sources if "retry_safe" in source]
    if any(source.get("audit_ok") is False for source in sources):
        merged["retry_safe"] = False
    elif retry_values:
        merged["retry_safe"] = all(value is not False for value in retry_values)
    if any(source.get("audit_ok") is True for source in sources) and "audit_ok" not in merged:
        merged["audit_ok"] = True
    if any(source.get("target_ok") is True for source in sources) and "target_ok" not in merged:
        merged["target_ok"] = True
    return merged


@dataclass(frozen=True)
class TestStep:
    action: str
    arguments: JsonObject
    # `device` is format v3's one route key: every step names the configured
    # entry it drives, and the config knows which class that name belongs to.
    device: str | None = None
    # The kind-specific keys v2 used, which stay valid as readable aliases: a
    # debugger action names a `debuggers` entry, a UART action names a
    # `com_ports` entry, a CAN action names a `can_buses` entry. Each is the
    # `route_field` of the device class that serves the action, so this list and
    # ROUTE_FIELDS cannot disagree. `debugger` may be None only while the project
    # configures exactly one probe. A step names its device once: an alias
    # beside `device:` is refused at preflight rather than silently preferred.
    debugger: str | None = None
    port_id: str | None = None
    bus_id: str | None = None
    # The subset a block step repeats, in order. Empty for every other action:
    # only `repeat` nests, and nesting is what makes a plan a tree of steps
    # rather than the flat list every version before 4 could write.
    steps: tuple[TestStep, ...] = ()

    @property
    def route(self) -> str:
        return next((named for field_name in ROUTE_FIELDS if (named := getattr(self, field_name))), "-")

    @property
    def route_keys(self) -> list[str]:
        """Every routing key this step actually set."""
        return [field_name for field_name in ROUTE_FIELDS if getattr(self, field_name) is not None]


@dataclass(frozen=True)
class StepOutcome:
    """Why a list of steps did not run to its end.

    Two reasons and no third: a step failed, or the run was asked to stop. Both
    end the list where they are and travel the same way out of however many
    blocks contain it, which is why they are one type: everything between here
    and the run's own result treats them identically, and only the verdict at
    the end tells them apart."""

    # Where in *this* list it happened. For a failure, the step that failed; for
    # a stop, the last step that ran, which is 0 when the stop came before the
    # first one and the step itself when that step is what the stop cut short.
    index: int
    # The failing step's own result, or None for a stop. Nothing failed when a
    # run is stopped, and inventing a failure to carry would be the whole of
    # what makes a stop read as an incident.
    failure: JsonObject | None

    @property
    def stopped(self) -> bool:
        return self.failure is None


@dataclass(frozen=True)
class StepLocation:
    """Where a step is in a plan: which top-level step contains it, and the path
    that names it exactly.

    Two things rather than one, because a report answers two questions with
    them. ``step`` is the top-level step number a run stopped at, which is what
    ``failed_step`` has always meant and what an operator counts down the file
    to find. ``path`` is the field path, ``steps[2].steps[1]``, which is the
    only way to point at a step inside a block that contains it."""

    step: int
    path: str

    @classmethod
    def top(cls, index: int) -> StepLocation:
        return cls(index, f"steps[{index - 1}]")

    def nested(self, index: int) -> StepLocation:
        """One step of the subset this block step repeats. The top-level number
        does not move: the step a run stopped at is still the block."""
        return StepLocation(self.step, f"{self.path}.steps[{index - 1}]")


@dataclass
class PlanState:
    """What a plan has already done to the bench, at the step preflight is on.

    Carried by the walk rather than by device objects, because preflight must
    build nothing: a plan that fails validation has opened no service, taken no
    lock and created no directory, and the surest way to keep that true is for
    preflight to have nowhere to put such a thing. It also makes preflight
    repeatable: the same plan asked twice gets the same answer."""

    # The debugger holding this plan's one debug session, if any. Plan-wide
    # rather than per probe: flashing is refused while *any* session is open.
    debug_session: str | None = None
    # (kind, config entry) of every session the plan has opened and not closed.
    open_sessions: set[tuple[str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class TestConfig:
    __test__ = False

    path: str
    name: str
    steps: list[TestStep]
    # The SHA-256 of the exact bytes this plan was parsed from, captured at load
    # so a run's provenance is the plan that ran and not whatever the file holds
    # by the time evidence is collected (review round 0, finding 4). `None` only
    # for a `TestConfig` built directly in a test that did not read a file.
    sha256: str | None = None


def load_test_config(test_config_path: str | None = None, work_dir: str | None = None) -> TestConfig:
    base = Path(work_dir or Path.cwd()).resolve()
    requested = Path(test_config_path or DEFAULT_TEST_CONFIG_PATH)
    lexical_path = absolute_without_symlinks(requested if requested.is_absolute() else base / requested)
    path = lexical_path
    if not is_path_within_frozen(path, base):
        raise ConfigError(
            "test_config_invalid",
            "Test plan must remain inside the configured workspace.",
            {"path": str(path), "workspace_root": str(base)},
        )
    try:
        raw_bytes = safe_read_bytes(path, workspace=base)
    except FileNotFoundError as error:
        raise ConfigError("test_config_not_found", "Test reactor configuration file could not be found.", {"path": str(path)}) from error
    except OSError as error:
        raise ConfigError(
            "test_config_unreadable",
            "Test reactor configuration file could not be read.",
            {"path": str(path), "backend_error": str(error)},
        ) from error
    # The digest is of the bytes on disk, taken here and nowhere else: the same
    # bytes are decoded and parsed below, so the digest, the name and the steps
    # this returns are all one reading of one file.
    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        loaded = yaml.load(raw_bytes.decode("utf-8"), Loader=UniqueKeyLoader)
    except UnicodeDecodeError as error:
        raise ConfigError(
            "test_config_invalid",
            "Test reactor configuration file is not valid UTF-8 text.",
            {"path": str(path)},
        ) from error
    except yaml.YAMLError as error:
        details: JsonObject = {"path": str(path), "backend_error": str(error)}
        mark = getattr(error, "problem_mark", None)
        if mark is not None:
            details.update({"line": mark.line + 1, "column": mark.column + 1})
        raise ConfigError(
            "test_config_invalid",
            "Test reactor configuration file is not valid YAML or JSON.",
            details,
        ) from error

    raw: Any = loaded or {}
    if not isinstance(raw, dict):
        raise ConfigError("test_config_invalid", "Test reactor configuration root must be a mapping.", {"path": str(path)})
    reject_nonfinite_numbers(raw, "test_config_invalid", str(path))
    reject_superseded_plan_version(raw, str(path))
    validate_test_config_schema(raw, str(path))
    reject_features_newer_than_plan_version(raw, str(path))
    return TestConfig(path=str(path), name=str(raw.get("name") or path.stem), steps=[build_test_step(step) for step in raw["steps"]], sha256=digest)


# The report field carrying the plan's load-time digest. `run-evidence` reads it
# as the run's provenance and never re-hashes the file the report merely names.
TEST_CONFIG_SHA256_KEY = "test_config_sha256"


def plan_digest_field(test_config: TestConfig) -> JsonObject:
    """The digest field for a report, or nothing when the plan carried no digest.

    Absent rather than null so a report from a `TestConfig` built without reading
    a file (the direct-construction path some tests take) reads the same as one
    predating the field, and both fall back to the file the way they always did.
    """
    digest = getattr(test_config, "sha256", None)
    return {TEST_CONFIG_SHA256_KEY: digest} if digest else {}


def build_test_step(raw: JsonObject) -> TestStep:
    """One validated step document, as the reactor holds it.

    A block step's nested subset is built into `TestStep` objects rather than
    left in `arguments` as raw mappings: everything downstream (preflight, the
    lock declaration, the executor) walks steps, and a nested list that was not
    also a list of steps would be a part of the plan none of them could see."""
    nested = raw.get("steps") if raw["action"] == REPEAT_ACTION else None
    excluded = {"action", *ROUTE_FIELDS} if nested is None else {"action", "steps", *ROUTE_FIELDS}
    return TestStep(
        action=str(raw["action"]),
        arguments={key: value for key, value in raw.items() if key not in excluded},
        steps=tuple(build_test_step(item) for item in nested or ()),
        **{name: None if raw.get(name) is None else str(raw[name]) for name in ROUTE_FIELDS},
    )


def reject_superseded_plan_version(raw: JsonObject, path: str | None = None) -> None:
    """Refuse a version 1 plan by name.

    Version 1 steps addressed a `device:` naming a single modelled unit the
    config no longer has, not the `device:` of version 3, which names one entry
    of the `debuggers`, `com_ports` or `can_buses` sections. Leaving the plan at
    version 1 would have made every old plan invalid with only a bare const
    mismatch to go on, so the break gets a version boundary and a message that
    says which key replaced which."""
    if raw.get("version") != 1:
        return
    raise ConfigError(
        "test_config_invalid",
        "This test plan is version 1, whose steps address a `device:` that no longer exists. Set `version: 2` and route each step by the name it drives.",
        {
            "path": path,
            "field": "version",
            "migration": {
                "device (flash, debug_start, run_until_breakpoint, dump_memory, debug_stop)": "debugger: <name of a debuggers entry>, or omit it while the project configures exactly one probe",
                "device (uart_open, uart_close)": "port_id: <name of a com_ports entry>",
                "(new in version 2) can_open, can_close, can_send, can_read": "bus_id: <name of a can_buses entry>; version 1 had no CAN steps, so there is nothing here to rewrite",
            },
        },
    )


def resolve_schema_node(node: Any, schema: JsonObject) -> JsonObject:
    """One schema node with a local `$ref` followed, so a marker on the shared
    definition is read wherever that definition is used."""
    if not isinstance(node, dict):
        return {}
    reference = node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        return {**schema["$defs"].get(reference.rsplit("/", 1)[-1], {}), **node}
    return node


def reject_features_newer_than_plan_version(raw: JsonObject, path: str | None = None) -> None:
    """Refuse a plan that uses a step format its own `version` predates.

    The version a plan declares is what tells another install whether it can run
    it, so a plan saying `version: 2` while using a v3 action or a v3 key would
    be a plan that works here and fails there for no stated reason. Which things
    are newer is read off the bundled schema's own `x-since-version` markers
    rather than from a list kept here, so the schema stays the single authority
    on what the format contains and when it arrived."""
    version = raw.get("version")
    if not isinstance(version, int):
        return
    reject_newer_features_in_steps(raw["steps"], version, test_config_schema(), "steps", path)


def reject_newer_features_in_steps(steps: list[Any], version: int, schema: JsonObject, prefix: str, path: str | None) -> None:
    """The version gate, for one list of steps and the lists nested inside it.

    The walk descends into a block step's own subset, so a `version: 3` plan
    cannot smuggle a version 4 action past the gate by putting it inside a
    `repeat`, which would be exactly the plan that runs here and is refused on
    an older install for no stated reason. Reached only after schema validation,
    so every step is a mapping and every action is one the schema knows."""
    for index, step in enumerate(steps):
        definition = schema["$defs"][ACTION_SCHEMAS[step["action"]]]
        introduced = definition.get(PLAN_FEATURE_VERSION_KEY)
        if isinstance(introduced, int) and version < introduced:
            raise_outdated_plan_version(path, f"{prefix}[{index}].action", step["action"], version, introduced)
        for key in step:
            property_schema = resolve_schema_node(definition.get("properties", {}).get(key), schema)
            introduced = property_schema.get(PLAN_FEATURE_VERSION_KEY)
            if isinstance(introduced, int) and version < introduced:
                raise_outdated_plan_version(path, f"{prefix}[{index}].{key}", key, version, introduced)
        nested = step.get("steps")
        if step["action"] == REPEAT_ACTION and isinstance(nested, list):
            reject_newer_features_in_steps(nested, version, schema, f"{prefix}[{index}].steps", path)


def raise_outdated_plan_version(path: str | None, field_name: str, value: str, version: int, introduced: int) -> None:
    raise ConfigError(
        "test_config_invalid",
        f"This test plan says `version: {version}`, but this step uses `{value}`, which test plan format version {introduced} introduced.",
        {
            "path": path,
            "field": field_name,
            "value": value,
            "plan_version": version,
            "requires_plan_version": introduced,
            "migration": f"Set `version: {introduced}` on this plan, or write the step the way version {version} spells it.",
        },
    )


def validate_test_config_schema(raw: JsonObject, path: str | None = None) -> None:
    schema = test_config_schema()
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ConfigError(
            "test_config_schema_invalid",
            "Bundled test reactor configuration schema is invalid.",
            {"schema": TEST_CONFIG_SCHEMA_RESOURCE, "schema_error": str(error)},
        ) from error
    # One blanking, everywhere a step list appears. Both the plan's own `steps:`
    # and a block step's nested one are `#/$defs/steps`, so emptying that
    # definition's `items` leaves every list validated for its shape and length
    # while each step is held to its own action's entry below, which is what
    # turns an opaque `oneOf` failure into a field path naming the step.
    blanked = {**schema, "$defs": {**schema["$defs"], "steps": {**schema["$defs"]["steps"], "items": {}}}}
    errors = sorted(Draft202012Validator(blanked).iter_errors(raw), key=lambda item: list(item.absolute_path))
    if errors:
        raise_test_config_validation_error(errors[0], path)

    validate_test_steps_schema(raw["steps"], blanked, path, ["steps"])


def validate_test_steps_schema(steps: list[Any], schema: JsonObject, path: str | None, prefix: list[str]) -> None:
    """Hold one list of steps to the schema, and descend into the nested lists.

    `prefix` is the field path of the list, so a step inside a block step is
    named where it actually is (`steps[2].steps[1].action`) rather than at the
    top level, where a reader would count down the file and find another step."""
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ConfigError(
                "test_config_invalid",
                "Test reactor step must be a mapping.",
                {"path": path, "field": format_test_config_field([*prefix, str(index)]), "value": step},
            )
        action = step.get("action")
        if action not in ACTION_SCHEMAS:
            raise ConfigError(
                "test_config_invalid",
                "Test reactor action has an unsupported value.",
                {
                    "path": path,
                    "field": format_test_config_field([*prefix, str(index), "action"]),
                    "value": action,
                    "allowed_values": list(ACTION_SCHEMAS),
                },
            )
        step_schema = {**schema["$defs"][ACTION_SCHEMAS[action]], "$defs": schema["$defs"]}
        step_errors = sorted(Draft202012Validator(step_schema).iter_errors(step), key=lambda item: list(item.absolute_path))
        if step_errors:
            raise_test_config_validation_error(step_errors[0], path, [*prefix, str(index)])
        nested = step.get("steps")
        if action == REPEAT_ACTION and isinstance(nested, list):
            validate_test_steps_schema(nested, schema, path, [*prefix, str(index), "steps"])


def raise_test_config_validation_error(error: Any, path: str | None, prefix: list[str] | None = None) -> None:
    parts = [*(prefix or []), *(str(part) for part in error.absolute_path)]
    if error.validator == "required":
        missing = next((name for name in error.validator_value if name not in error.instance), None)
        if missing is not None:
            parts.append(str(missing))
    elif error.validator == "additionalProperties":
        match = re.search(r"'([^']+)' was unexpected", error.message)
        if match:
            parts.append(match.group(1))
    field = format_test_config_field(parts)
    details: JsonObject = {
        "path": path,
        "field": field,
        "schema_error": "Value does not satisfy the test configuration schema.",
        "validator": error.validator,
    }
    if isinstance(error.instance, (str, int, float, bool)) or error.instance is None:
        details["value"] = error.instance[:128] if isinstance(error.instance, str) else error.instance
    if error.validator in {"enum", "const"}:
        details["allowed_values"] = error.validator_value if error.validator == "enum" else [error.validator_value]
    if error.validator == "oneOf":
        # A step whose alternatives are "exactly one of these keys": the shape
        # `uart_expect` uses for `text` / `pattern`. Bare, a oneOf failure names
        # only the step, which is the difference between "this step is wrong
        # somewhere" and "say one of these two". The keys come off the branches
        # rather than from a list here, so they cannot drift from the schema.
        alternatives = list(
            dict.fromkeys(name for branch in error.validator_value if isinstance(branch, dict) for name in branch.get("required", ()))
        )
        if alternatives:
            details["exactly_one_of"] = alternatives
    raise ConfigError("test_config_invalid", "Test reactor configuration failed schema validation.", details) from error


def format_test_config_field(parts: list[str]) -> str:
    field = ""
    for part in parts:
        if part.isdigit():
            field = f"{field}[{part}]" if field else f"[{part}]"
        else:
            field = f"{field}.{part}" if field else part
    return field or "$"


@dataclass(frozen=True)
class StepActionSpec:
    """One step action, exactly as the method that serves it declared it.

    This is the whole of what the reactor knows about an action: which schema
    validates it, which MCP tool it calls, what the schema documents as defaults,
    whether the step's own arguments are that tool's arguments, and whether it
    drives the device at all. Nothing outside a `@step_action` decoration adds to
    this, which is what keeps the declaration the single authority the hierarchy
    already demanded of the hand-written tables it replaces."""

    name: str
    # The `$defs` entry in the bundled plan schema that validates this step.
    schema: str
    # The attribute on the device class that implements it.
    method: str
    # The MCP tool the implementation calls, or None when it calls several or
    # none. Held to the tool vocabulary of the class's own `device_class`.
    tool: str | None = None
    # Arguments the tool call takes when the plan leaves them out. The schema
    # documents these as defaults, and JSON Schema defaults do not apply
    # themselves.
    defaults: JsonObject = field(default_factory=dict)
    # "open" / "close" for the two ends of a session, read by SessionDevice;
    # empty for an action that is neither.
    role: str = ""
    # True when the step's own arguments are the tool's arguments, so preflight
    # can put them through that tool's MCP contract. False for an action that
    # reads its arguments itself (a deadline and an expectation are not
    # `com_read` arguments) and builds the call it makes.
    forwards_arguments: bool = True
    # False for an action that drives no hardware: it still routes to a device
    # and still appears in the run's steps, but nothing about sessions,
    # permissions or plan state applies to it.
    touches_device: bool = True


@dataclass(frozen=True)
class BoundStepAction:
    """One device's declared action, bound to the object that will run it."""

    spec: StepActionSpec
    call: Callable[[TestStep], JsonObject]


STEP_ACTION_DECLARATIONS = "agentic_hil_step_actions"


def step_action(
    name: str,
    *,
    schema: str,
    tool: str | None = None,
    defaults: JsonObject | None = None,
    role: str = "",
    forwards_arguments: bool = True,
    touches_device: bool = True,
) -> Callable[[Any], Any]:
    """Declare that this method serves a step action.

    Declaration-on-the-method rather than a dictionary built in `__init__`: the
    plan schema is exported by reading these off the classes, which must keep
    working without constructing a device and therefore without a bench. Applied
    more than once to one method, it declares that method under each name, which
    is how an older action name stays valid beside a newer one."""

    def declare(method: Any) -> Any:
        spec = StepActionSpec(
            name=name,
            schema=schema,
            method=method.__name__,
            tool=tool,
            defaults=dict(defaults or {}),
            role=role,
            forwards_arguments=forwards_arguments,
            touches_device=touches_device,
        )
        setattr(method, STEP_ACTION_DECLARATIONS, (*getattr(method, STEP_ACTION_DECLARATIONS, ()), spec))
        return method

    return declare


def collect_step_actions(device_class: type) -> dict[str, StepActionSpec]:
    """Every action a device class serves, its own and its ancestors'.

    Walked base-first, so a subclass redeclaring an inherited name replaces it
    rather than being shadowed by it, and an action declared once on the base
    (`delay`) is served by every kind without any of them repeating it."""
    collected: dict[str, StepActionSpec] = {}
    for ancestor in reversed(device_class.__mro__):
        for attribute in vars(ancestor).values():
            for spec in getattr(attribute, STEP_ACTION_DECLARATIONS, ()):
                collected[spec.name] = spec
    return collected


def test_config_schema() -> JsonObject:
    """The bundled plan schema, read once. Callers must not mutate it.

    The same object `agentic-hil://reference/test-plan` is generated from, so
    the document a plan author reads describes the schema this validates
    against rather than a copy of it."""
    return plan_schema_document()


def action_schema_errors(schema_name: str, document: JsonObject) -> list[Any]:
    """A reconstructed step, held to its own action's schema entry."""
    schema = test_config_schema()
    definition = schema["$defs"].get(schema_name)
    if definition is None:
        return []
    validator = Draft202012Validator({**definition, "$defs": schema["$defs"]})
    return sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))


class StepDevice:
    """One configured device, as a test plan drives it.

    This is the single authority on what a step means. A device kind declares the
    actions it serves and the schema `$defs` entry each validates against, which
    MCP tool an action calls, what its preflight refuses, and what its cleanup
    closes. The reactor resolves a step to one of these and calls the interface;
    it never asks what kind of thing it is holding, so a new device kind is a new
    subclass and nothing else.

    ``StepDevice`` is deliberately not a ``devices.Device``. That hierarchy
    answers what a unit *is* (its hardware-derived lock key, its tool
    vocabulary, its mutex), and it is imported by the backends themselves; a plan
    step is a different question, asked one layer up. Each subclass here names
    its ``device_class`` and builds one for identity, so there is still exactly
    one place that says what a serial line is.

    Naming and preflight are classmethods on purpose: they run before anything is
    built, and building a per-probe service or a device object merely to ask a
    question about a plan is exactly what a plan that fails validation must not
    have caused."""

    kind: ClassVar[str] = "device"
    # The identity/mutex class in `devices` this kind drives.
    device_class: ClassVar[type[Device]] = Device
    # The plan key naming this kind's config entry, the result key an unknown
    # name is answered with, and the sentence that answers it. `device:` is the
    # v3 spelling of the same thing and is read for every kind.
    route_field: ClassVar[str] = ""
    configured_field: ClassVar[str] = ""
    unknown_name_summary: ClassVar[str] = ""
    # Every action this kind serves, collected from the `@step_action`
    # decorations on the methods that implement them. Derived, never written:
    # `step_actions` is the action → schema projection ACTION_SCHEMAS is built
    # from, so neither can disagree with the method that actually runs.
    step_action_specs: ClassVar[dict[str, StepActionSpec]] = {}
    step_actions: ClassVar[dict[str, str]] = {}
    # Where this kind falls when a run closes what it left open. Debug sessions
    # close before serial lines and CAN buses: a still-attached debugger can keep
    # writing to a line or a bus this plan is about to close.
    cleanup_order: ClassVar[int] = 1

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.step_action_specs = collect_step_actions(cls)
        cls.step_actions = {name: spec.schema for name, spec in cls.step_action_specs.items()}

    def __init__(self, config_id: str, device: Device, service: AgenticHILToolService, *, stop_requested: Callable[[], bool] = never_stopped):
        self.id = config_id
        self.device = device
        self.service = service
        # Whether this run has been asked to stop. A narrow hook rather than a
        # reference to the reactor: the only thing a device does with the answer
        # is cut a wait short, and a device that could see the run could see
        # everything the reactor knows.
        self.stop_requested = stop_requested
        self.cleanup_interrupts: list[BaseException] = []
        # True once this device has actually run an action that drives it. A
        # device object is built on first use, and `delay` routes to one without
        # touching it, so being in the run's `devices` map is not proof the run
        # drove this unit. Failure recovery asks this instead.
        self.drove_device = False
        # This device's actions, bound: the string a plan writes, the schema that
        # validates it and the method that runs it, in one place, built once.
        self.actions: dict[str, BoundStepAction] = {
            name: BoundStepAction(spec=spec, call=getattr(self, spec.method)) for name, spec in type(self).step_action_specs.items()
        }

    # --- naming: which config entry a step drives ------------------------

    @classmethod
    def config_entries(cls, config: AgenticHILConfig) -> Mapping[str, Any]:
        """This kind's section of the authoritative config."""
        raise NotImplementedError

    @classmethod
    def build_device(cls, config: AgenticHILConfig, config_id: str) -> Device:
        """The identity object for one named entry, or DeviceError."""
        raise NotImplementedError

    @classmethod
    def step_config_id(cls, config: AgenticHILConfig, step: TestStep) -> str | None:
        """The config entry this step names, by this kind's own key or by the
        universal `device:`. Both spellings mean the same entry, and a step that
        writes both is refused before this is asked."""
        named = getattr(step, cls.route_field)
        return step.device if named is None else named

    @classmethod
    def service_for(cls, reactor: TestReactor, config_id: str) -> AgenticHILToolService:
        """The tool service this device's calls go through. The run's own for
        every kind whose tools name their config entry in the arguments; only a
        debugger needs another, because no debugger tool carries a probe name."""
        return reactor.service

    @classmethod
    def identify(cls, config: AgenticHILConfig, step: TestStep) -> Device | None:
        """The configured unit a step drives, or None when the config does not
        know the name the step gave. Left out rather than raised: preflight
        refuses that step with a message about the name, which is a better answer
        than a lock failure about a device nobody configured."""
        name = cls.step_config_id(config, step)
        return None if name is None or name not in cls.config_entries(config) else cls.build_device(config, name)

    @classmethod
    def name_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep) -> JsonObject | None:
        entries = cls.config_entries(reactor.config)
        name = cls.step_config_id(reactor.config, step)
        if name is None:
            return cls.unnamed_refusal(reactor, location, step)
        if name not in entries:
            return preflight_error(location, step, cls.route_field, cls.unknown_name_summary, {cls.configured_field: sorted(entries)})
        return None

    @classmethod
    def unnamed_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep) -> JsonObject | None:
        """A step that named no entry. Only a debugger may leave its route field
        out, and only while the project configures exactly one probe; the schema
        requires every other kind's."""
        return preflight_error(location, step, cls.route_field, cls.unknown_name_summary, {cls.configured_field: sorted(cls.config_entries(reactor.config))})

    # --- plan time -------------------------------------------------------

    @classmethod
    def preflight(cls, reactor: TestReactor, location: StepLocation, step: TestStep, state: PlanState) -> JsonObject | None:
        """Refuse this step against the config and what the plan has already done,
        or None. Runs for every step before the first one executes."""
        return None

    @classmethod
    def tool_calls(cls, config: AgenticHILConfig, step: TestStep) -> list[tuple[str, JsonObject]]:
        """The plan-supplied tool calls a step will make, as (tool, arguments),
        so preflight can put them through the exact MCP contract validators the
        dispatcher enforces. A step this reactor's schema accepted but the tool
        contract rejects then fails before any backend builds hardware state.

        Read off the declaration: an action that forwards the step's own
        arguments is checked here, and one that reads them itself is not, because
        what it hands the tool is not what the plan wrote."""
        spec = cls.step_action_specs.get(step.action)
        name = cls.step_config_id(config, step)
        if spec is None or spec.tool is None or not spec.forwards_arguments or name is None:
            return []
        return [(spec.tool, cls.call_arguments(name, step))]

    @classmethod
    def call_arguments(cls, config_id: str, step: TestStep) -> JsonObject:
        """A step's tool arguments: what the plan wrote, under the defaults the
        schema documents, addressed to this device's own config entry so a step
        cannot name one entry and reach another."""
        spec = cls.step_action_specs.get(step.action)
        arguments = {**(spec.defaults if spec is not None else {}), **step.arguments}
        scope = cls.device_class.scope_field
        if scope is not None:
            arguments[scope] = config_id
        return arguments

    # --- run time --------------------------------------------------------

    def execute(self, step: TestStep) -> JsonObject:
        """Dispatch, written exactly once for every device kind there is.

        Look the action string up among this device's declarations, hold the step
        to that action's own schema, call the method. A string this kind does not
        declare is `not_supported` naming the kind by construction. There is no
        table to forget to add it to."""
        bound = self.actions.get(step.action)
        if bound is None:
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": "not_supported",
                "summary": f"The {self.kind} device kind does not serve this test reactor action.",
                self.route_field: self.id,
                "action": step.action,
                "supported_actions": sorted(self.actions),
            }
        invalid = self.schema_refusal(bound.spec, step)
        if invalid is not None:
            return invalid
        if bound.spec.touches_device:
            # Recorded before the call, not after: an action that drove the probe
            # and then failed (the reset this run is recovering from) still
            # drove it, and is exactly the device recovery must not skip.
            self.drove_device = True
        return bound.call(step)

    def schema_refusal(self, spec: StepActionSpec, step: TestStep) -> JsonObject | None:
        """The step, put back together and held to its action's schema.

        `load_test_config` already does this for a plan it read; this is the same
        check on the dispatch path, so a step some other caller assembled cannot
        reach a device method carrying arguments the format never allowed."""
        errors = action_schema_errors(spec.schema, {"action": spec.name, self.route_field: self.id, **step.arguments})
        if not errors:
            return None
        return {
            "ok": False,
            "tool": "test_reactor",
            "error_type": "invalid_argument",
            "summary": "Test step does not satisfy its action's schema.",
            self.route_field: self.id,
            "action": step.action,
            "field": format_test_config_field([str(part) for part in errors[0].absolute_path]),
            "validator": errors[0].validator,
        }

    def call_tool(self, step: TestStep) -> JsonObject:
        """Hand this step's own arguments to the tool its action declared. The
        implementation of every action whose step *is* the tool call."""
        return self.service.call(str(self.actions[step.action].spec.tool), self.call_arguments(self.id, step))

    @step_action("delay", schema="delay", touches_device=False)
    def _delay(self, step: TestStep) -> JsonObject:
        """Wait, and drive nothing.

        Declared once here, so every device kind serves it and none of them had
        to be told: a plan waiting on a board is waiting on that board whatever
        kind of thing it is. It is still a step (it routes to a named device and
        it appears in the run's result) because a wait that left no trace would
        be a gap in the record of what a test did.

        The wait is sliced, and the slices are counted against one deadline
        rather than added up, so an uninterrupted wait takes exactly as long as
        the plan asked. What the slicing buys is the only other thing that can
        happen during it: a stop request, which ends the wait early and says so
        rather than being answered ten minutes later."""
        duration_ms = int(step.arguments["duration_ms"])
        started = time.monotonic()
        deadline = started + duration_ms / 1000.0
        while not self.stop_requested():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(remaining, DELAY_STOP_POLL_MS / 1000.0))
        waited_ms = int(round((time.monotonic() - started) * 1000))
        result: JsonObject = {
            "ok": True,
            "tool": "test_reactor",
            "summary": "Test plan waited.",
            self.route_field: self.id,
            "action": "delay",
            "duration_ms": duration_ms,
        }
        if waited_ms < duration_ms:
            # A wait cut short is not a failed step, and reporting it as one
            # would turn an orderly stop into an incident. It is a wait that did
            # not finish, which the record has to say so nobody reads the plan's
            # own number as what the board was given.
            result["stop_requested"] = True
            result["waited_ms"] = waited_ms
            result["summary"] = "Test plan waited until a stop was requested."
        return result

    def cleanup(self) -> list[JsonObject]:
        """Close what this device is still holding for this plan."""
        return []

    def cleanup_record(self, action: str, result: JsonObject) -> JsonObject:
        return {self.route_field: self.id, "action": action, "result": result}

    def cleanup_call(self, tool: str, arguments: JsonObject | None = None) -> JsonObject:
        try:
            return self.service.call(tool, arguments)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                self.cleanup_interrupts.append(error)
            return exception_result(tool, "cleanup_exception", "Cleanup action raised an exception.", error)


# `StepDevice` declares `delay` itself, and `__init_subclass__` only fires for
# subclasses, so the base's own collection is made here: one call, the same one
# every kind gets.
StepDevice.step_action_specs = collect_step_actions(StepDevice)
StepDevice.step_actions = {name: spec.schema for name, spec in StepDevice.step_action_specs.items()}


class SessionDevice(StepDevice):
    """A device a plan opens and closes: a serial line, a CAN bus.

    The two differ in their permissions and in the traffic they carry, not in
    their session: both can be left open by a plan that fails, both may only be
    closed by the run that opened them, and both are closed again by that run's
    cleanup. That is written once, here.

    Which of a kind's actions are those two ends is read off the declarations
    (`role="open"` / `role="close"`), not named again in a class attribute."""

    open_action: ClassVar[str] = ""
    close_action: ClassVar[str] = ""
    # How this kind's session is named in a refusal, e.g. "UART session ...".
    session_noun: ClassVar[str] = ""
    not_owned_error: ClassVar[str] = ""
    not_owned_summary: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.open_action = cls.action_in_role("open")
        cls.close_action = cls.action_in_role("close")

    @classmethod
    def action_in_role(cls, role: str) -> str:
        return next((name for name, spec in cls.step_action_specs.items() if spec.role == role), "")

    def __init__(self, config_id: str, device: Device, service: AgenticHILToolService, **kwargs: Any):
        super().__init__(config_id, device, service, **kwargs)
        # True while this plan holds the session it opened.
        self._owns_session = False

    @classmethod
    def preflight(cls, reactor: TestReactor, location: StepLocation, step: TestStep, state: PlanState) -> JsonObject | None:
        name = str(cls.step_config_id(reactor.config, step))
        refusal = cls.permission_refusal(reactor, location, step, cls.config_entries(reactor.config)[name])
        if refusal is not None:
            return refusal
        key = (cls.kind, name)
        if step.action == cls.open_action:
            if key in state.open_sessions:
                return preflight_error(location, step, "action", f"{cls.session_noun} session is already open in this test plan.")
            state.open_sessions.add(key)
            return None
        if key not in state.open_sessions:
            closing = step.action == cls.close_action
            return preflight_error(location, step, "action", f"{cls.session_noun} session must be opened before {'it can be closed' if closing else 'this action'}.")
        if step.action == cls.close_action:
            state.open_sessions.discard(key)
        return None

    @classmethod
    def permission_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep, entry: Any) -> JsonObject | None:
        """Refuse a step this entry's own permissions do not allow."""
        return None

    def open_session(self, step: TestStep) -> JsonObject:
        """Open, and take ownership of what this plan opened. The body of every
        kind's `role="open"` action.

        Ownership is taken once and kept, rather than re-decided on each open. An
        open inside a `repeat` block runs once per iteration, and every open
        after the first answers `already_active` because the first one is still
        in force, so re-deciding would hand ownership back on the second
        iteration and leave cleanup with nothing to close, which is a session
        left open by the run that opened it."""
        result = self.call_tool(step)
        self._owns_session = self._owns_session or (result.get("ok") is True and not result.get("already_active", False))
        return result

    def close_session(self, step: TestStep) -> JsonObject:
        """Close, but only a session this plan is the one holding. The body of
        every kind's `role="close"` action."""
        if not self._owns_session:
            return {
                "ok": False,
                "tool": "test_reactor",
                "error_type": self.not_owned_error,
                "summary": self.not_owned_summary,
                self.route_field: self.id,
            }
        result = self.call_tool(step)
        if result.get("ok") is True:
            self._owns_session = False
        return result

    def cleanup(self) -> list[JsonObject]:
        if not self._owns_session:
            return []
        result = self.cleanup_call(str(self.step_action_specs[self.close_action].tool), {str(self.device_class.scope_field): self.id})
        if result.get("ok") is True:
            self._owns_session = False
        return [self.cleanup_record(self.close_action, result)]


class UartRunner(SessionDevice):
    """A configured serial line, as a plan opens it, waits on it and closes it."""

    kind: ClassVar[str] = "uart"
    device_class: ClassVar[type[Device]] = UartDevice
    route_field: ClassVar[str] = "port_id"
    configured_field: ClassVar[str] = "configured_com_ports"
    unknown_name_summary: ClassVar[str] = "Test step references a COM port that is not in the authoritative config."
    # The two ways a v2 `uart_expect` says what to wait for, as its schema names
    # them. Exactly one appears on a step; the key is also the one the result
    # reports under, so `expected_text` and `expected_pattern` say which kind of
    # claim went unmet.
    expect_fields: ClassVar[tuple[str, ...]] = ("text", "pattern")
    session_noun: ClassVar[str] = "UART"
    not_owned_error: ClassVar[str] = "uart_session_not_owned"
    not_owned_summary: ClassVar[str] = "A test plan cannot close a UART session it did not open."

    @classmethod
    def config_entries(cls, config: AgenticHILConfig) -> Mapping[str, Any]:
        return config.com_ports

    @classmethod
    def build_device(cls, config: AgenticHILConfig, config_id: str) -> Device:
        return uart_device(config, config_id)

    # --- the actions this kind serves ------------------------------------

    @step_action("uart_open", schema="uartOpen", tool="com_session_start", defaults={"clear_buffer": True}, role="open")
    def _uart_open(self, step: TestStep) -> JsonObject:
        return self.open_session(step)

    @step_action("uart_close", schema="uartClose", tool="com_session_stop", role="close")
    def _uart_close(self, step: TestStep) -> JsonObject:
        return self.close_session(step)

    @step_action("uart_write", schema="uartWrite", tool="com_write")
    def _uart_write(self, step: TestStep) -> JsonObject:
        """Serial stimulus, straight through `com_write`: the same tool and the
        same permission an agent driving the line by hand would meet. A port the
        config will not let a plan write to is refused by name at preflight, so
        the plan is corrected rather than the bench half driven."""
        return self.call_tool(step)

    @step_action("uart_expect", schema="uartExpect", tool="com_read", forwards_arguments=False)
    def _uart_expect(self, step: TestStep) -> JsonObject:
        # Exactly one of the two is present (the schema's own `oneOf` is what
        # holds that), so the first one found names both the claim and the key
        # the result reports it under.
        field_name = next(name for name in self.expect_fields if step.arguments.get(name) is not None)
        return self._expect(field_name, str(step.arguments[field_name]), float(step.arguments["timeout_s"]))

    @step_action("uart_read", schema="uartRead", tool="com_read", forwards_arguments=False)
    def _uart_read(self, step: TestStep) -> JsonObject:
        """Read the line, and judge what came back if the plan said what it wants.

        With a `comparator:` this is read-until-match on the same rolling buffer
        `uart_expect` uses, failing with the tail of what the port did say.
        Without one it is a plain read: one `com_read`, answered unchanged."""
        raw = step.arguments.get("comparator")
        if not raw:
            return self.service.call("com_read", {"port_id": self.id, "wait_timeout_s": float(step.arguments.get("timeout_s", 0.0))})
        return self._compare(StepComparator(raw), float(step.arguments["timeout_s"]))

    # --- plan time -------------------------------------------------------

    @classmethod
    def permission_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep, entry: Any) -> JsonObject | None:
        # The open is asked about reading, and that covers every action that only
        # listens: each of them requires a session this plan opened, so a port
        # the config will not let a plan read is refused before any of them.
        # Writing is its own grant and its own refusal, named where it applies.
        if step.action == cls.open_action and not reactor.config.com_read_allowed(entry):
            return preflight_error(location, step, "action", "Reading this COM port is disabled by the authoritative config.")
        if step.action == "uart_write" and not entry.permissions.allow_write:
            return preflight_error(location, step, "action", "Writing to this COM port is disabled by the authoritative config.", {"permission": "allow_write"})
        return None

    @classmethod
    def preflight(cls, reactor: TestReactor, location: StepLocation, step: TestStep, state: PlanState) -> JsonObject | None:
        refusal = cls._pattern_refusal(location, step)
        if refusal is not None:
            return refusal
        return super().preflight(reactor, location, step, state)

    @classmethod
    def _pattern_refusal(cls, location: StepLocation, step: TestStep) -> JsonObject | None:
        """Refuse a regular expression this plan cannot mean, before the run.

        The comparator's half of this is device-neutral and lives in
        `comparator_pattern_refusal`; what is left here is the v2 `uart_expect`
        spelling, whose `pattern:` is a bare key on the step and never carries a
        range."""
        if step.action == "uart_expect":
            return pattern_refusal(location, step, "pattern", step.arguments.get("pattern"), "Test step's uart_expect pattern is not a valid Python regular expression.", needs_capture=False)
        if step.action == "uart_read":
            return comparator_pattern_refusal(location, step)
        return None

    # --- reading the line ------------------------------------------------

    @staticmethod
    def _matcher(field_name: str, expected: str) -> Callable[[str, bool], bool]:
        """What counts as a match for one of the two v2 expectation kinds.

        A pattern is `re.search`, so it is anchored where it is written and
        nowhere else: a plan that means "the line starts this way" writes the
        `^` itself. Compiled once per step rather than per read: the pattern
        already compiled cleanly at preflight, so this cannot raise. Neither kind
        judges the last pass differently, so both ignore `final`."""
        if field_name == "text":
            return lambda decoded, final: expected in decoded
        search = re.compile(expected).search
        return lambda decoded, final: search(decoded) is not None

    def _expect(self, field_name: str, expected: str, timeout_s: float) -> JsonObject:
        """The v2 `uart_expect`, reported under the names a v2 report already
        uses. The waiting itself is `_read_until`, shared with `uart_read`."""
        appeared, missing = UART_EXPECT_SUMMARIES[field_name]
        outcome = self._read_until(timeout_s, expected, self._matcher(field_name, expected))
        if outcome.read_failure is not None:
            return outcome.read_failure
        if outcome.matched:
            return {
                "ok": True,
                "tool": "test_reactor",
                "summary": appeared,
                "port_id": self.id,
                f"expected_{field_name}": expected,
                "timeout_s": timeout_s,
                "bytes_received": outcome.bytes_received,
                "reads": outcome.reads,
            }
        return {
            "ok": False,
            "tool": "test_reactor",
            "error_type": "uart_expect_timeout",
            "summary": missing,
            "port_id": self.id,
            f"expected_{field_name}": expected,
            "timeout_s": timeout_s,
            "bytes_received": outcome.bytes_received,
            "reads": outcome.reads,
            # What the port actually said, so a red result is readable without
            # reconnecting to the board.
            **outcome.tail_result,
        }

    def _compare(self, comparator: StepComparator, timeout_s: float) -> JsonObject:
        """Read until the comparator is met, or fail saying what was received,
        and, for a range, which value was captured against which bounds."""
        met, unmet = COMPARATOR_SUMMARIES[comparator.claim]
        outcome = self._read_until(timeout_s, comparator.written, comparator.matches)
        if outcome.read_failure is not None:
            return outcome.read_failure
        common = {"port_id": self.id, **comparator.report(), "timeout_s": timeout_s, "bytes_received": outcome.bytes_received, "reads": outcome.reads}
        if outcome.matched:
            return {"ok": True, "tool": "test_reactor", "summary": met, **common, **outcome.matched_result(comparator.matched_text)}
        return {"ok": False, "tool": "test_reactor", "error_type": "comparator_unmet", "summary": unmet, **common, **outcome.tail_result}

    def _read_until(self, timeout_s: float, written: str, matches: Callable[[str, bool], bool]) -> UartReadOutcome:
        """Read this line until `matches` is satisfied or the deadline passes.

        The plan's own read path and nothing else: each pass is one `com_read`
        (the tool an agent would call by hand), waiting out whatever is left of
        the step's deadline. `com_read` returns the moment the session has bytes,
        so a banner that arrives in pieces is several short reads rather than one
        long one, and the match is therefore made against everything read so far
        and never against the chunk that happened to arrive last. A line that
        says nothing at all costs exactly one read.

        Bytes are carried across passes rather than decoded text: a character
        split across two reads decodes to replacement characters on each side of
        the seam, and a plan would then be waiting for text that did arrive. The
        same seam is why a pattern is matched against the decoded window rather
        than against each chunk: a value that arrives in two reads is one match
        here and none at all to a per-chunk search."""
        deadline = time.monotonic() + timeout_s
        # Enough tail to still find a match that only completes in the next
        # chunk, and enough to answer with when the deadline passes. Four bytes
        # is the widest any one character encodes to in UTF-8, so this cannot cut
        # into a match no matter how long the expected text is. A pattern is
        # measured the same way, by what the plan wrote: the floor is what keeps
        # a short pattern matching across a read boundary, and a pattern whose
        # match is longer than the window is bounded by it like everything else.
        window = max(UART_EXPECT_TAIL_BYTES, 4 * len(written) + 16)
        received = bytearray()
        received_bytes = 0
        encoding = "utf-8"
        reads = 0
        while True:
            result = self.service.call("com_read", {"port_id": self.id, "wait_timeout_s": max(0.0, deadline - time.monotonic())})
            reads += 1
            if result_failed(result):
                return UartReadOutcome(False, reads, received_bytes, bytes(received[-UART_EXPECT_TAIL_BYTES:]), encoding, result)
            data = result.get("data") or {}
            encoding = str(data.get("encoding") or encoding)
            chunk = bytes.fromhex(str(data.get("hex") or ""))
            received_bytes += len(chunk)
            received.extend(chunk)
            del received[:-window]
            # Asked before the match, so the pass the deadline has run out on is
            # the one that gets the last look, where a claim that can only be
            # settled by "nothing more is coming" is allowed to settle.
            expired = time.monotonic() >= deadline
            raw = bytes(received)
            if matches(decode_bytes(raw, encoding), expired):
                # The comparator recorded where in the decoded window it matched;
                # slice the raw wire bytes out of the same buffer so the report's
                # `matched_text.hex` is what arrived, never a re-encoding of it.
                span = getattr(getattr(matches, "__self__", None), "matched_span", None)
                matched_raw = matched_span_bytes(raw, encoding, span) if span is not None else None
                return UartReadOutcome(True, reads, received_bytes, bytes(received[-UART_EXPECT_TAIL_BYTES:]), encoding, matched_raw=matched_raw)
            if expired:
                return UartReadOutcome(False, reads, received_bytes, bytes(received[-UART_EXPECT_TAIL_BYTES:]), encoding)
            if not chunk:
                # A read that returned nothing while time was left had its own
                # wait cut short (`com_read` ends its wait at once on a session
                # that is no longer active), so pace the next pass rather than
                # spinning on a line that has stopped talking.
                time.sleep(min(UART_EXPECT_IDLE_POLL_S, max(0.0, deadline - time.monotonic())))


class CanFrameComparator(StepComparator):
    """Which frame a `can_read` step requires.

    The claim vocabulary is the base class's (`equals`, `pattern`, `pattern`
    with `range`), asked of the medium a bus actually has. Two things follow from
    a frame being a complete unit that arrives with an identifier on it, and they
    are the whole of the difference:

    The claim applies only to frames the identifier filter selects. A bus carries
    every node's traffic, so a payload matched without saying whose frame it was
    is a green another ECU can produce; a frame the filter rejects is not judged
    at all, and nothing is captured from it.

    A payload is compared whole, not against a rolling window. There is no
    half-arrived frame to wait out and no seam to match across, so `equals` is an
    exact comparison against the payload as hexadecimal rather than the
    line-and-terminator reading a serial value needs."""

    def __init__(self, raw: JsonObject):
        super().__init__(raw)
        # `equals` is bytes on this medium, so the plan's spelling is normalized
        # once, here: `01 FF`, `01ff` and `01FF` are one claim, and the frames
        # this is compared against already carry `data_hex` in that same form. A
        # value the schema would have refused is left as written, so it simply
        # equals no payload rather than quietly becoming a different claim.
        if self.equals is not None:
            parsed_equals = parse_hex_bytes(self.equals)
            self.equals = self.equals if parsed_equals is None else parsed_equals.hex()
        # The schema requires `id` and holds both it and `id_mask` to an integer
        # or a hexadecimal string, so these parse. A comparator built by hand
        # without an id selects nothing rather than everything: the safe
        # direction, because the unsafe one is a step that passes on a frame
        # nobody asked about.
        self.frame_id: int | None = parse_can_id(raw.get("id"))
        self.id_mask: int | None = None if raw.get("id_mask") is None else parse_can_id(raw["id_mask"])
        # A standard 0x123 and an extended 0x123 are two different frames on the
        # wire, so the frame type is part of the claim, not a decoration. The
        # schema defaults it to `false`, so a comparator that does not say waits
        # for a standard frame; the received frames each carry `extended`.
        self.extended: bool = bool(raw.get("extended", False))

    def number(self, text: str) -> float:
        """A capture taken out of payload hexadecimal is a hexadecimal numeral."""
        return float(int(text, 16))

    def selects(self, frame: JsonObject) -> bool:
        """Whether this is a frame the plan is asking about at all."""
        if self.frame_id is None:
            return False
        if bool(frame.get("extended", False)) != self.extended:
            # An identifier match across the two namespaces is not a match: a
            # standard frame and its extended twin are distinct frames, the same
            # distinction the broker's participant filters draw. A comparator can
            # therefore never pass on traffic from the wrong frame type.
            return False
        if self.id_mask is None:
            return int(frame["id"]) == self.frame_id
        return int(frame["id"]) & self.id_mask == self.frame_id & self.id_mask

    def matches_frame(self, frame: JsonObject) -> bool:
        """Whether one received frame meets this claim."""
        if not self.selects(frame):
            return False
        payload = str(frame.get("data_hex") or "")
        if self.equals is not None:
            return payload == self.equals
        # `final` decides nothing on this medium. It exists so a serial value
        # that never carried a terminator can still be judged when time runs out,
        # and a frame is never in that state.
        return self.matches(payload, False)


@dataclass(frozen=True)
class CanReadOutcome:
    """What one bounded read of a CAN bus came back with."""

    frame: JsonObject | None
    reads: int
    frames_read: int
    # The last few frames seen, whether or not the identifier filter selected
    # them: a plan waiting for the wrong id is the failure this makes readable.
    tail: list[JsonObject]
    # The read's own refusal, when `can_read` itself failed. A closed session, a
    # denied permission or a broken audit latch is not a claim that went unmet,
    # and answering it as a timeout would send an operator to look at firmware.
    read_failure: JsonObject | None = None

    @property
    def tail_result(self) -> JsonObject:
        return {"frames_tail": self.tail, "frames_tail_truncated": self.frames_read > len(self.tail)}


class CanRunner(SessionDevice):
    """A configured CAN bus, as a plan opens it, drives it and closes it.

    The action set is the CAN tool surface and nothing beyond it: open, close,
    send one classic frame, read frames. Each permission refusal below is the one
    the backend would raise, moved to where a plan can still be corrected: a
    contradiction between a plan and the bus it names belongs to the plan, not to
    the bench half way through a run."""

    kind: ClassVar[str] = "can"
    device_class: ClassVar[type[Device]] = CanDevice
    route_field: ClassVar[str] = "bus_id"
    configured_field: ClassVar[str] = "configured_can_buses"
    unknown_name_summary: ClassVar[str] = "Test step references a CAN bus that is not in the authoritative config."
    session_noun: ClassVar[str] = "CAN bus"
    not_owned_error: ClassVar[str] = "can_session_not_owned"
    not_owned_summary: ClassVar[str] = "A test plan cannot close a CAN bus session it did not open."

    @classmethod
    def config_entries(cls, config: AgenticHILConfig) -> Mapping[str, Any]:
        return config.can_buses

    @classmethod
    def build_device(cls, config: AgenticHILConfig, config_id: str) -> Device:
        return can_device(config, config_id)

    # --- the actions this kind serves ------------------------------------

    @step_action("can_open", schema="canOpen", tool="can_session_start", defaults={"clear_rx_queue": True}, role="open")
    def _can_open(self, step: TestStep) -> JsonObject:
        return self.open_session(step)

    @step_action("can_close", schema="canClose", tool="can_session_stop", role="close")
    def _can_close(self, step: TestStep) -> JsonObject:
        return self.close_session(step)

    @step_action("can_send", schema="canSend", tool="can_send")
    def _can_send(self, step: TestStep) -> JsonObject:
        return self.call_tool(step)

    @step_action("can_read", schema="canRead", tool="can_read", forwards_arguments=False)
    def _can_read(self, step: TestStep) -> JsonObject:
        """Read the bus, and judge what came back if the plan said which frame it
        wants.

        With a `comparator:` this is read-until-match against the frames the same
        `can_read` tool yields, failing with the tail of what the bus did carry.
        Without one it is the v2 step unchanged: one `can_read`, the plan's own
        `max_frames` and `wait_timeout_s`, answered exactly as the tool answers
        it."""
        raw = step.arguments.get("comparator")
        if not raw:
            return self.call_tool(step)
        return self._compare(CanFrameComparator(raw), step.arguments.get("max_frames"), float(step.arguments["timeout_s"]))

    @classmethod
    def tool_calls(cls, config: AgenticHILConfig, step: TestStep) -> list[tuple[str, JsonObject]]:
        """A `can_read` reads its own arguments when it carries a comparator (a
        deadline and a claim are not `can_read` arguments), so the call preflight
        checks is the one this step will really make, built the same way."""
        name = cls.step_config_id(config, step)
        if step.action != "can_read" or not step.arguments.get("comparator") or name is None:
            return super().tool_calls(config, step)
        arguments: JsonObject = {"bus_id": name, "wait_timeout_s": step.arguments["timeout_s"]}
        if step.arguments.get("max_frames") is not None:
            arguments["max_frames"] = step.arguments["max_frames"]
        return [("can_read", arguments)]

    @classmethod
    def preflight(cls, reactor: TestReactor, location: StepLocation, step: TestStep, state: PlanState) -> JsonObject | None:
        if step.action == "can_read":
            refusal = comparator_pattern_refusal(location, step)
            if refusal is not None:
                return refusal
            if step.arguments.get("comparator") and step.arguments.get("wait_timeout_s") is not None:
                # Two deadlines with different owners. `timeout_s` is the step's,
                # and each read under a comparator waits out whatever is left of
                # it; `wait_timeout_s` is one read's, which is not the plan's
                # business once the step is waiting for a claim. Honouring one
                # and ignoring the other is how a plan comes to say a number
                # nothing reads.
                return preflight_error(
                    location,
                    step,
                    "wait_timeout_s",
                    "A `can_read` with a `comparator:` waits out its own `timeout_s`, reading again until the claim is met, so `wait_timeout_s` (how long one read waits) has nothing left to mean. Name one of the two.",
                    {"error_type": "invalid_argument"},
                )
        return super().preflight(reactor, location, step, state)

    @classmethod
    def permission_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep, entry: Any) -> JsonObject | None:
        readable = reactor.config.can_read_allowed(entry)
        if step.action == cls.open_action:
            if not readable and not entry.permissions.allow_write:
                return preflight_error(location, step, "action", "Reading and writing this CAN bus are disabled by the authoritative config.")
            if step.arguments.get("clear_rx_queue", True) and not readable:
                return preflight_error(location, step, "clear_rx_queue", "Clearing this CAN bus receive queue requires permissions.allow_read on the bus.", {"permission": "allow_read"})
            return None
        if step.action == "can_read" and not readable:
            return preflight_error(location, step, "action", "Reading this CAN bus is disabled by the authoritative config.", {"permission": "allow_read"})
        if step.action != "can_send":
            return None
        if not entry.permissions.allow_write:
            return preflight_error(location, step, "action", "Writing to this CAN bus is disabled by the authoritative config.", {"permission": "allow_write"})
        if entry.listen_only:
            # `listen_only: true` is not an obstacle standing in the way of the
            # send: it is the claim that observing this bus sends nothing, which
            # is the bus this plan itself declared. A controller held to it emits
            # no dominant bit, so the send could never leave it, and the adapter
            # settles the mode when the session opens, long before the send step
            # would be reached. Refusing the plan says which of its two halves to
            # change; refusing at the bench would only say the frame did not go.
            #
            # Named `can_listen_only_mode`, the same as the tool's own gate. The
            # plan route reaches it earlier (at preflight, before a session is
            # opened), but it is one refusal with one catalogue entry, so a
            # reader who has met it once has met it everywhere. Note the ordering
            # above: `allow_write` is checked first here and the mode first in
            # the tool. A plan that names a bus it cannot write is a plan to
            # correct whichever way round, and preflight answers about the plan;
            # the tool answers about the bench, where the mode is the fact that
            # cannot be granted away.
            return preflight_error(
                location,
                step,
                "action",
                "This CAN bus is configured `listen_only: true` (the claim that observing it sends nothing), so a plan cannot also send on it. Send on a second `can_buses` entry configured `listen_only: false`, drop the send step, or set `listen_only: false` on this entry if the bus may be transmitted on.",
                {"error_type": LISTEN_ONLY_MODE_ERROR, "listen_only": True, "config_field": f"can_buses.{cls.step_config_id(reactor.config, step)}.listen_only"},
            )
        return None

    # --- reading the bus -------------------------------------------------

    def _compare(self, comparator: CanFrameComparator, max_frames: object | None, timeout_s: float) -> JsonObject:
        """Read until a frame meets the comparator, or fail saying which frames
        the bus did carry, and, for a range, which value was captured against
        which bounds."""
        met, unmet = CAN_COMPARATOR_SUMMARIES[comparator.claim]
        outcome = self._read_until(comparator, max_frames, timeout_s)
        if outcome.read_failure is not None:
            return outcome.read_failure
        common = {"bus_id": self.id, **comparator.report(), "timeout_s": timeout_s, "frames_read": outcome.frames_read, "reads": outcome.reads}
        if outcome.frame is not None:
            return {"ok": True, "tool": "test_reactor", "summary": met, **common, "frame": outcome.frame}
        return {"ok": False, "tool": "test_reactor", "error_type": "comparator_unmet", "summary": unmet, **common, **outcome.tail_result}

    def _read_until(self, comparator: CanFrameComparator, max_frames: object | None, timeout_s: float) -> CanReadOutcome:
        """Read this bus until a frame meets the claim or the deadline passes.

        The plan's own read path and nothing else: each pass is one `can_read`
        (the tool an agent would call by hand), waiting out whatever is left of the
        step's deadline, and every frame it yields is judged as it arrives. A
        frame is atomic, so nothing is carried across passes the way a serial
        line's bytes are; what the passes are for is that the frame this plan
        wants may simply not be among the ones already queued.

        Frames the identifier filter rejects are ignored for the claim and still
        recorded, because a plan waiting on the wrong identifier is the failure a
        red step must be able to show rather than merely assert."""
        deadline = time.monotonic() + timeout_s
        seen: list[JsonObject] = []
        frames_read = 0
        reads = 0
        while True:
            arguments: JsonObject = {"bus_id": self.id, "wait_timeout_s": max(0.0, deadline - time.monotonic())}
            if max_frames is not None:
                arguments["max_frames"] = int(max_frames)  # type: ignore[arg-type]
            result = self.service.call("can_read", arguments)
            reads += 1
            if result_failed(result):
                return CanReadOutcome(None, reads, frames_read, seen, result)
            frames = result.get("frames") or []
            frames_read += len(frames)
            for frame in frames:
                # Keep `extended` beside id/payload: the comparator treats a
                # standard 0x123 and its extended twin as distinct frames, so a
                # tail entry that dropped the frame type would show a rejected
                # extended frame as identical to the requested standard one and
                # omit the only field that explains the mismatch.
                seen.append({"id_hex": frame.get("id_hex"), "data_hex": frame.get("data_hex"), "extended": bool(frame.get("extended", False))})
                del seen[:-CAN_COMPARATOR_TAIL_FRAMES]
                if comparator.matches_frame(frame):
                    return CanReadOutcome(frame, reads, frames_read, seen)
            if time.monotonic() >= deadline:
                return CanReadOutcome(None, reads, frames_read, seen)
            if not frames:
                # A read that returned nothing while time was left had its own
                # wait cut short, so pace the next pass rather than spinning on a
                # bus that is quiet.
                time.sleep(min(CAN_COMPARATOR_IDLE_POLL_S, max(0.0, deadline - time.monotonic())))


class SymbolComparator:
    """What a `read_symbol` step claims about the number a symbol holds.

    Deliberately not a `StepComparator`. That class is a claim about text judged
    again and again against a rolling window as more of it arrives, and neither
    half of that exists here: a symbol is bytes, not characters, and a halted
    target's memory does not change under the step, so there is one read, one
    look and one verdict. Sharing the base class would have meant inheriting a
    deadline, a window and a capture group that mean nothing about a word of
    memory.

    Three claims over the integer `debug_symbol_value` already decodes the bytes
    into: `equals` is one value, `range` is inclusive bounds, and `mask` beside
    `equals` is a claim about bits, `(value & mask) == equals`, which is how a
    plan asserts one flag of a status word without stating the rest of it.

    `signed` picks which of the two readings the claim is about, because the tool
    returns both: the same bytes are honestly both, and above the top bit the two
    disagree. A mask has no use for it and is refused beside it by the schema,
    since a mask selects bits while a sign is a reading of the whole width."""

    def __init__(self, raw: JsonObject):
        self.raw = dict(raw)
        self.equals: int | None = None if raw.get("equals") is None else int(raw["equals"])
        bounds = raw.get("range")
        self.bounds: tuple[int, int] | None = None if bounds is None else (int(bounds["min"]), int(bounds["max"]))
        self.mask: int | None = None if raw.get("mask") is None else int(raw["mask"])
        self.signed = bool(raw.get("signed", False))
        # The value this claim was judged on, kept so an unmet claim can say what
        # the symbol did hold. Both are filled by `matches`, and `masked_value`
        # stays None for a claim that masks nothing.
        self.captured_value: int | None = None
        self.masked_value: int | None = None

    @property
    def claim(self) -> str:
        if self.mask is not None:
            return "mask"
        return "equals" if self.equals is not None else "range"

    @property
    def reading(self) -> str:
        """Which of the tool's two integer readings this claim is made against.
        Reported beside the verdict, so nobody has to infer from `signed` which
        of two numbers a step was really talking about."""
        return "value_signed" if self.signed else "value_unsigned"

    def matches(self, value: int) -> bool:
        self.captured_value = value
        self.masked_value = None if self.mask is None else value & self.mask
        judged = value if self.masked_value is None else self.masked_value
        if self.bounds is not None:
            low, high = self.bounds
            return low <= judged <= high
        return judged == self.equals

    def report(self) -> JsonObject:
        """What the result says about this comparator, met or unmet."""
        report: JsonObject = {"comparator": self.raw, "reading": self.reading, "captured_value": self.captured_value}
        if self.masked_value is not None:
            report["masked_value"] = self.masked_value
        return report


class DebuggerRunner(StepDevice):
    """Executes one test plan's debugger steps against a single named probe and
    owns the debug session it opened, so cleanup can close exactly what this
    plan started on that board."""

    kind: ClassVar[str] = "debugger"
    device_class: ClassVar[type[Device]] = DebuggerDevice
    route_field: ClassVar[str] = "debugger"
    configured_field: ClassVar[str] = "configured_debuggers"
    unknown_name_summary: ClassVar[str] = "Test step references a debugger that is not in the authoritative config."
    # A debug session closes before any serial line or CAN bus this plan opened.
    cleanup_order: ClassVar[int] = 0
    # This kind's own typed-debug actions: the session's lifecycle, the two reads
    # and the breakpoint run. Internal to the class that serves them, not a second
    # answer to "which device kind runs this action". Which of them a given plan
    # step actually needs a live session for is not decided here, because it is
    # not the same on every bench: see `_runs_without_a_session`.
    debug_session_actions: ClassVar[frozenset[str]] = frozenset({"debug_start", "run_until_breakpoint", "dump_memory", "read_symbol", "debug_stop"})

    def __init__(self, config_id: str, device: Device, service: AgenticHILToolService, **kwargs: Any):
        super().__init__(config_id, device, service, **kwargs)
        self._owns_debug_session = False

    @property
    def debugger(self) -> DebuggerConfig:
        return self.device.debugger  # type: ignore[attr-defined]

    @classmethod
    def config_entries(cls, config: AgenticHILConfig) -> Mapping[str, Any]:
        return config.debuggers

    @classmethod
    def build_device(cls, config: AgenticHILConfig, config_id: str) -> Device:
        return debugger_device(config, config_id)

    # --- the actions this kind serves ------------------------------------

    @step_action("flash", schema="flash", tool="flash_firmware")
    def _flash(self, step: TestStep) -> JsonObject:
        return self.call_tool(step)

    # `reset_target` reads its own default the same way, but the reactor writes
    # every argument it sends: the schema documents `run`, and a step that leaves
    # the mode out must reach the tool saying which reset it asked for.
    @step_action("reset", schema="reset", tool="reset_target", defaults={"mode": "run"})
    def _reset(self, step: TestStep) -> JsonObject:
        return self.call_tool(step)

    @step_action("dump_memory", schema="dumpMemory", tool="debug_dump_symbol_ihex")
    def _dump_memory(self, step: TestStep) -> JsonObject:
        return self.call_tool(step)

    @step_action("read_symbol", schema="readSymbol", tool="debug_symbol_value", forwards_arguments=False)
    def _read_symbol(self, step: TestStep) -> JsonObject:
        """Read one allowed symbol, and judge the number it holds if the plan
        said what it wants.

        The read is `debug_symbol_value` and nothing more: the same allowlist,
        the same `debug.max_dump_size_bytes` ceiling on the resolved size, the
        same resolution (the image's debug information first, the ELF's own
        symbol table where there is no type to work with), through whichever of
        the two this bench has: the debug session the plan opened, or the
        standalone read a backend that needs none serves. A plan therefore reads
        exactly what an agent calling the tool by hand reads, and a refusal the
        tool gives is the step's result unchanged rather than a sentence written
        here about somebody else's rule.

        The step's own arguments are not the tool's, which is why it does not
        forward them: `size_bytes` is a claim about the symbol and `comparator:`
        is a claim about its value, and neither is something `debug_symbol_value`
        has ever been asked. Without either, this is a plain read whose answer is
        the tool's answer, which is how a plan records a value it does not want
        to assert."""
        result = self.service.call("debug_symbol_value", {"symbol": str(step.arguments["symbol"])})
        if result_failed(result):
            return result
        return self._judge_symbol(step, result)

    def _judge_symbol(self, step: TestStep, result: JsonObject) -> JsonObject:
        """The read, held to whatever the step claimed about it.

        Both claims fail as steps rather than as refusals of the plan, because
        both are statements about the board that only the board can answer: a
        width the plan declared and the target does not have is a firmware whose
        type moved, and a comparator is the assertion the step exists to make."""
        read: JsonObject = {key: result[key] for key in SYMBOL_VALUE_FIELDS if key in result}
        declared = step.arguments.get("size_bytes")
        size_bytes = result.get("size_bytes")
        if declared is not None and int(declared) != int(size_bytes or 0):
            return merge_result_status({
                "ok": False,
                "tool": "test_reactor",
                "error_type": "symbol_size_mismatch",
                "summary": "The symbol is not the width this step declared.",
                "debugger": self.id,
                "expected_size_bytes": int(declared),
                **read,
            }, result)
        comparator = step.arguments.get("comparator")
        if not comparator:
            return result
        claim = SymbolComparator(comparator)
        if claim.reading not in result:
            # The width has no integer reading, and the plan did not declare a
            # width for preflight to have refused. Its own error rather than an
            # unmet claim: nothing was judged and nothing was wrong with the
            # value, so calling it a failed assertion would send an operator
            # looking at firmware that is behaving.
            return merge_result_status({
                "ok": False,
                "tool": "test_reactor",
                "error_type": "symbol_width_not_numeric",
                "summary": "The symbol's width carries no integer reading, so this step's comparator could not be judged.",
                "debugger": self.id,
                "comparator": claim.raw,
                "integer_widths": sorted(INTEGER_VALUE_WIDTHS),
                **read,
                **({"value_not_decoded": result["value_not_decoded"]} if "value_not_decoded" in result else {}),
            }, result)
        met, unmet = SYMBOL_COMPARATOR_SUMMARIES[claim.claim]
        matched = claim.matches(int(result[claim.reading]))
        common = {"debugger": self.id, **read, **claim.report()}
        if matched:
            return merge_result_status({"ok": True, "tool": "test_reactor", "summary": met, **common}, result)
        return merge_result_status({"ok": False, "tool": "test_reactor", "error_type": "comparator_unmet", "summary": unmet, **common}, result)

    @step_action("debug_start", schema="debugStart", tool="debug_start_session")
    def _debug_start(self, step: TestStep) -> JsonObject:
        self._owns_debug_session = True
        result = self.call_tool(step)
        if result.get("ok") is True:
            if result.get("target_ok") is False:
                result = {
                    **result,
                    "ok": False,
                    "error_type": result.get("target_error_type", "target_stop"),
                    "summary": "Debug session started, but the target is stopped abnormally.",
                }
        else:
            self._owns_debug_session = result.get("cleanup_required") is True
        return result

    @step_action("debug_stop", schema="debugStop", tool="debug_stop_session")
    def _debug_stop(self, step: TestStep) -> JsonObject:
        result = self.call_tool(step)
        if result.get("ok") is True:
            self._owns_debug_session = False
        return result

    # Two tool calls and a breakpoint to clean up either way, so this one both
    # declares no single tool and answers for its own preflight contract check.
    @step_action("run_until_breakpoint", schema="runUntilBreakpoint")
    def _run_until_breakpoint_step(self, step: TestStep) -> JsonObject:
        return self._run_until_breakpoint(step.arguments)

    @classmethod
    def step_config_id(cls, config: AgenticHILConfig, step: TestStep) -> str | None:
        return step_debugger_id(config, step)

    @classmethod
    def unnamed_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep) -> JsonObject | None:
        """Resolve which probe a debugger step runs on, or refuse the plan.

        A plan that omits the name while several probes are configured is not
        defaulted to one: picking a board for the author is how the wrong board
        gets flashed."""
        if not reactor.config.debuggers:
            return preflight_error(location, step, "debugger", "The authoritative config declares no debuggers, so this step cannot run.")
        return preflight_error(location, step, "debugger", "Name the debugger this step runs on; the authoritative config declares several.", {"configured_debuggers": sorted(reactor.config.debuggers)})

    @classmethod
    def name_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep) -> JsonObject | None:
        refusal = super().name_refusal(reactor, location, step)
        if refusal is not None:
            return refusal
        if cls.step_config_id(reactor.config, step) != reactor.config.debugger_id and reactor._service_factory is None:
            return preflight_error(location, step, "debugger", "Running a step on another debugger needs a service factory to build that probe's service; the reactor would otherwise execute it on the bound probe.", {"bound_debugger": reactor.config.debugger_id})
        return None

    @classmethod
    def service_for(cls, reactor: TestReactor, config_id: str) -> AgenticHILToolService:
        """A probe other than the base service's bound one needs its own service:
        without a factory the step would silently execute on whatever probe the
        base service holds, which is the wrong board."""
        if config_id == reactor.config.debugger_id or reactor._service_factory is None:
            return reactor.service
        # bind_debugger() already applies this probe's target override.
        service = reactor._service_factory(bind_debugger(reactor.config, config_id))
        reactor._owned_services.append(service)
        return service

    @classmethod
    def tool_calls(cls, config: AgenticHILConfig, step: TestStep) -> list[tuple[str, JsonObject]]:
        if step.action != "run_until_breakpoint":
            return super().tool_calls(config, step)
        calls: list[tuple[str, JsonObject]] = [("debug_set_breakpoint", {"location": step.arguments.get("location")})]
        if step.arguments.get("timeout_s") is not None:
            calls.append(("debug_continue", {"timeout_s": step.arguments.get("timeout_s")}))
        return calls

    @classmethod
    def preflight(cls, reactor: TestReactor, location: StepLocation, step: TestStep, state: PlanState) -> JsonObject | None:
        config = reactor.config
        debugger_id = str(cls.step_config_id(config, step))
        debugger = config.debuggers[debugger_id]
        permissions = debugger.permissions

        if step.action == "flash":
            if state.debug_session is not None:
                return preflight_error(location, step, "action", "Firmware cannot be flashed while a debug session is active.", {"debug_session_debugger": state.debug_session})
            if not permissions.allow_flash:
                return preflight_error(location, step, "action", "Flashing is disabled for this debugger by the authoritative config.")
            if step.arguments.get("reset_after_flash", False) and not permissions.allow_reset:
                return preflight_error(location, step, "reset_after_flash", "Post-flash reset is disabled for this debugger by the authoritative config.")
            if permissions.allow_raw_debugger_commands:
                return preflight_error(location, step, "action", "Flashing is disabled while this debugger allows raw debugger commands.", {"permission": "allow_raw_debugger_commands"})
            if permissions.allow_mass_erase:
                return preflight_error(location, step, "action", "Flashing is disabled while this debugger allows mass erase.", {"permission": "allow_mass_erase"})
            return cls._artifact_refusal(reactor, location, step, require_elf=False)

        if step.action == "reset":
            # The two answers flash already gives, for the same reasons.
            # `reset_target` is a one-shot that cannot take the lease a live
            # debug session holds, so a reset inside one could only ever come
            # back busy; and the permission that governs it is the one the
            # config names for this probe. Which resets a backend can actually
            # perform is deliberately not asked here: a mode it will not run
            # (`init` off OpenOCD) is refused by that backend with its own
            # `not_supported`, and that refusal is the honest answer.
            if state.debug_session is not None:
                return preflight_error(location, step, "action", "The target cannot be reset while a debug session is active.", {"debug_session_debugger": state.debug_session})
            if not permissions.allow_reset:
                return preflight_error(location, step, "action", "Target reset is disabled for this debugger by the authoritative config.")
            return None

        # Whether this step is one the probe it names answers with no debug
        # session behind it. Asked before the gate below, because it is what
        # decides whether there is anything for the gate to be about.
        without_session = cls._runs_without_a_session(config, step, state, debugger_id)
        if step.action in cls.debug_session_actions and not without_session and debugger.type != "openocd":
            return cls._backend_debug_refusal(location, step, debugger, debugger_id, config)
        if step.action == "debug_start":
            if state.debug_session is not None:
                return preflight_error(location, step, "action", "A debug session is already active in this test plan.", {"debug_session_debugger": state.debug_session})
            mode = str(step.arguments.get("mode", "attach"))
            # Preflight is the only diagnosis the operator gets: it runs
            # before any tool call, so the backend's per-flag messages are
            # never reached. Name the flag that actually fired rather than
            # pointing at a permission the config plainly shows as false.
            if not config.probe_allowed(debugger):
                return preflight_error(location, step, "action", "Debug sessions require allow_probe on this debugger.", {"permission": "allow_probe"})
            if permissions.allow_raw_debugger_commands:
                return preflight_error(location, step, "action", "Debug sessions are disabled while this debugger allows raw debugger commands.", {"permission": "allow_raw_debugger_commands"})
            if mode != "attach" and not permissions.allow_reset:
                return preflight_error(location, step, "mode", f"Debug mode '{mode}' requires allow_reset on this debugger.", {"permission": "allow_reset"})
            if mode == "load" and not permissions.allow_flash:
                return preflight_error(location, step, "mode", "Debug load mode requires allow_flash on this debugger.", {"permission": "allow_flash"})
            if mode == "load" and permissions.allow_mass_erase:
                return preflight_error(location, step, "mode", "Debug load mode is disabled while this debugger allows mass erase.", {"permission": "allow_mass_erase"})
            artifact_error = cls._artifact_refusal(reactor, location, step, require_elf=True)
            if artifact_error is not None:
                return artifact_error
            state.debug_session = debugger_id
            return None
        if step.action == "debug_stop":
            if state.debug_session is None:
                return preflight_error(location, step, "action", "A debug session must be started before it can be stopped.")
            if state.debug_session != debugger_id:
                return preflight_error(location, step, "debugger", "A debug session may only be stopped on the debugger that started it.", {"debug_session_debugger": state.debug_session})
            state.debug_session = None
            return None
        if without_session and not config.probe_allowed(debugger):
            # A sessionless read still opens a probe onto a live core, so it
            # needs the same allow_probe a debug_start does, and preflight is
            # where that has to be caught. The backend refuses an ungranted read
            # on its own, but only when the read's own turn comes: a plan that
            # flashes and then reads would already have flashed before the read's
            # permission_denied arrived, mutating a board a plan that could never
            # run should never have reached. probe_allowed(), not the raw flag,
            # so a read-free (version 2) bench that grants reads by exclusivity
            # still admits the step.
            return preflight_error(location, step, "action", "Reading target memory requires allow_probe on this debugger.", {"permission": "allow_probe"})
        if not without_session:
            if state.debug_session is None:
                return preflight_error(location, step, "action", "A debug session must be started before this action.")
            if state.debug_session != debugger_id:
                return preflight_error(location, step, "debugger", "This action must run on the debugger that started the debug session.", {"debug_session_debugger": state.debug_session})
        if step.action == "run_until_breakpoint" and not permissions.allow_debug_execution:
            # `_run_until_breakpoint` always calls `debug_continue` once its
            # breakpoint is set, whatever this step's own `timeout_s` says, so
            # this is checked unconditionally rather than mirrored off that
            # argument. Named here for the same reason every other debug_start
            # flag is: the backend's own refusal is never reached because this
            # runs first, so pointing at a permission the config plainly shows
            # as false is the only diagnosis the operator gets.
            return preflight_error(location, step, "action", "Resuming target execution requires allow_debug_execution on this debugger.", {"permission": "allow_debug_execution"})
        symbol = breakpoint_symbol(step.arguments.get("location")) if step.action == "run_until_breakpoint" else step.arguments.get("symbol")
        if symbol is None and not config.debug.allow_all_symbols:
            return preflight_error(location, step, "location", "File/line breakpoints require debug.allow_all_symbols.")
        if symbol is not None and not symbol_allowed(config, str(symbol)):
            field_name = "location" if step.action == "run_until_breakpoint" else "symbol"
            return preflight_error(location, step, field_name, "Symbol is not allowed by the authoritative debug config.", {"symbol": symbol})
        if step.action == "read_symbol":
            return cls._declared_width_refusal(config, location, step)
        if step.action == "dump_memory" and hasattr(reactor.service, "artifacts"):
            output = reactor.service.artifacts.validate_output_path(str(step.arguments["output_path"]), "debug_dump_symbol_ihex")
            if output.get("ok") is not True:
                return preflight_error(
                    location,
                    step,
                    "output_path",
                    str(output.get("summary", "Memory dump output path is invalid.")),
                    {"validation": output},
                )
        return None

    @classmethod
    def _runs_without_a_session(cls, config: AgenticHILConfig, step: TestStep, state: PlanState, debugger_id: str) -> bool:
        """Whether this step is one the probe it names answers with no debug
        session open, so the plan needs no `debug_start` before it.

        The question goes to the backend, never to a list of backend types kept
        here: `configured_sessionless_debug_reads` is the one place that answers
        it, and it answers from what the backend implements. A backend that gains
        the ability later therefore starts admitting these plans with nothing in
        the reactor to change, which is the whole reason the answer is not a
        second copy of it.

        What is asked about is the step's own declared tool, not a second list of
        actions: an action is servable without a session exactly when the tool it
        calls is one the backend serves that way. So `run_until_breakpoint`, which
        declares no single tool, is never one of them, and neither is a session's
        own lifecycle.

        A session this plan opened on this same probe wins over all of it. Inside
        one, every typed debug step runs exactly as it always has, on that
        session's lease, whatever else the backend can also do standalone."""
        if step.action not in cls.debug_session_actions or state.debug_session == debugger_id:
            return False
        tool = cls.step_action_specs[step.action].tool
        return tool is not None and tool in configured_sessionless_debug_reads(config, debugger_id)

    @classmethod
    def _backend_debug_refusal(cls, location: StepLocation, step: TestStep, debugger: DebuggerConfig, debugger_id: str, config: AgenticHILConfig) -> JsonObject:
        """Refuse a typed debug step the probe it names cannot serve either way,
        and say what would make it run.

        Named rather than generic, because the two ways a step gets here are two
        different facts about the bench and lead to two different next moves. A
        memory read is refused only where the backend serves it neither standalone
        nor through a session it could open, which is a capability this
        configuration does not have; the session lifecycle and a breakpoint are
        refused wherever the backend opens no session at all. Both name the
        backend, the step and the step number, and both end at the configuration
        change that would work, taken from the same table the tools' own
        `not_supported` refusals quote so a plan author and a tool caller are
        never sent two different ways out."""
        way_out = DEBUG_SESSION_WAY_OUT.get(debugger.type)
        reach = f" To run it on this bench, {way_out}." if way_out else ""
        served = sorted(configured_sessionless_debug_reads(config, debugger_id))
        if cls.step_action_specs[step.action].tool in sessionless_capable_debug_tools():
            summary = (
                f"Step {location.step}'s '{step.action}' reads target memory, and the '{debugger.type}' backend on "
                f"debugger '{debugger_id}' serves that read neither without a debug session nor inside one."
            )
        else:
            summary = (
                f"Step {location.step}'s '{step.action}' needs a typed debug session, and debugger '{debugger_id}' "
                f"runs the '{debugger.type}' backend, which opens none."
            )
        return preflight_error(
            location,
            step,
            "action",
            f"{summary}{reach}",
            {
                "debugger": debugger_id,
                "debugger_type": debugger.type,
                "sessionless_debug_reads": served,
                **({"way_out": way_out} if way_out else {}),
            },
        )

    @classmethod
    def _declared_width_refusal(cls, config: AgenticHILConfig, location: StepLocation, step: TestStep) -> JsonObject | None:
        """Refuse a `read_symbol` whose width this bench or this format cannot
        serve, before the run.

        Only a width the plan states can be answered here. Preflight builds
        nothing and reaches no board, so the size of a symbol that only the
        target's own image describes is not knowable at plan time and is judged
        at runtime instead, with a stated reason; a plan that does declare one
        has said something checkable, and both gates it has to pass are checked
        against it rather than against a board an hour into a run.

        The two are the bench's and the format's. `debug.max_dump_size_bytes` is
        the operator's ceiling on what may be read at all, applied here to the
        same number the backend will apply it to; and the decoded integer a
        comparator judges exists only at 1, 2, 4 and 8 bytes, so a numeric claim
        on a 3-byte or 408-byte object is a claim the format cannot make. A width
        without a comparator is untouched by the second: reading a structure and
        reporting its bytes is exactly what a plain `read_symbol` is for."""
        declared = step.arguments.get("size_bytes")
        if declared is None:
            return None
        size_bytes = int(declared)
        if size_bytes > config.debug.max_dump_size_bytes:
            return preflight_error(
                location,
                step,
                "size_bytes",
                "Symbol read exceeds debug.max_dump_size_bytes.",
                {"size_bytes": size_bytes, "max_dump_size_bytes": config.debug.max_dump_size_bytes},
            )
        if step.arguments.get("comparator") and size_bytes not in INTEGER_VALUE_WIDTHS:
            return preflight_error(
                location,
                step,
                "size_bytes",
                "A comparator judges the symbol's decoded integer, which exists only at 1, 2, 4 and 8 bytes.",
                {"size_bytes": size_bytes, "integer_widths": sorted(INTEGER_VALUE_WIDTHS)},
            )
        return None

    @classmethod
    def _artifact_refusal(cls, reactor: TestReactor, location: StepLocation, step: TestStep, require_elf: bool) -> JsonObject | None:
        if not hasattr(reactor.service, "artifacts"):
            return None
        image_path = str(step.arguments["image_path"])
        validation = reactor.service.artifacts.validate_local_path(image_path)
        if validation.get("ok") is not True:
            return preflight_error(
                location,
                step,
                "image_path",
                str(validation.get("summary", "Firmware artifact is invalid.")),
                {"validation": validation},
            )
        if require_elf and Path(image_path).suffix.lower() != ".elf":
            return preflight_error(location, step, "image_path", "Debug sessions require an ELF artifact with debug symbols.")
        return None

    def cleanup(self) -> list[JsonObject]:
        if not self._owns_debug_session:
            return []
        result = self.cleanup_call("debug_stop_session")
        if result.get("ok") is True:
            self._owns_debug_session = False
        return [self.cleanup_record("debug_stop", result)]

    def _run_until_breakpoint(self, arguments: JsonObject) -> JsonObject:
        breakpoint_result = self.service.call("debug_set_breakpoint", {"location": arguments["location"]})
        if result_failed(breakpoint_result):
            if breakpoint_result.get("side_effect_committed") is True or breakpoint_result.get("breakpoint"):
                cleared = self.service.call("debug_clear_breakpoints")
                return merge_result_status({**breakpoint_result, "breakpoint_cleanup": cleared}, breakpoint_result, cleared)
            return breakpoint_result
        continued = self.service.call("debug_continue", {} if arguments.get("timeout_s") is None else {"timeout_s": arguments["timeout_s"]})
        cleared = self.service.call("debug_clear_breakpoints")
        if result_failed(cleared):
            if result_failed(continued):
                return merge_result_status({**continued, "breakpoint_cleanup": cleared}, breakpoint_result, continued, cleared)
            return merge_result_status({
                "ok": False,
                "tool": "test_reactor",
                "error_type": result_error_type(cleared) if cleared.get("audit_ok") is False or cleared.get("target_ok") is False else "breakpoint_cleanup_failed",
                "summary": "Target stopped, but the reactor breakpoint could not be removed.",
                "debugger": self.id,
                "breakpoint": breakpoint_result.get("breakpoint"),
                "breakpoint_cleanup": cleared,
            }, breakpoint_result, continued, cleared)
        if result_failed(continued):
            return merge_result_status(continued, breakpoint_result, continued, cleared)
        expected_id = breakpoint_result.get("breakpoint", {}).get("id")
        actual_id = continued.get("stop", {}).get("breakpoint_id")
        if continued.get("stop_reason") != "breakpoint_hit" or actual_id != expected_id:
            return merge_result_status({
                "ok": False,
                "tool": "test_reactor",
                "error_type": "unexpected_stop",
                "summary": "Target did not stop at the breakpoint created by this test step.",
                "debugger": self.id,
                "expected_breakpoint_id": expected_id,
                "stop": continued.get("stop"),
            }, breakpoint_result, continued, cleared)
        return merge_result_status({
            "ok": True,
            "tool": "test_reactor",
            "summary": "Target stopped at the expected breakpoint.",
            "breakpoint": breakpoint_result["breakpoint"],
            "stop_reason": continued["stop_reason"],
            "stop": continued["stop"],
        }, breakpoint_result, continued, cleared)


# Every device kind a test plan can drive, and everything derived from them. A
# kind appears here once; the action table, the routing keys and the schema
# mapping all follow from what each class says about itself, so none of them can
# disagree with the class that actually serves the action.
STEP_DEVICE_CLASSES: tuple[type[StepDevice], ...] = (DebuggerRunner, UartRunner, CanRunner)
# The actions the reactor runs itself, as action name -> schema `$defs` entry.
# Same projection every device kind contributes, so the plan format's action
# vocabulary stays one table however a given action happens to be served.
REACTOR_ACTION_SCHEMAS: dict[str, str] = {REPEAT_ACTION: "repeat"}
ACTION_SCHEMAS = {
    **{action: schema for device_class in STEP_DEVICE_CLASSES for action, schema in device_class.step_actions.items()},
    **REACTOR_ACTION_SCHEMAS,
}
# `device` is format v3's universal routing key and is read for every kind; the
# rest are the v2 spellings, which stay valid as aliases.
ROUTE_FIELDS: tuple[str, ...] = ("device", *dict.fromkeys(device_class.route_field for device_class in STEP_DEVICE_CLASSES))
# An action can be served by more than one kind (`delay` is declared once on the
# base and therefore inherited by all of them), so this maps to every claimant
# and the name the step gave settles which one runs it.
STEP_DEVICE_CLASSES_BY_ACTION: dict[str, tuple[type[StepDevice], ...]] = {
    action: tuple(device_class for device_class in STEP_DEVICE_CLASSES if action in device_class.step_actions)
    for action in ACTION_SCHEMAS
    if action not in REACTOR_ACTION_SCHEMAS
}


def step_device_classes(action: str) -> tuple[type[StepDevice], ...]:
    """Every device kind that serves an action; empty for one no kind claims."""
    return STEP_DEVICE_CLASSES_BY_ACTION.get(action, ())


def step_results(records: Sequence[JsonObject]) -> list[JsonObject]:
    """Every result a run's step records hold, an iteration's included.

    A block step's record carries its iterations rather than one result of its
    own doing, so a walk that read only the top level would lose every audit
    failure and every target failure a loop's steps reported, which are exactly
    the things the run-level status is propagated from."""
    collected: list[JsonObject] = []
    for record in records:
        result = record.get("result")
        if isinstance(result, dict):
            collected.append(result)
        for iteration in record.get("iterations") or ():
            collected.extend(step_results(iteration.get("steps") or ()))
    return collected


def flatten_steps(steps: Sequence[TestStep]) -> Iterator[TestStep]:
    """Every step a plan contains, a block step's own subset included.

    Depth-first and in written order, so a caller asking what a plan touches
    gets the same answer whether the plan nests or not. The block step itself is
    yielded too: it is a step of the plan, and leaving it out would make a
    walker that counts steps disagree with the report that records them."""
    for step in steps:
        yield step
        yield from flatten_steps(step.steps)


def step_device_class(config: AgenticHILConfig, step: TestStep) -> type[StepDevice] | None:
    """Which device kind runs this step, or None when the step does not say.

    One claimant is the ordinary case and the action alone answers it. For an
    action every kind serves, the routing key is what decides: a v2 alias names
    its kind outright, and a `device:` is looked up in the configured entries,
    which is where "the configuration knows which class a name belongs to" is
    actually cashed. A name in no section, or in two of them, is not answered
    here; preflight refuses it saying which."""
    candidates = step_device_classes(step.action)
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    pinned = [device_class for device_class in candidates if getattr(step, device_class.route_field) is not None]
    if pinned:
        return pinned[0]
    named = [device_class for device_class in candidates if step.device is not None and step.device in device_class.config_entries(config)]
    return named[0] if len(named) == 1 else None


class TestReactor:
    __test__ = False

    def __init__(
        self,
        config: AgenticHILConfig,
        service: AgenticHILToolService,
        service_factory: Callable[[AgenticHILConfig], AgenticHILToolService] | None = None,
        *,
        stop_requested: Callable[[], bool] = never_stopped,
        on_progress: Callable[[JsonObject], None] | None = None,
    ):
        self.config = config
        self.service = service
        self._service_factory = service_factory
        # Asked between every step and inside every wait. A run nobody can
        # address answers no forever, which is what a reactor built before
        # stopping existed did.
        self.stop_requested = stop_requested
        self._on_progress = on_progress
        # Where the run is, as the thing watching it needs to read it: the
        # top-level step, and, inside a block, which iteration and which step of
        # its subset. Rebuilt at the top level rather than added to, so a block's
        # iteration cannot outlive the block.
        self._progress: JsonObject = {}
        # Services built for a probe other than the base service's bound one are
        # owned here and closed by close(); the base service is the caller's.
        self._owned_services: list[AgenticHILToolService] = []
        # (kind, config entry) -> the device driving it, built on first use. One
        # object per configured unit for the whole run, so whatever a step leaves
        # open is still held by something cleanup can ask.
        self.devices: dict[tuple[str, str], StepDevice] = {}
        self.cleanup_interrupts: list[BaseException] = []

    def device_for(self, device_class: type[StepDevice], config_id: str) -> StepDevice:
        """This run's device for one named config entry, built on first use."""
        key = (device_class.kind, config_id)
        existing = self.devices.get(key)
        if existing is not None:
            return existing
        built = device_class(
            config_id,
            device_class.build_device(self.config, config_id),
            device_class.service_for(self, config_id),
            stop_requested=self.stop_requested,
        )
        self.devices[key] = built
        return built

    def runner(self, debugger_id: str) -> DebuggerRunner:
        """The runner for one named probe, built on first use."""
        runner = self.device_for(DebuggerRunner, debugger_id)
        assert isinstance(runner, DebuggerRunner)
        return runner

    def step_device(self, step: TestStep) -> StepDevice:
        """The device a step drives. DeviceError when the plan named something
        the config does not declare. Preflight refuses that before any step
        runs, so reaching it here means the plan was never validated."""
        device_class = step_device_class(self.config, step)
        name = None if device_class is None else device_class.step_config_id(self.config, step)
        if device_class is None or name is None:
            raise DeviceError(
                {
                    "ok": False,
                    "error_type": "unknown_action" if device_class is None else "invalid_argument",
                    "summary": "No configured device kind serves this test reactor action." if device_class is None else "This test step names no configured device.",
                    "action": step.action,
                    "side_effect_committed": False,
                    "retry_safe": False,
                }
            )
        return self.device_for(device_class, name)

    def _close_owned_services(self) -> list[BaseException]:
        errors: list[BaseException] = []
        for built in self._owned_services:
            try:
                built.close()
            except BaseException as error:  # noqa: BLE001 - aggregate per-probe close errors
                errors.append(error)
        self._owned_services = []
        return errors

    def close(self) -> None:
        errors = self._close_owned_services()
        if not errors:
            return
        interrupts = [error for error in errors if isinstance(error, (KeyboardInterrupt, SystemExit))]
        if interrupts:
            raise interrupts[0]
        detail = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        raise RuntimeError(f"Per-debugger service cleanup failed: {detail}") from errors[0]

    def execute_step(self, step: TestStep) -> JsonObject:
        """Run one step on the device that serves its action.

        The device supplies its own config-entry name, so a step cannot address
        one entry and reach another, and the reactor does not need to know which
        kind of thing it just asked."""
        try:
            device = self.step_device(step)
        except DeviceError as error:
            return {"tool": "test_reactor", **error.result}
        return device.execute(step)

    def execute_steps(self, steps: Sequence[TestStep], records: list[JsonObject], *, top_level: bool = False) -> StepOutcome | None:
        """Run one list of steps in order, recording each, and stop at the first
        failure or at the first stop request.

        Returns why the list did not run to its end, positioned in *this* list,
        or None when every step passed. The position is what the caller needs
        and nothing more: the top-level call reads it as `failed_step` or as
        where a stop landed, and a block step reads it as which step of its
        subset it was.

        The stop is asked before a step rather than after, so a run that has
        been asked to end does not start work it will not be judged on. It is
        not asked after the last step, because a list that ran to its end is not
        a list that was stopped."""
        for index, step in enumerate(steps, start=1):
            if self.stop_requested():
                return StepOutcome(index - 1, None)
            self._advance(index, step, top_level=top_level)
            record: JsonObject = {"index": index, "route": step.route, "action": step.action}
            records.append(record)
            if step.action == REPEAT_ACTION:
                inner = self.execute_repeat(step, record)
                if inner is not None:
                    return StepOutcome(index, inner.failure)
                continue
            failure = self.execute_recorded_step(step, record)
            if failure is not None:
                return StepOutcome(index, failure)
            if record["result"].get("stop_requested") is True:
                # The step ended early because a stop was asked for. Read off
                # the step's own result rather than by asking again, so this is
                # true of the plan's last step too: a wait cut short is a run
                # that was stopped, and asking only before a step would have
                # called it a completed plan because there was no next step to
                # ask before.
                return StepOutcome(index, None)
        return None

    def _advance(self, index: int, step: TestStep, *, top_level: bool) -> None:
        """Say which step is about to run, for whoever is watching this run."""
        if top_level:
            self._progress = {"step": index, "action": step.action, "route": step.route}
        else:
            self._progress = {**self._progress, "nested_step": index, "nested_action": step.action, "nested_route": step.route}
        if self._on_progress is not None:
            self._on_progress(dict(self._progress))

    def execute_recorded_step(self, step: TestStep, record: JsonObject) -> JsonObject | None:
        """One ordinary step, executed onto its own record. The failing result,
        or None."""
        try:
            result: JsonObject = self.execute_step(step)
        except Exception as error:
            result = exception_result("test_reactor", "step_exception", "Test reactor step raised an exception.", error)
        record["result"] = result
        return result if result_failed(result) else None

    def execute_repeat(self, step: TestStep, record: JsonObject) -> StepOutcome | None:
        """Run a block step's subset until one of its bounds is reached.

        Implemented here rather than as a device action, because there is no
        device: what repeats is a piece of the sequence, which is the reactor's
        own subject. Nothing is opened, closed or reset between iterations, so a
        session the plan holds is still held on the next one and end-of-run
        cleanup closes exactly what it would have closed without the loop.

        Both bounds are asked at the bottom of the loop and nowhere else, which
        is what "between iterations" means structurally rather than by promise:
        a cycle is never cut in half, and a block always runs at least one whole
        iteration however small its `duration_s` is. `count` is asked first, so
        a loop whose count is exhausted says so even when the same iteration
        reached the time bound too; otherwise the first bound reached ends the
        loop, and reaching either is the green exit.

        A failed nested step ends the run exactly as a failed top-level step
        does: the failure is handed back up, the loop stops where it is, and the
        record says which iteration and which step of the subset it stopped at
        so the failure can be found without replaying the plan. A stop request
        travels the same way and is answered in the same places, which are the
        block's own step boundaries: a block is not a step that has to finish,
        it is a list of steps that do, so a stop ends it at the boundary it
        reached and the record says which iteration and which step that was."""
        count = None if step.arguments.get("count") is None else int(step.arguments["count"])
        duration_s = None if step.arguments.get("duration_s") is None else float(step.arguments["duration_s"])
        bounds: JsonObject = {key: value for key, value in (("count", count), ("duration_s", duration_s)) if value is not None}
        iterations: list[JsonObject] = []
        record["iterations"] = iterations
        started = time.monotonic()
        completed = 0
        while True:
            self._progress = {**self._progress, "iteration": completed + 1}
            nested: list[JsonObject] = []
            iterations.append({"iteration": completed + 1, "steps": nested})
            aborted = self.execute_steps(step.steps, nested)
            completed += 1
            if aborted is not None and aborted.stopped:
                record["result"] = {
                    "ok": False,
                    "tool": "test_reactor",
                    "action": REPEAT_ACTION,
                    "error_type": RUN_STOPPED_ERROR,
                    "summary": f"A stop was requested on iteration {completed} of this repeat block.",
                    **bounds,
                    "iterations_run": completed,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "exit_reason": "stopped",
                    "stopped_after_nested_step": aborted.index,
                }
                return aborted
            if aborted is not None:
                failed_index, failure = aborted.index, aborted.failure
                record["result"] = {
                    "ok": False,
                    "tool": "test_reactor",
                    "action": REPEAT_ACTION,
                    "error_type": result_error_type(failure or {}),
                    "summary": f"A step failed on iteration {completed} of this repeat block, so the run stopped there.",
                    **bounds,
                    "iterations_run": completed,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "exit_reason": "step_failed",
                    "failed_iteration": completed,
                    "failed_nested_step": failed_index,
                    "failed_nested_action": step.steps[failed_index - 1].action,
                }
                return aborted
            if count is not None and completed >= count:
                exit_reason = "count"
                break
            if duration_s is not None and time.monotonic() - started >= duration_s:
                exit_reason = "duration_s"
                break
        record["result"] = {
            "ok": True,
            "tool": "test_reactor",
            "action": REPEAT_ACTION,
            "summary": f"Repeat block ran {completed} iteration(s) and ended on its {exit_reason} bound.",
            **bounds,
            "iterations_run": completed,
            "elapsed_s": round(time.monotonic() - started, 3),
            "exit_reason": exit_reason,
        }
        return None

    def cleanup_devices(self) -> list[StepDevice]:
        """Every device this run touched, in the order their sessions close.

        Each kind declares where it falls (``cleanup_order``): debug sessions
        before serial lines and CAN buses, because a still-attached debugger can
        keep writing to a line this plan is about to close. Within one kind the
        last device opened closes first."""
        return sorted(reversed(list(self.devices.values())), key=lambda device: device.cleanup_order)

    def run(self, test_config: TestConfig) -> JsonObject:
        try:
            validation_error = self.preflight(test_config)
        except Exception as error:
            validation_error = {
                "field": "$",
                "summary": "Test reactor preflight raised an exception.",
                **exception_result("test_reactor", "preflight_exception", "Test reactor preflight raised an exception.", error),
            }
        if validation_error is not None:
            result: JsonObject = {
                "ok": False,
                "tool": "test_reactor",
                "name": test_config.name,
                "test_config_path": test_config.path,
                **plan_digest_field(test_config),
                "error_type": validation_error.get("error_type", "test_config_invalid"),
                "validation_error": validation_error,
                "steps": [],
                "cleanup": [],
                "cleanup_ok": True,
                "summary": "Test reactor configuration failed semantic validation; no steps were executed.",
            }
            if "step" in validation_error:
                result["failed_step"] = validation_error["step"]
            return result

        completed: list[JsonObject] = []
        aborted: StepOutcome | None = None
        primary_error: BaseException | None = None
        try:
            aborted = self.execute_steps(test_config.steps, completed, top_level=True)
        except BaseException as error:
            primary_error = error
        finally:
            cleanup = [record for device in self.cleanup_devices() for record in device.cleanup()]

        cleanup_interrupts = [*self.cleanup_interrupts, *(error for device in self.devices.values() for error in device.cleanup_interrupts)]
        if primary_error is not None:
            primary_error.agentic_hil_cleanup = cleanup
            if cleanup_interrupts:
                primary_error.args = (*primary_error.args, "Cleanup interrupts: " + "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_interrupts))
            raise primary_error
        if cleanup_interrupts:
            interrupt = cleanup_interrupts[0]
            interrupt.agentic_hil_cleanup = cleanup
            raise interrupt

        cleanup_errors = [item for item in cleanup if result_failed(item["result"])]
        cleanup_ok = not cleanup_errors
        failure = None if aborted is None else aborted.failure
        stopped = aborted is not None and aborted.stopped
        ok = failure is None and cleanup_ok and not stopped
        result: JsonObject = {
            "ok": ok,
            "tool": "test_reactor",
            "name": test_config.name,
            "test_config_path": test_config.path,
            **plan_digest_field(test_config),
            "steps": completed,
            "cleanup": cleanup,
            "cleanup_ok": cleanup_ok,
            "cleanup_errors": cleanup_errors,
            "summary": "Test reactor sequence completed." if ok else "Test reactor sequence failed.",
        }
        propagate_result_status(result, [*step_results(completed), *(item["result"] for item in cleanup)])
        if failure is not None:
            result["failed_step"] = aborted.index if aborted is not None else None
            # The failing step's own result, which inside a block step is the
            # nested step that failed rather than the block: `failed_step` says
            # which top-level step the run stopped at and `step_error_type` says
            # what went wrong there, and a loop's own summary would answer
            # neither question.
            step_error_type = result_error_type(failure)
            result["step_error_type"] = step_error_type
            result["error_type"] = "cleanup_failed" if not cleanup_ok else step_error_type
        elif stopped:
            # A stopped run is not a passed run and it is not a failed one
            # either, so it says which of the two it is by name. The steps that
            # did run keep their records: the caller decides what a partial run
            # is worth, and it can only decide that from what actually ran.
            result["stopped"] = True
            result["stopped_after_step"] = aborted.index if aborted is not None else 0
            result["error_type"] = "cleanup_failed" if not cleanup_ok else RUN_STOPPED_ERROR
            result["summary"] = (
                f"Test reactor sequence was stopped on request after step {result['stopped_after_step']}; "
                "the devices it opened were closed and this report was written."
            )
        elif not cleanup_ok:
            result["error_type"] = "cleanup_failed"
        if failure is not None or not cleanup_ok:
            # A run that failed aborts, and the abort calls the recovery action.
            # After the devices are closed and after this result exists, so the
            # evidence is written before anything drives the board again, and
            # `ok` is not touched: recovering the bench does not un-fail a test.
            # Reached for a failed cleanup as well as for a failed step:
            # cleanup is where the unconfirmed-effect incidents come from, and
            # those are precisely the ones that used to sit until a person came.
            #
            # A clean stop is deliberately not here, and that is the whole
            # difference between asking a run to end and killing it. Its devices
            # were closed and confirmed by the same cleanup as a passing run's,
            # so there is nothing unconfirmed to recover, and driving the board
            # again to establish a state nothing disturbed would be the incident.
            result["recovery"] = self._recover_after_failed_run()
        return result

    def _recover_after_failed_run(self) -> JsonObject:
        """Recover every probe the run drove, each through its own bound service.

        Debugger steps run on per-probe services (see `DebuggerRunner.service_for`),
        and both reset-into-halt and the probe re-read act on one probe's backend.
        The base service is either unbound (the multi-probe CLI leaves
        `config.debugger` None) or bound to a probe this run may never have
        driven, so recovering through it resets the wrong board, or withholds
        reset as `allow_reset_missing`, while the probes the plan actually drove
        are left as the failed run put them. So each touched probe is recovered
        through the service that drove it, no probe the run never named is
        touched, and the per-probe outcomes are aggregated.

        A run that drove no probe keeps the old base-service behaviour: a
        single-probe bench recovers its one board, and an unbound base service
        reports honestly that nothing here establishes a target's state.

        Only a probe the run actually drove is recovered: `device.drove_device`,
        not mere presence in `devices`. A probe used solely as a `delay` route is
        constructed like any other but touched nothing, so resetting and halting
        it here would drive a board no hardware action in the plan ever reached."""
        touched = [(config_id, device) for (kind, config_id), device in sorted(self.devices.items()) if kind == DebuggerRunner.kind and device.drove_device]
        if not touched:
            return self.service.recover_after_failed_run(sorted({config_id for _, config_id in self.devices}))
        if len(touched) == 1:
            config_id, device = touched[0]
            return device.service.recover_after_failed_run([config_id])
        blocks = {config_id: device.service.recover_after_failed_run([config_id]) for config_id, device in touched}
        return self._aggregate_recovery(blocks)

    @staticmethod
    def _aggregate_recovery(blocks: dict[str, JsonObject]) -> JsonObject:
        """One `recovery` block for a run that drove several probes.

        Each probe's own block is kept whole under `recoveries`, and the
        top-level fields answer the run's question (did recovery run, and did
        every probe it drove come back) without a caller having to walk the map.
        The bench is recovered only when every touched probe is; any probe left
        failed, errored or withheld pulls the aggregate down to the worst of
        them."""
        outcomes = [str(block.get("outcome", "skipped")) for block in blocks.values()]
        if all(name == "recovered" for name in outcomes):
            outcome = "recovered"
        else:
            outcome = next((name for name in ("error", "failed", "skipped") if name in outcomes), "failed")
        return {
            "attempted": any(bool(block.get("attempted")) for block in blocks.values()),
            "outcome": outcome,
            "devices": sorted(blocks),
            "recovered_debuggers": sorted(config_id for config_id, block in blocks.items() if block.get("outcome") == "recovered"),
            "recoveries": blocks,
            "summary": f"Recovery ran on {len(blocks)} probes the failed run drove; aggregate outcome {outcome}.",
        }

    def preflight(self, test_config: TestConfig) -> JsonObject | None:
        """Refuse a plan before its first step runs, or None.

        The walk is device-kind-blind: each step is handed to the class that
        serves its action, which answers for its own config entry, its own
        permissions and its own session. Nothing here is built: see PlanState."""
        state = PlanState()
        return self._preflight_steps(test_config.steps, StepLocation.top, state)

    def _preflight_steps(self, steps: Sequence[TestStep], locate: Callable[[int], StepLocation], state: PlanState) -> JsonObject | None:
        for index, step in enumerate(steps, start=1):
            refusal = self._preflight_step(locate(index), step, state)
            if refusal is not None:
                return refusal
        return None

    def _preflight_step(self, location: StepLocation, step: TestStep, state: PlanState) -> JsonObject | None:
        if step.action == REPEAT_ACTION:
            return self._preflight_repeat(location, step, state)
        named = step.route_keys
        if len(named) > 1:
            # `device:` and its v2 alias mean the same entry, so a step
            # carrying both is a plan that has not decided. Refused rather
            # than resolved by precedence: a step that names one board under
            # one key and another under the other must be corrected, not run.
            return preflight_error(location, step, named[1], "A test step names the device it drives once.", {"route_keys": named})
        candidates = step_device_classes(step.action)
        if not candidates:
            # Unreachable through load_test_config, whose schema pass refuses
            # an unknown action first; reachable by a caller that built a
            # TestConfig itself, and silence would be a step nobody checked.
            return preflight_error(location, step, "action", "No configured device kind serves this test reactor action.", {"allowed_values": sorted(ACTION_SCHEMAS)})
        device_class = step_device_class(self.config, step)
        if device_class is None:
            return preflight_error(
                location,
                step,
                ROUTE_FIELDS[0],
                "Every device kind serves this action, so the step's `device:` must name exactly one configured entry: this one names none, or names an entry in more than one section.",
                {claimant.configured_field: sorted(claimant.config_entries(self.config)) for claimant in candidates},
            )
        name_error = device_class.name_refusal(self, location, step)
        if name_error is not None:
            return name_error
        contract_error = self._preflight_tool_contract(location, step, device_class)
        if contract_error is not None:
            return contract_error
        if not device_class.step_action_specs[step.action].touches_device:
            # An action that drives nothing has no session to be in, no
            # permission to hold and nothing to leave behind, so the kind's
            # own refusals are about a step this one is not.
            return None
        return device_class.preflight(self, location, step, state)

    def _preflight_repeat(self, location: StepLocation, step: TestStep, state: PlanState) -> JsonObject | None:
        """Refuse a block step, or the subset it repeats.

        The block itself names no device and drives nothing, so what is left to
        answer for is its subset, which is walked exactly once against the same
        `PlanState` the rest of the plan uses. Once, not once per iteration: a
        plan is a document, and asking the same question `count` times would give
        the same answer `count` times while making a refusal's step path depend
        on a number nothing has run yet."""
        if not step.steps:
            return preflight_error(location, step, "steps", "A repeat step repeats at least one step.")
        return self._preflight_steps(step.steps, location.nested, state)

    def step_debugger_id(self, step: TestStep) -> str | None:
        return step_debugger_id(self.config, step)

    def _preflight_tool_contract(self, location: StepLocation, step: TestStep, device_class: type[StepDevice]) -> JsonObject | None:
        # Validate each step's plan-supplied tool arguments against the exact MCP
        # contract validators, so a step the reactor schema accepted but the tool
        # contract rejects (e.g. a traversal path) fails before any backend call
        # builds hardware or process state. Which calls a step makes is the
        # device class's own answer (`tool_calls`).
        for tool, arguments in device_class.tool_calls(self.config, step):
            error = validate_tool_arguments(tool, arguments)
            if error is not None:
                return preflight_error(location, step, error.get("field", "arguments"), f"Step arguments are rejected by the {tool} contract: {error.get('summary', 'invalid argument')}", {"tool": tool, "validation": error})
        return None


def step_debugger_id(config: AgenticHILConfig, step: TestStep) -> str | None:
    """The probe a debugger step runs on: the name the plan gave, or the only
    configured one. None means the plan must name it: with several probes
    configured, picking one for the author is how the wrong board gets
    flashed."""
    named = step.debugger if step.debugger is not None else step.device
    if named is not None:
        return named
    return next(iter(config.debuggers)) if len(config.debuggers) == 1 else None


def plan_devices(config: AgenticHILConfig, test_config: TestConfig) -> DeviceSet:
    """Every device this plan says it touches, in acquisition order.

    This is what the run locks before its first step, and what every call inside
    it is checked against, so the plan stays the honest record of what a test
    reaches for. A name the config does not know is left out on purpose:
    preflight refuses that step with a message about the name, which is a better
    answer than a lock failure about a device nobody configured.

    Two steps naming one physical unit (a probe and its virtual COM port under
    one resource_id, the same port under two config entries) yield one device,
    because DeviceSet keys on the hardware and not on what the plan called it.

    Each step's own device class says which unit it drives (`identify`), so a
    device kind cannot be served by the reactor and left out of what the run
    locks: a plan that can drive a bus declares that bus.

    The walk descends into a block step's own subset, because a device a plan
    touches only inside a `repeat` is a device that plan touches. A block step
    itself resolves to no kind and so contributes nothing, which is exactly what
    it is: a statement about the sequence, not about the bench."""
    devices: list[Device] = []
    for step in flatten_steps(test_config.steps):
        device_class = step_device_class(config, step)
        # Named, not bound: a device's lock identity comes from its own config
        # entry, so which probe this config happens to be bound to cannot change
        # which board a step is declared to lock.
        identified = None if device_class is None else device_class.identify(config, step)
        if identified is not None:
            devices.append(identified)
    return DeviceSet.of(devices)


def declared_devices(config: AgenticHILConfig, test_config: TestConfig) -> list[str]:
    """The lock keys of `plan_devices`, which is what the mutex takes."""
    return plan_devices(config, test_config).lock_keys


def step_tool_arguments(config: AgenticHILConfig, step: TestStep) -> list[tuple[str, JsonObject]]:
    """The plan-supplied tool calls a step will make, as (tool, arguments).

    Delegates to the device class that serves the action; kept as a function
    because a caller holding a step should not have to resolve the class first."""
    device_class = step_device_class(config, step)
    return [] if device_class is None else device_class.tool_calls(config, step)


def breakpoint_symbol(location: object) -> str | None:
    if isinstance(location, str):
        return location
    if isinstance(location, dict):
        symbol = location.get("symbol", location.get("function"))
        return symbol if isinstance(symbol, str) else None
    return None


def symbol_allowed(config: AgenticHILConfig, symbol: str) -> bool:
    return config.debug.allow_all_symbols or symbol in config.debug.allowed_symbols


def preflight_error(location: StepLocation, step: TestStep, field: str, summary: str, details: JsonObject | None = None) -> JsonObject:
    return {
        "step": location.step,
        "field": f"{location.path}.{field}",
        "route": step.route,
        "action": step.action,
        "summary": summary,
        **(details or {}),
    }


def exception_result(tool: str, error_type: str, summary: str, error: BaseException) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": error_type,
        "summary": summary,
        "exception_type": type(error).__name__,
        "backend_error": str(error),
    }
